#!/usr/bin/env python3
"""
Download 180 days of aggregated Open Interest + Funding Rate history from
CoinGlass (Hobbyist tier) into two new tables: coinglass_oi and
coinglass_funding.

Usage:
    .venv/bin/python scripts/backfill_coinglass_oi.py [--days 180]
    .venv/bin/python scripts/backfill_coinglass_oi.py --coin BTC --verbose
    .venv/bin/python scripts/backfill_coinglass_oi.py --skip-funding

Requires: LIQ_COINGLASS_API_KEY in .env
Rate limit: 30 req/min on Hobbyist tier → 2.5s pause between requests.
Idempotent: ON CONFLICT (timestamp, symbol) DO NOTHING on both tables.

Design note — Hobbyist tier ignores startTime/endTime on `aggregated-history`
and `oi-weight-ohlc-history`, returning the latest ≤1000 buckets. So we make
one request per (coin, endpoint), filter the window client-side, and rely on
the UNIQUE constraint for idempotency. Same pattern as backfill_coinglass.py.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
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

CG_BASE = "https://open-api-v4.coinglass.com/api/futures"
OI_PATH = "/open-interest/aggregated-history"
# Funding rate endpoints — try in order until one returns non-empty data.
# Per CoinGlass v4 docs (2026-04), the aggregated forms are oi-weight-history
# and vol-weight-history (no `-ohlc-` infix). `aggregated-history` exists for
# liquidations + OI but NOT for funding.
FUNDING_PATHS = (
    "/funding-rate/oi-weight-history",
    "/funding-rate/vol-weight-history",
)
# Funding is naturally 8-hour-cadence on Binance; try h8 first. Fall back to
# h4 (potentially interpolated) if h8 isn't accepted for this symbol.
FUNDING_INTERVALS = ("h8", "h4")

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
CG_EXCHANGES = "Binance,OKX,Bybit,Bitget,dYdX,Huobi,Gate,Bitmex,CoinEx,Kraken"


# --------------------------------------------------------------------------- #
# HTTP helpers                                                                #
# --------------------------------------------------------------------------- #

async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    api_key: str,
    params: dict,
    label: str,
    verbose: bool,
) -> dict | None:
    """Wrap aiohttp GET → JSON with uniform error handling + verbose logging."""
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


def _probe_dump(records: list[dict], label: str) -> None:
    """Print the first record (pretty JSON) so we can verify field names."""
    if not records:
        return
    print(f"  🔍 {label} sample record (first of {len(records)}):")
    try:
        print("     " + json.dumps(records[0], indent=2).replace("\n", "\n     "))
    except Exception:
        print(f"     {records[0]!r}")


# --------------------------------------------------------------------------- #
# OI fetcher                                                                  #
# --------------------------------------------------------------------------- #

async def fetch_oi(
    session: aiohttp.ClientSession,
    api_key: str,
    symbol_cg: str,
    start_ts: int,
    end_ts: int,
    verbose: bool = False,
) -> list[dict]:
    """Fetch aggregated OI OHLC (h4) for a symbol. Single request."""
    url = f"{CG_BASE}{OI_PATH}"
    params = {
        "symbol": symbol_cg,
        "interval": "h4",
        "exchange_list": CG_EXCHANGES,
        "startTime": start_ts,
        "endTime": end_ts,
    }
    data = await _get_json(
        session, url, api_key, params,
        label=f"OI {symbol_cg}", verbose=verbose,
    )
    if data is None:
        return []
    if data.get("code") != "0" or not data.get("data"):
        print(f"  Warning: OI {data.get('msg', 'empty')} for {symbol_cg}")
        return []
    return _window_filter(data["data"], start_ts, end_ts)


# --------------------------------------------------------------------------- #
# Funding fetcher                                                             #
# --------------------------------------------------------------------------- #

async def fetch_funding(
    session: aiohttp.ClientSession,
    api_key: str,
    symbol_cg: str,
    start_ts: int,
    end_ts: int,
    verbose: bool = False,
) -> tuple[list[dict], str | None]:
    """
    Try (path, interval) combos until one returns non-empty data.
    Returns (records, "path:interval" tag) — tag is None if nothing worked.
    """
    last_error_msg: str | None = None
    for path in FUNDING_PATHS:
        for interval in FUNDING_INTERVALS:
            url = f"{CG_BASE}{path}"
            params = {
                "symbol": symbol_cg,
                "interval": interval,
                "exchange_list": CG_EXCHANGES,
                "startTime": start_ts,
                "endTime": end_ts,
            }
            label = f"FR {symbol_cg} {path.rsplit('/', 1)[-1]}@{interval}"
            data = await _get_json(
                session, url, api_key, params,
                label=label, verbose=verbose,
            )
            # Space requests even when iterating combos (rate limit shared).
            await asyncio.sleep(REQUEST_SLEEP_S)
            if data is None:
                last_error_msg = "HTTP error/timeout"
                continue
            if data.get("code") != "0":
                last_error_msg = str(data.get("msg", "non-zero code"))
                if verbose:
                    print(f"    (skipping: code={data.get('code')} msg={last_error_msg})")
                continue
            records = data.get("data") or []
            records = _window_filter(records, start_ts, end_ts)
            if records:
                return records, f"{path.rsplit('/', 1)[-1]}@{interval}"
            last_error_msg = "empty data"

    if last_error_msg:
        print(f"  Warning: funding for {symbol_cg} — all combos failed ({last_error_msg})")
    return [], None


# --------------------------------------------------------------------------- #
# Row parsers (null-tolerant against unknown field names)                     #
# --------------------------------------------------------------------------- #

def _to_ts(r: dict) -> datetime:
    t_raw = int(r.get("time") or r.get("t") or 0)
    t_sec = t_raw / 1000 if t_raw > 10**12 else t_raw
    return datetime.fromtimestamp(t_sec, tz=timezone.utc)


def _pick_float(r: dict, keys: tuple[str, ...]) -> float:
    """First non-None/non-empty numeric value across candidate keys."""
    for k in keys:
        v = r.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def build_oi_rows(records: list[dict], coin: str) -> list[tuple]:
    # Expected shape (OHLC): {"time", "open", "high", "low", "close"} with values
    # in USD. Fallback key names cover documented variants.
    rows: list[tuple] = []
    for r in records:
        rows.append((
            _to_ts(r),
            coin,
            _pick_float(r, ("close", "c", "openInterest",
                            "aggregated_open_interest_usd",
                            "open_interest_usd", "oi")),
            _pick_float(r, ("high", "h", "oi_high")),
            _pick_float(r, ("low", "l", "oi_low")),
        ))
    return rows


def build_funding_rows(records: list[dict], coin: str) -> list[tuple]:
    # Funding endpoints return OHLC of the rate; `close` is the period-end rate.
    rows: list[tuple] = []
    for r in records:
        rows.append((
            _to_ts(r),
            coin,
            _pick_float(r, ("close", "c", "fundingRate", "funding_rate",
                            "rate", "value")),
        ))
    return rows


# --------------------------------------------------------------------------- #
# DB setup + inserts                                                          #
# --------------------------------------------------------------------------- #

def ensure_tables() -> None:
    """Create coinglass_oi + coinglass_funding if they don't exist."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS coinglass_oi (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL,
                    symbol TEXT NOT NULL,
                    open_interest DOUBLE PRECISION,
                    oi_high DOUBLE PRECISION,
                    oi_low DOUBLE PRECISION,
                    CONSTRAINT uq_cg_oi UNIQUE (timestamp, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_cg_oi_sym_ts
                    ON coinglass_oi(symbol, timestamp);

                CREATE TABLE IF NOT EXISTS coinglass_funding (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL,
                    symbol TEXT NOT NULL,
                    funding_rate DOUBLE PRECISION,
                    CONSTRAINT uq_cg_fr UNIQUE (timestamp, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_cg_fr_sym_ts
                    ON coinglass_funding(symbol, timestamp);
                """
            )


def insert_oi(rows: list[tuple], coin: str) -> tuple[int, int]:
    """Insert OI rows; return (before_count, after_count) for this coin."""
    from psycopg2.extras import execute_values
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM coinglass_oi WHERE symbol=%s", (coin,),
            )
            before = cur.fetchone()[0]
            execute_values(
                cur,
                """
                INSERT INTO coinglass_oi
                    (timestamp, symbol, open_interest, oi_high, oi_low)
                VALUES %s
                ON CONFLICT (timestamp, symbol) DO NOTHING
                """,
                rows,
                page_size=500,
            )
            cur.execute(
                "SELECT COUNT(*) FROM coinglass_oi WHERE symbol=%s", (coin,),
            )
            after = cur.fetchone()[0]
    return before, after


def insert_funding(rows: list[tuple], coin: str) -> tuple[int, int]:
    from psycopg2.extras import execute_values
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM coinglass_funding WHERE symbol=%s", (coin,),
            )
            before = cur.fetchone()[0]
            execute_values(
                cur,
                """
                INSERT INTO coinglass_funding
                    (timestamp, symbol, funding_rate)
                VALUES %s
                ON CONFLICT (timestamp, symbol) DO NOTHING
                """,
                rows,
                page_size=500,
            )
            cur.execute(
                "SELECT COUNT(*) FROM coinglass_funding WHERE symbol=%s", (coin,),
            )
            after = cur.fetchone()[0]
    return before, after


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill CoinGlass aggregated OI + Funding history."
    )
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument(
        "--coin", type=str, default=None,
        help="Limit to a single canonical coin (e.g. BTC). Default: all 10.",
    )
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-request URL, status, timing.")
    parser.add_argument("--skip-oi", action="store_true",
                        help="Skip OI fetch (funding only).")
    parser.add_argument("--skip-funding", action="store_true",
                        help="Skip funding fetch (OI only).")
    args = parser.parse_args()

    if args.days < 1 or args.days > 365:
        parser.error("--days must be between 1 and 365")
    if args.skip_oi and args.skip_funding:
        parser.error("--skip-oi and --skip-funding can't both be set")

    cfg = get_config()
    init_pool(cfg)

    api_key = cfg.coinglass_api_key
    if not api_key:
        print("ERROR: LIQ_COINGLASS_API_KEY not set in .env")
        return

    ensure_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM coinglass_oi")
            oi_before = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM coinglass_funding")
            fr_before = cur.fetchone()[0]
    print(
        f"DB: coinglass_oi={oi_before} rows, "
        f"coinglass_funding={fr_before} rows before backfill"
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
        f"Backfilling {args.days} days: "
        f"{datetime.fromtimestamp(start_ts, tz=timezone.utc).date()} → "
        f"{datetime.fromtimestamp(end_ts, tz=timezone.utc).date()}  "
        f"({len(coins)} coin{'s' if len(coins) != 1 else ''}; "
        f"oi={'skip' if args.skip_oi else 'on'}, "
        f"funding={'skip' if args.skip_funding else 'on'})"
    )

    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    # First-coin-of-each-endpoint probe: print raw sample once, so we can verify
    # field names even on a non-verbose run.
    probed_oi = False
    probed_fr = False
    # Track which funding (path, interval) worked first — so session note can
    # record the real cadence.
    funding_combo_used: str | None = None

    async with aiohttp.ClientSession(connector=connector) as session:
        for coin, primary_sym in coins:
            t_coin = time.monotonic()

            # -------- OI --------
            if not args.skip_oi:
                print(f"[{coin}] OI (symbol={primary_sym})...")
                oi_records = await fetch_oi(
                    session, api_key, primary_sym, start_ts, end_ts,
                    verbose=args.verbose,
                )
                if not oi_records and coin in CG_FALLBACKS:
                    fb = CG_FALLBACKS[coin]
                    print(f"  No OI for {primary_sym}, trying fallback {fb}...")
                    oi_records = await fetch_oi(
                        session, api_key, fb, start_ts, end_ts,
                        verbose=args.verbose,
                    )

                if oi_records:
                    if not probed_oi:
                        _probe_dump(oi_records, "OI")
                        probed_oi = True
                    rows = build_oi_rows(oi_records, coin)
                    try:
                        n_before, n_after = insert_oi(rows, coin)
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

            # -------- Funding --------
            if not args.skip_funding:
                print(f"[{coin}] Funding (symbol={primary_sym})...")
                fr_records, combo = await fetch_funding(
                    session, api_key, primary_sym, start_ts, end_ts,
                    verbose=args.verbose,
                )
                if not fr_records and coin in CG_FALLBACKS:
                    fb = CG_FALLBACKS[coin]
                    print(f"  No funding for {primary_sym}, trying fallback {fb}...")
                    fr_records, combo = await fetch_funding(
                        session, api_key, fb, start_ts, end_ts,
                        verbose=args.verbose,
                    )

                if fr_records:
                    if funding_combo_used is None and combo:
                        funding_combo_used = combo
                    if not probed_fr:
                        _probe_dump(fr_records, f"Funding [{combo}]")
                        probed_fr = True
                    rows = build_funding_rows(fr_records, coin)
                    try:
                        n_before, n_after = insert_funding(rows, coin)
                        print(
                            f"  ✅ Funding {coin} [{combo}]: fetched {len(rows)} rows, "
                            f"inserted {n_after - n_before} new "
                            f"(was {n_before}, now {n_after})"
                        )
                    except Exception as e:
                        print(f"  ❌ Funding DB insert failed for {coin}: {e}")
                else:
                    print(f"  ❌ No funding data for {coin}")
                # Note: fetch_funding sleeps internally between combo tries;
                # no extra sleep needed here before the next coin's OI.

            print(f"  [{coin}] done in {time.monotonic() - t_coin:.1f}s")

    # Summary
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 'coinglass_oi' AS tbl, symbol, COUNT(*),
                       MIN(timestamp)::date, MAX(timestamp)::date
                FROM coinglass_oi GROUP BY symbol
                UNION ALL
                SELECT 'coinglass_funding', symbol, COUNT(*),
                       MIN(timestamp)::date, MAX(timestamp)::date
                FROM coinglass_funding GROUP BY symbol
                ORDER BY 1, 2
                """
            )
            rows = cur.fetchall()

    print("\nSummary:")
    print(f"  {'Table':<20} {'Symbol':<8} {'Rows':>6}  {'From':>12}  {'To':>12}")
    for row in rows:
        print(f"  {row[0]:<20} {row[1]:<8} {row[2]:>6}  {row[3]}  {row[4]}")
    if not rows:
        print("  (both tables empty)")
    if funding_combo_used:
        print(f"\nFunding combo used: {funding_combo_used}")


if __name__ == "__main__":
    asyncio.run(main())
