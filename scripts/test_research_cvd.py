#!/usr/bin/env python3
"""
L13 Phase 2: Offline tests for research_cvd.py.

No DB, no API, no network for blocks 1-3. Block 4 is an optional live DB
smoke test that gracefully skips when the database is unreachable.

Usage:
    .venv/bin/python scripts/test_research_cvd.py
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
from backtest_market_flush_multitf import (
    MARKET_FLUSH_FILTERS,
    RANK_HOLDING_HOURS,
    WF_FOLDS,
    WF_MIN_TRADES,
    _z_window,
)
from walkforward_h1_flush import split_folds

from research_cvd import (
    attach_cvd,
    build_cvd_features,
    build_hypothesis_filters,
    run_variant,
    run_walkforward,
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
# Helpers
# ---------------------------------------------------------------------------

def _make_cvd_df(n: int, bar_hours: int = 4, seed: int = 42) -> pd.DataFrame:
    """Synthetic CVD DataFrame indexed by UTC timestamps."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-10-01", periods=n, freq=f"{bar_hours}h", tz="UTC")
    # Force strictly positive taker volumes so buy_ratio is well-defined.
    buy = np.abs(rng.normal(1_000_000, 200_000, n)) + 1.0
    sell = np.abs(rng.normal(1_000_000, 200_000, n)) + 1.0
    return pd.DataFrame(
        {"agg_taker_buy_vol": buy, "agg_taker_sell_vol": sell},
        index=idx,
    )


# ---------------------------------------------------------------------------
# Block 1 — feature engineering (5 assertions)
# ---------------------------------------------------------------------------

def test_block1_features() -> None:
    print("\n--- Block 1: feature engineering ---")
    bar_hours = 4
    window = _z_window(bar_hours)
    n = window + 50
    cvd = _make_cvd_df(n, bar_hours=bar_hours)

    feat = build_cvd_features(cvd, cvd.index, bar_hours)

    # 1. exactly 3 added columns with expected names
    expected_cols = {"buy_ratio", "per_bar_delta", "per_bar_delta_zscore"}
    report(
        "3 new columns with expected names",
        set(feat.columns) == expected_cols,
        f"got={sorted(feat.columns)}",
    )

    # 2. buy_ratio math: (buy=60, sell=40) → 0.6
    small = pd.DataFrame(
        {"agg_taker_buy_vol": [60.0, 30.0, 100.0],
         "agg_taker_sell_vol": [40.0, 70.0, 100.0]},
        index=pd.date_range("2025-10-01", periods=3, freq="4h", tz="UTC"),
    )
    small_feat = build_cvd_features(small, small.index, bar_hours=4)
    ratios = small_feat["buy_ratio"].tolist()
    report(
        "buy_ratio = buy/(buy+sell) for known inputs",
        abs(ratios[0] - 0.6) < 1e-12
        and abs(ratios[1] - 0.3) < 1e-12
        and abs(ratios[2] - 0.5) < 1e-12,
        f"ratios={ratios}",
    )

    # 3. buy_ratio on (0, 0) is NaN (no div-by-zero exception)
    zero = pd.DataFrame(
        {"agg_taker_buy_vol": [0.0, 50.0],
         "agg_taker_sell_vol": [0.0, 50.0]},
        index=pd.date_range("2025-10-01", periods=2, freq="4h", tz="UTC"),
    )
    zero_feat = build_cvd_features(zero, zero.index, bar_hours=4)
    ratios_z = zero_feat["buy_ratio"].tolist()
    report(
        "buy_ratio(0,0) = NaN (no zero-division)",
        pd.isna(ratios_z[0]) and abs(ratios_z[1] - 0.5) < 1e-12,
        f"ratios={ratios_z}",
    )

    # 4. z-score window scales with bar_hours (sanity on imported helper)
    ok_windows = (_z_window(4) == 90 and _z_window(2) == 180 and _z_window(1) == 360)
    report("z-score window scales: h4=90 h2=180 h1=360", ok_windows)

    # 5. per_bar_delta = buy - sell exact equality
    delta = feat["per_bar_delta"]
    hand = cvd["agg_taker_buy_vol"] - cvd["agg_taker_sell_vol"]
    report(
        "per_bar_delta = buy - sell (element-wise)",
        bool(np.allclose(delta.values, hand.values)),
    )


