#!/usr/bin/env python3
"""
Backfill 30 days of Binance futures historical data.

Populates binance_oi, binance_funding, binance_ls_ratio, binance_taker so
analysis scripts have data immediately instead of waiting for the hourly
collector to accumulate rows.

Usage:
    .venv/bin/python scripts/backfill_binance.py [--days 30]

Idempotent: uses ON CONFLICT (timestamp, symbol) DO NOTHING so reruns and
coexistence with the hourly collector are safe. Unique constraints are
added lazily on first run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import ccxt
import certifi
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import (
    Config,
    binance_ccxt_symbol,
    binance_raw_symbol,
    get_config,
)
from collectors.db import get_conn, init_pool

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Binance /futures/data/* endpoints cap each response at 500 records.
MAX_LIMIT = 500
# Rate-limit courtesy between paginated requests.
REQUEST_SLEEP_S = 0.2
# Courtesy pause between coins.
COIN_SLEEP_S = 0.5


# ---------------------------------------------------------------------------
# Schema: add UNIQUE constraints (idempotent)
# ---------------------------------------------------------------------------

UNIQUE_CONSTRAINTS_SQL = """
DO $$ BEGIN
    ALTER TABLE binance_oi       ADD CONSTRAINT uq_boi UNIQUE (timestamp, symbol);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE binance_funding  ADD CONSTRAINT uq_bfr UNIQUE (timestamp, symbol);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE binance_ls_ratio ADD CONSTRAINT uq_bls UNIQUE (timestamp, symbol);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE binance_taker    ADD CONSTRAINT uq_btk UNIQUE (timestamp, symbol);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
