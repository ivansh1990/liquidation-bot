#!/usr/bin/env python3
"""
L13 Phase 2: CVD (Cumulative Volume Delta) research script.

Tests whether CVD-derived filters improve the L8 market_flush signal.
Matrix: 2 hypotheses × 3 thresholds × 3 timeframes = 18 variants + baseline
sanity per timeframe. Research-only — no changes to bot/, exchange/, or the
locked L8 backtest.

Hypotheses:
  H3 Taker Buy Dominance:  market_flush AND buy_ratio > threshold
                           (buy_ratio = agg_taker_buy_vol / (buy + sell))
  H4 CVD Delta Divergence: market_flush AND per_bar_delta_zscore > threshold
                           (per_bar_delta = agg_taker_buy_vol - agg_taker_sell_vol)

Note: coinglass_cvd.cum_vol_delta is *cumulative since start-of-history*
(not a per-bar delta), so it is **unused** in Phase 2. The per-bar delta
is recomputed on-the-fly from the taker buy/sell columns.

Usage:
    .venv/bin/python scripts/research_cvd.py
    .venv/bin/python scripts/research_cvd.py --intervals h4 --hypotheses H3 --thresholds-h3 0.55
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)

from collectors.config import COINS, binance_ccxt_symbol, get_config  # noqa: E402
from collectors.db import get_conn, init_pool  # noqa: E402

from backtest_market_flush_multitf import (  # noqa: E402
    CROSS_COIN_FLUSH_Z,
    MARKET_FLUSH_FILTERS,
    RANK_HOLDING_HOURS,
    WF_FOLDS,
    WF_MIN_TRADES,
    _interval_to_bar_hours,
    _zscore_tf,
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
    L8_H4_REFERENCE,
    SUSPICIOUS_SHARPE,
    _fmt_num,
    evaluate_verdict,
    format_final_ranking,
    format_variant_block,
    run_variant,
    run_walkforward,
)


HYPOTHESES = ("H3", "H4")
DEFAULT_THRESHOLDS_H3 = (0.52, 0.55, 0.58)   # ratio-based
DEFAULT_THRESHOLDS_H4 = (0.5, 1.0, 1.5)      # zscore-based
DEFAULT_INTERVALS = ("h1", "h2", "h4")


# ---------------------------------------------------------------------------
# DB loader
# ---------------------------------------------------------------------------

def load_cvd_tf(
    symbol: str,
    interval: str,
    include_cum_delta: bool = False,
) -> pd.DataFrame:
    """
    Load CVD columns from coinglass_cvd_{interval}. Returns a UTC-indexed
    DataFrame.

    By default only `agg_taker_buy_vol` + `agg_taker_sell_vol` are selected —
    `cum_vol_delta` is cumulative-since-history-start and unused in Phase 2.
    Pass `include_cum_delta=True` (L13 Phase 3) to also load `cum_vol_delta`
    for price/CVD divergence analysis.
    """
    table = f"coinglass_cvd_{interval}"
    extra_col = ", cum_vol_delta" if include_cum_delta else ""
    base_cols = ["agg_taker_buy_vol", "agg_taker_sell_vol"]
    extra_cols = ["cum_vol_delta"] if include_cum_delta else []
    all_cols = base_cols + extra_cols
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT timestamp, agg_taker_buy_vol, agg_taker_sell_vol{extra_col}
                FROM {table}
                WHERE symbol = %s
                ORDER BY timestamp
                """,
                (symbol,),
            )
            rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=all_cols)
    df = pd.DataFrame(rows, columns=["timestamp"] + all_cols)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.drop_duplicates(subset="timestamp").set_index("timestamp").sort_index()
    return df.astype({c: float for c in all_cols})


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def build_cvd_features(
    cvd_df: pd.DataFrame,
    index: pd.DatetimeIndex,
    bar_hours: int,
) -> pd.DataFrame:
    """
    Reindex `cvd_df` onto `index` (ffill) and compute per-coin CVD features.
    Returns a DataFrame with exactly three columns:
      - buy_ratio              (buy / (buy + sell), NaN when denominator == 0)
      - per_bar_delta          (buy - sell)
      - per_bar_delta_zscore   (rolling z, 15 calendar days)

    If `cvd_df` is empty the columns are filled with NaN so downstream
    `apply_combo` yields False on every row rather than crashing.
    """
    out = pd.DataFrame(index=index)
    if cvd_df is None or cvd_df.empty:
        out["buy_ratio"] = np.nan
        out["per_bar_delta"] = np.nan
        out["per_bar_delta_zscore"] = np.nan
        return out
    aligned = cvd_df.reindex(index, method="ffill")
    buy = aligned["agg_taker_buy_vol"]
    sell = aligned["agg_taker_sell_vol"]
    denom = buy + sell
    # NaN-on-zero-denominator guard (defensive — real data is positive).
    ratio = np.where(denom == 0, np.nan, buy / denom.where(denom != 0, np.nan))
    out["buy_ratio"] = pd.Series(ratio, index=index)
    out["per_bar_delta"] = buy - sell
    out["per_bar_delta_zscore"] = _zscore_tf(out["per_bar_delta"], bar_hours)
    return out


