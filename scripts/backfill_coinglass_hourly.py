#!/usr/bin/env python3
"""
L8: Download 180 days of 1H/2H liquidation + OI history from CoinGlass.

Usage:
    .venv/bin/python scripts/backfill_coinglass_hourly.py --interval h1 --days 180
    .venv/bin/python scripts/backfill_coinglass_hourly.py --interval h2 --days 180
    .venv/bin/python scripts/backfill_coinglass_hourly.py --interval h1 --coin BTC --verbose
    .venv/bin/python scripts/backfill_coinglass_hourly.py --interval h2 --skip-oi

Requires: LIQ_COINGLASS_API_KEY in .env (CoinGlass Startup tier for h1/h2).
Rate limit: 2.5s pause between requests (safe for both Hobbyist 30 req/min
and Startup 80 req/min tiers).

Idempotent: ON CONFLICT (timestamp, symbol) DO NOTHING on all tables.

Creates tables inline (same pattern as backfill_coinglass.py):
  - coinglass_liquidations_{h1,h2}  — same schema as coinglass_liquidations
  - coinglass_oi_{h1,h2}            — same schema as coinglass_oi
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import os
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone

import aiohttp

# Force unbuffered stdout so progress lines appear in real time on VPS.
print = functools.partial(print, flush=True)  # noqa: A001

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import get_config
from collectors.db import get_conn, init_pool

# Reuse constants and helpers from existing backfill scripts.
from scripts.backfill_coinglass import (
    CG_EXCHANGES,
    CG_FALLBACKS,
    CG_SYMBOLS,
    REQUEST_SLEEP_S,
)
from scripts.backfill_coinglass_oi import (
    OI_PATH,
    _pick_float,
    build_oi_rows,
)


CG_BASE_LIQ = "https://open-api-v4.coinglass.com/api/futures/liquidation"
CG_BASE_OI = "https://open-api-v4.coinglass.com/api/futures"


# ---------------------------------------------------------------------------
# Table setup
# ---------------------------------------------------------------------------

def ensure_tables(interval: str) -> None:
    """Create liquidation + OI tables for the specified interval."""
    liq_table = f"coinglass_liquidations_{interval}"
    oi_table = f"coinglass_oi_{interval}"
    uq_liq = f"uq_cg_liq_{interval}"
    uq_oi = f"uq_cg_oi_{interval}"
    idx_liq = f"idx_cg_liq_{interval}_sym_ts"
    idx_oi = f"idx_cg_oi_{interval}_sym_ts"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {liq_table} (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL,
                    symbol TEXT NOT NULL,
                    long_vol_usd DOUBLE PRECISION,
                    short_vol_usd DOUBLE PRECISION,
                    long_count INTEGER DEFAULT 0,
                    short_count INTEGER DEFAULT 0,
                    CONSTRAINT {uq_liq} UNIQUE (timestamp, symbol)
                );
                CREATE INDEX IF NOT EXISTS {idx_liq}
                    ON {liq_table}(symbol, timestamp);

                CREATE TABLE IF NOT EXISTS {oi_table} (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL,
                    symbol TEXT NOT NULL,
                    open_interest DOUBLE PRECISION,
                    oi_high DOUBLE PRECISION,
                    oi_low DOUBLE PRECISION,
                    CONSTRAINT {uq_oi} UNIQUE (timestamp, symbol)
                );
                CREATE INDEX IF NOT EXISTS {idx_oi}
                    ON {oi_table}(symbol, timestamp);
            """)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    api_key: str,
    params: dict,
    label: str,
    verbose: bool,
) -> dict | None:
    """GET → JSON with CG-API-KEY header."""
    headers = {"CG-API-KEY": api_key}
    t0 = time.monotonic()
    if verbose:
        print(f"    → GET {label}  params={params}")
    try:
        async with session.get(
            url, headers=headers, params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            status = resp.status
            data = await resp.json()
    except asyncio.TimeoutError:
        print(f"    ❌ Timeout on {label}")
        return None
    except Exception as e:
        print(f"    ❌ HTTP error on {label}: {e}")
        return None

    elapsed = time.monotonic() - t0
    if verbose:
        print(f"    ← HTTP {status} in {elapsed:.1f}s, code={data.get('code')}")
    return data


def _window_filter(records: list[dict], start_ts: int, end_ts: int) -> list[dict]:
    """Filter response rows to [start_ts, end_ts], normalizing ms→s."""
    def _t(r: dict) -> int:
        t = int(r.get("time") or r.get("t") or 0)
        return t // 1000 if t > 10**12 else t
    return [r for r in records if start_ts <= _t(r) <= end_ts]


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

async def fetch_liquidations(
    session: aiohttp.ClientSession,
    api_key: str,
    symbol_cg: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    verbose: bool = False,
) -> list[dict]:
    """Fetch aggregated liquidation history for a symbol at h1/h2."""
    url = f"{CG_BASE_LIQ}/aggregated-history"
    params = {
        "symbol": symbol_cg,
        "interval": interval,
        "exchange_list": CG_EXCHANGES,
        "startTime": start_ts,
        "endTime": end_ts,
    }
    data = await _get_json(
        session, url, api_key, params,
        label=f"LIQ {symbol_cg}@{interval}", verbose=verbose,
    )
    if data is None:
        return []
    if data.get("code") != "0" or not data.get("data"):
        msg = data.get("msg", "empty")
        if verbose:
            print(f"  Warning: LIQ {msg} for {symbol_cg}@{interval}")
        return []
    return _window_filter(data["data"], start_ts, end_ts)


async def fetch_oi_hourly(
    session: aiohttp.ClientSession,
    api_key: str,
    symbol_cg: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    verbose: bool = False,
) -> list[dict]:
    """Fetch aggregated OI OHLC for a symbol at h1/h2."""
    url = f"{CG_BASE_OI}{OI_PATH}"
    params = {
        "symbol": symbol_cg,
        "interval": interval,
        "exchange_list": CG_EXCHANGES,
        "startTime": start_ts,
        "endTime": end_ts,
    }
    data = await _get_json(
        session, url, api_key, params,
        label=f"OI {symbol_cg}@{interval}", verbose=verbose,
    )
    if data is None:
        return []
    if data.get("code") != "0" or not data.get("data"):
        msg = data.get("msg", "empty")
        if verbose:
            print(f"  Warning: OI {msg} for {symbol_cg}@{interval}")
        return []
    return _window_filter(data["data"], start_ts, end_ts)


# ---------------------------------------------------------------------------
# Row parsers
# ---------------------------------------------------------------------------

def build_liq_rows(records: list[dict], coin: str) -> list[tuple]:
    """Parse CoinGlass liquidation records → insert tuples."""
    rows: list[tuple] = []
    for r in records:
        t_raw = int(r.get("time") or r.get("t") or 0)
        t_sec = t_raw / 1000 if t_raw > 10**12 else t_raw
        ts = datetime.fromtimestamp(t_sec, tz=timezone.utc)
        rows.append((
            ts,
            coin,
            float(r.get("aggregated_long_liquidation_usd")
                  or r.get("longVolUsd") or 0),
            float(r.get("aggregated_short_liquidation_usd")
                  or r.get("shortVolUsd") or 0),
            int(r.get("longCount") or 0),
            int(r.get("shortCount") or 0),
        ))
    return rows


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------

def insert_liquidations(
    rows: list[tuple], coin: str, interval: str,
) -> tuple[int, int]:
    """Insert liquidation rows; return (before_count, after_count)."""
    from psycopg2.extras import execute_values
    table = f"coinglass_liquidations_{interval}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM {table} WHERE symbol=%s", (coin,),
            )
            before = cur.fetchone()[0]
            execute_values(
                cur,
                f"""
                INSERT INTO {table}
                    (timestamp, symbol, long_vol_usd, short_vol_usd,
                     long_count, short_count)
                VALUES %s
                ON CONFLICT (timestamp, symbol) DO NOTHING
                """,
                rows,
                page_size=500,
            )
            cur.execute(
                f"SELECT COUNT(*) FROM {table} WHERE symbol=%s", (coin,),
            )
            after = cur.fetchone()[0]
    return before, after