"""


def ensure_unique_constraints() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(UNIQUE_CONSTRAINTS_SQL)
    log.info("UNIQUE(timestamp, symbol) constraints ensured on all 4 tables")


# ---------------------------------------------------------------------------
# Pagination helper for /futures/data/* endpoints (OI, L/S, taker)
# ---------------------------------------------------------------------------

async def _fetch_paginated(
    session: aiohttp.ClientSession,
    url: str,
    raw_symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    records: list[dict] = []
    cur_start = start_ms
    while cur_start < end_ms:
        params = {
            "symbol": raw_symbol,
            "period": "1h",
            "limit": MAX_LIMIT,
            "startTime": cur_start,
            "endTime": end_ms,
        }
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            resp.raise_for_status()
            batch = await resp.json()
        if not batch:
            break
        records.extend(batch)
        last_ts = int(batch[-1]["timestamp"])
        cur_start = last_ts + 1
        if len(batch) < MAX_LIMIT:
            break
        await asyncio.sleep(REQUEST_SLEEP_S)
    return records


def _ts(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Backfill functions (one per table)
# ---------------------------------------------------------------------------

async def backfill_oi(
    session: aiohttp.ClientSession,
    cfg: Config,
    coin: str,
    raw_symbol: str,
    start_ms: int,
    end_ms: int,
) -> int:
    url = f"{cfg.binance_fapi_url}/futures/data/openInterestHist"
    records = await _fetch_paginated(session, url, raw_symbol, start_ms, end_ms)
    if not records:
        return 0
    rows = [
        (
            _ts(r["timestamp"]),
            coin,
            float(r["sumOpenInterest"]),
            float(r["sumOpenInterestValue"]),
        )
        for r in records
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO binance_oi "
                "(timestamp, symbol, open_interest, open_interest_usd) "
                "VALUES %s ON CONFLICT (timestamp, symbol) DO NOTHING",
                rows,
                page_size=500,
            )
    return len(rows)


async def backfill_funding(
    exchange: ccxt.binance,
    coin: str,
    ccxt_symbol: str,
    start_ms: int,
) -> int:
    # ccxt call is blocking; run it off the event loop.
    records = await asyncio.to_thread(
        exchange.fetch_funding_rate_history, ccxt_symbol, start_ms, 1000
    )
    if not records:
        return 0
    rows = []
    for r in records:
        ts_ms = r.get("timestamp")
        if ts_ms is None:
            continue
        funding_rate = r.get("fundingRate")
        if funding_rate is None:
            continue
        mark_px = r.get("markPrice")
        info = r.get("info") or {}
        if mark_px is None:
            # Binance includes markPrice in info on some responses.
            raw_mp = info.get("markPrice")
            if raw_mp is not None:
                try:
                    mark_px = float(raw_mp)
                except (TypeError, ValueError):
                    mark_px = None
        rows.append(
            (
                _ts(ts_ms),
                coin,
                float(funding_rate),
                float(mark_px) if mark_px is not None else None,
            )
        )
    if not rows:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO binance_funding "
                "(timestamp, symbol, funding_rate, mark_price) "
                "VALUES %s ON CONFLICT (timestamp, symbol) DO NOTHING",
                rows,
                page_size=500,
            )
    return len(rows)


async def backfill_ls_ratio(
    session: aiohttp.ClientSession,
    cfg: Config,
    coin: str,
    raw_symbol: str,
    start_ms: int,
    end_ms: int,
) -> int:
    url = f"{cfg.binance_fapi_url}/futures/data/topLongShortAccountRatio"
    records = await _fetch_paginated(session, url, raw_symbol, start_ms, end_ms)
    if not records:
        return 0
    rows = [
        (
            _ts(r["timestamp"]),
            coin,
            float(r["longAccount"]),
            float(r["shortAccount"]),
            float(r["longShortRatio"]),
        )
        for r in records
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO binance_ls_ratio "
                "(timestamp, symbol, long_account_pct, short_account_pct, long_short_ratio) "
                "VALUES %s ON CONFLICT (timestamp, symbol) DO NOTHING",
                rows,
                page_size=500,
            )
    return len(rows)


async def backfill_taker(
    session: aiohttp.ClientSession,
    cfg: Config,
    coin: str,
    raw_symbol: str,
    start_ms: int,
    end_ms: int,
) -> int:
    url = f"{cfg.binance_fapi_url}/futures/data/takerlongshortRatio"
    records = await _fetch_paginated(session, url, raw_symbol, start_ms, end_ms)
    if not records:
        return 0
    rows = [
        (
            _ts(r["timestamp"]),
            coin,
            float(r["buyVol"]),
            float(r["sellVol"]),
            float(r["buySellRatio"]),
        )
        for r in records
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO binance_taker "
                "(timestamp, symbol, buy_vol, sell_vol, buy_sell_ratio) "
                "VALUES %s ON CONFLICT (timestamp, symbol) DO NOTHING",
                rows,
                page_size=500,
            )
    return len(rows)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run(cfg: Config, days: int) -> None:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000
    )
    log.info(
        "Backfilling %d days: %s → %s",
        days,
        _ts(start_ms).isoformat(timespec="seconds"),
        _ts(end_ms).isoformat(timespec="seconds"),
    )

    exchange = ccxt.binance({"options": {"defaultType": "swap"}})

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as session:
        for coin in cfg.hl_coins:
            raw_symbol = binance_raw_symbol(coin)
            ccxt_symbol = binance_ccxt_symbol(coin)
            t0 = time.time()

            n_oi = await backfill_oi(
                session, cfg, coin, raw_symbol, start_ms, end_ms
            )
            n_funding = await backfill_funding(
                exchange, coin, ccxt_symbol, start_ms
            )
            n_ls = await backfill_ls_ratio(
                session, cfg, coin, raw_symbol, start_ms, end_ms
            )
            n_taker = await backfill_taker(
                session, cfg, coin, raw_symbol, start_ms, end_ms
            )

            log.info(
                "[%s] OI=%d Funding=%d L/S=%d Taker=%d (%.1fs)",
                coin, n_oi, n_funding, n_ls, n_taker, time.time() - t0,
            )
            await asyncio.sleep(COIN_SLEEP_S)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

SUMMARY_TABLES = ["binance_oi", "binance_funding", "binance_ls_ratio", "binance_taker"]


def print_summary() -> None:
    log.info("Summary:")
    log.info("  %-18s %8s  %-10s  %-10s", "Table", "Rows", "From", "To")
    with get_conn() as conn:
        with conn.cursor() as cur:
            for tbl in SUMMARY_TABLES:
                cur.execute(
                    f"SELECT COUNT(*), MIN(timestamp)::date, MAX(timestamp)::date "
                    f"FROM {tbl}"
                )
                count, dmin, dmax = cur.fetchone()
                log.info(
                    "  %-18s %8d  %-10s  %-10s",
                    tbl, count or 0,
                    dmin.isoformat() if dmin else "-",
                    dmax.isoformat() if dmax else "-",
                )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill 30 days of Binance futures history."
    )
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    if args.days < 1 or args.days > 30:
        parser.error("--days must be between 1 and 30")

    cfg = get_config()
    init_pool(cfg)
    ensure_unique_constraints()

    t0 = time.time()
    asyncio.run(run(cfg, args.days))
    log.info("Backfill complete in %.1fs", time.time() - t0)
    print_summary()


if __name__ == "__main__":
    main()
