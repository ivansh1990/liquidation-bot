#!/usr/bin/env python3
"""
L10 Phase 1: Offline + live-smoke tests for
scripts/backfill_coinglass_netposition.py.

Block 1 (always runs, no network/DB): constants + pair mapping.
Block 2 (always runs, no network/DB): build_netposition_rows parser.
Block 3 (skipped without LIQ_COINGLASS_API_KEY): live HTTP probe of
BTCUSDT h4 for 5 days.

Exit code 0 iff failed == 0. A skipped Block 3 does not increment `failed`,
so a no-key run with Blocks 1+2 clean exits 0.

Usage:
    .venv/bin/python scripts/test_backfill_netposition.py
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
from scripts.backfill_coinglass import CG_SYMBOLS
from scripts.backfill_coinglass_hourly import INTERVAL_BARS_PER_DAY, _t
from scripts.backfill_coinglass_netposition import (
    NETPOS_FALLBACK_PAIRS,
    NETPOS_PAIRS,
    build_netposition_rows,
    fetch_netposition,
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
        "NETPOS_PAIRS['BTC'] == 'BTCUSDT'",
        NETPOS_PAIRS.get("BTC") == "BTCUSDT",
        f"got {NETPOS_PAIRS.get('BTC')!r}",
    )
    report(
        "NETPOS_PAIRS['PEPE'] == 'PEPEUSDT'",
        NETPOS_PAIRS.get("PEPE") == "PEPEUSDT",
        f"got {NETPOS_PAIRS.get('PEPE')!r}",
    )
    report(
        "NETPOS_FALLBACK_PAIRS['PEPE'] == '1000PEPEUSDT'",
        NETPOS_FALLBACK_PAIRS.get("PEPE") == "1000PEPEUSDT",
        f"got {NETPOS_FALLBACK_PAIRS.get('PEPE')!r}",
    )
    report(
        "NETPOS_PAIRS keys match CG_SYMBOLS keys",
        set(NETPOS_PAIRS.keys()) == set(CG_SYMBOLS.keys()),
        f"symmetric diff = "
        f"{set(NETPOS_PAIRS) ^ set(CG_SYMBOLS)}",
    )
    lim_h1 = 180 * INTERVAL_BARS_PER_DAY["h1"]
    report(
        "180d × h1 = 4320 (CLAUDE.md L8 consistency)",
        lim_h1 == 4320,
        f"got {lim_h1}",
    )


# ---------------------------------------------------------------------------
# Block 2: build_netposition_rows parser
# ---------------------------------------------------------------------------

def test_parser() -> None:
    print("\n--- Block 2: build_netposition_rows parser ---")

    # Two synthetic records with known values.
    t0_ms = 1_700_000_000_000  # 2023-11-14T22:13:20Z
    t1_ms = 1_700_000_014_400_000  # (nonsense, but _t normalises ms→s)
    records = [
        {
            "time": t0_ms,
            "net_long_change": 12.5,
            "net_short_change": -7.25,
            "net_long_change_cum": 1000.0,
            "net_short_change_cum": -500.0,
            "net_position_change_cum": 1500.0,
        },
        {
            "time": t0_ms + 3_600_000,  # +1h
            "net_long_change": "3.3",
            "net_short_change": "1.1",
            "net_long_change_cum": "1003.3",
            "net_short_change_cum": "-498.9",
            "net_position_change_cum": "1502.2",
        },
    ]

    rows = build_netposition_rows("PEPE", "Binance", records)

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
        "first row net_long_change is float(12.5)",
        isinstance(rows[0][3], float) and rows[0][3] == 12.5,
        f"got {type(rows[0][3]).__name__}={rows[0][3]!r}",
    )

    report(
        "first row symbol equals canonical coin (not pair)",
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
        records = await fetch_netposition(
            session, cfg.coinglass_api_key, "BTCUSDT", "h4", days,
            verbose=False,
        )

    # 5 days × 6 h4 bars/day = 30, ±2 slack.
    expected = days * INTERVAL_BARS_PER_DAY["h4"]
    n = len(records)
    report(
        f"BTCUSDT h4 5d returns ~{expected} rows (lower bound)",
        n >= expected - 2,
        f"got {n}",
    )
    report(
        f"BTCUSDT h4 5d returns ~{expected} rows (upper bound)",
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
    print("\n--- Block 3: Live smoke test (BTCUSDT h4 5d) ---")
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
