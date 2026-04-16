#!/usr/bin/env python3
"""
Offline tests for collectors/coinglass_oi_collector.py.

Six blocks (no live APIs or DB required):
  1. _pick_float — multi-key fallback, string parsing, missing keys.
  2. build_oi_rows — synthetic CoinGlass OI response → tuple structure.
  3. build_funding_rows — synthetic funding response → tuple structure.
  4. fetch_latest_oi — mocked HTTP, PEPE fallback, tail slicing.
  5. fetch_latest_funding — mocked HTTP, combo fallback.
  6. (Optional) Live smoke test — fetch BTC OI, skipped if no API key.

Run directly: .venv/bin/python scripts/test_coinglass_collector.py
Exit code 0 on all-pass, 1 otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backfill_coinglass_oi import (
    _pick_float,
    build_funding_rows,
    build_oi_rows,
)
from collectors.coinglass_oi_collector import (
    TAIL_BARS,
    _cg_symbols,
    fetch_latest_funding,
    fetch_latest_oi,
)

logging.basicConfig(
    level=logging.WARNING,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
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
# Block 1: _pick_float
# ---------------------------------------------------------------------------

def test_pick_float() -> None:
    print("\n--- Block 1: _pick_float ---")

    # First matching key wins
    r = {"close": "123.45", "c": "999"}
    val = _pick_float(r, ("close", "c"))
    report("first key wins", abs(val - 123.45) < 1e-9)

    # Skip None, pick next
    r2 = {"close": None, "c": "42.0"}
    val2 = _pick_float(r2, ("close", "c"))
    report("skip None", abs(val2 - 42.0) < 1e-9)

    # Skip empty string
    r3 = {"close": "", "openInterest": "100"}
    val3 = _pick_float(r3, ("close", "openInterest"))
    report("skip empty string", abs(val3 - 100.0) < 1e-9)

    # All missing → 0.0
    r4 = {"foo": "bar"}
    val4 = _pick_float(r4, ("close", "c", "oi"))
    report("all missing → 0.0", val4 == 0.0)

    # String-valued large number (CoinGlass returns strings)
    r5 = {"close": "74879897315"}
    val5 = _pick_float(r5, ("close",))
    report("large string float", abs(val5 - 74879897315.0) < 1.0)


# ---------------------------------------------------------------------------
# Block 2: build_oi_rows
# ---------------------------------------------------------------------------

def test_build_oi_rows() -> None:
    print("\n--- Block 2: build_oi_rows ---")

    records = [
        {
            "time": 1713100800000,  # 2024-04-14 16:00 UTC (ms)
            "open": "70000000000",
            "high": "72000000000",
            "low": "69000000000",
            "close": "71000000000",
        },
        {
            "time": 1713115200000,
            "close": "71500000000",
            "high": "73000000000",
            "low": "70000000000",
        },
    ]
    rows = build_oi_rows(records, "BTC")
    report("row count", len(rows) == 2)
    report("coin field", rows[0][1] == "BTC")
    report("timestamp is datetime", isinstance(rows[0][0], datetime))
    report("timestamp has tz", rows[0][0].tzinfo is not None)
    report("close parsed", abs(rows[0][2] - 71000000000.0) < 1.0)
    report("high parsed", abs(rows[0][3] - 72000000000.0) < 1.0)
    report("low parsed", abs(rows[0][4] - 69000000000.0) < 1.0)


# ---------------------------------------------------------------------------
# Block 3: build_funding_rows
# ---------------------------------------------------------------------------

def test_build_funding_rows() -> None:
    print("\n--- Block 3: build_funding_rows ---")

    records = [
        {"time": 1713100800, "close": "0.003537"},  # seconds
        {"time": 1713129600, "close": "-0.001200"},
    ]
    rows = build_funding_rows(records, "ETH")
    report("row count", len(rows) == 2)
    report("coin field", rows[0][1] == "ETH")
    report("funding_rate parsed", abs(rows[0][2] - 0.003537) < 1e-9)
    report("negative rate", abs(rows[1][2] - (-0.001200)) < 1e-9)


# ---------------------------------------------------------------------------
# Block 4: fetch_latest_oi (mocked HTTP)
# ---------------------------------------------------------------------------

def test_fetch_latest_oi() -> None:
    print("\n--- Block 4: fetch_latest_oi ---")

    # Generate 10 fake bars
    bars = [
        {"time": 1713100800000 + i * 14400000, "close": str(70000 + i * 100),
         "high": str(71000 + i * 100), "low": str(69000 + i * 100)}
        for i in range(10)
    ]
    ok_response = {"code": "0", "data": bars}

    async def run_oi_test():
        # Normal coin: returns last TAIL_BARS
        with patch(
            "collectors.coinglass_oi_collector._get_json",
            new_callable=AsyncMock,
            return_value=ok_response,
        ):
            session = AsyncMock()
            records = await fetch_latest_oi(session, "BTC", "fake-key")
            report("returns records", len(records) == TAIL_BARS)
            report("last bar is newest", records[-1] == bars[-1])

        # PEPE fallback: first call returns empty, second returns data
        call_count = 0

        async def mock_get_json_pepe(session, url, api_key, params, label):
            nonlocal call_count
            call_count += 1
            if params["symbol"] == "PEPE":
                return {"code": "0", "data": []}
            return ok_response

        with patch(
            "collectors.coinglass_oi_collector._get_json",
            side_effect=mock_get_json_pepe,
        ):
            session = AsyncMock()
            records = await fetch_latest_oi(session, "PEPE", "fake-key")
            report("PEPE fallback triggered", call_count == 2)
            report("PEPE fallback returns data", len(records) == TAIL_BARS)

        # All symbols fail → empty
        with patch(
            "collectors.coinglass_oi_collector._get_json",
            new_callable=AsyncMock,
            return_value={"code": "1", "data": None},
        ):
            session = AsyncMock()
            records = await fetch_latest_oi(session, "SOL", "fake-key")
            report("failure returns empty", records == [])

    asyncio.run(run_oi_test())


# ---------------------------------------------------------------------------
# Block 5: fetch_latest_funding (mocked HTTP, combo fallback)
# ---------------------------------------------------------------------------

def test_fetch_latest_funding() -> None:
    print("\n--- Block 5: fetch_latest_funding ---")

    bars = [
        {"time": 1713100800000 + i * 28800000, "close": "0.0001"}
        for i in range(3)
    ]

    async def run_funding_test():
        combo_call_count = 0

        async def mock_get_json_combo(session, url, api_key, params, label):
            nonlocal combo_call_count
            combo_call_count += 1
            # First combo (oi-weight-history@h8) returns empty
            if "oi-weight-history" in url and params["interval"] == "h8":
                return {"code": "0", "data": []}
            # Second combo (oi-weight-history@h4) returns data
            if "oi-weight-history" in url and params["interval"] == "h4":
                return {"code": "0", "data": bars}
            return {"code": "0", "data": []}

        with patch(
            "collectors.coinglass_oi_collector._get_json",
            side_effect=mock_get_json_combo,
        ), patch("collectors.coinglass_oi_collector.asyncio.sleep", new_callable=AsyncMock):
            session = AsyncMock()
            records, tag = await fetch_latest_funding(session, "BTC", "fake-key")
            report("funding returns records", len(records) == 3)
            report("tag includes interval", tag is not None and "@h4" in tag)

        # All combos fail → empty
        with patch(
            "collectors.coinglass_oi_collector._get_json",
            new_callable=AsyncMock,
            return_value={"code": "0", "data": []},
        ), patch("collectors.coinglass_oi_collector.asyncio.sleep", new_callable=AsyncMock):
            session = AsyncMock()
            records, tag = await fetch_latest_funding(session, "BTC", "fake-key")
            report("all combos fail → empty", records == [])
            report("tag is None on failure", tag is None)

    asyncio.run(run_funding_test())


# ---------------------------------------------------------------------------
# Block 6: _cg_symbols helper
# ---------------------------------------------------------------------------

def test_cg_symbols() -> None:
    print("\n--- Block 6: _cg_symbols ---")

    btc = _cg_symbols("BTC")
    report("BTC has 1 symbol", len(btc) == 1 and btc[0] == "BTC")

    pepe = _cg_symbols("PEPE")
    report("PEPE has 2 symbols", len(pepe) == 2)
    report("PEPE primary first", pepe[0] == "PEPE")
    report("PEPE fallback second", pepe[1] == "1000PEPE")


# ---------------------------------------------------------------------------
# Block 7 (optional): Live smoke test
# ---------------------------------------------------------------------------

def test_live_smoke() -> None:
    print("\n--- Block 7: Live smoke test ---")

    api_key = os.environ.get("LIQ_COINGLASS_API_KEY", "")
    if not api_key:
        print("  [SKIP] LIQ_COINGLASS_API_KEY not set")
        return

    import ssl
    import aiohttp
    import certifi

    async def run_live():
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            records = await fetch_latest_oi(session, "BTC", api_key)
            report("live BTC OI returns data", len(records) > 0)
            if records:
                rows = build_oi_rows(records, "BTC")
                report("live rows parseable", len(rows) > 0 and rows[0][2] > 0)

    asyncio.run(run_live())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== CoinGlass OI Collector Tests ===")
    test_pick_float()
    test_build_oi_rows()
    test_build_funding_rows()
    test_fetch_latest_oi()
    test_fetch_latest_funding()
    test_cg_symbols()
    test_live_smoke()
    print(f"\n{'='*40}")
    print(f"PASS: {passed} | FAIL: {failed}")
    sys.exit(1 if failed else 0)
