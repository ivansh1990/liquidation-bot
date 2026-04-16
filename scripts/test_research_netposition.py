#!/usr/bin/env python3
"""
L10 Phase 2: Offline tests for research_netposition.py.

No DB, no API, no network for blocks 1-3. Block 4 is an optional live DB
smoke test that gracefully skips when the database is unreachable.

Usage:
    .venv/bin/python scripts/test_research_netposition.py
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
    MARKET_FLUSH_FILTERS,
    RANK_HOLDING_HOURS,
    WF_FOLDS,
    WF_MIN_TRADES,
    _z_window,
)
from walkforward_h1_flush import split_folds

from research_netposition import (
    attach_netposition,
    build_hypothesis_filters,
    build_netposition_features,
    evaluate_verdict,
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

def _make_netpos_df(n: int, bar_hours: int = 4, seed: int = 42) -> pd.DataFrame:
    """Synthetic net-position DataFrame indexed by UTC timestamps."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-10-01", periods=n, freq=f"{bar_hours}h", tz="UTC")
    return pd.DataFrame(
        {
            "net_long_change": rng.normal(0, 100, n),
            "net_short_change": rng.normal(0, 120, n),
        },
        index=idx,
    )


def _make_features_frame(
    n: int,
    bar_hours: int = 4,
    *,
    z_self: float | None = None,
    n_coins: int | None = None,
    z_long: float | None = None,
    z_short: float | None = None,
    returns: float | None = None,
) -> pd.DataFrame:
    """Hand-crafted feature frame whose column values are known constants."""
    idx = pd.date_range("2025-10-01", periods=n, freq=f"{bar_hours}h", tz="UTC")
    rng = np.random.default_rng(0)
    df = pd.DataFrame(index=idx)
    df["long_vol_zscore"] = z_self if z_self is not None else rng.normal(0, 1, n)
    df["n_coins_flushing"] = n_coins if n_coins is not None else rng.integers(0, 10, n)
    df["net_long_change_zscore"] = z_long if z_long is not None else rng.normal(0, 1, n)
    df["net_short_change_zscore"] = z_short if z_short is not None else rng.normal(0, 1, n)
    df[f"return_{RANK_HOLDING_HOURS}h"] = returns if returns is not None else rng.normal(0.1, 0.5, n)
    return df


# ---------------------------------------------------------------------------
# Block 1 — feature engineering (5 assertions)
# ---------------------------------------------------------------------------

def test_block1_features() -> None:
    print("\n--- Block 1: feature engineering ---")
    bar_hours = 4
    window = _z_window(bar_hours)
    n = window + 50  # enough bars to have valid z-scores after cold start
    netpos = _make_netpos_df(n, bar_hours=bar_hours)

    feat = build_netposition_features(netpos, netpos.index, bar_hours)

    # 1. exactly 2 added columns with expected names
    expected_cols = {"net_long_change_zscore", "net_short_change_zscore"}
    report(
        "2 new columns with expected names",
        set(feat.columns) == expected_cols,
        f"got={sorted(feat.columns)}",
    )

    # 2. first (window - 1) rows are NaN (rolling cold start)
    first_valid = int(feat["net_long_change_zscore"].first_valid_index() is not None)
    cold = feat["net_long_change_zscore"].iloc[: window - 1]
    warm_head = feat["net_long_change_zscore"].iloc[window - 1]
    report(
        f"cold start: first {window - 1} rows NaN, row {window - 1} valid",
        cold.isna().all() and not pd.isna(warm_head) and first_valid == 1,
    )

    # 3. z-score matches hand-computed (x - mean) / std at a known row
    row = window + 10
    hand_mean = netpos["net_long_change"].iloc[row - window + 1 : row + 1].mean()
    hand_std = netpos["net_long_change"].iloc[row - window + 1 : row + 1].std()
    hand_z = (netpos["net_long_change"].iloc[row] - hand_mean) / hand_std
    got_z = feat["net_long_change_zscore"].iloc[row]
    report(
        "z-score matches hand-computed rolling (x-mean)/std",
        abs(hand_z - got_z) < 1e-10,
        f"hand={hand_z:.6g} got={got_z:.6g}",
    )

    # 4. z-score window scales with bar_hours
    ok_windows = (_z_window(4) == 90 and _z_window(2) == 180 and _z_window(1) == 360)
    report("z-score window scales: h4=90 h2=180 h1=360", ok_windows)

    # 5. zero-stddev series produces NaN (not inf / 0)
    constant = pd.DataFrame(
        {"net_long_change": [5.0] * 100, "net_short_change": [3.0] * 100},
        index=pd.date_range("2025-10-01", periods=100, freq="4h", tz="UTC"),
    )
    # Shorter window to ensure we reach the warm region on 100 rows.
    const_feat = build_netposition_features(constant, constant.index, bar_hours=4)
    # With window=90, rows 89-99 are in the warm region, all with std=0 → NaN.
    warm = const_feat["net_long_change_zscore"].iloc[89:]
    report(
        "zero-stddev produces NaN (no inf)",
        warm.isna().all() and not np.isinf(warm.fillna(0)).any(),
    )


# ---------------------------------------------------------------------------
# Block 2 — filter application (4 assertions)
# ---------------------------------------------------------------------------

