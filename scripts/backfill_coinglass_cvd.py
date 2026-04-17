#!/usr/bin/env python3
"""
L13 Phase 1: Backfill CoinGlass aggregated CVD (Cumulative Volume Delta)
history for 10 coins across h1/h2/h4 intervals.

Usage:
    .venv/bin/python scripts/backfill_coinglass_cvd.py --interval h1 --days 180
    .venv/bin/python scripts/backfill_coinglass_cvd.py --interval h2 --days 180
    .venv/bin/python scripts/backfill_coinglass_cvd.py --interval h4 --days 180
    .venv/bin/python scripts/backfill_coinglass_cvd.py --interval h4 --coin BTC --verbose

Requires: LIQ_COINGLASS_API_KEY in .env (CoinGlass Startup tier).
Rate limit: 2.5s pause between requests.

Idempotent: ON CONFLICT (timestamp, symbol) DO NOTHING.

Creates three tables inline (same pattern as backfill_coinglass_hourly.py):
  - coinglass_cvd_h1
  - coinglass_cvd_h2
  - coinglass_cvd_h4

Endpoint notes (probed 17 Apr 2026, Startup tier):
  - URL: /api/futures/aggregated-cvd/history
  - Params: exchange_list=<CG_EXCHANGES> (multi-exchange aggregation),
    symbol=<COIN> (coin-level, not pair), interval, limit.
  - `startTime`/`endTime` silently ignored; `limit` honored up to tier ceiling.
  - Strategy: `limit = days × bars_per_day` — single request per coin, no
    pagination. 10 coins = 10 requests ≈ 25s per run.
  - Response per bar: time (ms), agg_taker_buy_vol (USD),
    agg_taker_sell_vol (USD), cum_vol_delta (USD per-bar delta).
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import os
import ssl
import sys
import time
from datetime import datetime, timezone

import aiohttp

# Force unbuffered stdout so progress lines appear in real time on VPS.
print = functools.partial(print, flush=True)  # noqa: A001

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import get_config
from collectors.db import get_conn, init_pool

# Reuse constants/helpers from sibling backfill scripts.
from scripts.backfill_coinglass import CG_EXCHANGES, CG_SYMBOLS, REQUEST_SLEEP_S
from scripts.backfill_coinglass_oi import _pick_float
from scripts.backfill_coinglass_hourly import INTERVAL_BARS_PER_DAY, _get_json, _t


CVD_BASE = (
    "https://open-api-v4.coinglass.com/api/futures/aggregated-cvd/history"
)

# Coin-level fallback (endpoint uses symbol=COIN, not pair format).
# PEPE's CoinGlass listing may require "1000PEPE" on some exchanges.
CVD_FALLBACKS: dict[str, str] = {"PEPE": "1000PEPE"}


# ---------------------------------------------------------------------------
# Table setup
# ---------------------------------------------------------------------------

def ensure_table(interval: str) -> None:
    """Create coinglass_cvd_{interval} + index if missing."""
    table = f"coinglass_cvd_{interval}"
    uq = f"uq_cg_cvd_{interval}"
    idx = f"idx_cg_cvd_{interval}_sym_ts"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL,
                    symbol TEXT NOT NULL,
                    agg_taker_buy_vol DOUBLE PRECISION NOT NULL,
                    agg_taker_sell_vol DOUBLE PRECISION NOT NULL,
                    cum_vol_delta DOUBLE PRECISION NOT NULL,
                    CONSTRAINT {uq} UNIQUE (timestamp, symbol)
                );
                CREATE INDEX IF NOT EXISTS {idx}
                    ON {table}(symbol, timestamp);
            """)


# ---------------------------------------------------------------------------
# Fetcher (single-request, Startup tier)
# ---------------------------------------------------------------------------

async def fetch_cvd(
    session: aiohttp.ClientSession,
    api_key: str,
    symbol_cg: str,
    interval: str,
    days: int,
    verbose: bool = False,
) -> list[dict]:
    """
    Fetch aggregated CVD history for a (symbol, interval) in one request.

    Omits startTime/endTime (silently ignored by server) and relies on
    `limit = days × bars_per_day` to cover the window.
    """
    limit = days * INTERVAL_BARS_PER_DAY[interval]
    params = {
        "exchange_list": CG_EXCHANGES,
        "symbol": symbol_cg,
        "interval": interval,
        "limit": limit,
    }
    data = await _get_json(
        session, CVD_BASE, api_key, params,
        label=f"CVD {symbol_cg}@{interval}", verbose=verbose,
    )
    if data is None:
        return []
    if data.get("code") != "0" or not data.get("data"):
        msg = data.get("msg", "empty") if data else "empty"
        if verbose:
            print(f"  Warning: CVD {msg} for {symbol_cg}@{interval}")
        return []
    return data["data"]


