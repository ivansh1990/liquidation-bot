#!/usr/bin/env python3
"""
L15 Phase 2: Offline tests for research_oi_standalone.py.

No DB, no API, no network for blocks 1-4. Block 5 is an optional live DB
smoke test that gracefully skips when the database is unreachable.

Usage:
    .venv/bin/python scripts/test_research_oi.py
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

from research_oi_standalone import (  # noqa: E402
    HYPOTHESES,
    Z_WINDOW_OI,
    apply_direction,
    build_oi_filters,
    compute_oi_velocity_zscore,
    evaluate_verdict,
    extract_trade_records,
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
# Block 1 — OI velocity z-score computation (4 assertions)
# ---------------------------------------------------------------------------

def test_block1_zscore() -> None:
    print("\n--- Block 1: OI velocity z-score computation ---")

    # 1. Synthetic OI series (random walk) → z-score of pct_change matches
    #    hand-computed (pct - μ) / σ over the 90-bar rolling window.
    rng = np.random.default_rng(42)
    n = Z_WINDOW_OI + 30
    idx = pd.date_range("2025-10-01", periods=n, freq="4h", tz="UTC")
    # Realistic OI-like positive series — random walk around 1e9.
    values = 1e9 + rng.normal(0, 1e6, n).cumsum()
    s = pd.Series(values, index=idx)
    z = compute_oi_velocity_zscore(s)

    pct = s.pct_change(1)
    row = Z_WINDOW_OI + 10
    hand_window = pct.iloc[row - Z_WINDOW_OI + 1 : row + 1]
    hand_mean = hand_window.mean()
    hand_std = hand_window.std()
    hand_z = (pct.iloc[row] - hand_mean) / hand_std
    got_z = z.iloc[row]
    report(
        "z-score matches hand-computed rolling (pct-μ)/σ",
        abs(hand_z - got_z) < 1e-10,
        f"hand={hand_z:.6g} got={got_z:.6g}",
    )

    # 2. First Z_WINDOW_OI rows are NaN — pct_change introduces a leading
    #    NaN so the first valid rolling window lands at index Z_WINDOW_OI.
    cold = z.iloc[: Z_WINDOW_OI]
    warm_first = z.iloc[Z_WINDOW_OI]
    report(
        f"cold start: first {Z_WINDOW_OI} rows NaN, row {Z_WINDOW_OI} valid",
        cold.isna().all() and not pd.isna(warm_first),
    )

    # 3. Constant OI → pct_change all zero → rolling stddev zero → NaN.
    constant = pd.Series([1e9] * 150, index=pd.date_range(
        "2025-10-01", periods=150, freq="4h", tz="UTC",
    ))
    const_z = compute_oi_velocity_zscore(constant)
    warm = const_z.iloc[Z_WINDOW_OI + 1 :]
    report(
        "zero-stddev (constant OI) → NaN (not inf)",
        warm.isna().all() and not np.isinf(warm.fillna(0)).any(),
    )

    # 4. Default window = 90 (h4 × 6 bars/day × 15 days).
    report(
        "default window = 90 (h4 × 6 bars/day × 15 days)",
        Z_WINDOW_OI == 90,
        f"got={Z_WINDOW_OI}",
    )


# ---------------------------------------------------------------------------
# Block 2 — hypothesis filter (4 assertions)
# ---------------------------------------------------------------------------

def test_block2_filters() -> None:
    print("\n--- Block 2: hypothesis filter ---")
    from backtest_combo import apply_combo

    # 1. H1 z=2.0: filter = (oi_velocity_zscore > 2.0, price_change_1 > 0),
    #    direction="short" (over-extended longs → pullback).
    filters_h1, dir_h1 = build_oi_filters("H1", 2.0)
    expected_h1 = [
        ("oi_velocity_zscore", ">", 2.0),
        ("price_change_1", ">", 0.0),
    ]
    report(
        "H1 returns (oi>z AND price_up, direction='short')",
        filters_h1 == expected_h1 and dir_h1 == "short",
        f"filters={filters_h1} dir={dir_h1}",
    )

    # 2. H2 z=2.0: filter = (oi_velocity_zscore > 2.0, price_change_1 < 0),
    #    direction="long" (over-extended shorts → squeeze).
    filters_h2, dir_h2 = build_oi_filters("H2", 2.0)
    expected_h2 = [
        ("oi_velocity_zscore", ">", 2.0),
        ("price_change_1", "<", 0.0),
    ]
    report(
        "H2 returns (oi>z AND price_down, direction='long')",
        filters_h2 == expected_h2 and dir_h2 == "long",
        f"filters={filters_h2} dir={dir_h2}",
    )

    # Synthetic frame spanning z=0..3 with mixed price direction.
    n = 20
    idx = pd.date_range("2025-10-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame(index=idx)
    # 10 high-z rows (5 price-up, 5 price-down) + 10 low-z rows.
    df["oi_velocity_zscore"] = (
        [3.0, 2.7, 2.4, 2.1, 1.8] +   # high-z, price up
        [2.9, 2.6, 2.3, 2.0, 1.7] +   # high-z, price down
        [0.5] * 10                    # low-z
    )
    df["price_change_1"] = (
        [0.01] * 5 +    # up
        [-0.01] * 5 +   # down
        [0.01, -0.01] * 5
    )
    df["return_8h"] = 0.5

    # 3. z=1.5 (H1) count >= z=2.5 (H1) count on same data — monotonicity.
    f15, _ = build_oi_filters("H1", 1.5)
    f25, _ = build_oi_filters("H1", 2.5)
    mask_15 = apply_combo(df, f15)
    mask_25 = apply_combo(df, f25)
    report(
        "H1 monotonicity: z=1.5 count >= z=2.5 count",
        int(mask_15.sum()) >= int(mask_25.sum()),
        f"z1.5={int(mask_15.sum())} z2.5={int(mask_25.sum())}",
    )

    # 4. H1 and H2 masks are disjoint on rows with non-zero price_change_1.
    #    (A bar cannot be both price-up and price-down at the same time.)
    f_h1_test, _ = build_oi_filters("H1", 1.5)
    f_h2_test, _ = build_oi_filters("H2", 1.5)
    mask_h1 = apply_combo(df, f_h1_test)
    mask_h2 = apply_combo(df, f_h2_test)
    overlap = mask_h1 & mask_h2
    report(
        "H1 and H2 masks disjoint on non-zero price bars",
        not overlap.any() and mask_h1.any() and mask_h2.any(),
        f"h1={int(mask_h1.sum())} h2={int(mask_h2.sum())} overlap={int(overlap.sum())}",
    )


# ---------------------------------------------------------------------------
# Block 3 — trade extraction & direction (3 assertions)
# ---------------------------------------------------------------------------

def test_block3_extraction() -> None:
    print("\n--- Block 3: trade extraction & direction ---")

    n = 10
    idx = pd.date_range("2025-10-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame(index=idx)
    df["coin"] = "BTC"
    df["oi_velocity_zscore"] = 0.0
    df["price_change_1"] = 0.01
    df["return_8h"] = [1.0, -2.0, 0.5, -1.5, 2.0, -0.5, 1.5, -1.0, 0.0, 0.0]
    # Trigger on rows 0, 2, 4, 6.
    df.loc[df.index[[0, 2, 4, 6]], "oi_velocity_zscore"] = 3.0
    mask = df["oi_velocity_zscore"] > 2.5

    # 1. direction="long" preserves return sign.
    df_long = apply_direction(df, "long", holding_hours=8)
    rec_long = extract_trade_records(df_long, mask, "long", holding_hours=8)
    expected_long = [1.0, 0.5, 2.0, 1.5]
    got_long = [r["pnl_pct"] for r in rec_long]
    report(
        "direction='long' preserves return sign",
        got_long == expected_long and all(r["direction"] == "long" for r in rec_long),
        f"expected={expected_long} got={got_long}",
    )

    # 2. direction="short" inverts return sign.
    df_short = apply_direction(df, "short", holding_hours=8)
    rec_short = extract_trade_records(df_short, mask, "short", holding_hours=8)
    expected_short = [-1.0, -0.5, -2.0, -1.5]
    got_short = [r["pnl_pct"] for r in rec_short]
    report(
        "direction='short' inverts return sign",
        got_short == expected_short and all(r["direction"] == "short" for r in rec_short),
        f"expected={expected_short} got={got_short}",
    )

    # 3. exit_ts - entry_ts = holding_hours (8h at h4 = 2 bars forward).
    entry_ts_0 = rec_long[0]["entry_ts"]
    exit_ts_0 = rec_long[0]["exit_ts"]
    delta = (exit_ts_0 - entry_ts_0).total_seconds() / 3600.0
    report(
        "exit_ts - entry_ts = holding_hours (8h)",
        abs(delta - 8.0) < 1e-6,
        f"delta={delta}",
    )


# ---------------------------------------------------------------------------
# Block 4 — Smart Filter integration & verdict logic (2 assertions)
# ---------------------------------------------------------------------------

def test_block4_verdict() -> None:
    print("\n--- Block 4: SF integration & verdict ---")
    from smart_filter_adequacy import (
        compute_daily_metrics,
        simulate_smart_filter_windows,
    )

    # Synthetic daily pnl → 60 days, active every other day, mostly winning.
    dates = pd.date_range("2025-10-01", periods=60, freq="D", tz="UTC")
    rng = np.random.default_rng(0)
    trades = []
    for i, d in enumerate(dates):
        if i % 2 == 0:
            trades.append({
                "coin": "BTC",
                "entry_ts": d,
                "exit_ts": d,
                "pnl_pct": rng.choice([0.8, -0.3], p=[0.7, 0.3]),
                "direction": "long",
            })
    daily = compute_daily_metrics(trades, dates, capital_usd=1000.0)
    sf30 = simulate_smart_filter_windows(daily, 30, 14, 0.65, 20.0)
    expected_cols = {
        "trading_days_in_window", "pnl_sum_usd",
        "win_days_ratio", "mdd_in_window_pct",
        "g_trading_days", "g_pnl_positive", "g_win_days", "g_mdd", "passed",
    }
    report(
        "simulate_smart_filter_windows returns expected columns",
        set(sf30.columns) == expected_cols,
        f"got={sorted(sf30.columns)}",
    )

    # Verdict branches.
    def _mk_inputs(
        sharpe, win, n, oos_pos, skipped,
        min_td, median_td, median_wdr, max_mdd,
    ):
        variant = {"pooled": {"n": n, "win_pct": win}, "trades_per_day": 1.0}
        wf = {
            "skipped": skipped,
            "pooled_oos": {"sharpe": sharpe, "n": n} if sharpe is not None else None,
            "oos_positive": oos_pos,
            "oos_total": 3,
        }
        if min_td is None:
            sf_df = pd.DataFrame()
        else:
            sf_df = pd.DataFrame({
                "trading_days_in_window": [min_td, median_td, median_td + 2],
                "win_days_ratio": [median_wdr, median_wdr, median_wdr],
                "mdd_in_window_pct": [max_mdd, -1.0, -2.0],
            })
        return variant, wf, sf_df

    # PASS: all 9 met.
    v, w, sf = _mk_inputs(3.0, 60.0, 200, 3, False, 15, 16, 0.70, -5.0)
    verdict_pass = evaluate_verdict(v, w, sf)
    # MARGINAL: primary met, strict min_td<14.
    v, w, sf = _mk_inputs(3.0, 60.0, 200, 3, False, 10, 12, 0.70, -5.0)
    verdict_marginal = evaluate_verdict(v, w, sf)
    # FAIL: wf skipped.
    v, w, sf = _mk_inputs(None, 60.0, 20, 0, True, None, None, None, None)
    verdict_fail = evaluate_verdict(v, w, sf)
    # MARGINAL: Sharpe > 8 (look-ahead).
    v, w, sf = _mk_inputs(9.0, 60.0, 200, 3, False, 15, 16, 0.70, -5.0)
    verdict_suspicious = evaluate_verdict(v, w, sf)

    ok = (
        verdict_pass == "PASS"
        and verdict_marginal == "MARGINAL"
        and verdict_fail == "FAIL"
        and verdict_suspicious == "MARGINAL"
    )
    report(
        "evaluate_verdict branches: PASS / MARGINAL / FAIL / suspicious→MARGINAL",
        ok,
        f"pass={verdict_pass} marg={verdict_marginal} fail={verdict_fail} "
        f"susp={verdict_suspicious}",
    )


# ---------------------------------------------------------------------------
# Block 5 — optional live DB smoke (2 assertions, skip without DB)
# ---------------------------------------------------------------------------

def test_block5_db_smoke() -> None:
    print("\n--- Block 5: live DB smoke (optional) ---")
    if os.getenv("LIQ_SKIP_DB_TESTS"):
        print("  [SKIP] live DB test — LIQ_SKIP_DB_TESTS set")
        return
    try:
        from collectors.config import get_config
        from collectors.db import init_pool
        from research_oi_standalone import load_oi_velocity_features_h4

        init_pool(get_config())
        h4_idx = pd.date_range(
            end=pd.Timestamp.utcnow().floor("4h"),
            periods=180 * 6,
            freq="4h",
            tz="UTC",
        )
        df = load_oi_velocity_features_h4("BTC", h4_idx)
    except Exception as e:
        print(f"  [SKIP] live DB test — {type(e).__name__}: {str(e)[:80]}")
        return

    has_cols = {"open_interest", "oi_velocity_zscore"}.issubset(df.columns)
    enough_rows = len(df) >= 1000
    report(
        "load_oi_velocity_features_h4 returns >= 1000 rows with both columns",
        has_cols and enough_rows,
        f"cols={sorted(df.columns)} rows={len(df)}",
    )

    warm = df["oi_velocity_zscore"].iloc[Z_WINDOW_OI * 2 :]
    report(
        "oi_velocity_zscore has non-NaN values post warm-up",
        warm.notna().any(),
        f"non_nan={warm.notna().sum()}/{len(warm)}",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("L15 PHASE 2 — research_oi_standalone.py tests")
    print("=" * 60)
    test_block1_zscore()
    test_block2_filters()
    test_block3_extraction()
    test_block4_verdict()
    test_block5_db_smoke()
    print()
    print("=" * 60)
    print(f"PASS: {passed}  |  FAIL: {failed}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

# Sanity: hypothesis tuple must be exactly H1 and H2.
assert HYPOTHESES == ("H1", "H2"), HYPOTHESES
