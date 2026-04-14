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
    verbose: bool = False,
) -> list[dict]:
    """Fetch aggregated liquidation history (4H) for a symbol, paginated."""
    url = f"{CG_BASE}/aggregated-history"
    headers = {"CG-API-KEY": api_key}
    all_records: list[dict] = []
    current_start = start_ts
    page = 0

    while current_start < end_ts:
        page += 1
        params = {
            "symbol": symbol_cg,
            "interval": "h4",
            "exchange_list": CG_EXCHANGES,
            "startTime": current_start,
            "endTime": end_ts,
        }
        t0 = time.monotonic()
        if verbose:
            print(
                f"    → GET page {page}  startTime={current_start}  "
                f"({datetime.fromtimestamp(current_start, tz=timezone.utc).date()})"
            )
        try:
            async with session.get(
                url, headers=headers, params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                status = resp.status
                data = await resp.json()
        except asyncio.TimeoutError:
            print(f"    ❌ Timeout on {symbol_cg} page {page}")
            break
        except Exception as e:
            print(f"    ❌ HTTP error on {symbol_cg} page {page}: {e}")
            break

        elapsed = time.monotonic() - t0
        if verbose:
            print(f"    ← HTTP {status} in {elapsed:.1f}s, code={data.get('code')}")

        if data.get("code") != "0" or not data.get("data"):
            print(f"  Warning: {data.get('msg', 'empty')} for {symbol_cg}")
            break

        records = data["data"]
        all_records.extend(records)
        if verbose:
            print(f"    page {page}: +{len(records)} records (total {len(all_records)})")

        # CoinGlass v4 aggregated-history returns up to 1000 records per call.
        if len(records) < 1000:
            break

        # Advance cursor. Normalize ms → s if the API returns ms.
        last_t = int(records[-1]["time"])
        last_sec = last_t // 1000 if last_t > 10**12 else last_t
        current_start = last_sec + 1
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
    parser.add_argument(
        "--coin", type=str, default=None,
        help="Limit to a single canonical coin (e.g. BTC). Default: all 10.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print per-request progress (URL, status, timing).",
    )
    args = parser.parse_args()

    if args.days < 1 or args.days > 365:
        parser.error("--days must be between 1 and 365")

    cfg = get_config()
    init_pool(cfg)

    api_key = cfg.coinglass_api_key
    if not api_key:
        print("ERROR: LIQ_COINGLASS_API_KEY not set in .env")
        return

    # Pre-flight DB check so we see problems immediately instead of after 10
    # slow HTTP calls.
    ensure_table()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM coinglass_liquidations")
            before = cur.fetchone()[0]
    print(f"DB: {before} rows in coinglass_liquidations before backfill")

    end_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = int(
        (datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp()
    )

    coins = (
        [(args.coin, CG_SYMBOLS[args.coin])] if args.coin
        else list(CG_SYMBOLS.items())
    )
    if args.coin and args.coin not in CG_SYMBOLS:
        parser.error(f"--coin must be one of: {list(CG_SYMBOLS)}")

    print(
        f"Backfilling {args.days} days: "
        f"{datetime.fromtimestamp(start_ts, tz=timezone.utc).date()} → "
        f"{datetime.fromtimestamp(end_ts, tz=timezone.utc).date()}  "
        f"({len(coins)} coin{'s' if len(coins) != 1 else ''})"
    )

    # Use certifi for SSL trust store to avoid LibreSSL/certstore surprises.
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    async with aiohttp.ClientSession(connector=connector) as session:
        for coin, primary_sym in coins:
            t_coin = time.monotonic()
            print(f"[{coin}] Fetching from CoinGlass (symbol={primary_sym})...")
            records = await fetch_liquidations(
                session, api_key, primary_sym, start_ts, end_ts,
                verbose=args.verbose,
            )

            if not records and coin in CG_FALLBACKS:
                fallback_sym = CG_FALLBACKS[coin]
                print(f"  No data for {primary_sym}, trying fallback {fallback_sym}...")
                records = await fetch_liquidations(
                    session, api_key, fallback_sym, start_ts, end_ts,
                    verbose=args.verbose,
                )

            if not records:
                print(f"  ❌ No data for {coin}")
                await asyncio.sleep(REQUEST_SLEEP_S)
                continue

            # Build rows with null-tolerant conversion.
            # CoinGlass v4 aggregated-history response shape:
            #   {"time": <ms>, "aggregated_long_liquidation_usd": ...,
            #    "aggregated_short_liquidation_usd": ...}
            # No count fields are returned on this endpoint → default 0.
            rows = []
            for r in records:
                t_raw = int(r["time"])
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

            from psycopg2.extras import execute_values
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT COUNT(*) FROM coinglass_liquidations WHERE symbol=%s",
                            (coin,),
                        )
                        n_before = cur.fetchone()[0]
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
                        cur.execute(
                            "SELECT COUNT(*) FROM coinglass_liquidations WHERE symbol=%s",
                            (coin,),
                        )
                        n_after = cur.fetchone()[0]
            except Exception as e:
                print(f"  ❌ DB insert failed for {coin}: {e}")
                continue

            print(
                f"  ✅ {coin}: fetched {len(rows)} rows, "
                f"inserted {n_after - n_before} new (was {n_before}, now {n_after}) "
                f"in {time.monotonic() - t_coin:.1f}s"
            )
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
            rows = cur.fetchall()
    print("\nSummary:")
    print(f"  {'Symbol':<8} {'Rows':>6}  {'From':>12}  {'To':>12}")
    for row in rows:
        print(f"  {row[0]:<8} {row[1]:>6}  {row[2]}  {row[3]}")
    if not rows:
        print("  (table is empty)")


if __name__ == "__main__":
    asyncio.run(main())