def test_block2_filters() -> None:
    print("\n--- Block 2: filter application ---")

    # 1. build_hypothesis_filters returns MARKET_FLUSH_FILTERS + net_short tuple
    got = build_hypothesis_filters("H1", 1.0)
    expected = list(MARKET_FLUSH_FILTERS) + [("net_short_change_zscore", ">", 1.0)]
    report(
        "build_hypothesis_filters H1 appends net_short tuple",
        got == expected,
        f"got={got}",
    )

    # Construct a frame where baseline market_flush triggers on rows 0, 5, 10.
    from research_netposition import build_hypothesis_filters as _bhf  # noqa
    from backtest_combo import apply_combo  # noqa

    n = 30
    idx = pd.date_range("2025-10-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame(index=idx)
    df["long_vol_zscore"] = 0.0
    df["n_coins_flushing"] = 0
    df["net_long_change_zscore"] = 0.0
    df["net_short_change_zscore"] = 0.0
    # Make baseline fire on rows 0, 5, 10
    for r in (0, 5, 10):
        df.loc[df.index[r], "long_vol_zscore"] = 2.0
        df.loc[df.index[r], "n_coins_flushing"] = 5
    # Net-short z: high on rows 0 + 5, low on 10. Net-long high only on 5.
    df.loc[df.index[0], "net_short_change_zscore"] = 2.0
    df.loc[df.index[5], "net_short_change_zscore"] = 1.5
    df.loc[df.index[10], "net_short_change_zscore"] = -0.2
    df.loc[df.index[5], "net_long_change_zscore"] = 2.0

    baseline_mask = apply_combo(df, list(MARKET_FLUSH_FILTERS))
    h1_mask = apply_combo(df, build_hypothesis_filters("H1", 1.0))
    h2_mask_z0 = apply_combo(df, build_hypothesis_filters("H2", 0.0))
    h2_mask_z15 = apply_combo(df, build_hypothesis_filters("H2", 1.5))

    # 2. H1 z=1.0: rows with baseline AND net_short_zscore > 1.0 → rows 0 + 5
    report(
        "H1 z=1.0 keeps baseline rows with net_short_zscore > 1.0",
        int(h1_mask.sum()) == 2 and bool(h1_mask.iloc[0]) and bool(h1_mask.iloc[5]),
        f"count={int(h1_mask.sum())}",
    )

    # 3. H2 z=0.0 ≥ H2 z=1.5 count (monotonicity)
    report(
        "H2 monotonicity: z=0.0 count >= z=1.5 count",
        int(h2_mask_z0.sum()) >= int(h2_mask_z15.sum()),
        f"z0={int(h2_mask_z0.sum())} z1.5={int(h2_mask_z15.sum())}",
    )

    # 4. Subset property: filter count never exceeds baseline count
    report(
        "subset: filtered count <= baseline count",
        int(h1_mask.sum()) <= int(baseline_mask.sum())
        and int(h2_mask_z0.sum()) <= int(baseline_mask.sum()),
    )


# ---------------------------------------------------------------------------
# Block 3 — metrics & walk-forward (3 assertions)
# ---------------------------------------------------------------------------

def test_block3_metrics_and_wf() -> None:
    print("\n--- Block 3: metrics & walk-forward ---")

    # 1. run_variant on a synthetic 2-coin frame — pooled N = sum of trades per coin
    n = 50
    idx = pd.date_range("2025-10-01", periods=n, freq="4h", tz="UTC")

    def _mk_coin(return_val: float, fire_rows: list[int]) -> pd.DataFrame:
        df = pd.DataFrame(index=idx)
        df["long_vol_zscore"] = 0.0
        df["n_coins_flushing"] = 0
        df["net_long_change_zscore"] = 0.0
        df["net_short_change_zscore"] = 0.0
        df[f"return_{RANK_HOLDING_HOURS}h"] = return_val
        for r in fire_rows:
            df.loc[df.index[r], "long_vol_zscore"] = 2.0
            df.loc[df.index[r], "n_coins_flushing"] = 5
        return df

    btc = _mk_coin(0.5, [0, 10, 20, 30, 40])   # 5 trades, all +0.5
    eth = _mk_coin(-0.3, [5, 15, 25])           # 3 trades, all -0.3
    variant = run_variant({"BTC": btc, "ETH": eth}, list(MARKET_FLUSH_FILTERS))
    # 8 pooled trades → _metrics_for_trades needs >=5 → returns dict
    pooled = variant["pooled"]
    report(
        "run_variant pools N across coins correctly",
        pooled is not None
        and pooled["n"] == 8
        and pooled["win_pct"] == round(5 / 8 * 100, 1),
        f"pooled={pooled}",
    )

    # 2. split_folds returns 4 (start, end) tuples for a 100-day index
    long_idx = pd.date_range("2025-01-01", "2025-04-10", freq="4h", tz="UTC")
    folds = split_folds(long_idx, WF_FOLDS)
    ok_4 = len(folds) == WF_FOLDS and all(len(t) == 2 for t in folds)
    monotone = all(folds[i][1] <= folds[i + 1][0] or folds[i][1] == folds[i + 1][0]
                   for i in range(len(folds) - 1))
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
        f"wf={wf}",
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
        from research_netposition import load_netposition_tf

        init_pool(get_config())
        df = load_netposition_tf("BTC", "h4")
    except Exception as e:
        print(f"  [SKIP] live DB test — {type(e).__name__}: {str(e)[:80]}")
        return

    report(
        "load_netposition_tf('BTC', 'h4') returns >= 500 rows",
        len(df) >= 500,
        f"rows={len(df)}",
    )
    report(
        "expected columns present",
        set(df.columns) == {"net_long_change", "net_short_change"},
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
    print("L10 Phase 2 — research_netposition offline tests")
    print("=" * 60)
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
