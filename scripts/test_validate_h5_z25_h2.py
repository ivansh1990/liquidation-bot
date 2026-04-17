#!/usr/bin/env python3
"""
L13 Phase 3b: Offline tests for validate_h5_z25_h2.py.

Thin-reuse path: the underlying aggregation / correlation / rolling Sharpe /
combined-portfolio helpers are imported from validate_h1_z15_h2.py and are
already covered by test_validate_h1_z15_h2.py (15 PASS). These tests verify
integration wiring only — imports, filter construction, verdict branches,
and report formatting.

No DB, no API, no network for the required blocks.

Usage:
    .venv/bin/python scripts/test_validate_h5_z25_h2.py
"""
from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)

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
# Block 1 — imports & integration smoke (3 assertions)
# ---------------------------------------------------------------------------

def test_block1_imports_and_smoke() -> None:
    print("\n--- Block 1: imports & integration smoke ---")

    # 1. All module-level symbols resolve without DB init.
    try:
        from validate_h5_z25_h2 import (  # noqa: F401
            H5_Z_THRESHOLD,
            H5_INTERVAL,
            format_report,
            recommend,
            run_h4_baseline,
            run_h5_z25_h2,
        )
        from validate_h1_z15_h2 import (  # noqa: F401
            extract_trade_records,
            aggregate_daily_pnl,
            compute_correlation_test,
            compute_rolling_sharpe_test,
            compute_combined_portfolio_test,
            STD_EPS,
        )
        from research_cvd_standalone import (  # noqa: F401
            _load_coins_for_interval,
            build_hypothesis_filters,
        )
        from research_netposition import run_variant  # noqa: F401
        from backtest_market_flush_multitf import (  # noqa: F401
            MARKET_FLUSH_FILTERS,
            RANK_HOLDING_HOURS,
        )
        ok = True
        err = ""
    except Exception as exc:
        ok = False
        err = repr(exc)
    report("all reused symbols importable", ok, err)

    # 2. build_hypothesis_filters("H5", 2.5) structure is correct for the mask.
    from research_cvd_standalone import build_hypothesis_filters
    filters = build_hypothesis_filters("H5", 2.5)
    # Expect two filter tuples: first on per_bar_delta_zscore < -2.5, second on
    # consecutive_negative_delta_bars >= 3.
    ok_shape = (
        len(filters) == 2
        and filters[0] == ("per_bar_delta_zscore", "<", -2.5)
        and filters[1][0] == "consecutive_negative_delta_bars"
        and filters[1][1] == ">="
        and int(filters[1][2]) == 3
    )
    report(
        "H5 z=2.5 filters have expected structure",
        ok_shape,
        f"filters={filters}",
    )

    # 3. format_report produces a non-empty str on synthetic test dicts (and
    #    surfaces the verdict token in the output). Exercises every branch of
    #    the template; actual verdict-branch coverage is in Block 2.
    from validate_h5_z25_h2 import format_report, DEFAULT_CAPITAL_USD
    _ = DEFAULT_CAPITAL_USD
    strategy_stats = {
        "h4": {"n": 428, "win_pct": 61.0, "sharpe": 5.87},
        "h5": {"n": 137, "win_pct": 56.2, "sharpe": 6.20},
    }
    t1 = {
        "correlation": 0.2,
        "n_days": 180,
        "active_h4_pct": 50.0,
        "active_h2_pct": 30.0,
        "both_active_pct": 15.0,
        "n_both": 27,
        "n_only_h4": 50,
        "n_only_h2": 25,
        "n_none": 78,
        "both_win_pct": 30.0,
        "both_lose_pct": 20.0,
        "h4_win_h2_lose_pct": 25.0,
        "h4_lose_h2_win_pct": 25.0,
        "mixed_pct": 50.0,
        "passed": True,
    }
    t2 = {
        "n_total_windows": 151,
        "n_windows": 140,
        "n_dropped_low_activity": 11,
        "n_dropped_zero_std": 0,
        "dropped_low_activity_pct": 7.3,
        "dropped_zero_std_pct": 0.0,
        "min": 1.2, "p05": 1.5, "median": 3.0, "p95": 5.0, "max": 6.0,
        "pct_above_2": 70.0, "pct_win_days_above_65": 60.0, "passed": True,
    }
    # compute_combined_portfolio_test uses "h2_solo_*" naming for the
    # partner strategy regardless of what we call it — format_report reads
    # the same keys. Mirror that here.
    t3 = {
        "combined_sharpe": 4.5, "combined_mdd": -50.0,
        "combined_win_days": 65.0, "combined_trades_per_day": 1.3,
        "h4_solo_sharpe": 3.0, "h4_solo_mdd": -100.0,
        "h4_solo_win_days": 60.0, "h4_solo_trades_per_day": 0.8,
        "h2_solo_sharpe": 2.5, "h2_solo_mdd": -80.0,
        "h2_solo_win_days": 55.0, "h2_solo_trades_per_day": 0.7,
        "passed": True,
    }
    try:
        out = format_report(
            strategy_stats, t1, t2, t2, t3,
            "STRONG_GO", "test reasoning", "test next action",
        )
        ok_fmt = (
            isinstance(out, str)
            and "STRONG_GO" in out
            and "H5_z2.5_h2" in out
        )
        err_detail = "" if ok_fmt else out[:200]
    except Exception as exc:
        ok_fmt = False
        err_detail = repr(exc)
    report("format_report returns str with verdict + label", ok_fmt, err_detail)


