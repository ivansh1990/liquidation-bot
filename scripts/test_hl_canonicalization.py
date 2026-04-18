#!/usr/bin/env python3
"""
Offline regression tests for HL coin name canonicalization and price_step
bucket sizing used by build_liquidation_map.

Three blocks (no live APIs or DB required):
  1. canonical_coin / hl_coin — kPEPE ↔ PEPE, identity on other coins.
  2. price_step — produces a positive step for every canonical coin at
     realistic price magnitudes. Regression for L18a PEPE aggregation bug
     where price_step("PEPE", 0.003821) returned 0.0 and silently caused
     PEPE to be skipped by the `if step <= 0: continue` guard in
     collectors/hl_snapshots.py:build_liquidation_map.
  3. build_liquidation_map — PEPE positions with valid liquidation_px and
     a kPEPE mid price produce at least one map row.

Run: .venv/bin/python scripts/test_hl_canonicalization.py
Exit 0 on all-pass, 1 otherwise.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import (
    COINS,
    canonical_coin,
    hl_coin,
    price_step,
)
from collectors.hl_snapshots import build_liquidation_map

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
# Block 1: canonicalization round-trip
# ---------------------------------------------------------------------------

def test_canonicalization() -> None:
    print("\n--- Block 1: canonical_coin / hl_coin ---")

    report("canonical kPEPE → PEPE", canonical_coin("kPEPE") == "PEPE")
    report("canonical PEPE → PEPE (idempotent)", canonical_coin("PEPE") == "PEPE")
    report("hl_coin PEPE → kPEPE", hl_coin("PEPE") == "kPEPE")
    report("hl_coin kPEPE → kPEPE (idempotent)", hl_coin("kPEPE") == "kPEPE")

    # Non-meme coins pass through unchanged both directions.
    for coin in ("BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX", "SUI", "ARB", "WIF"):
        report(
            f"{coin} identity",
            canonical_coin(coin) == coin and hl_coin(coin) == coin,
        )


# ---------------------------------------------------------------------------
# Block 2: price_step — regression for L18a PEPE bug
# ---------------------------------------------------------------------------

def test_price_step() -> None:
    print("\n--- Block 2: price_step ---")

    # The bug: kPEPE mid price ≈ 0.003821 → raw = 3.82e-5 → old code
    # fell into `else: round(raw, 4)` → 0.0 → coin skipped.
    step_pepe = price_step("PEPE", 0.003821)
    report(
        "PEPE @ 0.003821 returns positive step",
        step_pepe > 0,
        f"step={step_pepe}",
    )

    # Even deeper: if kPEPE price drops to 0.0001, step must still be > 0.
    step_pepe_tiny = price_step("PEPE", 0.0001)
    report(
        "PEPE @ 0.0001 returns positive step",
        step_pepe_tiny > 0,
        f"step={step_pepe_tiny}",
    )

    # Zero / negative guard.
    report("price_step on 0.0 returns 0.0", price_step("PEPE", 0.0) == 0.0)
    report("price_step on -1.0 returns 0.0", price_step("FOO", -1.0) == 0.0)

    # Known-good coins: every coin in COINS at a plausible current price must
    # produce step > 0. Guards against this bug recurring for any new listing.
    plausible_prices = {
        "BTC": 65000.0,
        "ETH": 3200.0,
        "SOL": 150.0,
        "DOGE": 0.18,
        "LINK": 15.0,
        "AVAX": 35.0,
        "SUI": 2.5,
        "ARB": 0.6,
        "WIF": 1.8,
        "PEPE": 0.003821,  # HL reports kPEPE price
    }
    for coin in COINS:
        price = plausible_prices[coin]
        step = price_step(coin, price)
        report(
            f"{coin} @ {price} produces step > 0",
            step > 0,
            f"step={step}",
        )

    # BTC/ETH/SOL use hardcoded PRICE_STEPS — unchanged by fix.
    report("BTC step unchanged", price_step("BTC", 65000.0) == 200.0)
    report("ETH step unchanged", price_step("ETH", 3200.0) == 20.0)
    report("SOL step unchanged", price_step("SOL", 150.0) == 2.0)


# ---------------------------------------------------------------------------
# Block 3: build_liquidation_map emits rows for PEPE
# ---------------------------------------------------------------------------

def test_build_liquidation_map_pepe() -> None:
    print("\n--- Block 3: build_liquidation_map PEPE regression ---")

    snapshot_time = datetime(2026, 4, 18, 14, 0, tzinfo=timezone.utc)

    # Two synthetic PEPE positions with tiny liquidation prices at kPEPE scale.
    positions = [
        {
            "address": "0xabc",
            "snapshot_time": snapshot_time,
            "coin": "PEPE",
            "side": "long",
            "size_usd": 500_000.0,
            "entry_px": 0.003900,
            "liquidation_px": 0.003500,
            "leverage": 10.0,
            "is_liq_estimated": False,
        },
        {
            "address": "0xdef",
            "snapshot_time": snapshot_time,
            "coin": "PEPE",
            "side": "short",
            "size_usd": 300_000.0,
            "entry_px": 0.003800,
            "liquidation_px": 0.004100,
            "leverage": 8.0,
            "is_liq_estimated": False,
        },
    ]
    # allMids returns prices keyed by HL name (kPEPE, not PEPE).
    mids = {"kPEPE": 0.003821}

    rows = build_liquidation_map(positions, mids, snapshot_time)

    report("build_liquidation_map emits rows for PEPE", len(rows) > 0, f"n={len(rows)}")
    report(
        "all rows canonical coin=PEPE (not kPEPE)",
        all(r["coin"] == "PEPE" for r in rows),
    )
    report(
        "rows carry current_price from kPEPE mid",
        all(abs(r["current_price"] - 0.003821) < 1e-9 for r in rows),
    )
    # Long and short liquidations land in different buckets because the
    # liquidation prices differ by more than one step.
    has_long = any(r["long_liq_usd"] > 0 for r in rows)
    has_short = any(r["short_liq_usd"] > 0 for r in rows)
    report("long position aggregated", has_long)
    report("short position aggregated", has_short)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 64)
    print("L18a — HL canonicalization + price_step regression tests")
    print("=" * 64)

    test_canonicalization()
    test_price_step()
    test_build_liquidation_map_pepe()

    print(f"\n{'=' * 64}")
    print(f"PASS: {passed} | FAIL: {failed}")
    print("=" * 64)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
