#!/usr/bin/env python3
"""
L13 Phase 3: CVD standalone-signal research script.

Tests whether CVD features **alone** (no market_flush dependency) produce a
LONG edge. Phase 2 showed that CVD filters over market_flush all FAIL because
flush moments and CVD extremes barely overlap — CVD carries orthogonal
information. Phase 3 flips the framing and tests CVD as a standalone signal.

Hypotheses:
  H5 Aggressive Selling Exhaustion:
      per_bar_delta_zscore < -threshold AND consecutive_negative_delta_bars >= 3
      — sellers exhausted after 3+ consecutive aggressive-sell bars → reversion up.

  H7 Price-CVD Divergence:
      price_change_6bars < 0 AND cum_delta_change_zscore > threshold
      — price fell over 6 bars but aggressive-buy flow rose in same window →
      smart-money absorption → reversion up.

Matrix: 2 hypotheses × 3 thresholds × 3 intervals = 18 variants
        + 3 baseline reference rows (one per interval, no PASS/FAIL).

PASS criteria (all 8 must hold — strengthened after L10 Phase 2b ALARM):
  Primary L8:
    1. Pooled OOS Sharpe > 2.0
    2. Win% > 55
    3. N >= 100
    4. >=2/3 OOS folds positive
    5. Pooled OOS Sharpe > 1.0 (redundant, kept for convention)
  Strict (new):
    6. OOS3 (last fold) Sharpe > 0
    7. |max_oos_sharpe / min_oos_sharpe| < 5 (when min < 0)
    8. trades/day >= 1.5 absolute

Usage:
    .venv/bin/python scripts/research_cvd_standalone.py
    .venv/bin/python scripts/research_cvd_standalone.py --intervals h4 \\
        --hypotheses H5 --thresholds-h5 2.0
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

import numpy as np
import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)

from collectors.config import COINS, binance_ccxt_symbol, get_config  # noqa: E402
from collectors.db import init_pool  # noqa: E402

from backtest_market_flush_multitf import (  # noqa: E402
    CROSS_COIN_FLUSH_Z,
    MARKET_FLUSH_FILTERS,
    RANK_HOLDING_HOURS,
    WF_FOLDS,
    WF_MIN_TRADES,
    _interval_to_bar_hours,
    _z_window,
    build_features_tf,
    fetch_klines_ohlcv,
    load_funding,
    load_liquidations_tf,
    load_oi_tf,
)
from backtest_combo import (  # noqa: E402
    _try_load_with_pepe_fallback,
    compute_cross_coin_features,
)
from research_netposition import (  # noqa: E402
    SUSPICIOUS_SHARPE,
    _fmt_num,
    format_final_ranking,
    run_variant,
    run_walkforward,
)
from research_cvd import (  # noqa: E402
    attach_cvd,
    load_cvd_tf,
)


HYPOTHESES = ("H5", "H7")
DEFAULT_THRESHOLDS_H5 = (1.5, 2.0, 2.5)      # zscore-based (selling exhaustion)
DEFAULT_THRESHOLDS_H7 = (0.5, 1.0, 1.5)      # normalized zscore (divergence)
DEFAULT_INTERVALS = ("h1", "h2", "h4")

CUM_DELTA_WINDOW = 6                          # bars for divergence diff
EXHAUSTION_MIN_CONSECUTIVE = 3
TRADES_PER_DAY_FLOOR = 1.5
FOLD_RATIO_LIMIT = 5.0


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def _consecutive_count(mask: pd.Series) -> pd.Series:
    """Running count of consecutive True values; resets to 0 on False."""
    # Split on every sign change (including False→True and True→False) so that
    # within-group cumcount starts at 0 for each new run. Then zero out rows
    # where mask is False.
    groups = (mask != mask.shift()).fillna(True).cumsum()
    counts = mask.groupby(groups).cumcount() + 1
    return counts.where(mask, 0).astype(int)


def build_exhaustion_features(
    features_df: pd.DataFrame,
    window: int = EXHAUSTION_MIN_CONSECUTIVE,
) -> pd.DataFrame:
    """
    Add consecutive_negative_delta_bars and consecutive_positive_delta_bars
    columns to `features_df`. Both count running streaks of `per_bar_delta`
    sign and reset to 0 when the sign flips.

    `features_df` must already have `per_bar_delta` attached via
    `research_cvd.attach_cvd`. Operates on a single per-coin frame — the
    running count does NOT leak across coins because this function is called
    per-coin before cross-coin merging.

    The `window` argument is currently unused by the implementation but kept
    in the signature as a hint to callers about the minimum streak that
    downstream filters expect (see H5 threshold = 3).
    """
    _ = window  # documentation-only; see docstring
    if features_df is None or features_df.empty:
        return features_df
    if "per_bar_delta" not in features_df.columns:
        features_df["consecutive_negative_delta_bars"] = 0
        features_df["consecutive_positive_delta_bars"] = 0
        return features_df
    delta = features_df["per_bar_delta"]
    neg_mask = (delta < 0).fillna(False)
    pos_mask = (delta > 0).fillna(False)
    features_df["consecutive_negative_delta_bars"] = _consecutive_count(neg_mask)
    features_df["consecutive_positive_delta_bars"] = _consecutive_count(pos_mask)
    return features_df


def build_divergence_features(
    features_df: pd.DataFrame,
    cvd_df: pd.DataFrame,
    cum_delta_window: int = CUM_DELTA_WINDOW,
    bar_hours: int = 4,
) -> pd.DataFrame:
    """
    Add divergence columns to `features_df`:
      - cum_delta_change_6bars: cum_vol_delta[t] - cum_vol_delta[t-window]
      - cum_delta_change_stddev_15d: rolling stddev of that diff over 15 calendar days
      - cum_delta_change_zscore: diff / stddev (NaN on zero/NaN stddev)
      - price_change_6bars: features_df["price"].pct_change(window)

    `features_df["price"]` is populated by `build_features_tf` (it renames the
    ccxt "close" column to "price") — that is why we read "price", not "close".

    If `cvd_df` is missing `cum_vol_delta` (e.g. loader called without
    include_cum_delta=True), all new columns are NaN and downstream
    apply_combo yields False.
    """
    if features_df is None or features_df.empty:
        return features_df

    n = len(features_df)
    if cvd_df is None or cvd_df.empty or "cum_vol_delta" not in cvd_df.columns:
        features_df["cum_delta_change_6bars"] = np.nan
        features_df["cum_delta_change_stddev_15d"] = np.nan
        features_df["cum_delta_change_zscore"] = np.nan
    else:
        aligned = cvd_df["cum_vol_delta"].reindex(features_df.index, method="ffill")
        diff = aligned.diff(cum_delta_window)
        stddev = diff.rolling(_z_window(bar_hours)).std()
        # Avoid division-by-zero on constant stddev — let NaN propagate.
        safe_std = stddev.where(stddev > 0, np.nan)
        features_df["cum_delta_change_6bars"] = diff
        features_df["cum_delta_change_stddev_15d"] = stddev
        features_df["cum_delta_change_zscore"] = diff / safe_std

    if "price" in features_df.columns:
        features_df["price_change_6bars"] = features_df["price"].pct_change(
            cum_delta_window
        )
    else:
        features_df["price_change_6bars"] = np.nan

    _ = n  # silence unused (for future scale checks)
    return features_df


# ---------------------------------------------------------------------------
# Filter construction
# ---------------------------------------------------------------------------

def build_hypothesis_filters(hypothesis: str, threshold: float) -> list[tuple]:
    """
    Return the filter tuples for a given hypothesis. Standalone — does NOT
    extend MARKET_FLUSH_FILTERS.

    H5: per_bar_delta_zscore < -threshold AND consecutive_negative_delta_bars >= 3
    H7: price_change_6bars < 0 AND cum_delta_change_zscore > threshold
    """
    if hypothesis == "H5":
        return [
            ("per_bar_delta_zscore", "<", -threshold),
            ("consecutive_negative_delta_bars", ">=", EXHAUSTION_MIN_CONSECUTIVE),
        ]
    if hypothesis == "H7":
        return [
            ("price_change_6bars", "<", 0.0),
            ("cum_delta_change_zscore", ">", threshold),
        ]
    raise ValueError(
        f"Unknown hypothesis: {hypothesis!r} (valid: {HYPOTHESES})"
    )


def _variant_label(hypothesis: str, threshold: float, interval: str) -> str:
    return f"{hypothesis}_z{threshold}_{interval}"


def _variant_desc(hypothesis: str, threshold: float) -> str:
    if hypothesis == "H5":
        return (
            f"Aggressive Selling Exhaustion, "
            f"zscore<-{threshold} AND 3+ consecutive"
        )
    return (
        f"Price-CVD Divergence, "
        f"price_6b<0 AND cum_delta_zscore>{threshold}"
    )


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def custom_evaluate_verdict(variant: dict, wf: dict) -> str:
    """
    8-rule verdict ladder (Phase 3).

    PASS: all 8 met.
    MARGINAL: primary 5 met, any strict criterion (6/7/8) fails, OR pooled
              OOS Sharpe > SUSPICIOUS_SHARPE (look-ahead smell).
    FAIL: any primary criterion missed, or walk-forward skipped.
    """
    if wf.get("skipped"):
        return "FAIL"

    pooled = variant.get("pooled") or {}
    pooled_oos = wf.get("pooled_oos") or {}
    sharpe_oos = pooled_oos.get("sharpe")
    win_pct = pooled.get("win_pct")
    n = pooled.get("n", 0)
    oos_positive = wf.get("oos_positive", 0)

    primary_pass = (
        sharpe_oos is not None and sharpe_oos > 2.0
        and win_pct is not None and win_pct > 55.0
        and n >= 100
        and oos_positive >= 2
        and sharpe_oos > 1.0
    )
    if not primary_pass:
        return "FAIL"

    # Strict criteria
    folds = wf.get("folds", [])
    oos_sharpes = [
        f.get("sharpe") for f in folds
        if f.get("label", "").startswith("OOS") and f.get("sharpe") is not None
    ]
    oos3_sharpe = oos_sharpes[-1] if oos_sharpes else None

    # Rule 7: |max/min| < 5 when min < 0. Sentinel 1.0 when all positive.
    if oos_sharpes:
        max_s = max(oos_sharpes)
        min_s = min(oos_sharpes)
        if min_s > 0:
            fold_ratio = 1.0
        else:
            fold_ratio = abs(max_s / min_s) if min_s != 0 else float("inf")
    else:
        fold_ratio = float("inf")

    trades_per_day = variant.get("trades_per_day", 0.0)

    strict_pass = (
        oos3_sharpe is not None and oos3_sharpe > 0.0
        and fold_ratio < FOLD_RATIO_LIMIT
        and trades_per_day >= TRADES_PER_DAY_FLOOR
    )

    # Suspicious pooled Sharpe → never auto-PASS.
    if sharpe_oos is not None and sharpe_oos > SUSPICIOUS_SHARPE:
        return "MARGINAL"

    return "PASS" if strict_pass else "MARGINAL"


# ---------------------------------------------------------------------------
# Formatter (local — standalone signals have no baseline comparison)
# ---------------------------------------------------------------------------

def _format_standalone_block(
    name: str,
    variant: dict,
    wf: dict,
    verdict: str,
    hypothesis: str,
    threshold: float,
    interval: str,
) -> str:
    lines: list[str] = []
    lines.append("─" * 64)
    lines.append(
        f"VARIANT: {name}    ({_variant_desc(hypothesis, threshold)}, "
        f"{interval.upper()})"
    )
    lines.append("─" * 64)

    v_pooled = variant.get("pooled") or {}
    v_n = v_pooled.get("n", 0)
    lines.append(
        f"Signal metrics:  N={v_n}  "
        f"Win%={_fmt_num(v_pooled.get('win_pct'))}  "
        f"Sharpe={_fmt_num(v_pooled.get('sharpe'))}  "
        f"trades/day={variant.get('trades_per_day', 0.0):.2f}"
    )
    lines.append("")

    if wf.get("skipped"):
        lines.append(f"Walk-forward: SKIPPED ({wf.get('reason', 'n/a')})")
    else:
        lines.append("Walk-forward:")
        for fold in wf["folds"]:
            sign = ""
            if fold["label"].startswith("OOS") and fold.get("sharpe") is not None:
                sign = "  ← positive" if fold["sharpe"] > 0 else "  ← negative"
            lines.append(
                f"  {fold['label']:<6} N={fold['n']:<4} "
                f"Win={_fmt_num(fold.get('win_pct'))}%  "
                f"Sharpe={_fmt_num(fold.get('sharpe'))}{sign}"
            )
        pooled_oos = wf.get("pooled_oos")
        if pooled_oos:
            lines.append("")
            lines.append(
                f"Pooled OOS: N={pooled_oos['n']}  "
                f"Win={_fmt_num(pooled_oos.get('win_pct'))}%  "
                f"Sharpe={_fmt_num(pooled_oos.get('sharpe'))}  "
                f"positive folds {wf['oos_positive']}/{wf['oos_total']}"
            )
    lines.append("")

    pooled_oos = wf.get("pooled_oos") or {}
    sharpe_oos = pooled_oos.get("sharpe")
    win_pct = v_pooled.get("win_pct")
    oos_positive = wf.get("oos_positive", 0)
    primary_checks = [
        (
            "Pooled OOS Sharpe > 2.0",
            sharpe_oos is not None and sharpe_oos > 2.0,
            _fmt_num(sharpe_oos),
        ),
        (
            "Win% > 55%",
            win_pct is not None and win_pct > 55.0,
            _fmt_num(win_pct),
        ),
        ("N trades >= 100", v_n >= 100, str(v_n)),
        (
            ">=2/3 OOS folds positive",
            oos_positive >= 2,
            f"{oos_positive}/{wf.get('oos_total', 0)}",
        ),
        (
            "Pooled OOS Sharpe > 1.0",
            sharpe_oos is not None and sharpe_oos > 1.0,
            _fmt_num(sharpe_oos),
        ),
    ]
    lines.append("PASS criteria (primary):")
    for label, ok, val in primary_checks:
        icon = "[OK]" if ok else "[--]"
        lines.append(f"  {icon} {label:<38} ({val})")

    # Strict criteria
    folds = wf.get("folds", [])
    oos_sharpes = [
        f.get("sharpe") for f in folds
        if f.get("label", "").startswith("OOS") and f.get("sharpe") is not None
    ]
    oos3_sharpe = oos_sharpes[-1] if oos_sharpes else None

    if oos_sharpes:
        max_s = max(oos_sharpes)
        min_s = min(oos_sharpes)
        if min_s > 0:
            fold_ratio: float = 1.0
            fold_ratio_str = "1.00 (all OOS positive)"
        elif min_s == 0:
            fold_ratio = float("inf")
            fold_ratio_str = "inf"
        else:
            fold_ratio = abs(max_s / min_s)
            fold_ratio_str = f"{fold_ratio:.2f}"
    else:
        fold_ratio = float("inf")
        fold_ratio_str = "n/a"

    tpd = variant.get("trades_per_day", 0.0)
    strict_checks = [
        (
            "OOS3 Sharpe > 0",
            oos3_sharpe is not None and oos3_sharpe > 0,
            _fmt_num(oos3_sharpe),
        ),
        (
            "fold ratio < 5",
            fold_ratio < FOLD_RATIO_LIMIT,
            fold_ratio_str,
        ),
        (
            f"trades/day >= {TRADES_PER_DAY_FLOOR}",
            tpd >= TRADES_PER_DAY_FLOOR,
            f"{tpd:.2f}",
        ),
    ]
    lines.append("")
    lines.append("PASS criteria (strict, Phase 2b lessons):")
    for label, ok, val in strict_checks:
        icon = "[OK]" if ok else "[!!]"
        lines.append(f"  {icon} {label:<38} ({val})")

    if sharpe_oos is not None and sharpe_oos > SUSPICIOUS_SHARPE:
        lines.append(
            f"  [!!] suspicious Sharpe {sharpe_oos:.2f} > {SUSPICIOUS_SHARPE} "
            f"— manual review required"
        )

    lines.append("")
    lines.append(f"VERDICT: {verdict}")
    lines.append("─" * 64)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_coins_for_interval(interval: str) -> dict[str, pd.DataFrame]:
    bar_hours = _interval_to_bar_hours(interval)
    timeframe = f"{bar_hours}h"

    per_coin: dict[str, pd.DataFrame] = {}
    for coin in COINS:
        liq_sym, liq_df = _try_load_with_pepe_fallback(
            coin, lambda s: load_liquidations_tf(s, interval),
        )
        _, oi_df = _try_load_with_pepe_fallback(
            coin, lambda s: load_oi_tf(s, interval),
        )
        _, fund_df = _try_load_with_pepe_fallback(coin, load_funding)
        # Phase 3: load cum_vol_delta for H7 divergence features.
        cvd_df = load_cvd_tf(coin, interval, include_cum_delta=True)

        ccxt_sym = binance_ccxt_symbol(coin)
        if liq_df.empty:
            print(f"  {coin:<5}: no liquidation data — skipped")
            per_coin[coin] = pd.DataFrame()
            continue
        since_ms = int(liq_df["timestamp"].min().timestamp() * 1000)
        try:
            ohlcv_df = fetch_klines_ohlcv(ccxt_sym, since_ms, timeframe)
        except Exception as e:
            print(f"  {coin:<5}: ccxt fetch failed ({e}) — skipped")
            per_coin[coin] = pd.DataFrame()
            continue

        feat = build_features_tf(
            coin, liq_sym, liq_df, oi_df, fund_df, ohlcv_df, bar_hours,
        )
        if feat.empty:
            per_coin[coin] = feat
            continue
        # Attach CVD base features (buy_ratio, per_bar_delta, zscore) per-coin.
        attach_cvd(feat, cvd_df, bar_hours)
        # Per-coin exhaustion streak — does NOT leak across coins.
        build_exhaustion_features(feat, window=EXHAUSTION_MIN_CONSECUTIVE)
        # Per-coin divergence features (uses cvd_df.cum_vol_delta + feat.price).
        build_divergence_features(
            feat, cvd_df, cum_delta_window=CUM_DELTA_WINDOW, bar_hours=bar_hours,
        )
        per_coin[coin] = feat
        cvd_cov = (
            int(feat["per_bar_delta"].notna().mean() * 100)
            if "per_bar_delta" in feat.columns
            else 0
        )
        div_cov = (
            int(feat["cum_delta_change_zscore"].notna().mean() * 100)
            if "cum_delta_change_zscore" in feat.columns
            else 0
        )
        print(
            f"  {coin:<5}: rows={len(feat)}  cvd_cov={cvd_cov}%  "
            f"div_cov={div_cov}%"
        )

    # Inject cross-coin features for schema compatibility (standalone filters
    # don't reference n_coins_flushing, but build_features_tf's baseline path
    # expects the column in the frame).
    return compute_cross_coin_features(per_coin, flush_z=CROSS_COIN_FLUSH_Z)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_csv_floats(s: str) -> list[float]:
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def _parse_csv_strs(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="L13 Phase 3: CVD standalone research (offline backtest).",
    )
    parser.add_argument(
        "--intervals", type=str, default=",".join(DEFAULT_INTERVALS),
        help="Comma-separated intervals (default: h1,h2,h4).",
    )
    parser.add_argument(
        "--hypotheses", type=str, default=",".join(HYPOTHESES),
        help="Comma-separated hypotheses (default: H5,H7).",
    )
    parser.add_argument(
        "--thresholds-h5", type=str,
        default=",".join(str(t) for t in DEFAULT_THRESHOLDS_H5),
        help="Comma-separated per_bar_delta_zscore thresholds for H5 "
             "(default: 1.5,2.0,2.5).",
    )
    parser.add_argument(
        "--thresholds-h7", type=str,
        default=",".join(str(t) for t in DEFAULT_THRESHOLDS_H7),
        help="Comma-separated cum_delta_change_zscore thresholds for H7 "
             "(default: 0.5,1.0,1.5).",
    )
    args = parser.parse_args()

    intervals = _parse_csv_strs(args.intervals)
    hypotheses = _parse_csv_strs(args.hypotheses)
    thresholds_h5 = _parse_csv_floats(args.thresholds_h5)
    thresholds_h7 = _parse_csv_floats(args.thresholds_h7)

    for iv in intervals:
        if iv not in DEFAULT_INTERVALS:
            raise ValueError(f"Unknown interval {iv!r} (valid: {DEFAULT_INTERVALS})")
    for h in hypotheses:
        if h not in HYPOTHESES:
            raise ValueError(f"Unknown hypothesis {h!r} (valid: {HYPOTHESES})")

    init_pool(get_config())

    print("=" * 72)
    print("L13 PHASE 3 — CVD STANDALONE RESEARCH")
    print("=" * 72)
    print(f"  intervals={intervals}  hypotheses={hypotheses}")
    print(f"  thresholds H5={thresholds_h5}  thresholds H7={thresholds_h7}")
    print(
        f"  ranking at h={RANK_HOLDING_HOURS}  walk-forward {WF_FOLDS}-fold "
        f"(min N={WF_MIN_TRADES})"
    )
    print()

    ranking_rows: list[dict] = []

    for interval in intervals:
        print("=" * 72)
        print(f"INTERVAL {interval.upper()} — loading data")
        print("=" * 72)
        all_dfs = _load_coins_for_interval(interval)

        # Baseline reference row (market_flush, no PASS/FAIL — just context).
        baseline = run_variant(all_dfs, list(MARKET_FLUSH_FILTERS), RANK_HOLDING_HOURS)
        baseline_wf = run_walkforward(
            all_dfs, list(MARKET_FLUSH_FILTERS), RANK_HOLDING_HOURS,
        )
        b_pooled = baseline.get("pooled") or {}
        print()
        print(
            f"  BASELINE ({interval.upper()} market_flush ref): "
            f"N={b_pooled.get('n', 0)}  "
            f"Win%={_fmt_num(b_pooled.get('win_pct'))}  "
            f"Sharpe={_fmt_num(b_pooled.get('sharpe'))}  "
            f"trades/day={baseline.get('trades_per_day', 0.0):.2f}"
        )
        ranking_rows.append({
            "name": f"BASELINE_{interval}",
            "n": b_pooled.get("n", 0),
            "win_pct": b_pooled.get("win_pct"),
            "sharpe_oos": (baseline_wf.get("pooled_oos") or {}).get("sharpe"),
            "oos": f"{baseline_wf.get('oos_positive', 0)}/"
                   f"{baseline_wf.get('oos_total', 0)}",
            "trades_per_day": baseline.get("trades_per_day", 0.0),
            "verdict": "REF",
        })

        for hypothesis in hypotheses:
            thr_list = thresholds_h5 if hypothesis == "H5" else thresholds_h7
            for threshold in thr_list:
                name = _variant_label(hypothesis, threshold, interval)
                filters = build_hypothesis_filters(hypothesis, threshold)
                variant = run_variant(all_dfs, filters, RANK_HOLDING_HOURS)
                wf = run_walkforward(all_dfs, filters, RANK_HOLDING_HOURS)
                verdict = custom_evaluate_verdict(variant, wf)
                print()
                print(_format_standalone_block(
                    name, variant, wf, verdict, hypothesis, threshold, interval,
                ))
                v_pooled = variant.get("pooled") or {}
                ranking_rows.append({
                    "name": name,
                    "n": v_pooled.get("n", 0),
                    "win_pct": v_pooled.get("win_pct"),
                    "sharpe_oos": (wf.get("pooled_oos") or {}).get("sharpe"),
                    "oos": f"{wf.get('oos_positive', 0)}/{wf.get('oos_total', 0)}",
                    "trades_per_day": variant.get("trades_per_day", 0.0),
                    "verdict": verdict,
                })

    print()
    print(format_final_ranking(ranking_rows))

    print()
    print("=" * 72)
    print("RECOMMENDED ACTION")
    print("=" * 72)
    pass_rows = [r for r in ranking_rows if r["verdict"] == "PASS"]
    marginal_rows = [r for r in ranking_rows if r["verdict"] == "MARGINAL"]
    if pass_rows:
        print("  → L13 Phase 3b validation candidates (do NOT ship live without it):")
        for r in pass_rows:
            print(
                f"     • {r['name']}  Sharpe={_fmt_num(r['sharpe_oos'])}  "
                f"trades/d={r['trades_per_day']:.2f}"
            )
    elif marginal_rows:
        print("  → No PASS variants. MARGINAL list (review before L13 Phase 3b):")
        for r in marginal_rows:
            print(
                f"     • {r['name']}  Sharpe={_fmt_num(r['sharpe_oos'])}  "
                f"trades/d={r['trades_per_day']:.2f}"
            )
    else:
        print("  → No variant PASS or MARGINAL. Reject CVD standalone; "
              "consider L11 SHORT research.")


if __name__ == "__main__":
    main()