# ---------------------------------------------------------------------------
# Block 2 — recommend() verdict tree (4 assertions)
# ---------------------------------------------------------------------------

def test_block2_recommend_branches() -> None:
    print("\n--- Block 2: recommend() verdict tree ---")
    from validate_h5_z25_h2 import recommend

    def _t(p: bool) -> dict:
        return {"passed": p}

    # 1. ALARM when baseline (h4) fails rolling-stability test — regardless
    #    of test1/test3/test2_h5.
    v, _, _ = recommend(_t(True), _t(False), _t(True), _t(True))
    report("test2_h4 FAIL -> ALARM", v == "ALARM", f"got={v}")

    # 2. STRONG_GO when all four tests pass (baseline stable, diversified,
    #    synergistic, h5 monthly-stable).
    v, _, _ = recommend(_t(True), _t(True), _t(True), _t(True))
    report("all PASS -> STRONG_GO", v == "STRONG_GO", f"got={v}")

    # 3. WEAK_GO when diversification + synergy hold but h5 monthly stability
    #    fails.
    v, _, _ = recommend(_t(True), _t(True), _t(False), _t(True))
    report(
        "test1+test3 PASS, test2_h5 FAIL -> WEAK_GO",
        v == "WEAK_GO",
        f"got={v}",
    )

    # 4. REJECT when diversification fails (correlation too high or too few
    #    mixed days), even with baseline stable.
    v, _, _ = recommend(_t(False), _t(True), _t(True), _t(True))
    report("test1 FAIL -> REJECT", v == "REJECT", f"got={v}")


# ---------------------------------------------------------------------------
# Block 3 — optional DB smoke (skipped when DB unavailable)
# ---------------------------------------------------------------------------

def test_block3_db_smoke_optional() -> None:
    print("\n--- Block 3: optional DB smoke ---")
    try:
        from collectors.config import get_config
        from collectors.db import init_pool
        init_pool(get_config())
    except Exception as exc:
        print(f"  [SKIP] DB unavailable — {type(exc).__name__}")
        return

    try:
        from validate_h5_z25_h2 import run_h5_z25_h2
        summary, trades = run_h5_z25_h2()
    except Exception as exc:
        report("run_h5_z25_h2 executes", False, f"raised {exc!r}")
        return

    ok_shape = isinstance(trades, list) and (
        len(trades) == 0
        or all(
            isinstance(t, dict)
            and set(t.keys()) >= {"coin", "entry_ts", "exit_ts", "pnl_pct"}
            for t in trades[:5]
        )
    )
    report(
        "trades list shape correct",
        ok_shape,
        f"n={len(trades)} first={trades[0] if trades else 'empty'}",
    )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("L13 Phase 3b — validate_h5_z25_h2 offline tests")
    print("=" * 60)
    test_block1_imports_and_smoke()
    test_block2_recommend_branches()
    test_block3_db_smoke_optional()
    print()
    print("=" * 60)
    print(f"RESULT: PASS={passed}  FAIL={failed}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