# ---------------------------------------------------------------------------
# Block 2 — filter application (4 assertions)
# ---------------------------------------------------------------------------

def test_block2_filters() -> None:
    print("\n--- Block 2: filter application ---")

    # 1. H3 appends buy_ratio tuple
    got_h3 = build_hypothesis_filters("H3", 0.55)
    expected_h3 = list(MARKET_FLUSH_FILTERS) + [("buy_ratio", ">", 0.55)]
    report(
        "build_hypothesis_filters H3 appends buy_ratio tuple",
        got_h3 == expected_h3,
        f"got={got_h3}",
    )

    # 2. H4 appends per_bar_delta_zscore tuple
    got_h4 = build_hypothesis_filters("H4", 1.0)
    expected_h4 = list(MARKET_FLUSH_FILTERS) + [
        ("per_bar_delta_zscore", ">", 1.0),
    ]
    report(
        "build_hypothesis_filters H4 appends per_bar_delta_zscore tuple",
        got_h4 == expected_h4,
        f"got={got_h4}",
    )

    # Construct a frame where baseline fires on rows 0,5,10 with varied CVD fields.
    n = 30
    idx = pd.date_range("2025-10-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame(index=idx)
    df["long_vol_zscore"] = 0.0
    df["n_coins_flushing"] = 0
    df["buy_ratio"] = 0.5
    df["per_bar_delta"] = 0.0
    df["per_bar_delta_zscore"] = 0.0
    for r in (0, 5, 10):
        df.loc[df.index[r], "long_vol_zscore"] = 2.0
        df.loc[df.index[r], "n_coins_flushing"] = 5
    # Row 0: high buy_ratio 0.70; Row 5: medium 0.56; Row 10: low 0.40
    df.loc[df.index[0], "buy_ratio"] = 0.70
    df.loc[df.index[5], "buy_ratio"] = 0.56
    df.loc[df.index[10], "buy_ratio"] = 0.40

    mask_h3_050 = apply_combo(df, build_hypothesis_filters("H3", 0.50))
    mask_h3_055 = apply_combo(df, build_hypothesis_filters("H3", 0.55))
    mask_h3_060 = apply_combo(df, build_hypothesis_filters("H3", 0.60))
    baseline_mask = apply_combo(df, list(MARKET_FLUSH_FILTERS))

    # 3. Monotonicity: lower threshold keeps >= rows than higher threshold
    report(
        "H3 monotonicity: threshold 0.50 count >= 0.55 count >= 0.60 count",
        int(mask_h3_050.sum()) >= int(mask_h3_055.sum()) >= int(mask_h3_060.sum())
        and int(mask_h3_050.sum()) == 2  # rows 0+5 pass >0.50
        and int(mask_h3_055.sum()) == 2  # rows 0+5 pass >0.55 (0.56 > 0.55)
        and int(mask_h3_060.sum()) == 1,  # only row 0 passes >0.60
        f"counts: 0.50={int(mask_h3_050.sum())} "
        f"0.55={int(mask_h3_055.sum())} 0.60={int(mask_h3_060.sum())}",
    )

    # 4. Subset property: filter count <= baseline count
    report(
        "subset: filtered count <= baseline count",
        int(mask_h3_050.sum()) <= int(baseline_mask.sum())
        and int(mask_h3_060.sum()) <= int(baseline_mask.sum()),
        f"baseline={int(baseline_mask.sum())}",
    )


# ---------------------------------------------------------------------------
# Block 3 — metrics & walk-forward (3 assertions)
# ---------------------------------------------------------------------------

def test_block3_metrics_and_wf() -> None:
    print("\n--- Block 3: metrics & walk-forward ---")

    n = 50
    idx = pd.date_range("2025-10-01", periods=n, freq="4h", tz="UTC")

    def _mk_coin(return_val: float, fire_rows: list[int]) -> pd.DataFrame:
        df = pd.DataFrame(index=idx)
        df["long_vol_zscore"] = 0.0
        df["n_coins_flushing"] = 0
        df["buy_ratio"] = 0.5
        df["per_bar_delta"] = 0.0
        df["per_bar_delta_zscore"] = 0.0
        df[f"return_{RANK_HOLDING_HOURS}h"] = return_val
        for r in fire_rows:
            df.loc[df.index[r], "long_vol_zscore"] = 2.0
            df.loc[df.index[r], "n_coins_flushing"] = 5
        return df

    btc = _mk_coin(0.5, [0, 10, 20, 30, 40])   # 5 wins
    eth = _mk_coin(-0.3, [5, 15, 25])           # 3 losses
    variant = run_variant({"BTC": btc, "ETH": eth}, list(MARKET_FLUSH_FILTERS))
    pooled = variant["pooled"]
    report(
        "run_variant pools N across coins correctly",
        pooled is not None
        and pooled["n"] == 8
        and pooled["win_pct"] == round(5 / 8 * 100, 1),
        f"pooled={pooled}",
    )

    # 2. split_folds returns 4 tuples for a 100-day index
    long_idx = pd.date_range("2025-01-01", "2025-04-10", freq="4h", tz="UTC")
    folds = split_folds(long_idx, WF_FOLDS)
    ok_4 = len(folds) == WF_FOLDS and all(len(t) == 2 for t in folds)
    monotone = all(
        folds[i][1] <= folds[i + 1][0] or folds[i][1] == folds[i + 1][0]
        for i in range(len(folds) - 1)
    )
    report(
        "split_folds returns 4 contiguous (start, end) tuples",
        ok_4 and monotone,
        f"len={len(folds)}",
    )

    # 3. run_walkforward with N < WF_MIN_TRADES returns skipped=True
    small = _mk_coin(0.2, list(range(min(5, WF_MIN_TRADES - 1))))
    wf = run_walkforward({"BTC": small}, list(MARKET_FLUSH_FILTERS))
    report(
        f"run_walkforward skips when N < {WF_MIN_TRADES}",
        wf.get("skipped") is True
        and not wf.get("folds")
        and wf.get("pass_flag") is False,
        f"wf_skipped={wf.get('skipped')} reason={wf.get('reason')}",
    )


# ---------------------------------------------------------------------------
# Block 4 — optional DB smoke (skipped without DB)
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
        df = load_cvd_tf("BTC", "h4")
    except Exception as e:
        print(f"  [SKIP] live DB test — {type(e).__name__}: {str(e)[:80]}")
        return

    report(
        "load_cvd_tf('BTC', 'h4') returns >= 500 rows",
        len(df) >= 500,
        f"rows={len(df)}",
    )
    report(
        "expected columns present",
        set(df.columns) == {"agg_taker_buy_vol", "agg_taker_sell_vol"},
    )
    report(
        "index is tz-aware UTC",
        df.index.tz is not None,
    )


# ---------------------------------------------------------------------------
# Helper: attach_cvd smoke (included via Block 1 shape — not a scored test)
# ---------------------------------------------------------------------------

def _smoke_attach_cvd() -> None:
    # Sanity — ensures attach_cvd runs without exception on a realistic frame.
    cvd = _make_cvd_df(200, bar_hours=4)
    idx = cvd.index
    feat = pd.DataFrame(
        {"long_vol_zscore": 0.0, "n_coins_flushing": 0}, index=idx,
    )
    attached = attach_cvd(feat, cvd, 4)
    assert {"buy_ratio", "per_bar_delta", "per_bar_delta_zscore"}.issubset(
        attached.columns
    )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("L13 Phase 2 — research_cvd offline tests")
    print("=" * 60)
    _smoke_attach_cvd()
    test_block1_features()
    test_block2_filters()
    test_block3_metrics_and_wf()
    test_block4_db_smoke()
    print()
    print("=" * 60)
    print(f"RESULT: PASS={passed}  FAIL={failed}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
