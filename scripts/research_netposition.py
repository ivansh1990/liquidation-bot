#!/usr/bin/env python3
"""
L10 Phase 2: Net Position research script.

Tests whether Net Position flow filters improve the L8 market_flush signal.
Matrix: 2 hypotheses × 3 z-thresholds × 3 timeframes = 18 variants + baseline
sanity per timeframe. Research-only — no changes to bot/, exchange/, or the
locked L8 backtest.

Hypotheses:
  H1 Contrarian:   market_flush AND net_short_change_zscore > z_netpos
  H2 Confirmation: market_flush AND net_long_change_zscore  > z_netpos

Usage:
    .venv/bin/python scripts/research_netposition.py
    .venv/bin/python scripts/research_netposition.py --intervals h4 --hypotheses H1 --thresholds 1.0
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
from collectors.db import get_conn, init_pool  # noqa: E402

from backtest_market_flush_multitf import (  # noqa: E402
    CROSS_COIN_FLUSH_Z,
    HOLDING_HOURS_MAP,
    MARKET_FLUSH_FILTERS,
    RANK_HOLDING_HOURS,
    WF_FOLDS,
    WF_MIN_TRADES,
    _interval_to_bar_hours,
    _z_window,
    _zscore_tf,
    build_features_tf,
    fetch_klines_ohlcv,
    load_funding,
    load_liquidations_tf,
    load_oi_tf,
)
from backtest_combo import (  # noqa: E402
    _metrics_for_trades,
    _try_load_with_pepe_fallback,
    apply_combo,
    compute_cross_coin_features,
)
from walkforward_h1_flush import split_folds  # noqa: E402


# L8 reference numbers for h4 baseline parity check (market_flush @ h=8).
L8_H4_REFERENCE = {"n": 428, "win_pct": 61.0, "sharpe": 5.87}

HYPOTHESES = ("H1", "H2")
DEFAULT_THRESHOLDS = (0.5, 1.0, 1.5)
DEFAULT_INTERVALS = ("h1", "h2", "h4")

# Guardrail: Sharpe above this on OOS is suspicious — flag for manual review
# rather than auto-marking PASS (defence against silent look-ahead bugs).
SUSPICIOUS_SHARPE = 8.0


# ---------------------------------------------------------------------------
# DB loader
# ---------------------------------------------------------------------------

def load_netposition_tf(symbol: str, interval: str) -> pd.DataFrame:
    """
    Load net_long_change + net_short_change from coinglass_netposition_{interval}
    (filtered to the Binance exchange). Returns a UTC-indexed DataFrame.
    """
    table = f"coinglass_netposition_{interval}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT timestamp, net_long_change, net_short_change
                FROM {table}
                WHERE symbol = %s AND exchange = 'Binance'
                ORDER BY timestamp
                """,
                (symbol,),
            )
            rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=["net_long_change", "net_short_change"])
    df = pd.DataFrame(
        rows, columns=["timestamp", "net_long_change", "net_short_change"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.drop_duplicates(subset="timestamp").set_index("timestamp").sort_index()
    return df.astype({"net_long_change": float, "net_short_change": float})


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def build_netposition_features(
    netpos_df: pd.DataFrame,
    index: pd.DatetimeIndex,
    bar_hours: int,
) -> pd.DataFrame:
    """
    Reindex `netpos_df` onto `index` (ffill) and compute per-coin z-scores.
    Returns a DataFrame with exactly two columns:
    `net_long_change_zscore`, `net_short_change_zscore`.

    If `netpos_df` is empty the columns are filled with NaN so downstream
    `apply_combo` yields False on every row rather than crashing.
    """
    out = pd.DataFrame(index=index)
    if netpos_df is None or netpos_df.empty:
        out["net_long_change_zscore"] = np.nan
        out["net_short_change_zscore"] = np.nan
        return out
    aligned = netpos_df.reindex(index, method="ffill")
    out["net_long_change_zscore"] = _zscore_tf(aligned["net_long_change"], bar_hours)
    out["net_short_change_zscore"] = _zscore_tf(aligned["net_short_change"], bar_hours)
    return out


def attach_netposition(
    features_df: pd.DataFrame,
    netpos_df: pd.DataFrame,
    bar_hours: int,
) -> pd.DataFrame:
    """Attach net-position z-scores onto an existing per-coin feature frame."""
    if features_df is None or features_df.empty:
        return features_df
    netpos_feat = build_netposition_features(netpos_df, features_df.index, bar_hours)
    features_df["net_long_change_zscore"] = netpos_feat["net_long_change_zscore"]
    features_df["net_short_change_zscore"] = netpos_feat["net_short_change_zscore"]
    return features_df


# ---------------------------------------------------------------------------
# Filter construction
# ---------------------------------------------------------------------------

def build_hypothesis_filters(hypothesis: str, z_netpos: float) -> list[tuple]:
    """
    Extend locked MARKET_FLUSH_FILTERS with the hypothesis-specific net
    position condition. Net Position is additive — the locked L3b-2
    thresholds (z_self>1.0, n_coins>=4) are preserved verbatim.
    """
    if hypothesis == "H1":
        col = "net_short_change_zscore"
    elif hypothesis == "H2":
        col = "net_long_change_zscore"
    else:
        raise ValueError(f"Unknown hypothesis: {hypothesis!r} (valid: {HYPOTHESES})")
    return list(MARKET_FLUSH_FILTERS) + [(col, ">", z_netpos)]


# ---------------------------------------------------------------------------
# Variant runners (structured, non-printing mirrors of L8 helpers)
# ---------------------------------------------------------------------------

def run_variant(
    all_coin_dfs: dict[str, pd.DataFrame],
    filters: list[tuple],
    holding_hours: int = RANK_HOLDING_HOURS,
) -> dict:
    """
    Per-coin + pooled metrics for a single combo. Same shape as L8's
    `aggregate_across_coins` but returns a dict instead of printing.
    """
    ret_col = f"return_{holding_hours}h"
    per_coin: list[dict] = []
    pooled_trades: list[pd.Series] = []
    min_ts: Optional[pd.Timestamp] = None
    max_ts: Optional[pd.Timestamp] = None

    for coin, df in all_coin_dfs.items():
        if df is None or df.empty or ret_col not in df.columns:
            continue
        if min_ts is None or df.index.min() < min_ts:
            min_ts = df.index.min()
        if max_ts is None or df.index.max() > max_ts:
            max_ts = df.index.max()
        mask = apply_combo(df, filters)
        trades = df.loc[mask, ret_col].dropna()
        if len(trades) == 0:
            per_coin.append(
                {"coin": coin, "n": 0, "win_pct": None, "avg_ret": None, "sharpe": None}
            )
            continue
        m = _metrics_for_trades(trades, holding_hours)
        if m is None:
            per_coin.append(
                {
                    "coin": coin,
                    "n": int(len(trades)),
                    "win_pct": round((trades > 0).mean() * 100, 1),
                    "avg_ret": round(trades.mean(), 2),
                    "sharpe": None,
                }
            )
        else:
            per_coin.append({"coin": coin, **m})
        pooled_trades.append(trades)

    pooled: Optional[dict] = None
    if pooled_trades:
        combined = pd.concat(pooled_trades)
        pooled = _metrics_for_trades(combined, holding_hours)
        if pooled is None and len(combined) > 0:
            pooled = {
                "n": int(len(combined)),
                "win_pct": round((combined > 0).mean() * 100, 1),
                "avg_ret": round(combined.mean(), 2),
                "sharpe": None,
            }

    days_span = 0
    if min_ts is not None and max_ts is not None:
        days_span = max(1, (max_ts - min_ts).days)

    trades_per_day = 0.0
    if pooled and pooled.get("n") and days_span:
        trades_per_day = pooled["n"] / days_span

    return {
        "per_coin": per_coin,
        "pooled": pooled,
        "days_span": days_span,
        "trades_per_day": round(trades_per_day, 3),
    }


def run_walkforward(
    all_coin_dfs: dict[str, pd.DataFrame],
    filters: list[tuple],
    holding_hours: int = RANK_HOLDING_HOURS,
    n_folds: int = WF_FOLDS,
) -> dict:
    """
    Structured walk-forward. Fold 0 = train, 1..n-1 = OOS. Returns dict with
    per-fold metrics, pooled OOS metrics, and a pass_flag mirroring L8's
    verdict logic.
    """
    ret_col = f"return_{holding_hours}h"
    rows: list[tuple[pd.Timestamp, float]] = []
    all_index: list[pd.Timestamp] = []
    for df in all_coin_dfs.values():
        if df is None or df.empty or ret_col not in df.columns:
            continue
        all_index.extend(df.index.tolist())
        mask = apply_combo(df, filters)
        trades = df.loc[mask, ret_col].dropna()
        for ts, r in trades.items():
            rows.append((ts, float(r)))

    if len(rows) < WF_MIN_TRADES or not all_index:
        reason = (
            f"N={len(rows)} < {WF_MIN_TRADES}"
            if len(rows) < WF_MIN_TRADES
            else "no index"
        )
        return {
            "skipped": True,
            "reason": reason,
            "folds": [],
            "oos_positive": 0,
            "oos_total": max(1, n_folds - 1),
            "pooled_oos": None,
            "pass_flag": False,
        }

    idx_sorted = pd.DatetimeIndex(sorted(set(all_index)))
    fold_bounds = split_folds(idx_sorted, n_folds)
    trade_df = (
        pd.DataFrame(rows, columns=["ts", "ret"]).sort_values("ts").set_index("ts")
    )

    fold_results: list[dict] = []
    oos_sharpes: list[float] = []
    oos_pooled: list[float] = []

    for i, (start, end) in enumerate(fold_bounds):
        label = "Train" if i == 0 else f"OOS{i}"
        if i == len(fold_bounds) - 1:
            in_fold = trade_df.loc[
                (trade_df.index >= start) & (trade_df.index <= end), "ret"
            ]
        else:
            in_fold = trade_df.loc[
                (trade_df.index >= start) & (trade_df.index < end), "ret"
            ]
        entry: dict
        if len(in_fold) == 0:
            entry = {
                "label": label, "start": start, "end": end,
                "n": 0, "win_pct": None, "avg_ret": None, "sharpe": None,
            }
        else:
            m = _metrics_for_trades(in_fold, holding_hours)
            if m is None:
                entry = {
                    "label": label, "start": start, "end": end,
                    "n": int(len(in_fold)),
                    "win_pct": round((in_fold > 0).mean() * 100, 1),
                    "avg_ret": round(in_fold.mean(), 2),
                    "sharpe": None,
                }
            else:
                entry = {"label": label, "start": start, "end": end, **m}
            if i > 0:
                if entry.get("sharpe") is not None:
                    oos_sharpes.append(entry["sharpe"])
                oos_pooled.extend(in_fold.tolist())
        fold_results.append(entry)

    pooled_m = (
        _metrics_for_trades(pd.Series(oos_pooled), holding_hours)
        if oos_pooled
        else None
    )
    positive_folds = sum(1 for s in oos_sharpes if s > 0)
    pooled_sharpe = pooled_m["sharpe"] if pooled_m else None
    pass_flag = (
        pooled_sharpe is not None
        and pooled_sharpe > 1.0
        and positive_folds >= 2
        and (n_folds - 1) == 3
    )
    return {
        "skipped": False,
        "folds": fold_results,
        "oos_positive": positive_folds,
        "oos_total": max(1, n_folds - 1),
        "pooled_oos": pooled_m,
        "pass_flag": pass_flag,
    }


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def evaluate_verdict(
    variant: dict,
    wf: dict,
    baseline_trades_per_day: float,
) -> str:
    """
    PASS: all 6 criteria met.
    MARGINAL: primary 5 met but extended trades/day criterion failed,
              or Sharpe > SUSPICIOUS_SHARPE (manual review required).
    FAIL: any primary criterion not met.
    """
    pooled = variant.get("pooled")
    pooled_oos = wf.get("pooled_oos")
    if wf.get("skipped") or pooled is None or pooled_oos is None:
        return "FAIL"

    sharpe_oos = pooled_oos.get("sharpe")
    win_pct = pooled.get("win_pct")
    n_trades = pooled.get("n")

    primary_ok = (
        sharpe_oos is not None
        and sharpe_oos > 2.0
        and win_pct is not None
        and win_pct > 55.0
        and n_trades is not None
        and n_trades >= 100
        and wf.get("oos_positive", 0) >= 2
        and sharpe_oos > 1.0  # formally redundant with > 2.0, kept per spec
    )
    if not primary_ok:
        return "FAIL"

    # Suspicion guard: Sharpe > 8 is a look-ahead smell — never auto-PASS.
    if sharpe_oos > SUSPICIOUS_SHARPE:
        return "MARGINAL"

    # Extended criterion: trades/day vs baseline (Smart Filter frequency).
    if baseline_trades_per_day <= 0:
        return "MARGINAL"
    threshold = 0.7 * baseline_trades_per_day
    extended_ok = variant.get("trades_per_day", 0.0) >= threshold
    return "PASS" if extended_ok else "MARGINAL"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_num(x) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        if abs(x) >= 1000:
            return f"{x:.1f}"
        return f"{x:+.2f}"
    return str(x)


def format_variant_block(
    name: str,
    variant: dict,
    wf: dict,
    verdict: str,
    baseline: dict,
    hypothesis: str,
    z_netpos: float,
    interval: str,
) -> str:
    lines: list[str] = []
    lines.append("─" * 64)
    desc = (
        "Contrarian (net_short)"
        if hypothesis == "H1"
        else "Confirmation (net_long)"
    )
    lines.append(
        f"VARIANT: {name}    ({desc}, z_netpos>{z_netpos}, {interval.upper()})"
    )
    lines.append("─" * 64)

    b_pooled = baseline.get("pooled") or {}
    v_pooled = variant.get("pooled") or {}
    b_n = b_pooled.get("n", 0)
    v_n = v_pooled.get("n", 0)
    delta_pct = ((v_n - b_n) / b_n * 100) if b_n > 0 else 0.0
    lines.append(
        f"Baseline (no filter):  N={b_n}  "
        f"Win%={_fmt_num(b_pooled.get('win_pct'))}  "
        f"Sharpe={_fmt_num(b_pooled.get('sharpe'))}"
    )
    lines.append(
        f"With filter:           N={v_n}  "
        f"Win%={_fmt_num(v_pooled.get('win_pct'))}  "
        f"Sharpe={_fmt_num(v_pooled.get('sharpe'))}   "
        f"Δtrades={delta_pct:+.0f}%"
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
    checks = [
        (
            "Pooled OOS Sharpe > 2.0",
            sharpe_oos is not None and sharpe_oos > 2.0,
            _fmt_num(sharpe_oos),
        ),
        (
            "Win% > 55%",
            v_pooled.get("win_pct") is not None and v_pooled["win_pct"] > 55.0,
            _fmt_num(v_pooled.get("win_pct")),
        ),
        ("N trades >= 100", v_n >= 100, str(v_n)),
        (
            ">=2/3 OOS folds positive",
            wf.get("oos_positive", 0) >= 2,
            f"{wf.get('oos_positive', 0)}/{wf.get('oos_total', 0)}",
        ),
        (
            "Pooled OOS Sharpe > 1.0",
            sharpe_oos is not None and sharpe_oos > 1.0,
            _fmt_num(sharpe_oos),
        ),
    ]
    lines.append("PASS criteria (all must hold):")
    for label, ok, val in checks:
        icon = "[OK]" if ok else "[--]"
        lines.append(f"  {icon} {label:<38} ({val})")

    baseline_tpd = baseline.get("trades_per_day", 0.0)
    v_tpd = variant.get("trades_per_day", 0.0)
    ext_ok = baseline_tpd > 0 and v_tpd >= 0.7 * baseline_tpd
    icon = "[OK]" if ext_ok else "[!!]"
    ratio = (v_tpd / baseline_tpd * 100) if baseline_tpd > 0 else 0.0
    lines.append("")
    lines.append("Extended criteria (Smart Filter awareness):")
    lines.append(
        f"  {icon} trades/day median >= 70% baseline "
        f"({v_tpd:.2f} vs {baseline_tpd:.2f} = {ratio:.0f}%) "
        f"{'PASS' if ext_ok else 'FAIL'}"
    )

    if sharpe_oos is not None and sharpe_oos > SUSPICIOUS_SHARPE:
        lines.append(
            f"  [!!] suspicious Sharpe {sharpe_oos:.2f} > {SUSPICIOUS_SHARPE} "
            f"— manual review required"
        )

    lines.append("")
    lines.append(f"VERDICT: {verdict}")
    lines.append("─" * 64)
    return "\n".join(lines)


def format_final_ranking(rows: list[dict]) -> str:
    """Sort by pooled_oos Sharpe descending; missing values pushed to bottom."""
    def _sort_key(r: dict) -> float:
        s = r.get("sharpe_oos")
        return -s if s is not None else float("inf")

    sorted_rows = sorted(rows, key=_sort_key)

    lines: list[str] = []
    lines.append("=" * 80)
    lines.append("FINAL RANKING")
    lines.append("=" * 80)
    lines.append(
        f"{'Rank':<5} {'Variant':<20} {'N':>5} {'Win%':>6} {'Sharpe':>8} "
        f"{'OOS':>5} {'Trades/d':>9} {'Verdict':<10}"
    )
    lines.append("-" * 80)
    for i, r in enumerate(sorted_rows, 1):
        lines.append(
            f"{i:<5} {r['name']:<20} {r['n']:>5} "
            f"{_fmt_num(r.get('win_pct')):>6} "
            f"{_fmt_num(r.get('sharpe_oos')):>8} "
            f"{r.get('oos', ''):>5} "
            f"{r.get('trades_per_day', 0.0):>9.2f} "
            f"{r.get('verdict', ''):<10}"
        )
    lines.append("=" * 80)
    return "\n".join(lines)


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
        _, netpos_df = _try_load_with_pepe_fallback(
            coin, lambda s: load_netposition_tf(s, interval),
        )

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
        attach_netposition(feat, netpos_df, bar_hours)
        per_coin[coin] = feat
        coverage = (
            int(feat["net_long_change_zscore"].notna().mean() * 100)
            if "net_long_change_zscore" in feat.columns
            else 0
        )
        print(f"  {coin:<5}: rows={len(feat)}  netpos_coverage={coverage}%")

    return compute_cross_coin_features(per_coin, flush_z=CROSS_COIN_FLUSH_Z)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="L10 Phase 2: Net Position research (offline backtest)."
    )
    parser.add_argument(
        "--intervals", type=str, default=",".join(DEFAULT_INTERVALS),
        help="Comma-separated intervals (default: h1,h2,h4).",
    )
    parser.add_argument(
        "--hypotheses", type=str, default=",".join(HYPOTHESES),
        help="Comma-separated hypotheses (default: H1,H2).",
    )
    parser.add_argument(
        "--thresholds", type=str,
        default=",".join(str(t) for t in DEFAULT_THRESHOLDS),
        help="Comma-separated z_netpos thresholds (default: 0.5,1.0,1.5).",
    )
    args = parser.parse_args()

    intervals = [x.strip() for x in args.intervals.split(",") if x.strip()]
    hypotheses = [x.strip() for x in args.hypotheses.split(",") if x.strip()]
    thresholds = [float(x.strip()) for x in args.thresholds.split(",") if x.strip()]

    for iv in intervals:
        if iv not in DEFAULT_INTERVALS:
            raise ValueError(f"Unknown interval {iv!r} (valid: {DEFAULT_INTERVALS})")
    for h in hypotheses:
        if h not in HYPOTHESES:
            raise ValueError(f"Unknown hypothesis {h!r} (valid: {HYPOTHESES})")

    init_pool(get_config())

    print("=" * 72)
    print("L10 PHASE 2 — NET POSITION RESEARCH")
    print("=" * 72)
    print(f"  intervals={intervals}  hypotheses={hypotheses}  thresholds={thresholds}")
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
        b_name = f"BASELINE_{interval}"

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
            "name": b_name,
            "n": b_pooled.get("n", 0),
            "win_pct": b_pooled.get("win_pct"),
            "sharpe_oos": (baseline_wf.get("pooled_oos") or {}).get("sharpe"),
            "oos": f"{baseline_wf.get('oos_positive', 0)}/{baseline_wf.get('oos_total', 0)}",
            "trades_per_day": baseline.get("trades_per_day", 0.0),
            "verdict": "REF",
        })

        for hypothesis in hypotheses:
            for z_netpos in thresholds:
                name = f"{hypothesis}_z{z_netpos}_{interval}"
                filters = build_hypothesis_filters(hypothesis, z_netpos)
                variant = run_variant(all_dfs, filters, RANK_HOLDING_HOURS)
                wf = run_walkforward(all_dfs, filters, RANK_HOLDING_HOURS)
                verdict = evaluate_verdict(
                    variant, wf, baseline.get("trades_per_day", 0.0),
                )
                print()
                print(format_variant_block(
                    name, variant, wf, verdict, baseline,
                    hypothesis, z_netpos, interval,
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
        print("  → Phase 3 integration candidates:")
        for r in pass_rows:
            print(
                f"     • {r['name']}  Sharpe={_fmt_num(r['sharpe_oos'])}  "
                f"trades/d={r['trades_per_day']:.2f}"
            )
    elif marginal_rows:
        print("  → No PASS variants. MARGINAL list (review before L11 / Phase 3):")
        for r in marginal_rows:
            print(
                f"     • {r['name']}  Sharpe={_fmt_num(r['sharpe_oos'])}  "
                f"trades/d={r['trades_per_day']:.2f}"
            )
    else:
        print("  → No variant PASS or MARGINAL. Stick with h4 baseline only.")


if __name__ == "__main__":
    main()
