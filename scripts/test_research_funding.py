#!/usr/bin/env python3
"""
L15 Phase 1: Offline tests for research_funding_standalone.py.

No DB, no API, no network for blocks 1-4. Block 5 is an optional live DB
smoke test that gracefully skips when the database is unreachable.

Usage:
    .venv/bin/python scripts/test_research_funding.py
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

from research_funding_standalone import (  # noqa: E402
    HYPOTHESES,
    Z_WINDOW_FUNDING,
    apply_direction,
    build_funding_filters,
    compute_funding_zscore,
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
# Block 1 — z-score computation (4 assertions)
# ---------------------------------------------------------------------------

def test_block1_zscore() -> None:
    print("\n--- Block 1: z-score computation ---")

    # 1. Synthetic series with known μ/σ: z-score matches hand-computed (x-μ)/σ.
    rng = np.random.default_rng(42)
    n = Z_WINDOW_FUNDING + 30
    idx = pd.date_range("2025-10-01", periods=n, freq="8h", tz="UTC")
    values = rng.normal(0, 1, n)
    s = pd.Series(values, index=idx)
    z = compute_funding_zscore(s)

    row = Z_WINDOW_FUNDING + 10
    hand_mean = s.iloc[row - Z_WINDOW_FUNDING + 1 : row + 1].mean()
    hand_std = s.iloc[row - Z_WINDOW_FUNDING + 1 : row + 1].std()
    hand_z = (s.iloc[row] - hand_mean) / hand_std
    got_z = z.iloc[row]
    report(
        "z-score matches hand-computed rolling (x-μ)/σ",
        abs(hand_z - got_z) < 1e-10,
        f"hand={hand_z:.6g} got={got_z:.6g}",
    )

    # 2. First window-1 rows are NaN (strict min_periods=window cold start).
    cold = z.iloc[: Z_WINDOW_FUNDING - 1]
    warm_first = z.iloc[Z_WINDOW_FUNDING - 1]
    report(
        f"cold start: first {Z_WINDOW_FUNDING - 1} rows NaN, row "
        f"{Z_WINDOW_FUNDING - 1} valid",
        cold.isna().all() and not pd.isna(warm_first),
    )

    # 3. Zero-stddev period produces NaN, not inf.
    constant = pd.Series([0.003] * 100, index=pd.date_range(
        "2025-10-01", periods=100, freq="8h", tz="UTC",
    ))
    const_z = compute_funding_zscore(constant)
    warm = const_z.iloc[Z_WINDOW_FUNDING - 1 :]
    report(
        "zero-stddev → NaN (not inf)",
        warm.isna().all() and not np.isinf(warm.fillna(0)).any(),
    )

    # 4. Default window=45 (matches 15 calendar days at h8 cadence).
    report(
        "default window = 45 (h8 × 3 bars/day × 15 days)",
        Z_WINDOW_FUNDING == 45,
        f"got={Z_WINDOW_FUNDING}",
    )


# ---------------------------------------------------------------------------
# Block 2 — hypothesis filter (4 assertions)
# ---------------------------------------------------------------------------

def test_block2_filters() -> None:
    print("\n--- Block 2: hypothesis filter ---")
    from backtest_combo import apply_combo

    # 1. H1 z=2.0: filter is (funding_zscore > 2.0), direction="short".
    filters_h1, dir_h1 = build_funding_filters("H1", 2.0)
    expected_h1 = [("funding_zscore", ">", 2.0)]
    report(
        "H1 returns (funding_zscore > z, direction='short')",
        filters_h1 == expected_h1 and dir_h1 == "short",
        f"filters={filters_h1} dir={dir_h1}",
    )

    # 2. H2 z=2.0: filter is (funding_zscore < -2.0), direction="long".
    filters_h2, dir_h2 = build_funding_filters("H2", 2.0)
    expected_h2 = [("funding_zscore", "<", -2.0)]
    report(
        "H2 returns (funding_zscore < -z, direction='long')",
        filters_h2 == expected_h2 and dir_h2 == "long",
        f"filters={filters_h2} dir={dir_h2}",
    )

    # Synthetic frame for monotonicity + empty-mask tests.
    n = 30
    idx = pd.date_range("2025-10-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame(index=idx)
    df["funding_zscore"] = [
        3.0, 2.7, 2.4, 2.2, 1.9, 1.7, 1.4, 1.2, 0.9, 0.7,
        0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        -0.7, -0.9, -1.2, -1.4, -1.7, -1.9, -2.2, -2.4, -2.7, -3.0,
    ]
    df["return_8h"] = 0.5

    # 3. z=1.5 count >= z=2.5 count (monotonicity on same data).
    f15, _ = build_funding_filters("H1", 1.5)
    f25, _ = build_funding_filters("H1", 2.5)
    mask_15 = apply_combo(df, f15)
    mask_25 = apply_combo(df, f25)
    report(
        "H1 monotonicity: z=1.5 count >= z=2.5 count",
        int(mask_15.sum()) >= int(mask_25.sum()),
        f"z1.5={int(mask_15.sum())} z2.5={int(mask_25.sum())}",
    )

    # 4. Empty mask (no extremes): extract_trade_records returns empty list.
    flat = df.copy()
    flat["funding_zscore"] = 0.0
    flat["coin"] = "BTC"
    filters_strict, dir_strict = build_funding_filters("H1", 10.0)
    mask_empty = apply_combo(flat, filters_strict)
    records = extract_trade_records(flat, mask_empty, dir_strict, holding_hours=8)
    report(
        "empty mask returns [] (no crash)",
        records == [] and not mask_empty.any(),
    )


# ---------------------------------------------------------------------------
# Block 3 — trade extraction & direction (3 assertions)
# ---------------------------------------------------------------------------

def test_block3_extraction() -> None:
    print("\n--- Block 3: trade extraction & direction ---")

    # Synthetic per-coin frame with raw forward returns.
    n = 10
    idx = pd.date_range("2025-10-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame(index=idx)
    df["coin"] = "BTC"
    df["funding_zscore"] = 0.0
    df["return_8h"] = [1.0, -2.0, 0.5, -1.5, 2.0, -0.5, 1.5, -1.0, 0.0, 0.0]
    # Trigger on rows 0, 2, 4, 6 (funding_zscore > 2.5).
    df.loc[df.index[[0, 2, 4, 6]], "funding_zscore"] = 3.0
    mask = df["funding_zscore"] > 2.5

    # 1. direction="long" preserves return sign → extracted pnl_pct = +return.
    df_long = apply_direction(df, "long", holding_hours=8)
    rec_long = extract_trade_records(df_long, mask, "long", holding_hours=8)
    expected_long = [1.0, 0.5, 2.0, 1.5]
    got_long = [r["pnl_pct"] for r in rec_long]
    report(
        "direction='long' preserves return sign",
        got_long == expected_long and all(r["direction"] == "long" for r in rec_long),
        f"expected={expected_long} got={got_long}",
    )

    # 2. direction="short" inverts return sign → pnl_pct = -raw_return.
    df_short = apply_direction(df, "short", holding_hours=8)
    rec_short = extract_trade_records(df_short, mask, "short", holding_hours=8)
    expected_short = [-1.0, -0.5, -2.0, -1.5]
    got_short = [r["pnl_pct"] for r in rec_short]
    report(
        "direction='short' inverts return sign",
        got_short == expected_short and all(r["direction"] == "short" for r in rec_short),
        f"expected={expected_short} got={got_short}",
    )

    # 3. exit_ts = entry_ts + holding_hours for each trade.
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

    # Synthetic daily pnl: 60 days, active ~every other day, mostly winning.
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

    # Verdict branches: build 4 synthetic (variant, wf, sf) inputs.
    def _mk_inputs(
        sharpe: float | None,
        win: float | None,
        n: int,
        oos_pos: int,
        skipped: bool,
        min_td: int | None,
        median_td: int | None,
        median_wdr: float | None,
        max_mdd: float | None,
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

    # PASS: all 9 criteria met.
    v, w, sf = _mk_inputs(3.0, 60.0, 200, 3, False, 15, 16, 0.70, -5.0)
    verdict_pass = evaluate_verdict(v, w, sf)
    # MARGINAL: primary met, strict (min_td < 14) fails.
    v, w, sf = _mk_inputs(3.0, 60.0, 200, 3, False, 10, 12, 0.70, -5.0)
    verdict_marginal = evaluate_verdict(v, w, sf)
    # FAIL: walk-forward skipped.
    v, w, sf = _mk_inputs(None, 60.0, 20, 0, True, None, None, None, None)
    verdict_fail = evaluate_verdict(v, w, sf)
    # MARGINAL: Sharpe > 8 (look-ahead smell).
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
        from research_funding_standalone import load_funding_features_h4

        init_pool(get_config())
        # Build a realistic h4 index spanning 180 days.
        h4_idx = pd.date_range(
            end=pd.Timestamp.utcnow().floor("4h"),
            periods=180 * 6,
            freq="4h",
            tz="UTC",
        )
        df = load_funding_features_h4("BTC", h4_idx)
    except Exception as e:
        print(f"  [SKIP] live DB test — {type(e).__name__}: {str(e)[:80]}")
        return

    # 1. Returned DF has both columns and non-trivial length.
    has_cols = {"funding_rate", "funding_zscore"}.issubset(df.columns)
    enough_rows = len(df) >= 1000
    report(
        "load_funding_features_h4 returns >= 1000 rows with both columns",
        has_cols and enough_rows,
        f"cols={sorted(df.columns)} rows={len(df)}",
    )

    # 2. After warm-up period there should be non-NaN zscore values.
    warm = df["funding_zscore"].iloc[Z_WINDOW_FUNDING * 2 :]
    report(
        "funding_zscore has non-NaN values post warm-up",
        warm.notna().any(),
        f"non_nan={warm.notna().sum()}/{len(warm)}",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("L15 PHASE 1 — research_funding_standalone.py tests")
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

# Assert hypothesis tuple has exactly H1 and H2 (sanity).
assert HYPOTHESES == ("H1", "H2"), HYPOTHESES
