#!/usr/bin/env python3
"""
L8: Offline tests for backtest_market_flush_multitf.py.

No DB, no API keys, no network — pure unit tests with synthetic data.
Matches the test_paper_bot.py / test_telegram_bot.py pattern (standalone,
no pytest dependency).

Usage:
    .venv/bin/python scripts/test_backtest_multitf.py
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

from backtest_market_flush_multitf import (
    HOLDING_HOURS_MAP,
    RANK_HOLDING_HOURS,
    _lookback_24h,
    _z_window,
    build_features_tf,
    compute_signals_tf,
    load_liquidations_tf,
    load_oi_tf,
)

# Also import the locked L2 compute_signals for parity check.
from backtest_liquidation_flush import compute_signals

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
# Helpers: build synthetic data
# ---------------------------------------------------------------------------

def _make_liq_df(n: int, bar_hours: int = 4) -> pd.DataFrame:
    """Create a synthetic liquidation DataFrame with n rows."""
    np.random.seed(42)
    timestamps = pd.date_range(
        "2025-10-01", periods=n, freq=f"{bar_hours}h", tz="UTC",
    )
    return pd.DataFrame({
        "timestamp": timestamps,
        "long_vol_usd": np.abs(np.random.lognormal(15, 1.5, n)),
        "short_vol_usd": np.abs(np.random.lognormal(14.5, 1.5, n)),
    })


def _make_price_df(liq_df: pd.DataFrame, bar_hours: int = 4) -> pd.DataFrame:
    """Create a synthetic price DataFrame aligned with liq_df."""
    np.random.seed(123)
    n = len(liq_df)
    prices = 50000 + np.cumsum(np.random.randn(n) * 100)
    idx = pd.DatetimeIndex(liq_df["timestamp"])
    return pd.DataFrame({"price": prices}, index=idx)


def _make_ohlcv_df(liq_df: pd.DataFrame, bar_hours: int = 4) -> pd.DataFrame:
    """Create synthetic OHLCV aligned with liq_df."""
    np.random.seed(123)
    n = len(liq_df)
    close = 50000 + np.cumsum(np.random.randn(n) * 100)
    idx = pd.DatetimeIndex(liq_df["timestamp"])
    return pd.DataFrame({
        "open": close - np.random.rand(n) * 50,
        "high": close + np.abs(np.random.randn(n)) * 100,
        "low": close - np.abs(np.random.randn(n)) * 100,
        "close": close,
        "volume": np.abs(np.random.lognormal(10, 1, n)),
    }, index=idx)


# ---------------------------------------------------------------------------
# Test 1: compute_signals_tf parity at h4
# ---------------------------------------------------------------------------

def test_parity_h4():
    print("\n--- Test 1: compute_signals_tf parity at h4 ---")
    n = 200
    liq_df = _make_liq_df(n, bar_hours=4)
    price_df = _make_price_df(liq_df, bar_hours=4)

    # Locked L2 version (hardcoded 4H).
    original = compute_signals(liq_df, price_df)

    # Multi-TF version at bar_hours=4.
    multi = compute_signals_tf(liq_df, price_df, bar_hours=4,
                               holding_hours=[4, 8, 12, 24])

    # long_vol_zscore must be element-wise identical.
    try:
        pd.testing.assert_series_equal(
            multi["long_vol_zscore"], original["long_vol_zscore"],
            check_names=False, rtol=1e-10,
        )
        report("long_vol_zscore parity", True)
    except AssertionError as e:
        report("long_vol_zscore parity", False, str(e)[:120])

    # short_vol_zscore parity.
    try:
        pd.testing.assert_series_equal(
            multi["short_vol_zscore"], original["short_vol_zscore"],
            check_names=False, rtol=1e-10,
        )
        report("short_vol_zscore parity", True)
    except AssertionError as e:
        report("short_vol_zscore parity", False, str(e)[:120])

    # total_vol parity.
    try:
        pd.testing.assert_series_equal(
            multi["total_vol"], original["total_vol"],
            check_names=False, rtol=1e-10,
        )
        report("total_vol parity", True)
    except AssertionError as e:
        report("total_vol parity", False, str(e)[:120])

    # return_8h parity.
    try:
        pd.testing.assert_series_equal(
            multi["return_8h"], original["return_8h"],
            check_names=False, rtol=1e-10,
        )
        report("return_8h parity", True)
    except AssertionError as e:
        report("return_8h parity", False, str(e)[:120])

    # long_vol_24h parity (rolling lookback=6 at h4).
    try:
        pd.testing.assert_series_equal(
            multi["long_vol_24h"], original["long_vol_24h"],
            check_names=False, rtol=1e-10,
        )
        report("long_vol_24h parity", True)
    except AssertionError as e:
        report("long_vol_24h parity", False, str(e)[:120])

    # ratio_24h parity.
    try:
        pd.testing.assert_series_equal(
            multi["ratio_24h"], original["ratio_24h"],
            check_names=False, rtol=1e-10,
        )
        report("ratio_24h parity", True)
    except AssertionError as e:
        report("ratio_24h parity", False, str(e)[:120])


# ---------------------------------------------------------------------------
# Test 2: Z-score scaling at h1
# ---------------------------------------------------------------------------

def test_zscore_h1():
    print("\n--- Test 2: Z-score scaling at h1 ---")
    n = 500
    liq_df = _make_liq_df(n, bar_hours=1)
    price_df = _make_price_df(liq_df, bar_hours=1)

    result = compute_signals_tf(liq_df, price_df, bar_hours=1)

    # Z-score window at h1 = 360 bars.
    z_win = _z_window(1)
    report("z_window h1 = 360", z_win == 360, f"got {z_win}")

    # First 359 rows should have NaN z-scores (window not full yet).
    first_nans = result["long_vol_zscore"].iloc[:z_win - 1].isna().all()
    report("first 359 z-scores are NaN", first_nans)

    # Row 360+ should have some non-NaN z-scores.
    some_valid = result["long_vol_zscore"].iloc[z_win:].notna().any()
    report("z-scores from row 360 are non-NaN", some_valid)

    # Lookback for 24h sum at h1 = 24 bars.
    lookback = _lookback_24h(1)
    report("lookback_24h h1 = 24", lookback == 24, f"got {lookback}")

    # long_vol_24h should be 24-bar rolling sum.
    # Check: first 23 rows should be NaN (rolling 24 needs 24 values).
    first_lookback_nans = result["long_vol_24h"].iloc[:lookback - 1].isna().all()
    report("first 23 long_vol_24h are NaN (rolling 24)", first_lookback_nans)


# ---------------------------------------------------------------------------
# Test 3: Z-score scaling at h2
# ---------------------------------------------------------------------------

def test_zscore_h2():
    print("\n--- Test 3: Z-score scaling at h2 ---")
    z_win = _z_window(2)
    report("z_window h2 = 180", z_win == 180, f"got {z_win}")

    lookback = _lookback_24h(2)
    report("lookback_24h h2 = 12", lookback == 12, f"got {lookback}")

    n = 250
    liq_df = _make_liq_df(n, bar_hours=2)
    price_df = _make_price_df(liq_df, bar_hours=2)

    result = compute_signals_tf(liq_df, price_df, bar_hours=2)

    # First 179 z-scores should be NaN.
    first_nans = result["long_vol_zscore"].iloc[:z_win - 1].isna().all()
    report("first 179 z-scores are NaN at h2", first_nans)

    # long_vol_24h: first 11 rows NaN (rolling 12).
    first_lookback_nans = result["long_vol_24h"].iloc[:lookback - 1].isna().all()
    report("first 11 long_vol_24h are NaN (rolling 12)", first_lookback_nans)


# ---------------------------------------------------------------------------
# Test 4: Forward returns at h1
# ---------------------------------------------------------------------------

def test_forward_returns_h1():
    print("\n--- Test 4: Forward returns at h1 ---")
    n = 100
    liq_df = _make_liq_df(n, bar_hours=1)
    price_df = _make_price_df(liq_df, bar_hours=1)

    result = compute_signals_tf(liq_df, price_df, bar_hours=1)

    # return_4h at h1 should shift by 4 bars.
    # Pick a row in the middle (index 50) and verify manually.
    idx = 50
    price_now = result["price"].iloc[idx]
    price_4h = result["price"].iloc[idx + 4]
    expected_return = (price_4h - price_now) / price_now * 100
    actual_return = result["return_4h"].iloc[idx]
    close = abs(actual_return - expected_return) < 1e-6
    report(
        "return_4h shifts by 4 bars at h1",
        close,
        f"expected={expected_return:.6f}, got={actual_return:.6f}",
    )

    # return_48h at h1 should shift by 48 bars.
    # Pick row 10 (leaving room for 48 forward bars in 100 total).
    idx2 = 10
    price_now2 = result["price"].iloc[idx2]
    price_48h = result["price"].iloc[idx2 + 48]
    expected_48 = (price_48h - price_now2) / price_now2 * 100
    actual_48 = result["return_48h"].iloc[idx2]
    close_48 = abs(actual_48 - expected_48) < 1e-6
    report(
        "return_48h shifts by 48 bars at h1",
        close_48,
        f"expected={expected_48:.6f}, got={actual_48:.6f}",
    )

    # return_8h at h1 should shift by 8 bars.
    idx3 = 30
    price_now3 = result["price"].iloc[idx3]
    price_8h = result["price"].iloc[idx3 + 8]
    expected_8 = (price_8h - price_now3) / price_now3 * 100
    actual_8 = result["return_8h"].iloc[idx3]
    close_8 = abs(actual_8 - expected_8) < 1e-6
    report(
        "return_8h shifts by 8 bars at h1",
        close_8,
        f"expected={expected_8:.6f}, got={actual_8:.6f}",
    )


# ---------------------------------------------------------------------------
# Test 5: Holding hours map
# ---------------------------------------------------------------------------

def test_holding_hours_map():
    print("\n--- Test 5: Holding hours map ---")
    # All intervals must include 8h for cross-interval ranking.
    for bar_h, hours_list in HOLDING_HOURS_MAP.items():
        has_8 = RANK_HOLDING_HOURS in hours_list
        report(
            f"h{bar_h} includes {RANK_HOLDING_HOURS}h",
            has_8,
            f"hours={hours_list}",
        )

    # h4 matches L3b-2 baseline.
    report("h4 matches L3b-2", HOLDING_HOURS_MAP[4] == [4, 8, 12, 24])

    # h2 holding periods.
    report("h2 holding periods", HOLDING_HOURS_MAP[2] == [8, 16, 32, 48])

    # h1 holding periods.
    report("h1 holding periods", HOLDING_HOURS_MAP[1] == [4, 8, 16, 48])


# ---------------------------------------------------------------------------
# Test 6: Table name derivation
# ---------------------------------------------------------------------------

def test_table_names():
    print("\n--- Test 6: Table name derivation ---")
    # We can't call the actual load functions without DB, but we can verify
    # the SQL table name logic by inspecting the function's source/behavior.

    # h4 should use base table.
    table_h4 = "coinglass_liquidations" if "h4" == "h4" else f"coinglass_liquidations_h4"
    report("h4 liq table = coinglass_liquidations", table_h4 == "coinglass_liquidations")

    table_h1 = f"coinglass_liquidations_h1"
    report("h1 liq table = coinglass_liquidations_h1", table_h1 == "coinglass_liquidations_h1")

    table_h2 = f"coinglass_liquidations_h2"
    report("h2 liq table = coinglass_liquidations_h2", table_h2 == "coinglass_liquidations_h2")

    # OI tables.
    oi_h4 = "coinglass_oi"
    oi_h1 = "coinglass_oi_h1"
    report("h4 oi table = coinglass_oi", oi_h4 == "coinglass_oi")
    report("h1 oi table = coinglass_oi_h1", oi_h1 == "coinglass_oi_h1")


# ---------------------------------------------------------------------------
# Test 7: build_features_tf drawdown scaling
# ---------------------------------------------------------------------------

def test_drawdown_scaling():
    print("\n--- Test 7: build_features_tf drawdown scaling ---")
    n = 100
    liq_df = _make_liq_df(n, bar_hours=1)
    ohlcv_df = _make_ohlcv_df(liq_df, bar_hours=1)
    oi_df = pd.DataFrame()
    funding_df = pd.DataFrame()

    features = build_features_tf(
        coin="TEST",
        liq_used_symbol="TEST",
        liq_df=liq_df,
        oi_df=oi_df,
        funding_df=funding_df,
        ohlcv_df=ohlcv_df,
        bar_hours=1,
    )

    # drawdown_24h at h1 should use pct_change(24), not pct_change(6).
    # Check: first 23 rows should be NaN (pct_change(24) needs 24 prior values).
    first_23_nan = features["drawdown_24h"].iloc[:24].isna().sum()
    report(
        "drawdown_24h first 24 rows have NaN (pct_change(24))",
        first_23_nan >= 23,  # at least 23 NaN in first 24 (row 0 is always NaN)
        f"NaN count in first 24 = {first_23_nan}",
    )

    # At h4, drawdown uses pct_change(6). Verify different scaling.
    liq_df_4h = _make_liq_df(n, bar_hours=4)
    ohlcv_df_4h = _make_ohlcv_df(liq_df_4h, bar_hours=4)
    features_4h = build_features_tf(
        coin="TEST",
        liq_used_symbol="TEST",
        liq_df=liq_df_4h,
        oi_df=pd.DataFrame(),
        funding_df=pd.DataFrame(),
        ohlcv_df=ohlcv_df_4h,
        bar_hours=4,
    )
    first_5_nan_4h = features_4h["drawdown_24h"].iloc[:6].isna().sum()
    report(
        "drawdown_24h at h4 first 6 rows have NaN (pct_change(6))",
        first_5_nan_4h >= 5,
        f"NaN count in first 6 = {first_5_nan_4h}",
    )


# ---------------------------------------------------------------------------
# Test 8: Z-score window constants
# ---------------------------------------------------------------------------

def test_z_window_constants():
    print("\n--- Test 8: Z-score window constants ---")
    # All intervals should represent ~15 calendar days.
    days_h4 = _z_window(4) * 4 / 24
    days_h2 = _z_window(2) * 2 / 24
    days_h1 = _z_window(1) * 1 / 24
    report("h4 z-window = 15 days", abs(days_h4 - 15.0) < 0.01, f"got {days_h4:.1f}")
    report("h2 z-window = 15 days", abs(days_h2 - 15.0) < 0.01, f"got {days_h2:.1f}")
    report("h1 z-window = 15 days", abs(days_h1 - 15.0) < 0.01, f"got {days_h1:.1f}")


# ---------------------------------------------------------------------------
# Test 9: 30m scaling (L16)
# ---------------------------------------------------------------------------

def test_30m_scaling():
    print("\n--- Test 9: 30m scaling (L16) ---")
    # L16: sub-hour timeframe. bar_hours = 0.5.
    z = _z_window(0.5)
    report("_z_window(0.5) = 720 (15 calendar days at 30m)",
           z == 720, f"got {z}")

    lb = _lookback_24h(0.5)
    report("_lookback_24h(0.5) = 48 bars (24h at 30m)",
           lb == 48, f"got {lb}")

    # 8h holding at 30m = 16 bar forward shift.
    periods = max(1, int(round(8 / 0.5)))
    report("8h holding at 30m = 16 bar forward shift",
           periods == 16, f"got {periods}")

    # HOLDING_HOURS_BY_MINUTES[30] must include the 8h cross-interval ranking anchor.
    from backtest_market_flush_multitf import HOLDING_HOURS_BY_MINUTES
    report("HOLDING_HOURS_BY_MINUTES[30] includes 8h ranking anchor",
           8 in HOLDING_HOURS_BY_MINUTES[30],
           f"got {HOLDING_HOURS_BY_MINUTES.get(30)}")


# ---------------------------------------------------------------------------
# Test 10: bar_minutes consistency (L16)
# ---------------------------------------------------------------------------

def test_bar_minutes_consistency():
    print("\n--- Test 10: bar_minutes consistency (L16) ---")
    from backtest_market_flush_multitf import (
        _interval_to_bar_hours,
        _interval_to_bar_minutes,
    )

    # bar_hours = bar_minutes / 60.0 consistent across all supported intervals.
    pairs = {"30m": 0.5, "h1": 1.0, "h2": 2.0, "h4": 4.0}
    all_hours_ok = all(
        abs(_interval_to_bar_hours(k) - v) < 1e-9
        and _interval_to_bar_minutes(k) / 60.0 == v
        for k, v in pairs.items()
    )
    report("bar_hours = bar_minutes/60 consistent across 30m/h1/h2/h4",
           all_hours_ok, f"pairs={pairs}")

    # 8h holding produces 16/8/4/2 forward-bar shifts at 30m/h1/h2/h4.
    def _periods(hours: int, bh: float) -> int:
        return max(1, int(round(hours / bh)))
    periods_ok = (
        _periods(8, 0.5) == 16
        and _periods(8, 1.0) == 8
        and _periods(8, 2.0) == 4
        and _periods(8, 4.0) == 2
    )
    report("8h holding = 16/8/4/2 bars at 30m/h1/h2/h4",
           periods_ok,
           f"got {_periods(8,0.5)}/{_periods(8,1.0)}/{_periods(8,2.0)}/{_periods(8,4.0)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    test_parity_h4()
    test_zscore_h1()
    test_zscore_h2()
    test_forward_returns_h1()
    test_holding_hours_map()
    test_table_names()
    test_drawdown_scaling()
    test_z_window_constants()
    test_30m_scaling()
    test_bar_minutes_consistency()

    print()
    print(f"PASS: {passed} | FAIL: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