# ---------------------------------------------------------------------------
# Row parser
# ---------------------------------------------------------------------------

def build_cvd_rows(
    coin_canonical: str,
    records: list[dict],
) -> list[tuple]:
    """
    Parse CoinGlass aggregated-CVD records → insert tuples.

    Tuple shape:
        (timestamp_utc, coin_canonical,
         agg_taker_buy_vol, agg_taker_sell_vol, cum_vol_delta)

    Stores the canonical coin name (e.g. `PEPE`), not the CG fallback (`1000PEPE`).
    """
    rows: list[tuple] = []
    for r in records:
        ts = datetime.fromtimestamp(_t(r), tz=timezone.utc)
        rows.append((
            ts,
            coin_canonical,
            _pick_float(r, ("agg_taker_buy_vol",)),
            _pick_float(r, ("agg_taker_sell_vol",)),
            _pick_float(r, ("cum_vol_delta",)),
        ))
    return rows


# ---------------------------------------------------------------------------
# Insert helper
# ---------------------------------------------------------------------------

def insert_cvd(
    rows: list[tuple], coin: str, interval: str,
) -> tuple[int, int]:
    """Insert CVD rows; return (before_count, after_count)."""
    from psycopg2.extras import execute_values
    table = f"coinglass_cvd_{interval}"
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
                    (timestamp, symbol,
                     agg_taker_buy_vol, agg_taker_sell_vol, cum_vol_delta)
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
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="L13 Phase 1: Backfill CoinGlass aggregated CVD history."
    )
    parser.add_argument(
        "--interval", type=str, required=True, choices=["h1", "h2", "h4"],
        help="CoinGlass interval (h1, h2, or h4).",
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

    ensure_table(interval)

    table = f"coinglass_cvd_{interval}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            total_before = cur.fetchone()[0]
    print(f"DB: {table}={total_before} rows before backfill")

    if args.coin and args.coin not in CG_SYMBOLS:
        parser.error(f"--coin must be one of: {list(CG_SYMBOLS)}")
    coins = [args.coin] if args.coin else list(CG_SYMBOLS.keys())

    print(
        f"Backfilling {args.days} days @ {interval}  "
        f"({len(coins)} coin{'s' if len(coins) != 1 else ''})"
    )

    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    async with aiohttp.ClientSession(connector=connector) as session:
        for coin in coins:
            t_coin = time.monotonic()
            primary = CG_SYMBOLS[coin]

            print(f"[{coin}] CVD @{interval} (symbol={primary})...")
            records = await fetch_cvd(
                session, api_key, primary, interval, args.days,
                verbose=args.verbose,
            )
            if not records and coin in CVD_FALLBACKS:
                fb = CVD_FALLBACKS[coin]
                print(f"  No data for {primary}, trying fallback {fb}...")
                await asyncio.sleep(REQUEST_SLEEP_S)
                records = await fetch_cvd(
                    session, api_key, fb, interval, args.days,
                    verbose=args.verbose,
                )

            if records:
                rows = build_cvd_rows(coin, records)
                try:
                    n_before, n_after = insert_cvd(rows, coin, interval)
                    print(
                        f"  ✅ CVD {coin}: fetched {len(rows)} rows, "
                        f"inserted {n_after - n_before} new "
                        f"(was {n_before}, now {n_after})"
                    )
                except Exception as e:
                    print(f"  ❌ CVD DB insert failed for {coin}: {e}")
            else:
                print(f"  ❌ No CVD data for {coin}")

            print(f"  [{coin}] done in {time.monotonic() - t_coin:.1f}s")
            await asyncio.sleep(REQUEST_SLEEP_S)

    # Summary
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT symbol, COUNT(*),
                       MIN(timestamp)::date, MAX(timestamp)::date
                FROM {table}
                GROUP BY symbol
                ORDER BY symbol
            """)
            rows = cur.fetchall()

    print(f"\nSummary ({table}):")
    print(f"  {'Symbol':<8} {'Rows':>6}  {'From':>12}  {'To':>12}")
    for row in rows:
        print(f"  {row[0]:<8} {row[1]:>6}  {row[2]}  {row[3]}")
    if not rows:
        print("  (table empty)")


if __name__ == "__main__":
    asyncio.run(main())
