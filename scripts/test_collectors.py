#!/usr/bin/env python3
"""
Integration test for all API endpoints and DB connectivity.
Not a pytest suite — run directly: python scripts/test_collectors.py
"""

import asyncio
import json
import logging
import os
import sys
import time

import ssl

import aiohttp
import ccxt
import certifi
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import (
    COINS,
    binance_ccxt_symbol,
    binance_raw_symbol,
    get_config,
    hl_coin,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

passed = 0
failed = 0


def report(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")


# ---------------------------------------------------------------------------
# Hyperliquid tests
# ---------------------------------------------------------------------------

def test_hl_all_mids() -> dict:
    """Test allMids endpoint, return mid prices."""
    print("\n--- Hyperliquid API ---")
    try:
        resp = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "allMids"},
            timeout=10,
        )
        resp.raise_for_status()
        mids = resp.json()

        # Check all 10 coins
        missing = []
        for coin in COINS:
            hl_name = hl_coin(coin)
            if hl_name not in mids:
                missing.append(f"{coin}({hl_name})")

        if missing:
            report("allMids — all 10 coins", False, f"Missing: {', '.join(missing)}")
        else:
            sample = ", ".join(
                f"{c}=${float(mids[hl_coin(c)]):,.2f}" for c in ["BTC", "ETH", "SOL"]
            )
            report("allMids — all 10 coins", True, sample)

        return mids
    except Exception as e:
        report("allMids", False, str(e))
        return {}


def test_hl_clearinghouse(address: str) -> None:
    """Test clearinghouseState for a specific address."""
    try:
        resp = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "clearinghouseState", "user": address},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        positions = data.get("assetPositions", [])
        report(
            "clearinghouseState",
            True,
            f"address={address[:12]}..., {len(positions)} positions",
        )
    except Exception as e:
        report("clearinghouseState", False, str(e))


def test_hl_leaderboard() -> str | None:
    """Test leaderboard API, return a sample address."""
    try:
        resp = requests.get(
            "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("leaderboardRows", [])
        if rows:
            top = rows[0]
            addr = top["ethAddress"]
            val = float(top.get("accountValue", 0))
            report(
                "leaderboard",
                True,
                f"{len(rows)} entries, top: ${val:,.0f}",
            )
            return addr
        else:
            report("leaderboard", False, "empty response")
            return None
    except Exception as e:
        report("leaderboard", False, str(e))
        return None


def test_hl_pepe_mapping(mids: dict) -> None:
    """Verify PEPE→kPEPE mapping works."""
    hl_name = hl_coin("PEPE")
    if hl_name in mids:
        report("PEPE→kPEPE mapping", True, f"kPEPE=${float(mids[hl_name]):.6f}")
    else:
        report("PEPE→kPEPE mapping", False, f"'{hl_name}' not in mids")


# ---------------------------------------------------------------------------
# Binance tests
# ---------------------------------------------------------------------------

def test_binance_oi() -> None:
    print("\n--- Binance API ---")
    exchange = ccxt.binance({"options": {"defaultType": "swap"}})
    try:
        symbol = binance_ccxt_symbol("BTC")
        oi = exchange.fetch_open_interest(symbol)
        report(
            "Open Interest (BTC)",
            True,
            f"OI={float(oi.get('openInterestAmount', 0)):,.2f} BTC",
        )
    except Exception as e:
        report("Open Interest (BTC)", False, str(e))


def test_binance_funding() -> None:
    exchange = ccxt.binance({"options": {"defaultType": "swap"}})
    try:
        symbol = binance_ccxt_symbol("BTC")
        fr = exchange.fetch_funding_rate(symbol)
        rate = float(fr.get("fundingRate", 0))
        report("Funding Rate (BTC)", True, f"rate={rate:.6f}")
    except Exception as e:
        report("Funding Rate (BTC)", False, str(e))


def _make_ssl_session():
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    return aiohttp.ClientSession(connector=connector)


async def test_binance_ls_ratio() -> None:
    cfg = get_config()
    async with _make_ssl_session() as session:
        url = f"{cfg.binance_fapi_url}/futures/data/topLongShortAccountRatio"
        params = {"symbol": "BTCUSDT", "period": "1h", "limit": 1}
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                data = await resp.json()
            if data:
                entry = data[0]
                report(
                    "L/S Ratio (BTC, /futures/data/)",
                    True,
                    f"ratio={entry.get('longShortRatio')}, long={entry.get('longAccount')}",
                )
            else:
                report("L/S Ratio (BTC)", False, "empty response")
        except Exception as e:
            report("L/S Ratio (BTC)", False, str(e))


async def test_binance_taker() -> None:
    cfg = get_config()
    async with _make_ssl_session() as session:
        url = f"{cfg.binance_fapi_url}/futures/data/takerlongshortRatio"
        params = {"symbol": "BTCUSDT", "period": "1h", "limit": 1}
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                data = await resp.json()
            if data:
                entry = data[0]
                report(
                    "Taker Buy/Sell (BTC, /futures/data/)",
                    True,
                    f"ratio={entry.get('buySellRatio')}, buy={entry.get('buyVol')}",
                )
            else:
                report("Taker Buy/Sell (BTC)", False, "empty response")
        except Exception as e:
            report("Taker Buy/Sell (BTC)", False, str(e))


def test_binance_pepe() -> None:
    """Verify 1000PEPEUSDT works on Binance."""
    exchange = ccxt.binance({"options": {"defaultType": "swap"}})
    try:
        symbol = binance_ccxt_symbol("PEPE")  # 1000PEPE/USDT:USDT
        oi = exchange.fetch_open_interest(symbol)
        report(
            "PEPE→1000PEPEUSDT mapping",
            True,
            f"symbol={symbol}, OI={float(oi.get('openInterestAmount', 0)):,.0f}",
        )
    except Exception as e:
        report("PEPE→1000PEPEUSDT mapping", False, str(e))


# ---------------------------------------------------------------------------
# DB test
# ---------------------------------------------------------------------------

def test_db_connection() -> None:
    print("\n--- Database ---")
    try:
        from collectors.db import init_pool, get_conn
        cfg = get_config()
        init_pool(cfg)
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            tables = [row[0] for row in cur.fetchall()]

        expected = [
            "hl_addresses", "hl_position_snapshots", "hl_liquidation_map",
            "binance_oi", "binance_funding", "binance_ls_ratio", "binance_taker",
        ]
        missing = [t for t in expected if t not in tables]
        if missing:
            report("DB tables", False, f"Missing: {', '.join(missing)}")
        else:
            report("DB connection + tables", True, f"{len(tables)} tables found")

        # Count addresses
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM hl_addresses")
            count = cur.fetchone()[0]
        report("hl_addresses count", True, f"{count} addresses")

    except Exception as e:
        report("DB connection", False, str(e))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_async_tests() -> None:
    await test_binance_ls_ratio()
    await test_binance_taker()


def main() -> None:
    global passed, failed
    print("=" * 60)
    print("Liquidation Bot — Integration Tests")
    print("=" * 60)

    t0 = time.time()

    # Hyperliquid
    mids = test_hl_all_mids()
    test_hl_pepe_mapping(mids)
    addr = test_hl_leaderboard()
    if addr:
        test_hl_clearinghouse(addr)

    # Binance (sync)
    test_binance_oi()
    test_binance_funding()
    test_binance_pepe()

    # Binance (async)
    asyncio.run(run_async_tests())

    # Database
    test_db_connection()

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed ({elapsed:.1f}s)")
    print(f"{'=' * 60}")

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
