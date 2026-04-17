#!/usr/bin/env python3
"""
L13 Phase 1: Offline + live-smoke tests for
scripts/backfill_coinglass_cvd.py.

Block 1 (always runs, no network/DB): constants + pair mapping.
Block 2 (always runs, no network/DB): build_cvd_rows parser.
Block 3 (skipped without LIQ_COINGLASS_API_KEY): live HTTP probe of
BTC h4 for 5 days.

Exit code 0 iff failed == 0. A skipped Block 3 does not increment `failed`,
so a no-key run with Blocks 1+2 clean exits 0.

Usage:
    .venv/bin/python scripts/test_backfill_cvd.py
"""
from __future__ import annotations

import asyncio
import os
import ssl
import sys
from datetime import datetime, timedelta, timezone

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import get_config
from scripts.backfill_coinglass import CG_EXCHANGES
from scripts.backfill_coinglass_hourly import INTERVAL_BARS_PER_DAY, _t
from scripts.backfill_coinglass_cvd import (
    CVD_BASE,
    CVD_FALLBACKS,
    build_cvd_rows,
    fetch_cvd,
)

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
# Block 1: constants & pair mapping
# ---------------------------------------------------------------------------

def test_constants() -> None:
    print("\n--- Block 1: constants + pair mapping ---")

    report(
        "CVD_BASE ends with /aggregated-cvd/history",
        CVD_BASE.endswith("/aggregated-cvd/history"),
        f"got {CVD_BASE!r}",
    )
    report(
        "CVD_FALLBACKS['PEPE'] == '1000PEPE' (coin-level, not pair)",
        CVD_FALLBACKS.get("PEPE") == "1000PEPE",
        f"got {CVD_FALLBACKS.get('PEPE')!r}",
    )
    report(
        "CG_EXCHANGES is non-empty string",
        isinstance(CG_EXCHANGES, str) and len(CG_EXCHANGES) > 0,
        f"type={type(CG_EXCHANGES).__name__}, len={len(CG_EXCHANGES) if isinstance(CG_EXCHANGES, str) else 'N/A'}",
    )
    report(
        "INTERVAL_BARS_PER_DAY contains h1, h2, h4",
        {"h1", "h2", "h4"} <= set(INTERVAL_BARS_PER_DAY.keys()),
        f"keys={sorted(INTERVAL_BARS_PER_DAY.keys())}",
    )
    lim_h1 = 180 * INTERVAL_BARS_PER_DAY["h1"]
    report(
        "180d × h1 = 4320 (request-size consistency)",
        lim_h1 == 4320,
        f"got {lim_h1}",
    )


# ---------------------------------------------------------------------------
# Block 2: build_cvd_rows parser
# ---------------------------------------------------------------------------

def test_parser() -> None:
    print("\n--- Block 2: build_cvd_rows parser ---")

    # Two synthetic records with known values.
    t0_ms = 1_700_000_000_000  # 2023-11-14T22:13:20Z
    records = [
        {
            "time": t0_ms,
            "agg_taker_buy_vol": 1_250_000.0,
            "agg_taker_sell_vol": 900_000.0,
            "cum_vol_delta": 350_000.0,
        },
        {
            "time": t0_ms + 3_600_000,  # +1h
            "agg_taker_buy_vol": "555.5",
            "agg_taker_sell_vol": "111.1",
            "cum_vol_delta": "444.4",
        },
    ]

    rows = build_cvd_rows("PEPE", records)

    report(
        "output length matches input length",
        len(rows) == len(records),
        f"got {len(rows)}",
    )

    expected_ts0 = datetime.fromtimestamp(t0_ms / 1000, tz=timezone.utc)
    ts0 = rows[0][0]
    report(
        "first row timestamp is UTC and matches expected",
        ts0.tzinfo == timezone.utc and ts0 == expected_ts0,
        f"got tz={ts0.tzinfo}, value={ts0.isoformat()}",
    )

    report(
        "first row agg_taker_buy_vol is float(1_250_000.0)",
        isinstance(rows[0][2], float) and rows[0][2] == 1_250_000.0,
        f"got {type(rows[0][2]).__name__}={rows[0][2]!r}",
    )

    report(
        "first row symbol equals canonical coin (not CG fallback)",
        rows[0][1] == "PEPE",
        f"got {rows[0][1]!r}",
    )


# ---------------------------------------------------------------------------
# Block 3: live smoke test (skipped without key)
# ---------------------------------------------------------------------------

async def _live_smoke() -> None:
    cfg = get_config()
    if not cfg.coinglass_api_key:
        print("  [SKIP] live smoke test — no LIQ_COINGLASS_API_KEY")
        return

    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    days = 5
    async with aiohttp.ClientSession(connector=connector) as session:
        records = await fetch_cvd(
            session, cfg.coinglass_api_key, "BTC", "h4", days,
            verbose=False,
        )

    # 5 days × 6 h4 bars/day = 30, ±2 slack.
    expected = days * INTERVAL_BARS_PER_DAY["h4"]
    n = len(records)
    report(
        f"BTC h4 5d returns ~{expected} rows (lower bound)",
        n >= expected - 2,
        f"got {n}",
    )
    report(
        f"BTC h4 5d returns ~{expected} rows (upper bound)",
        n <= expected + 2,
        f"got {n}",
    )

    now = datetime.now(timezone.utc)
    lower = now - timedelta(days=6)
    ts0 = (
        datetime.fromtimestamp(_t(records[0]), tz=timezone.utc)
        if records else None
    )
    ok_ts = ts0 is not None and lower <= ts0 <= now
    report(
        "first row timestamp within [now-6d, now]",
        ok_ts,
        f"got {ts0.isoformat() if ts0 else 'no rows'}",
    )


def test_live_smoke() -> None:
    print("\n--- Block 3: Live smoke test (BTC h4 5d) ---")
    asyncio.run(_live_smoke())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    test_constants()
    test_parser()
    test_live_smoke()

    print()
    print(f"PASS: {passed} | FAIL: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