def attach_cvd(
    features_df: pd.DataFrame,
    cvd_df: pd.DataFrame,
    bar_hours: int,
) -> pd.DataFrame:
    """Attach CVD-derived columns onto an existing per-coin feature frame."""
    if features_df is None or features_df.empty:
        return features_df
    cvd_feat = build_cvd_features(cvd_df, features_df.index, bar_hours)
    features_df["buy_ratio"] = cvd_feat["buy_ratio"]
    features_df["per_bar_delta"] = cvd_feat["per_bar_delta"]
    features_df["per_bar_delta_zscore"] = cvd_feat["per_bar_delta_zscore"]
    return features_df


# ---------------------------------------------------------------------------
# Filter construction
# ---------------------------------------------------------------------------

def build_hypothesis_filters(hypothesis: str, threshold: float) -> list[tuple]:
    """
    Extend locked MARKET_FLUSH_FILTERS with the hypothesis-specific CVD
    condition. CVD is additive — the locked L3b-2 thresholds
    (z_self>1.0, n_coins>=4) are preserved verbatim.
    """
    if hypothesis == "H3":
        col = "buy_ratio"
    elif hypothesis == "H4":
        col = "per_bar_delta_zscore"
    else:
        raise ValueError(
            f"Unknown hypothesis: {hypothesis!r} (valid: {HYPOTHESES})"
        )
    return list(MARKET_FLUSH_FILTERS) + [(col, ">", threshold)]


# ---------------------------------------------------------------------------
# Variant label helper
# ---------------------------------------------------------------------------

def _variant_label(hypothesis: str, threshold: float, interval: str) -> str:
    """Human-readable unique variant name."""
    prefix = "r" if hypothesis == "H3" else "z"
    return f"{hypothesis}_{prefix}{threshold}_{interval}"


def _variant_desc(hypothesis: str, threshold: float) -> str:
    if hypothesis == "H3":
        return f"Taker Buy Dominance, buy_ratio>{threshold}"
    return f"CVD Delta Divergence, per_bar_delta_zscore>{threshold}"


# ---------------------------------------------------------------------------
# Data loading per interval
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
        # CVD: Phase 1 confirmed canonical "PEPE" is stored (no 1000PEPE).
        # Direct load — avoids a no-op fallback probe and keeps logs clean.
        cvd_df = load_cvd_tf(coin, interval)

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
        attach_cvd(feat, cvd_df, bar_hours)
        per_coin[coin] = feat
        coverage = (
            int(feat["buy_ratio"].notna().mean() * 100)
            if "buy_ratio" in feat.columns
            else 0
        )
        print(f"  {coin:<5}: rows={len(feat)}  cvd_coverage={coverage}%")

    return compute_cross_coin_features(per_coin, flush_z=CROSS_COIN_FLUSH_Z)


# ---------------------------------------------------------------------------
# CVD-specific variant block (thin wrapper reusing NetPos formatter shape)
# ---------------------------------------------------------------------------

