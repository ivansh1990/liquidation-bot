#!/usr/bin/env python3
"""
L8: Offline + live-smoke tests for scripts/backfill_coinglass_hourly.py.

Block 1 (always runs, no network/DB): verifies INTERVAL_BARS_PER_DAY and its
derived single-request `limit` values match the CLAUDE.md "Expected data
volumes" table.

Block 2 (skipped without LIQ_COINGLASS_API_KEY): live HTTP probe of BTC h1
for 5 days. Asserts that the single-request fetch returns roughly
5 × 24 = 120 bars covering a ~5-day window.

Exit code 0 iff failed == 0. A skipped Block 2 does not increment `failed`,
so a no-key run with a clean Block 1 exits 0.

Usage:
    .venv/bin/python scripts/test_backfill_hourly.py
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
from scripts.backfill_coinglass_hourly import (
    INTERVAL_BARS_PER_DAY,
    _t,
    fetch_liquidations,
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
# Block 1: INTERVAL_BARS_PER_DAY constant + limit derivation
# ---------------------------------------------------------------------------

def test_interval_bars_per_day() -> None:
    print("\n--- Block 1: INTERVAL_BARS_PER_DAY + limit derivation ---")

    report("h1 bars/day = 24", INTERVAL_BARS_PER_DAY["h1"] == 24,
           f"got {INTERVAL_BARS_PER_DAY['h1']}")
    report("h2 bars/day = 12", INTERVAL_BARS_PER_DAY["h2"] == 12,
           f"got {INTERVAL_BARS_PER_DAY['h2']}")
    report("h4 bars/day = 6", INTERVAL_BARS_PER_DAY["h4"] == 6,
           f"got {INTERVAL_BARS_PER_DAY['h4']}")

    # Binds the constant to the "Expected data volumes" table in CLAUDE.md L8.
    lim_h1 = 180 * INTERVAL_BARS_PER_DAY["h1"]
    lim_h2 = 180 * INTERVAL_BARS_PER_DAY["h2"]
    lim_h4 = 180 * INTERVAL_BARS_PER_DAY["h4"]
    report("180d × h1 = 4320", lim_h1 == 4320, f"got {lim_h1}")
    report("180d × h2 = 2160", lim_h2 == 2160, f"got {lim_h2}")
    report("180d × h4 = 1080", lim_h4 == 1080, f"got {lim_h4}")


# ---------------------------------------------------------------------------
# Block 2: Live smoke test (skipped without key, exit 0 still OK)
# ---------------------------------------------------------------------------

async def _live_smoke() -> None:
    cfg = get_config()
    if not cfg.coinglass_api_key:
        print("  [SKIP] live smoke test — no LIQ_COINGLASS_API_KEY")
        # Block 1 already ran; main() returns 0 if passed==6 and failed==0.
        return

    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    days = 5
    async with aiohttp.ClientSession(connector=connector) as session:
        rows = await fetch_liquidations(
            session, cfg.coinglass_api_key, "BTC", "h1", days, verbose=False,
        )

    # Upper and lower bounds: 5 days × 24 bars/day = 120, ±5 bars slack.
    expected = days * 24
    n = len(rows)
    report(
        f"BTC h1 5d returns ~{expected} rows (lower bound)",
        n >= expected - 5,
        f"got {n}",
    )
    report(
        f"BTC h1 5d returns ~{expected} rows (upper bound)",
        n <= expected + 5,
        f"got {n}",
    )

    # First row's timestamp should be within [now - 6d, now].
    # Rows are ordered oldest→newest by CoinGlass, so rows[0] is the window start.
    now = datetime.now(timezone.utc)
    lower = now - timedelta(days=6)
    upper = now
    ts0 = datetime.fromtimestamp(_t(rows[0]), tz=timezone.utc) if rows else None
    ok_ts = ts0 is not None and lower <= ts0 <= upper
    report(
        "first row timestamp within [now-6d, now]",
        ok_ts,
        f"got {ts0.isoformat() if ts0 else 'no rows'}",
    )


def test_live_smoke() -> None:
    print("\n--- Block 2: Live smoke test (BTC h1 5d) ---")
    asyncio.run(_live_smoke())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    test_interval_bars_per_day()
    test_live_smoke()

    print()
    print(f"PASS: {passed} | FAIL: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
