"""
CoinGlass OI + Funding live collector.

Fetches the latest aggregated Open Interest (h4) and Funding Rate (h8/h4)
from CoinGlass for all 10 tracked coins and inserts into coinglass_oi /
coinglass_funding tables. Designed to run every 4 hours via systemd timer.

Reuses row parsers and DB helpers from scripts/backfill_coinglass_oi.py.
Idempotent: ON CONFLICT (timestamp, symbol) DO NOTHING on both tables.

Usage:
    .venv/bin/python -m collectors.coinglass_oi_collector
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import time

import aiohttp
import certifi

from collectors.config import COINS, get_config
from collectors.db import get_conn, init_pool

# Reuse constants, row parsers, table setup, and insert helpers from backfill.
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from scripts.backfill_coinglass_oi import (
    CG_BASE,
    CG_EXCHANGES,
    CG_FALLBACKS,
    FUNDING_INTERVALS,
    FUNDING_PATHS,
    OI_PATH,
    REQUEST_SLEEP_S,
    build_funding_rows,
    build_oi_rows,
    ensure_tables,
    insert_funding,
    insert_oi,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Take the last N bars from CoinGlass response. 5 bars = 20 hours at h4,
# enough to cover a missed 4H cycle plus buffer.
TAIL_BARS = 5


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    api_key: str,
    params: dict,
    label: str,
) -> dict | None:
    """GET → JSON with CG-API-KEY header. Returns parsed dict or None."""
    headers = {"CG-API-KEY": api_key}
    try:
        async with session.get(
            url, headers=headers, params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            return await resp.json()
    except asyncio.TimeoutError:
        log.error("Timeout on %s", label)
        return None
    except Exception as e:
        log.error("HTTP error on %s: %s", label, e)
        return None


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def _cg_symbols(coin: str) -> list[str]:
    """Return CoinGlass symbol candidates (primary + fallback)."""
    symbols = [coin]
    if coin in CG_FALLBACKS:
        symbols.append(CG_FALLBACKS[coin])
    return symbols


async def fetch_latest_oi(
    session: aiohttp.ClientSession, coin: str, api_key: str,
) -> list[dict]:
    """
    Fetch latest OI bars (h4) for a coin.

    Hobbyist tier returns ≤1000 bars regardless of startTime/endTime.
    We take the last TAIL_BARS records — enough to cover missed runs.
    Tries PEPE → 1000PEPE fallback if primary returns empty.
    """
    url = f"{CG_BASE}{OI_PATH}"
    for symbol in _cg_symbols(coin):
        params = {
            "symbol": symbol,
            "interval": "h4",
            "exchange_list": CG_EXCHANGES,
        }
        data = await _get_json(session, url, api_key, params, f"OI {symbol}")
        if data is None:
            continue
        if data.get("code") != "0" or not data.get("data"):
            continue
        records = data["data"]
        return records[-TAIL_BARS:] if len(records) > TAIL_BARS else records
    return []


async def fetch_latest_funding(
    session: aiohttp.ClientSession, coin: str, api_key: str,
) -> tuple[list[dict], str | None]:
    """
    Fetch latest funding rate bars, trying path×interval combos.

    Returns (records, "path_tag@interval") or ([], None).
    Sleeps REQUEST_SLEEP_S between combo attempts (shared rate limit).
    Tries PEPE → 1000PEPE fallback per combo.
    """
    for path in FUNDING_PATHS:
        for interval in FUNDING_INTERVALS:
            url = f"{CG_BASE}{path}"
            tag = f"{path.rsplit('/', 1)[-1]}@{interval}"

            for symbol in _cg_symbols(coin):
                params = {
                    "symbol": symbol,
                    "interval": interval,
                    "exchange_list": CG_EXCHANGES,
                }
                data = await _get_json(
                    session, url, api_key, params, f"FR {symbol} {tag}",
                )
                if data is None:
                    continue
                if data.get("code") != "0":
                    continue
                records = data.get("data") or []
                if records:
                    tail = records[-TAIL_BARS:] if len(records) > TAIL_BARS else records
                    return tail, tag

            await asyncio.sleep(REQUEST_SLEEP_S)

    return [], None


# ---------------------------------------------------------------------------
# Main collection
# ---------------------------------------------------------------------------

async def collect_once() -> None:
    """
    Fetch latest OI + funding for all coins and insert into DB.

    Total: 10 coins × (1 OI req + ~1 funding req) × 2.5s ≈ 50-60s.
    """
    cfg = get_config()
    init_pool(cfg)

    api_key = cfg.coinglass_api_key
    if not api_key:
        log.error("LIQ_COINGLASS_API_KEY not set — aborting")
        return

    ensure_tables()

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    oi_total_new = 0
    fr_total_new = 0
    errors = 0

    async with aiohttp.ClientSession(connector=connector) as session:
        for coin in COINS:
            # --- OI ---
            try:
                oi_records = await fetch_latest_oi(session, coin, api_key)
                if oi_records:
                    rows = build_oi_rows(oi_records, coin)
                    before, after = insert_oi(rows, coin)
                    new = after - before
                    oi_total_new += new
                    log.info("OI %s: %d fetched, %d new", coin, len(rows), new)
                else:
                    log.warning("OI %s: no data", coin)
                    errors += 1
            except Exception as e:
                log.error("OI %s failed: %s", coin, e)
                errors += 1

            await asyncio.sleep(REQUEST_SLEEP_S)

            # --- Funding ---
            try:
                fr_records, combo = await fetch_latest_funding(
                    session, coin, api_key,
                )
                if fr_records:
                    rows = build_funding_rows(fr_records, coin)
                    before, after = insert_funding(rows, coin)
                    new = after - before
                    fr_total_new += new
                    log.info(
                        "Funding %s [%s]: %d fetched, %d new",
                        coin, combo, len(rows), new,
                    )
                else:
                    log.warning("Funding %s: no data (all combos failed)", coin)
                    errors += 1
            except Exception as e:
                log.error("Funding %s failed: %s", coin, e)
                errors += 1

            # Note: fetch_latest_funding sleeps internally between combos,
            # so no extra sleep needed before the next coin's OI request.

    log.info(
        "CoinGlass collection done: OI +%d rows, Funding +%d rows, %d errors",
        oi_total_new, fr_total_new, errors,
    )


if __name__ == "__main__":
    asyncio.run(collect_once())