def _format_cvd_variant_block(
    name: str,
    variant: dict,
    wf: dict,
    verdict: str,
    baseline: dict,
    hypothesis: str,
    threshold: float,
    interval: str,
) -> str:
    """
    Reuse `format_variant_block` from research_netposition for shape, but
    swap the per-variant description line so readers see "buy_ratio>X" or
    "per_bar_delta_zscore>X" instead of a net-position description.
    """
    block = format_variant_block(
        name, variant, wf, verdict, baseline,
        hypothesis, threshold, interval,
    )
    # NetPos formatter's description line mentions `z_netpos>...` with a
    # Contrarian/Confirmation prefix — overwrite that line with a CVD-aware one.
    lines = block.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("VARIANT: "):
            lines[i] = (
                f"VARIANT: {name}    "
                f"({_variant_desc(hypothesis, threshold)}, {interval.upper()})"
            )
            break
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="L13 Phase 2: CVD research (offline backtest).",
    )
    parser.add_argument(
        "--intervals", type=str, default=",".join(DEFAULT_INTERVALS),
        help="Comma-separated intervals (default: h1,h2,h4).",
    )
    parser.add_argument(
        "--hypotheses", type=str, default=",".join(HYPOTHESES),
        help="Comma-separated hypotheses (default: H3,H4).",
    )
    parser.add_argument(
        "--thresholds-h3", type=str,
        default=",".join(str(t) for t in DEFAULT_THRESHOLDS_H3),
        help="Comma-separated buy_ratio thresholds (default: 0.52,0.55,0.58).",
    )
    parser.add_argument(
        "--thresholds-h4", type=str,
        default=",".join(str(t) for t in DEFAULT_THRESHOLDS_H4),
        help="Comma-separated per_bar_delta_zscore thresholds "
             "(default: 0.5,1.0,1.5).",
    )
    args = parser.parse_args()

    intervals = [x.strip() for x in args.intervals.split(",") if x.strip()]
    hypotheses = [x.strip() for x in args.hypotheses.split(",") if x.strip()]
    thresholds_h3 = [
        float(x.strip()) for x in args.thresholds_h3.split(",") if x.strip()
    ]
    thresholds_h4 = [
        float(x.strip()) for x in args.thresholds_h4.split(",") if x.strip()
    ]

    for iv in intervals:
        if iv not in DEFAULT_INTERVALS:
            raise ValueError(f"Unknown interval {iv!r} (valid: {DEFAULT_INTERVALS})")
    for h in hypotheses:
        if h not in HYPOTHESES:
            raise ValueError(f"Unknown hypothesis {h!r} (valid: {HYPOTHESES})")

    init_pool(get_config())

    print("=" * 72)
    print("L13 PHASE 2 — CVD RESEARCH")
    print("=" * 72)
    print(
        f"  intervals={intervals}  hypotheses={hypotheses}"
    )
    print(
        f"  thresholds H3={thresholds_h3}  thresholds H4={thresholds_h4}"
    )
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

        baseline = run_variant(all_dfs, list(MARKET_FLUSH_FILTERS), RANK_HOLDING_HOURS)
        baseline_wf = run_walkforward(
            all_dfs, list(MARKET_FLUSH_FILTERS), RANK_HOLDING_HOURS,
        )

        if interval == "h4":
            b_pooled = baseline.get("pooled") or {}
            obs_sharpe = b_pooled.get("sharpe")
            obs_win = b_pooled.get("win_pct")
            obs_n = b_pooled.get("n")
            print()
            print("  H4 BASELINE PARITY:")
            print(f"    N:      observed={obs_n}      L8 ref={L8_H4_REFERENCE['n']}")
            print(f"    Win%:   observed={obs_win}   L8 ref={L8_H4_REFERENCE['win_pct']}")
            print(f"    Sharpe: observed={obs_sharpe} L8 ref={L8_H4_REFERENCE['sharpe']}")
            if obs_sharpe is not None:
                drift = abs(obs_sharpe - L8_H4_REFERENCE["sharpe"]) / L8_H4_REFERENCE["sharpe"]
                if drift > 0.05:
                    print(
                        f"  [!!] Sharpe drift {drift * 100:.1f}% > 5% — verify "
                        f"data hasn't regressed"
                    )
            print()

        b_pooled = baseline.get("pooled") or {}
        ranking_rows.append({
            "name": f"BASELINE_{interval}",
            "n": b_pooled.get("n", 0),
            "win_pct": b_pooled.get("win_pct"),
            "sharpe_oos": (baseline_wf.get("pooled_oos") or {}).get("sharpe"),
            "oos": f"{baseline_wf.get('oos_positive', 0)}/{baseline_wf.get('oos_total', 0)}",
            "trades_per_day": baseline.get("trades_per_day", 0.0),
            "verdict": "REF",
        })

        for hypothesis in hypotheses:
            thr_list = thresholds_h3 if hypothesis == "H3" else thresholds_h4
            for threshold in thr_list:
                name = _variant_label(hypothesis, threshold, interval)
                filters = build_hypothesis_filters(hypothesis, threshold)
                variant = run_variant(all_dfs, filters, RANK_HOLDING_HOURS)
                wf = run_walkforward(all_dfs, filters, RANK_HOLDING_HOURS)
                verdict = evaluate_verdict(
                    variant, wf, baseline.get("trades_per_day", 0.0),
                )
                # L13 Phase 2 strictness: trades/day >= 1.5 absolute floor.
                v_tpd = variant.get("trades_per_day", 0.0)
                if verdict == "PASS" and v_tpd < 1.5:
                    verdict = "MARGINAL"
                print()
                print(_format_cvd_variant_block(
                    name, variant, wf, verdict, baseline,
                    hypothesis, threshold, interval,
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
        print("  → L13 Phase 2b validation candidates (do NOT ship live without it):")
        for r in pass_rows:
            print(
                f"     • {r['name']}  Sharpe={_fmt_num(r['sharpe_oos'])}  "
                f"trades/d={r['trades_per_day']:.2f}"
            )
    elif marginal_rows:
        print("  → No PASS variants. MARGINAL list (review before L13 Phase 2b):")
        for r in marginal_rows:
            print(
                f"     • {r['name']}  Sharpe={_fmt_num(r['sharpe_oos'])}  "
                f"trades/d={r['trades_per_day']:.2f}"
            )
    else:
        print("  → No variant PASS or MARGINAL. Reject CVD filters; consider L11 SHORT.")


if __name__ == "__main__":
    main()
