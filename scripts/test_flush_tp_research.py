#!/usr/bin/env python3
"""
Offline tests for scripts/flush_tp_research.py — synthetic data exercises the
cluster-detection, TP-exit math, sweep aggregation, plateau detection, and
recommendation branches. No DB / ccxt required.

Run: .venv/bin/python scripts/test_flush_tp_research.py
Exit 0 on PASS: 7 | FAIL: 0.
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

from flush_tp_research import (  # noqa: E402
    build_recommendation,
    find_short_cluster_above,
    plateau_check,
    simulate_tp_exit,
    sweep_tp_levels,
    FRICTION_PP,
    CLUSTER_Z_THRESHOLD,
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
# Synthetic feature-frame builder
# ---------------------------------------------------------------------------

def _make_feat_df(
    start: str = "2026-01-01",
    n_bars: int = 200,
    price_series: list[float] | None = None,
    short_z_override: dict[int, float] | None = None,
    short_vol_base: float = 1e6,
) -> pd.DataFrame:
    """
    Build a synthetic per-coin feature frame matching what build_features_tf
    produces at h4. Columns used by flush_tp_research: price,
    short_vol_usd, short_vol_zscore.

    `short_z_override[i]` sets that bar's short_vol_zscore directly.
    """
    idx = pd.date_range(start, periods=n_bars, freq="4h", tz="UTC")
    rng = np.random.default_rng(42)
    if price_series is None:
        prices = 100.0 + rng.normal(0, 0.5, n_bars).cumsum() * 0.1
    else:
        prices = np.asarray(price_series, dtype=float)
    short_vol = np.full(n_bars, short_vol_base)
    df = pd.DataFrame({
        "price": prices,
        "short_vol_usd": short_vol,
        "long_vol_usd": short_vol * 0.8,
    }, index=idx)
    # Base z-score = 0 (uniform short_vol).
    df["short_vol_zscore"] = 0.0
    if short_z_override:
        for i, z in short_z_override.items():
            df.iloc[i, df.columns.get_loc("short_vol_zscore")] = z
    return df


def _make_ohlc_df(feat_df: pd.DataFrame, high_overrides: dict[int, float] | None = None) -> pd.DataFrame:
    """OHLC frame aligned to feat_df — by default high=low=close=price."""
    ohlc = pd.DataFrame(
        {
            "open": feat_df["price"],
            "high": feat_df["price"],
            "low": feat_df["price"],
            "close": feat_df["price"],
            "volume": 1e6,
        },
        index=feat_df.index,
    )
    if high_overrides:
        for i, h in high_overrides.items():
            ohlc.iloc[i, ohlc.columns.get_loc("high")] = h
    return ohlc


# ---------------------------------------------------------------------------
# Test 1 — Cluster identification
# ---------------------------------------------------------------------------

def test_cluster_identification() -> None:
    print("\n--- Test 1: cluster identification ---")
    # 100 bars, entry at bar 50 at price 100.  Bar 51 price 105 + short_z=3.0.
    # Bar 52 price 110 + short_z=2.5.
    prices = [100.0] * 100
    prices[50] = 100.0
    prices[51] = 105.0  # close
    prices[52] = 110.0
    feat = _make_feat_df(
        n_bars=100,
        price_series=prices,
        short_z_override={51: 3.0, 52: 2.5},
    )
    entry_ts = feat.index[50]
    cluster = find_short_cluster_above(
        feat, entry_ts=entry_ts, entry_price=100.0,
        window_hours=8, z_threshold=CLUSTER_Z_THRESHOLD,
    )
    report(
        "found=True",
        cluster["found"] is True,
        f"cluster={cluster}",
    )
    report(
        "picks earliest qualifying bar (idx 51)",
        cluster["ts"] == feat.index[51],
        f"got ts={cluster['ts']}",
    )
    report(
        "cluster_price = bar 51 close (105.0)",
        cluster["price"] == 105.0,
        f"got {cluster['price']}",
    )


# ---------------------------------------------------------------------------
# Test 2 — No-cluster fallback
# ---------------------------------------------------------------------------

def test_no_cluster_fallback() -> None:
    print("\n--- Test 2: no cluster → 8h time-exit fallback ---")
    # All bars have short_vol_zscore = 0, well below 2.0 threshold.
    feat = _make_feat_df(n_bars=100)
    # Entry at bar 50; bars 51, 52 exist but no spikes.
    entry_ts = feat.index[50]
    cluster = find_short_cluster_above(
        feat, entry_ts=entry_ts, entry_price=feat["price"].iloc[50],
        window_hours=8, z_threshold=CLUSTER_Z_THRESHOLD,
    )
    report("found=False", cluster["found"] is False, f"cluster={cluster}")
    report("ts is None", cluster["ts"] is None, "")
    report("price is None", cluster["price"] is None, "")

    # simulate_tp_exit with no cluster → 8h time exit at bar 52 close.
    ohlc = _make_ohlc_df(feat)
    trade = {
        "coin": "BTC", "entry_ts": entry_ts, "entry_price": 100.0,
        "exit_ts_baseline": entry_ts + pd.Timedelta(hours=8),
    }
    result = simulate_tp_exit(
        trade, tp_level=0.5, cluster=cluster,
        feat_df=feat, ohlc_df=ohlc, max_hold_hours=8,
    )
    report(
        "exit_reason = timeout_no_cluster",
        result["exit_reason"] == "timeout_no_cluster",
        f"got {result['exit_reason']}",
    )
    report(
        "no_cluster_found=True",
        result["no_cluster_found"] is True,
        "",
    )


# ---------------------------------------------------------------------------
# Test 3 — TP-level math
# ---------------------------------------------------------------------------

def test_tp_level_math() -> None:
    print("\n--- Test 3: TP-level math ---")
    # Entry=100, cluster=110 at bar entry+2.  Bar entry+1 high=106,
    # close=106; bar entry+2 high=115, close=110 (cluster).
    prices = [100.0] * 100
    prices[50] = 100.0
    prices[51] = 106.0
    prices[52] = 110.0
    feat = _make_feat_df(
        n_bars=100, price_series=prices,
        short_z_override={52: 3.0},
    )
    # Ensure bar 51 high is elevated so TP=0.5 (target=105) can fill there.
    ohlc = _make_ohlc_df(feat, high_overrides={51: 106.0, 52: 115.0})
    entry_ts = feat.index[50]
    entry_price = 100.0
    cluster = find_short_cluster_above(
        feat, entry_ts=entry_ts, entry_price=entry_price,
        window_hours=8, z_threshold=CLUSTER_Z_THRESHOLD,
    )
    # Sanity: found at bar 52.
    if not cluster["found"] or cluster["price"] != 110.0:
        report("cluster precondition", False, f"cluster={cluster}")
        return

    trade = {
        "coin": "BTC", "entry_ts": entry_ts, "entry_price": entry_price,
        "exit_ts_baseline": entry_ts + pd.Timedelta(hours=8),
    }
    # TP = 0.5 → target=105. Bar 51 high=106 → hit at target=105.
    r05 = simulate_tp_exit(trade, 0.5, cluster, feat, ohlc, max_hold_hours=8)
    report(
        "TP=0.5 → exit at 105",
        abs(r05["exit_price"] - 105.0) < 1e-9,
        f"got exit={r05['exit_price']}, reason={r05['exit_reason']}",
    )
    # TP = 1.0 → target=110. Bar 51 high=106 < 110; bar 52 high=115 → hit.
    r10 = simulate_tp_exit(trade, 1.0, cluster, feat, ohlc, max_hold_hours=8)
    report(
        "TP=1.0 → exit at 110",
        abs(r10["exit_price"] - 110.0) < 1e-9,
        f"got exit={r10['exit_price']}, reason={r10['exit_reason']}",
    )
    # TP = 1.1 → target=111.  Bar 52 high=115 ≥ 111 → fill at 111.
    r11 = simulate_tp_exit(trade, 1.1, cluster, feat, ohlc, max_hold_hours=8)
    report(
        "TP=1.1 → exit at 111",
        abs(r11["exit_price"] - 111.0) < 1e-9,
        f"got exit={r11['exit_price']}, reason={r11['exit_reason']}",
    )


# ---------------------------------------------------------------------------
# Test 4 — Max-hold cap
# ---------------------------------------------------------------------------

def test_max_hold_cap() -> None:
    print("\n--- Test 4: max-hold cap (TP never reached) ---")
    # Cluster at bar 52, price 120.  TP=1.1 → target=122.
    # Forward bar highs all < 122 → timeout at bar 52 close (=120).
    prices = [100.0] * 100
    prices[50] = 100.0
    prices[51] = 105.0
    prices[52] = 120.0
    feat = _make_feat_df(
        n_bars=100, price_series=prices,
        short_z_override={52: 3.0},
    )
    ohlc = _make_ohlc_df(feat, high_overrides={51: 105.0, 52: 120.5})
    entry_ts = feat.index[50]
    cluster = find_short_cluster_above(
        feat, entry_ts, 100.0, window_hours=8,
        z_threshold=CLUSTER_Z_THRESHOLD,
    )
    trade = {
        "coin": "BTC", "entry_ts": entry_ts, "entry_price": 100.0,
        "exit_ts_baseline": entry_ts + pd.Timedelta(hours=8),
    }
    r = simulate_tp_exit(trade, 1.1, cluster, feat, ohlc, max_hold_hours=8)
    # target=100 + (120-100)*1.1 = 122.  Bar 51 high 105, bar 52 high 120.5 < 122.
    report(
        "exit_reason = timeout",
        r["exit_reason"] == "timeout",
        f"got {r['exit_reason']}",
    )
    report(
        "exit_price = bar 52 close (120)",
        abs(r["exit_price"] - 120.0) < 1e-9,
        f"got exit_price={r['exit_price']}",
    )
    report(
        "duration_hours ≤ 8",
        r["duration_hours"] <= 8.0 + 1e-9,
        f"got duration={r['duration_hours']}",
    )


# ---------------------------------------------------------------------------
# Test 5 — Sweep runs all TP levels
# ---------------------------------------------------------------------------

def test_sweep_runs_all_levels() -> None:
    print("\n--- Test 5: sweep over all TP levels ---")
    tp_levels = [0.50, 0.75, 0.80, 0.90, 0.99, 0.995, 1.00, 1.05, 1.10]
    # Build 100 synthetic trades, each on coin C, each with simple feat/ohlc.
    prices = [100.0] * 100
    # Entry at bar 20, cluster at bar 22 (price 110), bar 21 high 106.
    prices[20] = 100.0
    prices[21] = 106.0
    prices[22] = 110.0
    feat = _make_feat_df(n_bars=100, price_series=prices, short_z_override={22: 3.0})
    ohlc = _make_ohlc_df(feat, high_overrides={21: 106.0, 22: 115.0})
    trades = [
        {
            "coin": "C",
            "entry_ts": feat.index[20],
            "entry_price": 100.0,
            "exit_ts_baseline": feat.index[20] + pd.Timedelta(hours=8),
            "pnl_pct_baseline": 5.0,  # matches (110-100)/100*100 time-exit close
        }
    ] * 100
    data_cache = {"C": {"feat": feat, "ohlc": ohlc}}
    results = sweep_tp_levels(trades, tp_levels, data_cache, max_hold_hours=8)
    report(
        f"sweep returned {len(tp_levels)} rows",
        len(results) == len(tp_levels),
        f"got {len(results)}",
    )
    report(
        "each row has n_trades = 100",
        all(r["n_trades"] == 100 for r in results),
        f"{[r['n_trades'] for r in results]}",
    )
    # TP=0.5 → target=105, all 100 trades hit → all wins.
    r05 = next(r for r in results if abs(r["tp_level"] - 0.5) < 1e-9)
    report(
        "TP=0.5 → all 100 trades win",
        r05["n_wins"] == 100,
        f"got n_wins={r05['n_wins']}",
    )
    # TP=1.10 → target=111 ≤ bar 22 high (115); all hit too → all wins.
    r110 = next(r for r in results if abs(r["tp_level"] - 1.10) < 1e-9)
    report(
        "TP=1.10 → all 100 trades win (bar 22 high=115 ≥ 111)",
        r110["n_wins"] == 100,
        f"got n_wins={r110['n_wins']}",
    )


# ---------------------------------------------------------------------------
# Test 6 — Plateau detection
# ---------------------------------------------------------------------------

def test_plateau_detection() -> None:
    print("\n--- Test 6: plateau detection ---")
    # Monotonically increasing Sharpe across the 9 TP levels → top is at the
    # edge; its only neighbor must also be in top-3.
    tp_levels = [0.50, 0.75, 0.80, 0.90, 0.99, 0.995, 1.00, 1.05, 1.10]
    sweep_mono = [
        {"tp_level": tp, "sharpe": 1.0 + i * 0.3, "n_trades": 100}
        for i, tp in enumerate(tp_levels)
    ]
    plateau = plateau_check(sweep_mono)
    report(
        "monotonic → plateau confirmed",
        plateau["confirmed"] is True,
        f"plateau={plateau}",
    )

    # Single-spike in the middle: idx 4 is huge, neighbors low → no plateau.
    sharpes = [1.0, 1.1, 1.2, 1.3, 10.0, 1.4, 1.5, 1.6, 1.7]
    sweep_spike = [
        {"tp_level": tp, "sharpe": s, "n_trades": 100}
        for tp, s in zip(tp_levels, sharpes)
    ]
    plateau_spike = plateau_check(sweep_spike)
    report(
        "single spike → plateau NOT confirmed",
        plateau_spike["confirmed"] is False,
        f"plateau={plateau_spike}",
    )


# ---------------------------------------------------------------------------
# Test 7 — Recommendation branching
# ---------------------------------------------------------------------------

def _mk_row(
    tp: float, sharpe: float, verdict: str = "GENUINE_ALPHA",
    sf_pass: bool = False,
) -> dict:
    return {
        "tp_level": tp,
        "sharpe": sharpe,
        "n_trades": 100,
        "n_wins": 60,
        "n_no_cluster_fallback": 5,
        "win_pct": 60.0,
        "total_return_pp": 100.0,
        "avg_trade_pp": 1.0,
        "profit_factor": 1.5,
        "max_dd_pct": -3.0,
        "avg_duration_hours": 4.0,
        "jensen": {
            "alpha": 0.2, "beta": 0.15, "r_squared": 0.08,
            "alpha_pvalue": 0.01, "verdict": verdict,
        },
        "smart_filter": {
            "median_30d_td": 15 if sf_pass else 9,
            "median_30d_wr": 0.70 if sf_pass else 0.55,
            "max_30d_abs_mdd": 12.0 if sf_pass else 25.0,
            "pass_rate_pct": 72.0 if sf_pass else 25.0,
            "sf_pass": sf_pass,
        },
    }


def test_recommendation_branching() -> None:
    print("\n--- Test 7: recommendation branching (5 labels) ---")
    baseline = {"tp_level": "baseline_8h_time", "sharpe": 2.0, "n_trades": 428}

    # --- DEPLOY_V2: SF pass, plateau, beats baseline, Jensen OK -------------
    tps = [0.50, 0.75, 0.80, 0.90, 0.99, 0.995, 1.00, 1.05, 1.10]
    # Monotonic rising Sharpe, with SF pass on all.
    sweep_deploy = [
        _mk_row(tp, sharpe=2.5 + i * 0.1, verdict="GENUINE_ALPHA", sf_pass=True)
        for i, tp in enumerate(tps)
    ]
    rec = build_recommendation(sweep_deploy, baseline)
    report(
        "DEPLOY_V2",
        rec["label"] == "DEPLOY_V2",
        f"got {rec['label']}",
    )

    # --- SF_PASS_MARGINAL: SF pass but single spike (no plateau) ------------
    sharpes = [2.0, 2.1, 2.2, 2.3, 10.0, 2.4, 2.5, 2.6, 2.7]
    sweep_marginal = [
        _mk_row(tp, sharpe=s, verdict="GENUINE_ALPHA", sf_pass=True)
        for tp, s in zip(tps, sharpes)
    ]
    rec2 = build_recommendation(sweep_marginal, baseline)
    report(
        "SF_PASS_MARGINAL (single spike breaks plateau)",
        rec2["label"] == "SF_PASS_MARGINAL",
        f"got {rec2['label']}",
    )

    # --- IMPROVEMENT_NO_SF: monotonic, beats baseline, Jensen OK, but ¬SF ---
    sweep_imp = [
        _mk_row(tp, sharpe=2.5 + i * 0.1, verdict="GENUINE_ALPHA", sf_pass=False)
        for i, tp in enumerate(tps)
    ]
    rec3 = build_recommendation(sweep_imp, baseline)
    report(
        "IMPROVEMENT_NO_SF",
        rec3["label"] == "IMPROVEMENT_NO_SF",
        f"got {rec3['label']}",
    )

    # --- NO_IMPROVEMENT: no TP beats baseline ------------------------------
    sweep_no_imp = [
        _mk_row(tp, sharpe=1.0, verdict="GENUINE_ALPHA", sf_pass=False)
        for tp in tps
    ]
    rec4 = build_recommendation(sweep_no_imp, baseline)
    report(
        "NO_IMPROVEMENT",
        rec4["label"] == "NO_IMPROVEMENT",
        f"got {rec4['label']}",
    )

    # --- STRATEGY_QUESTIONABLE: ALL TPs are LEVERAGED_BETA ------------------
    sweep_strat = [
        _mk_row(tp, sharpe=2.5 + i * 0.1, verdict="LEVERAGED_BETA", sf_pass=True)
        for i, tp in enumerate(tps)
    ]
    rec5 = build_recommendation(sweep_strat, baseline)
    report(
        "STRATEGY_QUESTIONABLE",
        rec5["label"] == "STRATEGY_QUESTIONABLE",
        f"got {rec5['label']}",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    test_cluster_identification()
    test_no_cluster_fallback()
    test_tp_level_math()
    test_max_hold_cap()
    test_sweep_runs_all_levels()
    test_plateau_detection()
    test_recommendation_branching()
    print(f"\nPASS: {passed} | FAIL: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
