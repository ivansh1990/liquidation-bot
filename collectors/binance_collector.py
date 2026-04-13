"""
Binance futures data collector.

Collects Open Interest, Funding Rate, Long/Short Ratio, and Taker Buy/Sell
for all tracked coins. All endpoints are public (no API key needed).

OI & Funding: via ccxt.
L/S Ratio & Taker: direct HTTP (ccxt uses wrong paths for these).

Run every hour via systemd timer.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import sys
import os
import time
from datetime import datetime, timezone

import aiohttp
import ccxt
import certifi

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import (
    COINS,
    Config,
    binance_ccxt_symbol,
    binance_raw_symbol,
    get_config,
)
from collectors.db import (
    get_conn,
    init_pool,
    insert_binance_funding,
    insert_binance_ls_ratio,
    insert_binance_oi,
    insert_binance_taker,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ccxt-based collectors (OI, Funding)
# ---------------------------------------------------------------------------

def collect_oi(exchange: ccxt.binance, coin: str, mark_price: float | None = None) -> dict | None:
    """Fetch open interest via ccxt. mark_price used to calculate USD value."""
    symbol = binance_ccxt_symbol(coin)
    try:
        oi = exchange.fetch_open_interest(symbol)
        oi_amount = float(oi.get("openInterestAmount") or 0)
        oi_value = oi.get("openInterestValue")
        if oi_value is not None:
            oi_usd = float(oi_value)
        elif mark_price and mark_price > 0:
            oi_usd = oi_amount * mark_price
        else:
            oi_usd = 0
        return {
            "open_interest": oi_amount,
            "open_interest_usd": oi_usd,
            "timestamp": datetime.fromtimestamp(
                oi["timestamp"] / 1000, tz=timezone.utc
            ) if oi.get("timestamp") else datetime.now(timezone.utc),
        }
    except Exception as e:
        log.error("OI fetch failed for %s: %s", coin, e)
        return None


def collect_funding(exchange: ccxt.binance, coin: str) -> dict | None:
    """Fetch funding rate via ccxt."""
    symbol = binance_ccxt_symbol(coin)
    try:
        fr = exchange.fetch_funding_rate(symbol)
        return {
            "funding_rate": float(fr.get("fundingRate", 0)),
            "mark_price": float(fr.get("markPrice", 0)),
            "timestamp": datetime.fromtimestamp(
                fr["fundingTimestamp"] / 1000, tz=timezone.utc
            ) if fr.get("fundingTimestamp") else datetime.now(timezone.utc),
        }
    except Exception as e:
        log.error("Funding fetch failed for %s: %s", coin, e)
        return None


# ---------------------------------------------------------------------------
# Direct HTTP collectors (L/S Ratio, Taker)
# ---------------------------------------------------------------------------

async def collect_ls_ratio(
    session: aiohttp.ClientSession, cfg: Config, coin: str
) -> dict | None:
    """
    Fetch top trader long/short account ratio.
    Endpoint: /futures/data/topLongShortAccountRatio (NOT /fapi/v1/).
    """
    raw_symbol = binance_raw_symbol(coin)
    url = f"{cfg.binance_fapi_url}/futures/data/topLongShortAccountRatio"
    params = {"symbol": raw_symbol, "period": "1h", "limit": 1}

    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if not data:
            return None

        entry = data[0]
        return {
            "long_pct": float(entry.get("longAccount", 0)),
            "short_pct": float(entry.get("shortAccount", 0)),
            "ratio": float(entry.get("longShortRatio", 0)),
            "timestamp": datetime.fromtimestamp(
                entry["timestamp"] / 1000, tz=timezone.utc
            ) if entry.get("timestamp") else datetime.now(timezone.utc),
        }
    except Exception as e:
        log.error("L/S ratio fetch failed for %s: %s", coin, e)
        return None


async def collect_taker(
    session: aiohttp.ClientSession, cfg: Config, coin: str
) -> dict | None:
    """
    Fetch taker buy/sell volume ratio.
    Endpoint: /futures/data/takerlongshortRatio (NOT /fapi/v1/).
    """
    raw_symbol = binance_raw_symbol(coin)
    url = f"{cfg.binance_fapi_url}/futures/data/takerlongshortRatio"
    params = {"symbol": raw_symbol, "period": "1h", "limit": 1}

    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if not data:
            return None

        entry = data[0]
        return {
            "buy_vol": float(entry.get("buyVol", 0)),
            "sell_vol": float(entry.get("sellVol", 0)),
            "ratio": float(entry.get("buySellRatio", 0)),
            "timestamp": datetime.fromtimestamp(
                entry["timestamp"] / 1000, tz=timezone.utc
            ) if entry.get("timestamp") else datetime.now(timezone.utc),
        }
    except Exception as e:
        log.error("Taker fetch failed for %s: %s", coin, e)
        return None


# ---------------------------------------------------------------------------
# Main collection cycle
# ---------------------------------------------------------------------------

async def run_collection(cfg: Config) -> None:
    log.info("Starting Binance collection for %d coins", len(COINS))

    # ccxt exchange instance (sync, used for OI and funding)
    exchange = ccxt.binance({"options": {"defaultType": "swap"}})

    errors = 0

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as session:
        for coin in COINS:
            log.info("Collecting %s ...", coin)
            symbol_display = binance_raw_symbol(coin)

            # Funding first (need mark_price for OI USD calculation)
            funding = collect_funding(exchange, coin)
            mark_price = funding["mark_price"] if funding else None
            if funding:
                with get_conn() as conn:
                    insert_binance_funding(
                        conn, coin, funding["timestamp"],
                        funding["funding_rate"], funding["mark_price"],
                    )
            else:
                errors += 1

            # OI (sync via ccxt, uses mark_price for USD calc)
            oi = collect_oi(exchange, coin, mark_price=mark_price)
            if oi:
                with get_conn() as conn:
                    insert_binance_oi(
                        conn, coin, oi["timestamp"],
                        oi["open_interest"], oi["open_interest_usd"],
                    )
            else:
                errors += 1

            # L/S Ratio (async, direct HTTP)
            ls = await collect_ls_ratio(session, cfg, coin)
            if ls:
                with get_conn() as conn:
                    insert_binance_ls_ratio(
                        conn, coin, ls["timestamp"],
                        ls["long_pct"], ls["short_pct"], ls["ratio"],
                    )
            else:
                errors += 1

            # Taker (async, direct HTTP)
            taker = await collect_taker(session, cfg, coin)
            if taker:
                with get_conn() as conn:
                    insert_binance_taker(
                        conn, coin, taker["timestamp"],
                        taker["buy_vol"], taker["sell_vol"], taker["ratio"],
                    )
            else:
                errors += 1

            # Courtesy delay between coins
            await asyncio.sleep(0.1)

    log.info(
        "Binance collection done: %d coins, %d errors",
        len(COINS), errors,
    )


async def main() -> None:
    cfg = get_config()
    init_pool(cfg)

    t0 = time.time()
    await run_collection(cfg)
    elapsed = time.time() - t0
    log.info("Total time: %.1fs", elapsed)


if __name__ == "__main__":
    asyncio.run(main())
