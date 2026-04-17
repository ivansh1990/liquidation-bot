#!/usr/bin/env python3
"""
L10 Phase 2b: Offline tests for validate_h1_z15_h2.py.

No DB, no API, no network. Pure-function tests on synthetic series.

Usage:
    .venv/bin/python scripts/test_validate_h1_z15_h2.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)

from validate_h1_z15_h2 import (  # noqa: E402
    _rolling_window_sharpe,
    aggregate_daily_pnl,
    compute_combined_portfolio_test,
    compute_correlation_test,
    compute_rolling_sharpe_test,
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
# Block 1 — daily aggregation (3 assertions)
# ---------------------------------------------------------------------------

def test_block1_daily_aggregation() -> None:
    print("\n--- Block 1: daily aggregation ---")

    # Date range = 2026-01-01 .. 2026-01-05 (5 days)
    date_range = pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC")

    # 3 trades:
    #   - exits 2026-01-01: +1.0
    #   - exits 2026-01-01: +2.0  (sums to 3.0 on day 0)
    #   - exits 2026-01-03: -0.5
    trades = [
        {"coin": "BTC", "entry_ts": pd.Timestamp("2025-12-31 16:00", tz="UTC"),
         "exit_ts": pd.Timestamp("2026-01-01 00:00", tz="UTC"), "pnl_pct": 1.0},
        {"coin": "ETH", "entry_ts": pd.Timestamp("2025-12-31 20:00", tz="UTC"),
         "exit_ts": pd.Timestamp("2026-01-01 04:00", tz="UTC"), "pnl_pct": 2.0},
        {"coin": "SOL", "entry_ts": pd.Timestamp("2026-01-02 20:00", tz="UTC"),
         "exit_ts": pd.Timestamp("2026-01-03 04:00", tz="UTC"), "pnl_pct": -0.5},
    ]

    daily = aggregate_daily_pnl(trades, date_range)

    # 1. summed correctly per day
    jan1 = daily.loc[pd.Timestamp("2026-01-01", tz="UTC")]
    jan3 = daily.loc[pd.Timestamp("2026-01-03", tz="UTC")]
    report(
        "sums trades by exit date",
        abs(jan1 - 3.0) < 1e-9 and abs(jan3 - (-0.5)) < 1e-9,
        f"jan1={jan1} jan3={jan3}",
    )

    # 2. empty days are 0.0, not NaN
    jan2 = daily.loc[pd.Timestamp("2026-01-02", tz="UTC")]
    jan4 = daily.loc[pd.Timestamp("2026-01-04", tz="UTC")]
    report(
        "empty days filled with 0.0 (not NaN)",
        jan2 == 0.0 and jan4 == 0.0 and not daily.isna().any(),
        f"jan2={jan2} jan4={jan4} any_nan={daily.isna().any()}",
    )

    # 3. output length matches date_range length
    report(
        "output Series length matches date_range",
        len(daily) == len(date_range),
        f"got={len(daily)} expected={len(date_range)}",
    )


# ---------------------------------------------------------------------------
# Block 2 — correlation & overlap breakdown (3 assertions)
# ---------------------------------------------------------------------------

def test_block2_correlation() -> None:
    print("\n--- Block 2: correlation & overlap ---")

    idx = pd.date_range("2026-01-01", periods=100, freq="D", tz="UTC")
    rng = np.random.default_rng(42)

    # 1. Identical non-zero series → corr ≈ 1.0, FAIL
    series = pd.Series(rng.normal(0.1, 1.0, 100), index=idx)
    r1 = compute_correlation_test(series, series.copy())
    report(
        "identical series: corr ≈ 1.0 and not passed",
        abs(r1["correlation"] - 1.0) < 1e-9 and r1["passed"] is False,
        f"corr={r1['correlation']:.4f} passed={r1['passed']}",
    )

    # 2. Independent positive-expectancy series → low corr, mixed days high → PASS
    a = pd.Series(rng.normal(0.1, 1.0, 100), index=idx)
    b = pd.Series(rng.normal(0.1, 1.0, 100), index=idx)
    r2 = compute_correlation_test(a, b)
    report(
        "independent series: low corr, mixed >= 15%, passed",
        abs(r2["correlation"]) < 0.5 and r2["mixed_pct"] >= 15 and r2["passed"] is True,
        f"corr={r2['correlation']:.4f} mixed={r2['mixed_pct']:.1f}% passed={r2['passed']}",
    )

    # 3. 4-way breakdown percentages (over active days) sum to 100 ± 0.5
    total = (
        r2["both_win_pct"]
        + r2["both_lose_pct"]
        + r2["h4_win_h2_lose_pct"]
        + r2["h4_lose_h2_win_pct"]
    )
    report(
        "4-way breakdown sums to 100%",
        abs(total - 100.0) < 0.5,
        f"sum={total:.2f}",
    )


# ---------------------------------------------------------------------------
# Block 3 — rolling Sharpe (2 assertions)
# ---------------------------------------------------------------------------

def test_block3_rolling_sharpe() -> None:
    print("\n--- Block 3: rolling 30-day Sharpe ---")

    idx = pd.date_range("2026-01-01", periods=180, freq="D", tz="UTC")

    # 1. Strongly positive signal (mean=0.3, std=0.05) → min > 0, median > 2, passed
    rng_pos = np.random.default_rng(7)
    pos_signal = pd.Series(rng_pos.normal(0.3, 0.05, 180), index=idx)
    r1 = compute_rolling_sharpe_test(pos_signal, window_days=30, min_trading_days=5)
    report(
        "strong positive signal: min>0, median>2, passed",
        r1["min"] > 0 and r1["median"] > 2.0 and r1["passed"] is True,
        f"min={r1['min']:.2f} median={r1['median']:.2f} passed={r1['passed']}",
    )

    # 2. Zero-mean Gaussian noise → median ≈ 0, not passed
    rng = np.random.default_rng(17)
    noise = pd.Series(rng.normal(0, 1.0, 180), index=idx)
    r2 = compute_rolling_sharpe_test(noise, window_days=30, min_trading_days=5)
    report(
        "zero-mean noise: median near 0, not passed",
        abs(r2["median"]) < 2.0 and r2["passed"] is False,
        f"median={r2['median']:.2f} passed={r2['passed']}",
    )


# ---------------------------------------------------------------------------
# Block 4 — combined portfolio (2 assertions)
# ---------------------------------------------------------------------------

def test_block4_combined_portfolio() -> None:
    print("\n--- Block 4: combined 50/50 portfolio ---")

    idx = pd.date_range("2026-01-01", periods=180, freq="D", tz="UTC")
    rng = np.random.default_rng(99)

    # 1. Two independent positive-expectancy series → combined Sharpe > max solo
    a = pd.Series(rng.normal(0.2, 1.0, 180), index=idx)
    b = pd.Series(rng.normal(0.2, 1.0, 180), index=idx)
    r1 = compute_combined_portfolio_test(a, b, capital=1000.0)
    max_solo = max(r1["h4_solo_sharpe"], r1["h2_solo_sharpe"])
    report(
        "independent positive series: combined Sharpe > max solo",
        r1["combined_sharpe"] > max_solo,
        f"combined={r1['combined_sharpe']:.3f} max_solo={max_solo:.3f}",
    )

    # 2. Perfectly anti-correlated series → combined MDD less in magnitude
    c = pd.Series(rng.normal(0.1, 1.0, 180), index=idx)
    d = -c.copy()
    r2 = compute_combined_portfolio_test(c, d, capital=1000.0)
    # MDD is a negative number: "less severe" = "closer to 0" = greater value.
    h4_mdd = r2["h4_solo_mdd"]
    combined_mdd = r2["combined_mdd"]
    report(
        "anti-correlated series: combined MDD less severe than solo",
        combined_mdd > h4_mdd,
        f"combined_mdd={combined_mdd:.2f} h4_solo_mdd={h4_mdd:.2f}",
    )


# ---------------------------------------------------------------------------
# Block 5 — edge-case handlers (explicit assertions for both skip paths)
# ---------------------------------------------------------------------------

def test_block5_edge_cases() -> None:
    print("\n--- Block 5: edge-case handlers ---")

    idx = pd.date_range("2026-01-01", periods=180, freq="D", tz="UTC")

    # 5a. Perfectly constant window -> std == 0 -> _rolling_window_sharpe returns None
    flat = pd.Series(0.5, index=pd.date_range("2026-01-01", periods=30, freq="D", tz="UTC"))
    report(
        "zero-std window returns None (no cap, no inf)",
        _rolling_window_sharpe(flat) is None,
        f"got={_rolling_window_sharpe(flat)}",
    )

    # 5b. All-zero series -> every window is low-activity -> all dropped,
    #     n_windows=0, n_dropped_low_activity == n_total_windows, passed=False
    zeros = pd.Series(0.0, index=idx)
    r_zeros = compute_rolling_sharpe_test(zeros, window_days=30, min_trading_days=5)
    expected_total = 180 - 30 + 1  # 151 windows
    report(
        "all-zero series: every window dropped as low-activity",
        r_zeros["n_windows"] == 0
        and r_zeros["n_dropped_low_activity"] == expected_total
        and r_zeros["n_total_windows"] == expected_total
        and r_zeros["passed"] is False,
        (
            f"n_windows={r_zeros['n_windows']} "
            f"dropped_low={r_zeros['n_dropped_low_activity']}/{r_zeros['n_total_windows']} "
            f"passed={r_zeros['passed']}"
        ),
    )

    # 5c. Sparse series with exactly 4 active days per window -> all dropped
    #     (threshold is min_trading_days=5). Spacing 8 days -> 4 active days
    #     in any 30-day window.
    sparse = pd.Series(0.0, index=idx)
    active_dates = pd.date_range("2026-01-01", periods=23, freq="8D", tz="UTC")
    sparse.loc[active_dates] = 0.5
    r_sparse = compute_rolling_sharpe_test(sparse, window_days=30, min_trading_days=5)
    # Every 30-day window contains at most ceil(30/8)=4 active days -> all dropped
    report(
        "4-active-days windows dropped at threshold=5",
        r_sparse["n_windows"] == 0 and r_sparse["dropped_low_activity_pct"] == 100.0,
        (
            f"n_windows={r_sparse['n_windows']} "
            f"drop_pct={r_sparse['dropped_low_activity_pct']:.1f}%"
        ),
    )

    # 5d. Dense series (every day active) -> nothing dropped
    dense = pd.Series(np.random.default_rng(5).normal(0.1, 1.0, 180), index=idx)
    r_dense = compute_rolling_sharpe_test(dense, window_days=30, min_trading_days=5)
    report(
        "dense daily activity: 0 windows dropped",
        r_dense["n_dropped_low_activity"] == 0
        and r_dense["dropped_low_activity_pct"] == 0.0
        and r_dense["n_windows"] == r_dense["n_total_windows"],
        (
            f"used={r_dense['n_windows']}/{r_dense['n_total_windows']} "
            f"drop_pct={r_dense['dropped_low_activity_pct']:.1f}%"
        ),
    )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("L10 Phase 2b — validate_h1_z15_h2 offline tests")
    print("=" * 60)
    test_block1_daily_aggregation()
    test_block2_correlation()
    test_block3_rolling_sharpe()
    test_block4_combined_portfolio()
    test_block5_edge_cases()
    print()
    print("=" * 60)
    print(f"RESULT: PASS={passed}  FAIL={failed}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
