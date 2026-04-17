#!/usr/bin/env python3
"""
L13 Phase 3: Offline tests for research_cvd_standalone.py.

No DB, no API, no network for blocks 1-3. Block 4 is an optional live DB
smoke test that gracefully skips when the database is unreachable.

Usage:
    .venv/bin/python scripts/test_research_cvd_standalone.py
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

from backtest_combo import apply_combo
from backtest_market_flush_multitf import _z_window

from research_cvd_standalone import (
    build_divergence_features,
    build_exhaustion_features,
    build_hypothesis_filters,
    custom_evaluate_verdict,
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
# Block 1 — feature engineering (5 assertions)
# ---------------------------------------------------------------------------

def test_block1_features() -> None:
    print("\n--- Block 1: feature engineering ---")

    # 1. build_exhaustion_features appends consecutive_negative_delta_bars col
    idx = pd.date_range("2025-10-01", periods=6, freq="4h", tz="UTC")
    feat = pd.DataFrame(
        {"per_bar_delta": [+1.0, -1.0, -1.0, -1.0, +1.0, -1.0]},
        index=idx,
    )
    out = build_exhaustion_features(feat, window=3)
    report(
        "build_exhaustion_features adds consecutive_negative_delta_bars",
        "consecutive_negative_delta_bars" in out.columns,
        f"cols={sorted(out.columns)}",
    )

    # 2. Consecutive logic on hand-crafted series:
    #    [+1, -1, -1, -1, +1, -1] → [0, 1, 2, 3, 0, 1]
    expected = [0, 1, 2, 3, 0, 1]
    got = out["consecutive_negative_delta_bars"].tolist()
    report(
        "consecutive_negative counter resets on sign flip",
        got == expected,
        f"got={got}  expected={expected}",
    )

    # 3. build_divergence_features.cum_delta_change_6bars = cum[t] - cum[t-6]
    n = 20
    bar_hours = 4
    idx_long = pd.date_range("2025-10-01", periods=n, freq="4h", tz="UTC")
    # Use a known cum_vol_delta series: 0, 10, 20, ..., (n-1)*10 → diff(6) = 60 const
    cvd_df = pd.DataFrame(
        {
            "agg_taker_buy_vol": np.ones(n),
            "agg_taker_sell_vol": np.ones(n),
            "cum_vol_delta": np.arange(n, dtype=float) * 10.0,
        },
        index=idx_long,
    )
    feat_long = pd.DataFrame(
        {"price": np.arange(n, dtype=float) + 100.0},
        index=idx_long,
    )
    out_div = build_divergence_features(
        feat_long, cvd_df, cum_delta_window=6, bar_hours=bar_hours,
    )
    # First 6 rows should be NaN (diff window), after that constant 60
    deltas = out_div["cum_delta_change_6bars"].tolist()
    ok_diff = (
        all(pd.isna(x) for x in deltas[:6])
        and all(abs(x - 60.0) < 1e-9 for x in deltas[6:])
    )
    report(
        "cum_delta_change_6bars = cum[t] - cum[t-6]",
        ok_diff,
        f"deltas[5:8]={deltas[5:8]}",
    )

    # 4. cum_delta_change_zscore is NaN when rolling stddev is 0
    #    Use a constant series → diff is 0 everywhere → stddev of diff is 0 → NaN.
    cvd_const = pd.DataFrame(
        {
            "agg_taker_buy_vol": np.ones(n),
            "agg_taker_sell_vol": np.ones(n),
            "cum_vol_delta": np.zeros(n),
        },
        index=idx_long,
    )
    feat_const = pd.DataFrame(
        {"price": np.arange(n, dtype=float) + 100.0}, index=idx_long,
    )
    out_const = build_divergence_features(
        feat_const, cvd_const, cum_delta_window=6, bar_hours=bar_hours,
    )
    zscore_vals = out_const["cum_delta_change_zscore"]
    report(
        "cum_delta_change_zscore = NaN when stddev == 0",
        zscore_vals.isna().all(),
        f"nan_count={int(zscore_vals.isna().sum())}/{len(zscore_vals)}",
    )

    # 5. price_change_6bars == price.pct_change(6) exact equality
    expected_pc = feat_long["price"].pct_change(6)
    got_pc = out_div["price_change_6bars"]
    ok_pc = pd.testing.assert_series_equal(
        got_pc.rename(None), expected_pc.rename(None), check_names=False
    ) is None  # assert_series_equal returns None on success
    report(
        "price_change_6bars == price.pct_change(6)",
        ok_pc,
    )


# ---------------------------------------------------------------------------
# Block 2 — filter application (4 assertions)
# ---------------------------------------------------------------------------

def test_block2_filters() -> None:
    print("\n--- Block 2: filter application ---")

    # 1. H5 returns the expected tuple list
    got_h5 = build_hypothesis_filters("H5", 2.0)
    expected_h5 = [
        ("per_bar_delta_zscore", "<", -2.0),
        ("consecutive_negative_delta_bars", ">=", 3),
    ]
    report(
        "build_hypothesis_filters H5 tuple shape",
        got_h5 == expected_h5,
        f"got={got_h5}",
    )

    # 2. H7 returns the expected tuple list
    got_h7 = build_hypothesis_filters("H7", 1.0)
    expected_h7 = [
        ("price_change_6bars", "<", 0.0),
        ("cum_delta_change_zscore", ">", 1.0),
    ]
    report(
        "build_hypothesis_filters H7 tuple shape",
        got_h7 == expected_h7,
        f"got={got_h7}",
    )

    # 3. Monotonicity — raising threshold yields non-increasing trade count.
    #    Construct a frame where H5 fires at varying zscore/consecutive counts.
    n = 30
    idx = pd.date_range("2025-10-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame(index=idx)
    df["per_bar_delta_zscore"] = 0.0
    df["consecutive_negative_delta_bars"] = 0
    # Row 5: zscore -1.6, consecutive 3  → fires for H5 at th=1.5 only
    # Row 15: zscore -2.6, consecutive 4 → fires at th=1.5, 2.0, 2.5
    # Row 25: zscore -2.1, consecutive 3 → fires at 1.5, 2.0 but not 2.5
    df.loc[df.index[5], "per_bar_delta_zscore"] = -1.6
    df.loc[df.index[5], "consecutive_negative_delta_bars"] = 3
    df.loc[df.index[15], "per_bar_delta_zscore"] = -2.6
    df.loc[df.index[15], "consecutive_negative_delta_bars"] = 4
    df.loc[df.index[25], "per_bar_delta_zscore"] = -2.1
    df.loc[df.index[25], "consecutive_negative_delta_bars"] = 3

    cnt_15 = int(apply_combo(df, build_hypothesis_filters("H5", 1.5)).sum())
    cnt_20 = int(apply_combo(df, build_hypothesis_filters("H5", 2.0)).sum())
    cnt_25 = int(apply_combo(df, build_hypothesis_filters("H5", 2.5)).sum())
    report(
        "H5 monotonicity: stricter threshold → fewer trades",
        cnt_15 >= cnt_20 >= cnt_25 and cnt_15 == 3 and cnt_20 == 2 and cnt_25 == 1,
        f"counts 1.5={cnt_15} 2.0={cnt_20} 2.5={cnt_25}",
    )

    # 4. Unknown hypothesis raises ValueError
    raised = False
    try:
        build_hypothesis_filters("HX", 1.0)
    except ValueError:
        raised = True
    report("unknown hypothesis raises ValueError", raised)


# ---------------------------------------------------------------------------
# Block 3 — verdict logic (3 assertions)
# ---------------------------------------------------------------------------

def _mk_variant(n: int = 150, win_pct: float = 60.0, sharpe: float = 3.5,
                trades_per_day: float = 2.0) -> dict:
    return {
        "per_coin": [],
        "pooled": {"n": n, "win_pct": win_pct, "avg_ret": 0.5, "sharpe": sharpe},
        "days_span": max(1, int(n / max(trades_per_day, 0.01))),
        "trades_per_day": trades_per_day,
    }


def _mk_wf(oos_sharpes: list[float], pooled_sharpe: float = 3.5,
           pooled_n: int = 100, skipped: bool = False) -> dict:
    if skipped:
        return {
            "skipped": True, "reason": "N<30", "folds": [],
            "oos_positive": 0, "oos_total": 3, "pooled_oos": None,
            "pass_flag": False,
        }
    folds = [{"label": "Train", "start": None, "end": None,
              "n": 50, "win_pct": 55.0, "avg_ret": 0.3, "sharpe": 2.0}]
    for i, s in enumerate(oos_sharpes, start=1):
        folds.append({
            "label": f"OOS{i}", "start": None, "end": None,
            "n": 40, "win_pct": 58.0, "avg_ret": 0.4, "sharpe": s,
        })
    positive = sum(1 for s in oos_sharpes if s > 0)
    return {
        "skipped": False, "folds": folds,
        "oos_positive": positive, "oos_total": len(oos_sharpes),
        "pooled_oos": {
            "n": pooled_n, "win_pct": 58.0, "avg_ret": 0.4,
            "sharpe": pooled_sharpe,
        },
        "pass_flag": pooled_sharpe > 1.0 and positive >= 2,
    }


def test_block3_verdict() -> None:
    print("\n--- Block 3: verdict logic ---")

    # 1. All 8 rules met → PASS
    variant = _mk_variant(n=150, win_pct=60.0, trades_per_day=2.0)
    wf = _mk_wf([1.5, 2.0, 2.5], pooled_sharpe=3.5, pooled_n=100)
    v1 = custom_evaluate_verdict(variant, wf)
    report("all 8 rules met → PASS", v1 == "PASS", f"got={v1}")

    # 2. OOS3 Sharpe negative but primary 5 pass → MARGINAL
    #    (must keep >=2/3 OOS positive, so 2 positive + 1 negative)
    variant2 = _mk_variant(n=150, win_pct=60.0, trades_per_day=2.0)
    wf2 = _mk_wf([2.5, 2.0, -0.5], pooled_sharpe=3.5, pooled_n=100)
    v2 = custom_evaluate_verdict(variant2, wf2)
    report("OOS3 < 0 but primary OK → MARGINAL", v2 == "MARGINAL", f"got={v2}")

    # 3. Primary Sharpe <= 2.0 → FAIL regardless of strict criteria
    variant3 = _mk_variant(n=150, win_pct=60.0, trades_per_day=2.0)
    wf3 = _mk_wf([1.5, 2.0, 2.5], pooled_sharpe=1.5, pooled_n=100)
    v3 = custom_evaluate_verdict(variant3, wf3)
    report("primary pooled Sharpe <= 2.0 → FAIL", v3 == "FAIL", f"got={v3}")


# ---------------------------------------------------------------------------
# Block 4 — optional DB smoke (3, skipped without DB)
# ---------------------------------------------------------------------------

def test_block4_db_smoke() -> None:
    print("\n--- Block 4: DB smoke (optional) ---")
    if os.getenv("LIQ_SKIP_DB_TESTS"):
        print("  [SKIP] live DB test — LIQ_SKIP_DB_TESTS set")
        return
    try:
        from collectors.config import get_config
        from collectors.db import init_pool
        from research_cvd import load_cvd_tf

        init_pool(get_config())
        df = load_cvd_tf("BTC", "h4", include_cum_delta=True)
    except Exception as e:
        print(f"  [SKIP] live DB test — {type(e).__name__}: {str(e)[:80]}")
        return

    report(
        "load_cvd_tf(include_cum_delta=True) returns cum_vol_delta column",
        "cum_vol_delta" in df.columns,
        f"cols={sorted(df.columns)}",
    )
    report(
        "per-coin h4 frame has >= 500 rows",
        len(df) >= 500,
        f"rows={len(df)}",
    )
    report(
        "index is tz-aware UTC",
        df.index.tz is not None,
    )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("L13 Phase 3 — research_cvd_standalone offline tests")
    print("=" * 60)
    test_block1_features()
    test_block2_filters()
    test_block3_verdict()
    test_block4_db_smoke()
    print()
    print("=" * 60)
    print(f"RESULT: PASS={passed}  FAIL={failed}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