def insert_oi_hourly(
    rows: list[tuple], coin: str, interval: str,
) -> tuple[int, int]:
    """Insert OI rows; return (before_count, after_count)."""
    from psycopg2.extras import execute_values
    table = f"coinglass_oi_{interval}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM {table} WHERE symbol=%s", (coin,),
            )
            before = cur.fetchone()[0]
            execute_values(
                cur,
                f"""
                INSERT INTO {table}
                    (timestamp, symbol, open_interest, oi_high, oi_low)
                VALUES %s
                ON CONFLICT (timestamp, symbol) DO NOTHING
                """,
                rows,
                page_size=500,
            )
            cur.execute(
                f"SELECT COUNT(*) FROM {table} WHERE symbol=%s", (coin,),
            )
            after = cur.fetchone()[0]
    return before, after


# ---------------------------------------------------------------------------
# API probe
# ---------------------------------------------------------------------------

async def probe_api(
    session: aiohttp.ClientSession,
    api_key: str,
    interval: str,
    verbose: bool,
) -> bool:
    """
    Probe CoinGlass with BTC at the requested interval.
    Returns True if the endpoint returns data, False otherwise.
    """
    print(f"API probe: testing BTC liquidations at interval={interval}...")
    records = await fetch_liquidations(
        session, api_key, "BTC", interval, 0, int(time.time()),
        verbose=verbose,
    )
    if records:
        print(f"  ✅ Probe OK: {len(records)} records returned")
        return True

    print(f"  ❌ Probe FAILED: CoinGlass returned no data for BTC@{interval}")
    print(f"  Possible causes:")
    print(f"    1. CoinGlass does not support interval={interval} on aggregated-history")
    print(f"    2. API key does not have Startup tier access")
    print(f"    3. Network / auth error")
    print(f"  Check the CoinGlass API docs or upgrade your tier.")
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="L8: Backfill CoinGlass h1/h2 liquidation + OI history."
    )
    parser.add_argument(
        "--interval", type=str, required=True, choices=["h1", "h2"],
        help="CoinGlass interval (h1 or h2).",
    )
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument(
        "--coin", type=str, default=None,
        help="Limit to a single canonical coin (e.g. BTC). Default: all 10.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print per-request progress (URL, status, timing).",
    )
    parser.add_argument(
        "--skip-oi", action="store_true",
        help="Skip OI fetch (liquidations only).",
    )
    args = parser.parse_args()

    interval = args.interval
    if args.days < 1 or args.days > 365:
        parser.error("--days must be between 1 and 365")

    cfg = get_config()
    init_pool(cfg)

    api_key = cfg.coinglass_api_key
    if not api_key:
        print("ERROR: LIQ_COINGLASS_API_KEY not set in .env")
        sys.exit(1)

    # Create tables.
    ensure_tables(interval)

    # Pre-flight DB check.
    liq_table = f"coinglass_liquidations_{interval}"
    oi_table = f"coinglass_oi_{interval}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {liq_table}")
            liq_before = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM {oi_table}")
            oi_before = cur.fetchone()[0]
    print(
        f"DB: {liq_table}={liq_before} rows, "
        f"{oi_table}={oi_before} rows before backfill"
    )

    end_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = int(
        (datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp()
    )

    if args.coin and args.coin not in CG_SYMBOLS:
        parser.error(f"--coin must be one of: {list(CG_SYMBOLS)}")
    coins = (
        [(args.coin, CG_SYMBOLS[args.coin])] if args.coin
        else list(CG_SYMBOLS.items())
    )

    print(
        f"Backfilling {args.days} days @ {interval}: "
        f"{datetime.fromtimestamp(start_ts, tz=timezone.utc).date()} → "
        f"{datetime.fromtimestamp(end_ts, tz=timezone.utc).date()}  "
        f"({len(coins)} coin{'s' if len(coins) != 1 else ''}; "
        f"oi={'skip' if args.skip_oi else 'on'})"
    )

    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    async with aiohttp.ClientSession(connector=connector) as session:
        # ---- API probe ----
        if not await probe_api(session, api_key, interval, args.verbose):
            sys.exit(1)
        await asyncio.sleep(REQUEST_SLEEP_S)

        # ---- Main loop ----
        for coin, primary_sym in coins:
            t_coin = time.monotonic()

            # ---- Liquidations ----
            print(f"[{coin}] LIQ @{interval} (symbol={primary_sym})...")
            liq_records = await fetch_liquidations(
                session, api_key, primary_sym, interval, start_ts, end_ts,
                verbose=args.verbose,
            )
            if not liq_records and coin in CG_FALLBACKS:
                fb = CG_FALLBACKS[coin]
                print(f"  No data for {primary_sym}, trying fallback {fb}...")
                liq_records = await fetch_liquidations(
                    session, api_key, fb, interval, start_ts, end_ts,
                    verbose=args.verbose,
                )

            if liq_records:
                rows = build_liq_rows(liq_records, coin)
                try:
                    n_before, n_after = insert_liquidations(rows, coin, interval)
                    print(
                        f"  ✅ LIQ {coin}: fetched {len(rows)} rows, "
                        f"inserted {n_after - n_before} new "
                        f"(was {n_before}, now {n_after})"
                    )
                except Exception as e:
                    print(f"  ❌ LIQ DB insert failed for {coin}: {e}")
            else:
                print(f"  ❌ No LIQ data for {coin}")
            await asyncio.sleep(REQUEST_SLEEP_S)

            # ---- OI ----
            if not args.skip_oi:
                print(f"[{coin}] OI @{interval} (symbol={primary_sym})...")
                oi_records = await fetch_oi_hourly(
                    session, api_key, primary_sym, interval, start_ts, end_ts,
                    verbose=args.verbose,
                )
                if not oi_records and coin in CG_FALLBACKS:
                    fb = CG_FALLBACKS[coin]
                    print(f"  No OI for {primary_sym}, trying fallback {fb}...")
                    oi_records = await fetch_oi_hourly(
                        session, api_key, fb, interval, start_ts, end_ts,
                        verbose=args.verbose,
                    )

                if oi_records:
                    rows = build_oi_rows(oi_records, coin)
                    try:
                        n_before, n_after = insert_oi_hourly(rows, coin, interval)
                        print(
                            f"  ✅ OI {coin}: fetched {len(rows)} rows, "
                            f"inserted {n_after - n_before} new "
                            f"(was {n_before}, now {n_after})"
                        )
                    except Exception as e:
                        print(f"  ❌ OI DB insert failed for {coin}: {e}")
                else:
                    print(f"  ❌ No OI data for {coin}")
                await asyncio.sleep(REQUEST_SLEEP_S)

            print(f"  [{coin}] done in {time.monotonic() - t_coin:.1f}s")

    # ---- Summary ----
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT '{liq_table}' AS tbl, symbol, COUNT(*),
                       MIN(timestamp)::date, MAX(timestamp)::date
                FROM {liq_table} GROUP BY symbol
                UNION ALL
                SELECT '{oi_table}', symbol, COUNT(*),
                       MIN(timestamp)::date, MAX(timestamp)::date
                FROM {oi_table} GROUP BY symbol
                ORDER BY 1, 2
            """)
            rows = cur.fetchall()

    print(f"\nSummary:")
    print(f"  {'Table':<30} {'Symbol':<8} {'Rows':>6}  {'From':>12}  {'To':>12}")
    for row in rows:
        print(f"  {row[0]:<30} {row[1]:<8} {row[2]:>6}  {row[3]}  {row[4]}")
    if not rows:
        print("  (tables empty)")


if __name__ == "__main__":
    asyncio.run(main())
