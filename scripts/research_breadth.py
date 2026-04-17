#!/usr/bin/env python3
"""
L14 Phase 1: Per-Coin Independent Flush Research.

Tests whether relaxing the breadth threshold K in `n_coins_flushing >= K`
gives temporal dispersion (more unique trading days per month) without
crushing Sharpe. L13 Phase 3c revealed the baseline (K=4) clusters trades
into ~13 unique trading days per 30-day window, structurally short of
Binance Copy Trading Smart Filter's 14-day minimum.

Matrix: K ∈ {0,1,2,3,4} × interval ∈ {h1,h2,h4} = 15 variants.
K=0 drops the breadth filter entirely (truly per-coin independent).
K=4 is the locked L3b-2 baseline.

Research-only — no changes to bot/, exchange/, or locked scripts.

Usage:
    .venv/bin/python scripts/research_breadth.py
    .venv/bin/python scripts/research_breadth.py --intervals h4 --breadth 2
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from typing import Optional

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
    run_variant,
    run_walkforward,
)
from validate_h1_z15_h2 import extract_trade_records  # noqa: E402


# L8 reference for h4 baseline parity check.
L8_H4_REFERENCE = {"n": 428, "win_pct": 61.0, "sharpe": 5.87}

DEFAULT_BREADTH: tuple[int, ...] = (0, 1, 2, 3, 4)
DEFAULT_INTERVALS: tuple[str, ...] = ("h1", "h2", "h4")

# Smart Filter adequacy floor (Binance Copy Trading).
MIN_TRADING_DAYS_30D = 14
ROLLING_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# Filter construction
# ---------------------------------------------------------------------------

def build_breadth_filters(K: int) -> list[tuple]:
    """
    Build market_flush filters with breadth threshold `K`.

    - K == 0 → returns only the locked per-coin z_self filter (breadth
      requirement dropped entirely, truly per-coin independent).
    - K >= 1 → locked z_self + ("n_coins_flushing", ">=", K).
    - K == 4 reproduces the locked L3b-2 MARKET_FLUSH_FILTERS.

    Raises ValueError on K outside [0, 10].
    """
    if not isinstance(K, int) or K < 0 or K > 10:
        raise ValueError(f"K must be an int in [0, 10], got {K!r}")
    base: list[tuple] = [("long_vol_zscore", ">", 1.0)]
    if K == 0:
        return base
    return base + [("n_coins_flushing", ">=", K)]


# ---------------------------------------------------------------------------
# Trading-days distribution
# ---------------------------------------------------------------------------

def compute_trading_days_distribution(
    trades: list[dict],
    window_days: int = ROLLING_WINDOW_DAYS,
) -> dict:
    """
    Compute unique-trading-day metrics over entry dates.

    Input: list of {coin, entry_ts, exit_ts, pnl_pct} (from
    `extract_trade_records`). Uses `entry_ts.date()` as the trading day.

    Returns:
        total_trading_days: unique entry dates
        calendar_span_days: (max-min).days + 1 over entry dates
        pct_active_days: total / span * 100
        n_rolling_windows: span - window_days + 1, min 0
        min_30d_td / median_30d_td / max_30d_td: per-window unique TD stats
        pct_windows_ge14: % of windows where unique TD >= MIN_TRADING_DAYS_30D

    Empty trade list → all zeros (no crash).
    Span < window_days → no windows, rolling metrics zero.
    """
    empty_out = {
        "total_trading_days": 0,
        "calendar_span_days": 0,
        "pct_active_days": 0.0,
        "n_rolling_windows": 0,
        "min_30d_td": 0,
        "median_30d_td": 0.0,
        "max_30d_td": 0,
        "pct_windows_ge14": 0.0,
    }
    if not trades:
        return empty_out

    entry_dates = sorted({t["entry_ts"].date() for t in trades})
    total_td = len(entry_dates)
    min_d = entry_dates[0]
    max_d = entry_dates[-1]
    span = (max_d - min_d).days + 1
    pct_active = (total_td / span * 100) if span > 0 else 0.0

    n_windows = max(0, span - window_days + 1)
    if n_windows == 0:
        return {
            **empty_out,
            "total_trading_days": total_td,
            "calendar_span_days": span,
            "pct_active_days": round(pct_active, 2),
        }

    entry_date_set = set(entry_dates)
    per_window_td: list[int] = []
    for offset in range(n_windows):
        w_start = min_d + pd.Timedelta(days=offset).to_pytimedelta()
        w_end = w_start + pd.Timedelta(days=window_days - 1).to_pytimedelta()
        count = sum(1 for d in entry_date_set if w_start <= d <= w_end)
        per_window_td.append(count)

    ge14 = sum(1 for c in per_window_td if c >= MIN_TRADING_DAYS_30D)
    return {
        "total_trading_days": total_td,
        "calendar_span_days": span,
        "pct_active_days": round(pct_active, 2),
        "n_rolling_windows": n_windows,
        "min_30d_td": min(per_window_td),
        "median_30d_td": round(statistics.median(per_window_td), 2),
        "max_30d_td": max(per_window_td),
        "pct_windows_ge14": round(ge14 / n_windows * 100, 2),
    }


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def evaluate_breadth_verdict(variant: dict, wf: dict, td_stats: dict) -> str:
    """
    Combined verdict across the Sharpe track (L8-inherited) and the Smart
    Filter adequacy track (trading-day distribution).

    STRONG_PASS → all primary (1-4) + both strict (5 min>=14, 6 median>=14)
    PASS        → all primary + strict-5 (at least one qualifying window)
    MARGINAL    → primary met, strict-5 fails OR pooled OOS Sharpe > 8.0
    FAIL        → any primary missed, or walk-forward skipped
    """
    pooled = variant.get("pooled")
    pooled_oos = wf.get("pooled_oos")
    if wf.get("skipped") or pooled is None or pooled_oos is None:
        return "FAIL"

    sharpe_oos = pooled_oos.get("sharpe")
    win_pct = pooled.get("win_pct")
    n_trades = pooled.get("n")
    oos_positive = wf.get("oos_positive", 0)

    primary_ok = (
        sharpe_oos is not None
        and sharpe_oos > 2.0
        and win_pct is not None
        and win_pct > 55.0
        and n_trades is not None
        and n_trades >= 100
        and oos_positive >= 2
    )
    if not primary_ok:
        return "FAIL"

    # Look-ahead guardrail: Sharpe > 8.0 never auto-PASSes.
    if sharpe_oos > SUSPICIOUS_SHARPE:
        return "MARGINAL"

    min_td = td_stats.get("min_30d_td", 0)
    median_td = td_stats.get("median_30d_td", 0.0)

    strict_5 = min_td >= MIN_TRADING_DAYS_30D
    strict_6 = median_td >= MIN_TRADING_DAYS_30D

    if not strict_5:
        return "MARGINAL"
    if strict_6:
        return "STRONG_PASS"
    return "PASS"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_coins_for_interval(interval: str) -> dict[str, pd.DataFrame]:
    """
    Load all 10 coins for a given interval and return per-coin feature
    frames with cross-coin `n_coins_flushing` injected.

    Mirrors the loader in research_netposition.py but does not need net
    position data — breadth research only touches liquidations/OI/funding/
    klines, which are already enough to compute `long_vol_zscore` and the
    cross-coin flush count.
    """
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
        per_coin[coin] = feat
        if not feat.empty:
            print(f"  {coin:<5}: rows={len(feat)}")

    return compute_cross_coin_features(per_coin, flush_z=CROSS_COIN_FLUSH_Z)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_variant_block(
    name: str,
    interval: str,
    K: int,
    variant: dict,
    wf: dict,
    td_stats: dict,
    verdict: str,
) -> str:
    lines: list[str] = []
    lines.append("─" * 72)
    breadth_desc = (
        "no breadth filter (truly per-coin independent)"
        if K == 0
        else f"n_coins_flushing >= {K}"
    )
    lines.append(f"VARIANT: {name}    ({breadth_desc}, {interval.upper()})")
    lines.append("─" * 72)

    v_pooled = variant.get("pooled") or {}
    lines.append(
        f"Signal metrics:  N={v_pooled.get('n', 0)}  "
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

    lines.append("Trading Days Distribution:")
    lines.append(
        f"  Unique trading days total:    {td_stats['total_trading_days']} "
        f"/ {td_stats['calendar_span_days']} calendar days "
        f"({td_stats['pct_active_days']:.1f}%)"
    )
    if td_stats["n_rolling_windows"] > 0:
        lines.append(f"  Rolling {ROLLING_WINDOW_DAYS}d windows ({td_stats['n_rolling_windows']}):")
        lines.append(f"    Min trading days:    {td_stats['min_30d_td']}")
        lines.append(f"    Median trading days: {td_stats['median_30d_td']}")
        lines.append(f"    Max trading days:    {td_stats['max_30d_td']}")
        lines.append(
            f"  % windows with >={MIN_TRADING_DAYS_30D} TD: "
            f"{td_stats['pct_windows_ge14']:.1f}%"
        )
    else:
        lines.append(
            f"  (span < {ROLLING_WINDOW_DAYS}d — no rolling windows to evaluate)"
        )
    lines.append("")

    pooled_oos = wf.get("pooled_oos") or {}
    sharpe_oos = pooled_oos.get("sharpe")
    primary_checks = [
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
        (
            "N trades >= 100",
            v_pooled.get("n", 0) >= 100,
            str(v_pooled.get("n", 0)),
        ),
        (
            ">=2/3 OOS folds positive",
            wf.get("oos_positive", 0) >= 2,
            f"{wf.get('oos_positive', 0)}/{wf.get('oos_total', 0)}",
        ),
    ]
    lines.append("PASS criteria (primary):")
    for label, ok, val in primary_checks:
        icon = "[OK]" if ok else "[--]"
        lines.append(f"  {icon} {label:<38} ({val})")

    lines.append("")
    lines.append("Strict criteria (Smart Filter adequacy):")
    strict_5_ok = td_stats.get("min_30d_td", 0) >= MIN_TRADING_DAYS_30D
    strict_6_ok = td_stats.get("median_30d_td", 0.0) >= MIN_TRADING_DAYS_30D
    icon_5 = "[OK]" if strict_5_ok else "[--]"
    icon_6 = "[OK]" if strict_6_ok else "[--]"
    lines.append(
        f"  {icon_5} Min 30d TD >= {MIN_TRADING_DAYS_30D:<23} "
        f"({td_stats.get('min_30d_td', 0)})"
    )
    lines.append(
        f"  {icon_6} Median 30d TD >= {MIN_TRADING_DAYS_30D:<20} "
        f"({td_stats.get('median_30d_td', 0.0)})"
    )

    if sharpe_oos is not None and sharpe_oos > SUSPICIOUS_SHARPE:
        lines.append("")
        lines.append(
            f"  [!!] suspicious Sharpe {sharpe_oos:.2f} > {SUSPICIOUS_SHARPE} "
            f"— manual review required"
        )

    lines.append("")
    lines.append(f"VERDICT: {verdict}")
    lines.append("─" * 72)
    return "\n".join(lines)


def format_final_ranking_with_days(rows: list[dict]) -> str:
    def _sort_key(r: dict) -> float:
        s = r.get("sharpe_oos")
        return -s if s is not None else float("inf")

    sorted_rows = sorted(rows, key=_sort_key)

    lines: list[str] = []
    lines.append("=" * 96)
    lines.append("FINAL RANKING")
    lines.append("=" * 96)
    lines.append(
        f"{'Rank':<5}{'Variant':<22}{'N':>5} {'Win%':>6} {'Sharpe':>8} "
        f"{'OOS':>5} {'Tr/d':>6} {'Min30dTD':>9} {'Med30dTD':>9} {'Verdict':<12}"
    )
    lines.append("-" * 96)
    for i, r in enumerate(sorted_rows, 1):
        lines.append(
            f"{i:<5}{r['name']:<22}"
            f"{r['n']:>5} "
            f"{_fmt_num(r.get('win_pct')):>6} "
            f"{_fmt_num(r.get('sharpe_oos')):>8} "
            f"{r.get('oos', ''):>5} "
            f"{r.get('trades_per_day', 0.0):>6.2f} "
            f"{r.get('min_30d_td', 0):>9} "
            f"{r.get('median_30d_td', 0.0):>9} "
            f"{r.get('verdict', ''):<12}"
        )
    lines.append("=" * 96)
    return "\n".join(lines)


def recommend(rows: list[dict]) -> str:
    strong = [r for r in rows if r["verdict"] == "STRONG_PASS"]
    passes = [r for r in rows if r["verdict"] == "PASS"]
    marginals = [r for r in rows if r["verdict"] == "MARGINAL"]

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("RECOMMENDED ACTION")
    lines.append("=" * 72)

    if strong:
        best = sorted(strong, key=lambda r: -(r.get("sharpe_oos") or 0.0))[0]
        lines.append(
            "  → STRONG_PASS found. Phase 2 integration candidates (best first):"
        )
        for r in strong:
            lines.append(
                f"     • {r['name']}  Sharpe={_fmt_num(r['sharpe_oos'])}  "
                f"Min30dTD={r.get('min_30d_td', 0)}  "
                f"Med30dTD={r.get('median_30d_td', 0.0)}  "
                f"tr/d={r.get('trades_per_day', 0.0):.2f}"
            )
        lines.append("")
        lines.append(f"  → best candidate: {best['name']} — deploy to paper parallel to h4")
    elif passes:
        lines.append(
            "  → PASS variants found (strict-5 only, median below 14 — "
            "not every month will clear Smart Filter):"
        )
        for r in passes:
            lines.append(
                f"     • {r['name']}  Sharpe={_fmt_num(r['sharpe_oos'])}  "
                f"Min30dTD={r.get('min_30d_td', 0)}  "
                f"Med30dTD={r.get('median_30d_td', 0.0)}  "
                f"tr/d={r.get('trades_per_day', 0.0):.2f}"
            )
        lines.append("")
        lines.append(
            "  → paper deploy with caveat; monitor month-to-month adequacy rate"
        )
    elif marginals:
        k0_marginals = [r for r in marginals if r["name"].startswith("BREADTH_K=0_")]
        lines.append("  → No PASS variants. MARGINAL variants (review case-by-case):")
        for r in marginals:
            lines.append(
                f"     • {r['name']}  Sharpe={_fmt_num(r['sharpe_oos'])}  "
                f"Min30dTD={r.get('min_30d_td', 0)}  "
                f"tr/d={r.get('trades_per_day', 0.0):.2f}"
            )
        if k0_marginals:
            lines.append("")
            lines.append(
                "  → K=0 appears only as MARGINAL: breadth relaxation alone "
                "does NOT resolve temporal clustering (Min30dTD still < 14). "
                "Next: L15 new signal class (orthogonal substrate)."
            )
        else:
            lines.append("")
            lines.append(
                "  → No K=0 MARGINAL either. Consider L15 new signal class."
            )
    else:
        lines.append("  → All variants FAIL. Structural issue confirmed.")
        lines.append("  → Next: L15 new signal class or L16 pivot.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_int_list(s: str) -> list[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def _parse_str_list(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="L14 Phase 1: Per-coin independent flush research."
    )
    parser.add_argument(
        "--intervals", type=str, default=",".join(DEFAULT_INTERVALS),
        help="Comma-separated intervals (default: h1,h2,h4).",
    )
    parser.add_argument(
        "--breadth", type=str,
        default=",".join(str(k) for k in DEFAULT_BREADTH),
        help="Comma-separated breadth thresholds K (default: 0,1,2,3,4).",
    )
    args = parser.parse_args()

    intervals = _parse_str_list(args.intervals)
    breadth_values = _parse_int_list(args.breadth)

    for iv in intervals:
        if iv not in DEFAULT_INTERVALS:
            raise ValueError(f"Unknown interval {iv!r} (valid: {DEFAULT_INTERVALS})")
    for K in breadth_values:
        build_breadth_filters(K)  # validates K range

    init_pool(get_config())

    print("=" * 72)
    print("L14 PHASE 1 — PER-COIN INDEPENDENT FLUSH RESEARCH")
    print("=" * 72)
    print(f"  intervals={intervals}  breadth K={breadth_values}")
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

        if interval == "h4" and 4 in breadth_values:
            baseline = run_variant(
                all_dfs, list(MARKET_FLUSH_FILTERS), RANK_HOLDING_HOURS,
            )
            b_pooled = baseline.get("pooled") or {}
            obs_sharpe = b_pooled.get("sharpe")
            obs_win = b_pooled.get("win_pct")
            obs_n = b_pooled.get("n")
            print()
            print("  H4 BASELINE PARITY (vs L8 reference):")
            print(f"    N:      observed={obs_n}      L8 ref={L8_H4_REFERENCE['n']}")
            print(f"    Win%:   observed={obs_win}   L8 ref={L8_H4_REFERENCE['win_pct']}")
            print(f"    Sharpe: observed={obs_sharpe} L8 ref={L8_H4_REFERENCE['sharpe']}")
            if obs_sharpe is not None:
                drift = abs(obs_sharpe - L8_H4_REFERENCE["sharpe"]) / L8_H4_REFERENCE["sharpe"]
                if drift > 0.05:
                    print(
                        f"  [!!] Sharpe drift {drift * 100:.1f}% > 5% — "
                        f"verify data hasn't regressed"
                    )
            print()

        for K in breadth_values:
            name = f"BREADTH_K={K}_{interval}"
            filters = build_breadth_filters(K)
            variant = run_variant(all_dfs, filters, RANK_HOLDING_HOURS)
            wf = run_walkforward(all_dfs, filters, RANK_HOLDING_HOURS)
            trades = extract_trade_records(all_dfs, filters, RANK_HOLDING_HOURS)
            td_stats = compute_trading_days_distribution(trades)
            verdict = evaluate_breadth_verdict(variant, wf, td_stats)

            print()
            print(format_variant_block(name, interval, K, variant, wf, td_stats, verdict))

            v_pooled = variant.get("pooled") or {}
            ranking_rows.append({
                "name": name,
                "n": v_pooled.get("n", 0),
                "win_pct": v_pooled.get("win_pct"),
                "sharpe_oos": (wf.get("pooled_oos") or {}).get("sharpe"),
                "oos": f"{wf.get('oos_positive', 0)}/{wf.get('oos_total', 0)}",
                "trades_per_day": variant.get("trades_per_day", 0.0),
                "min_30d_td": td_stats.get("min_30d_td", 0),
                "median_30d_td": td_stats.get("median_30d_td", 0.0),
                "verdict": verdict,
            })

    print()
    print(format_final_ranking_with_days(ranking_rows))
    print()
    print(recommend(ranking_rows))


if __name__ == "__main__":
    main()
