#!/usr/bin/env python3
"""
Download 180 days of liquidation history from CoinGlass API.

Usage:
    .venv/bin/python scripts/backfill_coinglass.py [--days 180]

Requires: LIQ_COINGLASS_API_KEY in .env
Rate limit: 30 req/min on Hobbyist tier → 2.5s pause between requests.

Idempotent: uses ON CONFLICT (timestamp, symbol) DO NOTHING.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import get_config
from collectors.db import get_conn, init_pool

CG_BASE = "https://open-api-v4.coinglass.com/api/futures/liquidation"

# Canonical → CoinGlass primary symbol
CG_SYMBOLS: dict[str, str] = {
    "BTC": "BTC", "ETH": "ETH", "SOL": "SOL",
    "DOGE": "DOGE", "LINK": "LINK", "AVAX": "AVAX",
    "SUI": "SUI", "ARB": "ARB", "WIF": "WIF",
    "PEPE": "PEPE",
}

# If the primary symbol returns nothing, try this fallback (e.g. 1000PEPE).
CG_FALLBACKS: dict[str, str] = {"PEPE": "1000PEPE"}

REQUEST_SLEEP_S = 2.5  # 30 req/min rate limit

# CoinGlass v4 requires an explicit exchange_list for aggregated endpoints.
# This captures ~all major perp-futures liquidation venues.
CG_EXCHANGES = "Binance,OKX,Bybit,Bitget,dYdX,Huobi,Gate,Bitmex,CoinEx,Kraken"


async def fetch_liquidations(
    session: aiohttp.ClientSession,
    api_key: str,
    symbol_cg: str,
    start_ts: int,
    end_ts: int,
) -> list[dict]:
    """Fetch aggregated liquidation history (4H) for a symbol, paginated."""
    url = f"{CG_BASE}/aggregated-history"
    headers = {"CG-API-KEY": api_key}
    all_records: list[dict] = []
    current_start = start_ts

    while current_start < end_ts:
        params = {
            "symbol": symbol_cg,
            "interval": "h4",
            "exchange_list": CG_EXCHANGES,
            "startTime": current_start,
            "endTime": end_ts,
        }
        async with session.get(
            url, headers=headers, params=params,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            data = await resp.json()

        if data.get("code") != "0" or not data.get("data"):
            print(f"  Warning: {data.get('msg', 'empty')} for {symbol_cg}")
            break

        records = data["data"]
        all_records.extend(records)

        if len(records) < 500:
            break

        current_start = int(records[-1]["time"]) + 1
        await asyncio.sleep(REQUEST_SLEEP_S)

    return all_records


def ensure_table() -> None:
    """Create the coinglass_liquidations table if it doesn't exist."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS coinglass_liquidations (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL,
                    symbol TEXT NOT NULL,
                    long_vol_usd DOUBLE PRECISION,
                    short_vol_usd DOUBLE PRECISION,
                    long_count INTEGER DEFAULT 0,
                    short_count INTEGER DEFAULT 0,
                    CONSTRAINT uq_cg_liq UNIQUE (timestamp, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_cg_liq_sym_ts
                    ON coinglass_liquidations(symbol, timestamp);
                """
            )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill CoinGlass aggregated liquidation history."
    )
    parser.add_argument("--days", type=int, default=180)
    args = parser.parse_args()

    if args.days < 1 or args.days > 365:
        parser.error("--days must be between 1 and 365")

    cfg = get_config()
    init_pool(cfg)

    api_key = cfg.coinglass_api_key
    if not api_key:
        print("ERROR: LIQ_COINGLASS_API_KEY not set in .env")
        return

    ensure_table()

    end_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = int(
        (datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp()
    )

    print(
        f"Backfilling {args.days} days: "
        f"{datetime.fromtimestamp(start_ts, tz=timezone.utc).date()} → "
        f"{datetime.fromtimestamp(end_ts, tz=timezone.utc).date()}"
    )

    async with aiohttp.ClientSession() as session:
        for coin, primary_sym in CG_SYMBOLS.items():
            print(f"[{coin}] Fetching from CoinGlass (symbol={primary_sym})...")
            records = await fetch_liquidations(
                session, api_key, primary_sym, start_ts, end_ts
            )

            if not records and coin in CG_FALLBACKS:
                fallback_sym = CG_FALLBACKS[coin]
                print(f"  No data for {primary_sym}, trying fallback {fallback_sym}...")
                records = await fetch_liquidations(
                    session, api_key, fallback_sym, start_ts, end_ts
                )

            if not records:
                print(f"  ❌ No data for {coin}")
                await asyncio.sleep(REQUEST_SLEEP_S)
                continue

            # Build rows with null-tolerant float conversion
            rows = []
            for r in records:
                ts = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
                rows.append((
                    ts,
                    coin,
                    float(r.get("longVolUsd") or 0),
                    float(r.get("shortVolUsd") or 0),
                    int(r.get("longCount") or 0),
                    int(r.get("shortCount") or 0),
                ))

            from psycopg2.extras import execute_values
            with get_conn() as conn:
                with conn.cursor() as cur:
                    execute_values(
                        cur,
                        """
                        INSERT INTO coinglass_liquidations
                            (timestamp, symbol, long_vol_usd, short_vol_usd,
                             long_count, short_count)
                        VALUES %s
                        ON CONFLICT (timestamp, symbol) DO NOTHING
                        """,
                        rows,
                        page_size=500,
                    )

            print(f"  ✅ {coin}: {len(rows)} records")
            await asyncio.sleep(REQUEST_SLEEP_S)

    # Summary
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, COUNT(*),
                       MIN(timestamp)::date, MAX(timestamp)::date
                FROM coinglass_liquidations
                GROUP BY symbol ORDER BY symbol
                """
            )
            print("\nSummary:")
            print(f"  {'Symbol':<8} {'Rows':>6}  {'From':>12}  {'To':>12}")
            for row in cur.fetchall():
                print(f"  {row[0]:<8} {row[1]:>6}  {row[2]}  {row[3]}")


if __name__ == "__main__":
    asyncio.run(main())
