#!/usr/bin/env python3
"""
L10 Phase 2b: H1_z1.5_h2 validation script.

Three additional tests on top of Phase 2 results to resolve the "deploy vs
reject" decision for the `H1_z1.5_h2` Net Position filter candidate:

  Test 1: Daily-return Pearson correlation with h4 baseline (diversification).
  Test 2: Rolling 30-day Sharpe stability (Smart Filter simulation) — for
          both the h4 baseline and H1_z1.5_h2 separately.
  Test 3: Combined 50/50 portfolio simulation (synergy).

Read-only: reuses Phase 2 loaders + L8 constants; does NOT modify any locked
script.

Usage:
    .venv/bin/python scripts/validate_h1_z15_h2.py
"""
from __future__ import annotations

import math
import os
import sys
from typing import Optional

import numpy as np
import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_ROLLING_WINDOW_ACTIVE_DAYS = 5
STD_EPS = 1e-12  # Treat std <= this as "zero" and skip the window (floating-point noise).
DEFAULT_CAPITAL_USD = 1000.0
H1_Z_NETPOS = 1.5
TRADING_DAYS_PER_YEAR = 365


# ---------------------------------------------------------------------------
# Pure functions (imported by tests)
# ---------------------------------------------------------------------------


def extract_trade_records(
    all_coin_dfs: "dict[str, pd.DataFrame]",
    filters: "list[tuple]",
    holding_hours: int,
) -> "list[dict]":
    """
    Replicate `run_variant`'s internal mask logic while preserving per-trade
    metadata. Returns a flat list of dicts:

        {"coin": str, "entry_ts": Timestamp, "exit_ts": Timestamp,
         "pnl_pct": float}

    `exit_ts = entry_ts + holding_hours` — matches the forward-return
    convention that L8 uses (return_{h}h is the trade's realized pnl_pct).
    """
    # Imports are deferred so that tests of the pure helpers below don't need
    # to touch the DB-aware research script.
    from backtest_combo import apply_combo  # noqa: E402

    ret_col = f"return_{holding_hours}h"
    out: "list[dict]" = []
    for coin, df in all_coin_dfs.items():
        if df is None or df.empty or ret_col not in df.columns:
            continue
        mask = apply_combo(df, filters)
        trades = df.loc[mask, ret_col].dropna()
        if len(trades) == 0:
            continue
        td = pd.Timedelta(hours=holding_hours)
        for entry_ts, pnl in trades.items():
            out.append({
                "coin": coin,
                "entry_ts": entry_ts,
                "exit_ts": entry_ts + td,
                "pnl_pct": float(pnl),
            })
    return out


def aggregate_daily_pnl(
    trades: "list[dict]",
    date_range: pd.DatetimeIndex,
) -> pd.Series:
    """
    Sum `pnl_pct` per UTC day indexed by `exit_ts.normalize()`. Days without
    trades are 0.0 (never NaN). Index is the supplied `date_range`.
    """
    out = pd.Series(0.0, index=date_range)
    if not trades:
        return out
    for t in trades:
        day = t["exit_ts"].normalize()
        if day in out.index:
            out.loc[day] += float(t["pnl_pct"])
    return out


def compute_correlation_test(
    daily_h4: pd.Series,
    daily_h2: pd.Series,
) -> dict:
    """
    Pearson correlation + active-day overlap + 4-way win/lose breakdown on
    common active days.

    PASS criteria:
      corr < 0.5  AND  (h4_win_h2_lose_pct + h4_lose_h2_win_pct) >= 15
    """
    aligned = pd.DataFrame({"h4": daily_h4, "h2": daily_h2}).fillna(0.0)
    n = len(aligned)

    active_h4 = aligned["h4"] != 0
    active_h2 = aligned["h2"] != 0
    both_active = active_h4 & active_h2

    if aligned["h4"].std() == 0 or aligned["h2"].std() == 0:
        correlation = 1.0 if aligned["h4"].equals(aligned["h2"]) else 0.0
    else:
        correlation = float(aligned["h4"].corr(aligned["h2"]))

    n_both = int(both_active.sum())
    n_only_h4 = int((active_h4 & ~active_h2).sum())
    n_only_h2 = int((~active_h4 & active_h2).sum())
    n_none = int((~active_h4 & ~active_h2).sum())

    if n_both > 0:
        h4_win = aligned.loc[both_active, "h4"] > 0
        h2_win = aligned.loc[both_active, "h2"] > 0
        both_win_pct = float(((h4_win) & (h2_win)).mean() * 100)
        both_lose_pct = float(((~h4_win) & (~h2_win)).mean() * 100)
        h4w_h2l_pct = float(((h4_win) & (~h2_win)).mean() * 100)
        h4l_h2w_pct = float(((~h4_win) & (h2_win)).mean() * 100)
    else:
        both_win_pct = both_lose_pct = h4w_h2l_pct = h4l_h2w_pct = 0.0

    mixed_pct = h4w_h2l_pct + h4l_h2w_pct

    passed = correlation < 0.5 and mixed_pct >= 15.0 and n_both > 0

    return {
        "correlation": correlation,
        "n_days": n,
        "active_h4_pct": float(active_h4.mean() * 100),
        "active_h2_pct": float(active_h2.mean() * 100),
        "both_active_pct": float(both_active.mean() * 100),
        "n_both": n_both,
        "n_only_h4": n_only_h4,
        "n_only_h2": n_only_h2,
        "n_none": n_none,
        "both_win_pct": both_win_pct,
        "both_lose_pct": both_lose_pct,
        "h4_win_h2_lose_pct": h4w_h2l_pct,
        "h4_lose_h2_win_pct": h4l_h2w_pct,
        "mixed_pct": mixed_pct,
        "passed": passed,
    }


def _rolling_window_sharpe(window: pd.Series) -> Optional[float]:
    """
    Annualized Sharpe on a window, or None if not computable.

    Zero-std windows are skipped (return None). On real trade data this case
    cannot arise because `pnl_pct` varies continuously across trades and
    active/inactive days always coexist — `std == 0` would require 30
    byte-identical values. The guard exists purely to absorb floating-point
    degeneracy (e.g. synthetic test inputs of exact constants).
    """
    if len(window) == 0:
        return None
    mean = window.mean()
    std = window.std(ddof=1)
    if pd.isna(std) or std <= STD_EPS:
        return None
    return float(mean / std * math.sqrt(TRADING_DAYS_PER_YEAR))


def compute_rolling_sharpe_test(
    daily_pnl: pd.Series,
    window_days: int = 30,
    min_trading_days: int = MIN_ROLLING_WINDOW_ACTIVE_DAYS,
) -> dict:
    """
    Slide `window_days` across the daily pnl series. Per window, skip when
    fewer than `min_trading_days` days are active (non-zero pnl). Return
    distribution stats + PASS flag.

    PASS criteria (all):
      min > 0
      median > 2.0
      pct_above_2 >= 60
      pct_win_days_above_65 >= 50
    """
    sharpes: "list[float]" = []
    win_day_pcts: "list[float]" = []
    n_total = 0
    n_dropped_low_activity = 0
    n_dropped_zero_std = 0

    if len(daily_pnl) >= window_days:
        for start in range(0, len(daily_pnl) - window_days + 1):
            n_total += 1
            window = daily_pnl.iloc[start : start + window_days]
            active = window[window != 0]
            if len(active) < min_trading_days:
                n_dropped_low_activity += 1
                continue
            sh = _rolling_window_sharpe(window)
            if sh is None:
                n_dropped_zero_std += 1
                continue
            sharpes.append(sh)
            win_days = int((active > 0).sum())
            win_day_pcts.append(win_days / len(active) * 100)

    n_windows = len(sharpes)
    dropped_low_activity_pct = (
        100.0 * n_dropped_low_activity / n_total if n_total else 0.0
    )
    dropped_zero_std_pct = 100.0 * n_dropped_zero_std / n_total if n_total else 0.0

    if n_windows == 0:
        return {
            "n_total_windows": n_total,
            "n_windows": 0,
            "n_dropped_low_activity": n_dropped_low_activity,
            "n_dropped_zero_std": n_dropped_zero_std,
            "dropped_low_activity_pct": dropped_low_activity_pct,
            "dropped_zero_std_pct": dropped_zero_std_pct,
            "min": None,
            "p05": None,
            "median": None,
            "p95": None,
            "max": None,
            "pct_above_2": 0.0,
            "pct_win_days_above_65": 0.0,
            "passed": False,
        }

    arr = np.asarray(sharpes)
    wda = np.asarray(win_day_pcts)

    min_v = float(arr.min())
    p05 = float(np.percentile(arr, 5))
    median = float(np.median(arr))
    p95 = float(np.percentile(arr, 95))
    max_v = float(arr.max())
    pct_above_2 = float((arr > 2.0).mean() * 100)
    pct_win_days_65 = float((wda >= 65.0).mean() * 100)

    passed = (
        min_v > 0
        and median > 2.0
        and pct_above_2 >= 60.0
        and pct_win_days_65 >= 50.0
    )

    return {
        "n_total_windows": n_total,
        "n_windows": n_windows,
        "n_dropped_low_activity": n_dropped_low_activity,
        "n_dropped_zero_std": n_dropped_zero_std,
        "dropped_low_activity_pct": dropped_low_activity_pct,
        "dropped_zero_std_pct": dropped_zero_std_pct,
        "min": min_v,
        "p05": p05,
        "median": median,
        "p95": p95,
        "max": max_v,
        "pct_above_2": pct_above_2,
        "pct_win_days_above_65": pct_win_days_65,
        "passed": passed,
    }


def _equity_curve_stats(daily_pnl_usd: pd.Series) -> dict:
    """Cumulative equity, annualized Sharpe, MDD (negative, more negative = worse)."""
    if len(daily_pnl_usd) == 0:
        return {"sharpe": 0.0, "mdd": 0.0, "win_days_pct": 0.0, "trades_per_day": 0.0}
    mean = daily_pnl_usd.mean()
    std = daily_pnl_usd.std(ddof=1)
    if std == 0 or pd.isna(std):
        sharpe = 0.0
    else:
        sharpe = float(mean / std * math.sqrt(TRADING_DAYS_PER_YEAR))
    equity = daily_pnl_usd.cumsum()
    running_max = equity.cummax()
    drawdown = equity - running_max
    mdd = float(drawdown.min()) if len(drawdown) else 0.0
    active = daily_pnl_usd[daily_pnl_usd != 0]
    win_days_pct = float((active > 0).mean() * 100) if len(active) else 0.0
    trades_per_day = float((daily_pnl_usd != 0).mean())
    return {
        "sharpe": sharpe,
        "mdd": mdd,
        "win_days_pct": win_days_pct,
        "trades_per_day": trades_per_day,
    }


def compute_combined_portfolio_test(
    daily_h4_pct: pd.Series,
    daily_h2_pct: pd.Series,
    capital: float = DEFAULT_CAPITAL_USD,
) -> dict:
    """
    50/50 portfolio simulation. Inputs are daily pnl in PERCENT units. Each
    strategy receives half the capital. Combined daily USD pnl is additive.

    PASS criteria (all):
      combined_sharpe > max(h4_solo_sharpe, h2_solo_sharpe)
      combined_mdd > h4_solo_mdd  (MDD < 0, "greater" = "less severe")
      combined_win_days >= h4_solo_win_days
    """
    aligned = pd.DataFrame({"h4": daily_h4_pct, "h2": daily_h2_pct}).fillna(0.0)

    h4_usd = 0.5 * capital * aligned["h4"] / 100.0
    h2_usd = 0.5 * capital * aligned["h2"] / 100.0
    combined_usd = h4_usd + h2_usd

    h4_solo_usd = capital * aligned["h4"] / 100.0
    h2_solo_usd = capital * aligned["h2"] / 100.0

    h4_stats = _equity_curve_stats(h4_solo_usd)
    h2_stats = _equity_curve_stats(h2_solo_usd)
    combined_stats = _equity_curve_stats(combined_usd)

    passed = (
        combined_stats["sharpe"] > max(h4_stats["sharpe"], h2_stats["sharpe"])
        and combined_stats["mdd"] > h4_stats["mdd"]
        and combined_stats["win_days_pct"] >= h4_stats["win_days_pct"]
    )

    return {
        "combined_sharpe": combined_stats["sharpe"],
        "combined_mdd": combined_stats["mdd"],
        "combined_win_days": combined_stats["win_days_pct"],
        "combined_trades_per_day": combined_stats["trades_per_day"],
        "h4_solo_sharpe": h4_stats["sharpe"],
        "h4_solo_mdd": h4_stats["mdd"],
        "h4_solo_win_days": h4_stats["win_days_pct"],
        "h4_solo_trades_per_day": h4_stats["trades_per_day"],
        "h2_solo_sharpe": h2_stats["sharpe"],
        "h2_solo_mdd": h2_stats["mdd"],
        "h2_solo_win_days": h2_stats["win_days_pct"],
        "h2_solo_trades_per_day": h2_stats["trades_per_day"],
        "passed": passed,
    }


def recommend(
    test1: dict,
    test2_h4: dict,
    test2_h2: dict,
    test3: dict,
) -> "tuple[str, str]":
    """Decision tree per plan; returns (verdict, one-paragraph reasoning)."""
    if not test2_h4["passed"]:
        return (
            "ALARM",
            "h4 baseline fails rolling 30-day Sharpe stability test. Even the "
            "locked production signal cannot consistently satisfy Smart Filter "
            "conditions in a rolling window. Pause Phase 3 and re-examine the "
            "baseline before layering additional filters.",
        )
    if not test1["passed"] or not test3["passed"]:
        return (
            "REJECT",
            "H1_z1.5_h2 either duplicates h4 baseline (test 1 fail) or fails "
            "to improve a combined portfolio (test 3 fail). It does not earn "
            "a slot — proceed to L11 (SHORT-side research) instead of Phase 3 "
            "integration.",
        )
    if test2_h2["passed"]:
        return (
            "STRONG_GO",
            "All three tests pass: H1_z1.5_h2 diversifies with the baseline, "
            "is monthly-stable on its own, and improves the combined portfolio. "
            "Proceed to Phase 3 integration + paper trading.",
        )
    return (
        "WEAK_GO",
        "Diversification (test 1) and synergy (test 3) hold, but H1_z1.5_h2 "
        "fails monthly Sharpe stability on its own. Paper trading before live "
        "deployment is mandatory; do NOT skip the paper gate.",
    )


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def _fmt(v, spec: str = ".2f") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    try:
        return format(v, spec)
    except Exception:
        return str(v)


def _ok(flag: bool) -> str:
    return "OK" if flag else "FAIL"


def format_report(
    strategy_stats: dict,
    test1: dict,
    test2_h4: dict,
    test2_h2: dict,
    test3: dict,
    verdict: str,
    reasoning: str,
) -> str:
    """Pure string builder for the validation report."""
    lines: "list[str]" = []
    W = 67
    lines.append("=" * W)
    lines.append("L10 PHASE 2B — H1_z1.5_h2 VALIDATION REPORT")
    lines.append("=" * W)
    lines.append("")
    lines.append("STRATEGIES:")
    h4s = strategy_stats["h4"]
    h2s = strategy_stats["h2"]
    lines.append(
        f"  h4 baseline:   N={h4s['n']}  Win%={_fmt(h4s['win_pct'])}  "
        f"pooled Sharpe={_fmt(h4s['sharpe'])}"
    )
    lines.append(
        f"  H1_z1.5_h2:    N={h2s['n']}  Win%={_fmt(h2s['win_pct'])}  "
        f"pooled Sharpe={_fmt(h2s['sharpe'])}"
    )
    lines.append("")
    lines.append("-" * W)
    lines.append("TEST 1: DAILY RETURN CORRELATION")
    lines.append("-" * W)
    lines.append(f"  Pearson correlation (h4 vs h2):   {_fmt(test1['correlation'], '.4f')}")
    lines.append(f"  Active days h4:     {_fmt(test1['active_h4_pct'], '.1f')}% of {test1['n_days']} days")
    lines.append(f"  Active days h2:     {_fmt(test1['active_h2_pct'], '.1f')}%")
    lines.append(f"  Both active days:   {_fmt(test1['both_active_pct'], '.1f')}%")
    lines.append("  Overlap breakdown on common active days:")
    lines.append(f"    Both winning:        {_fmt(test1['both_win_pct'], '.1f')}%")
    lines.append(f"    Both losing:         {_fmt(test1['both_lose_pct'], '.1f')}%")
    lines.append(f"    h4 win, h2 lose:     {_fmt(test1['h4_win_h2_lose_pct'], '.1f')}%  <- diversification")
    lines.append(f"    h4 lose, h2 win:     {_fmt(test1['h4_lose_h2_win_pct'], '.1f')}%  <- diversification")
    lines.append("")
    lines.append("  PASS criteria:")
    lines.append(f"    [{_ok(test1['correlation'] < 0.5)}] correlation < 0.5   ({_fmt(test1['correlation'], '.4f')})")
    lines.append(f"    [{_ok(test1['mixed_pct'] >= 15.0)}] mixed days >= 15%    ({_fmt(test1['mixed_pct'], '.1f')}%)")
    lines.append("")
    lines.append(f"  VERDICT TEST 1: {'PASS' if test1['passed'] else 'FAIL'}")
    lines.append("")
    lines.append("-" * W)
    lines.append("TEST 2: ROLLING 30-DAY SHARPE STABILITY")
    lines.append("-" * W)
    for label, r in (("h4 baseline", test2_h4), ("H1_z1.5_h2", test2_h2)):
        lines.append(
            f"  [{label}] used={r['n_windows']}/{r.get('n_total_windows', '?')} windows  "
            f"(skipped low-activity={r.get('n_dropped_low_activity', 0)}, "
            f"zero-std={r.get('n_dropped_zero_std', 0)})"
        )
        drop_pct = r.get("dropped_low_activity_pct", 0.0)
        warn = "  [!! WARN: drop >30% — reconsider min_trading_days]" if drop_pct > 30 else ""
        lines.append(f"    Low-activity drop:    {_fmt(drop_pct, '.1f')}%{warn}")
        lines.append(f"    Min rolling Sharpe:   {_fmt(r['min'])}")
        lines.append(
            f"    P05 / Median / P95:   {_fmt(r['p05'])} / {_fmt(r['median'])} / {_fmt(r['p95'])}"
        )
        lines.append(f"    Max:                  {_fmt(r['max'])}")
        lines.append(f"    % windows Sharpe > 2: {_fmt(r['pct_above_2'], '.1f')}%")
        lines.append(f"    % windows win days >= 65%: {_fmt(r['pct_win_days_above_65'], '.1f')}%")
        lines.append("")
    lines.append("  PASS criteria (each strategy):")
    lines.append("    min > 0  AND  median > 2.0  AND  >= 60% windows Sharpe > 2  AND")
    lines.append("    >= 50% windows win days >= 65%")
    lines.append("")
    lines.append(f"  VERDICT TEST 2 (h4): {'PASS' if test2_h4['passed'] else 'FAIL'}")
    lines.append(f"  VERDICT TEST 2 (h2): {'PASS' if test2_h2['passed'] else 'FAIL'}")
    lines.append("")
    lines.append("-" * W)
    lines.append("TEST 3: COMBINED PORTFOLIO (50/50)")
    lines.append("-" * W)
    lines.append(
        f"  Combined Sharpe:      {_fmt(test3['combined_sharpe'], '+.3f')}   "
        f"(h4 solo: {_fmt(test3['h4_solo_sharpe'], '+.3f')}, "
        f"h2 solo: {_fmt(test3['h2_solo_sharpe'], '+.3f')})"
    )
    lines.append(
        f"  Combined MDD:         {_fmt(test3['combined_mdd'], '.2f')} USD   "
        f"(h4 solo: {_fmt(test3['h4_solo_mdd'], '.2f')}, "
        f"h2 solo: {_fmt(test3['h2_solo_mdd'], '.2f')})"
    )
    lines.append(
        f"  Combined win days:    {_fmt(test3['combined_win_days'], '.1f')}%   "
        f"(h4 solo: {_fmt(test3['h4_solo_win_days'], '.1f')}%, "
        f"h2 solo: {_fmt(test3['h2_solo_win_days'], '.1f')}%)"
    )
    lines.append(
        f"  Combined trades/day:  {_fmt(test3['combined_trades_per_day'], '.3f')}   "
        f"(h4: {_fmt(test3['h4_solo_trades_per_day'], '.3f')}, "
        f"h2: {_fmt(test3['h2_solo_trades_per_day'], '.3f')})"
    )
    lines.append("")
    max_solo = max(test3["h4_solo_sharpe"], test3["h2_solo_sharpe"])
    lines.append("  PASS criteria:")
    lines.append(
        f"    [{_ok(test3['combined_sharpe'] > max_solo)}] combined Sharpe > max(h4, h2)  "
        f"({_fmt(test3['combined_sharpe'], '.3f')} vs {_fmt(max_solo, '.3f')})"
    )
    lines.append(
        f"    [{_ok(test3['combined_mdd'] > test3['h4_solo_mdd'])}] combined MDD less severe than h4 solo"
    )
    lines.append(
        f"    [{_ok(test3['combined_win_days'] >= test3['h4_solo_win_days'])}] combined win days >= h4 solo"
    )
    lines.append("")
    lines.append(f"  VERDICT TEST 3: {'PASS' if test3['passed'] else 'FAIL'}")
    lines.append("")
    lines.append("=" * W)
    lines.append("OVERALL RECOMMENDATION")
    lines.append("=" * W)
    lines.append(f"  -> {verdict}")
    lines.append("")
    lines.append("  Reasoning: " + reasoning)
    lines.append("=" * W)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _strategy_stats_from_trades(trades: "list[dict]", holding_hours: int) -> dict:
    """Pooled (N, Win%, Sharpe) from the flat trade list — parity check with Phase 2."""
    if not trades:
        return {"n": 0, "win_pct": None, "sharpe": None}
    returns = pd.Series([t["pnl_pct"] for t in trades])
    n = len(returns)
    win_pct = float((returns > 0).mean() * 100)
    std = returns.std(ddof=1)
    if std == 0 or pd.isna(std):
        sharpe = None
    else:
        bars_per_year = TRADING_DAYS_PER_YEAR * 24 / holding_hours
        sharpe = float(returns.mean() / std * math.sqrt(bars_per_year))
    return {"n": n, "win_pct": round(win_pct, 1), "sharpe": round(sharpe, 2) if sharpe else None}


def main() -> int:
    # Deferred imports so the script does not require DB/ccxt just for --help.
    from collectors.config import get_config
    from collectors.db import init_pool
    from research_netposition import _load_coins_for_interval, build_hypothesis_filters
    from backtest_market_flush_multitf import (
        MARKET_FLUSH_FILTERS,
        RANK_HOLDING_HOURS,
    )

    init_pool(get_config())

    print("Loading h4 coin dataframes ...")
    all_dfs_h4 = _load_coins_for_interval("h4")
    print()
    print("Loading h2 coin dataframes ...")
    all_dfs_h2 = _load_coins_for_interval("h2")
    print()

    h4_filters = list(MARKET_FLUSH_FILTERS)
    h2_filters = build_hypothesis_filters("H1", H1_Z_NETPOS)

    trades_h4 = extract_trade_records(all_dfs_h4, h4_filters, RANK_HOLDING_HOURS)
    trades_h2 = extract_trade_records(all_dfs_h2, h2_filters, RANK_HOLDING_HOURS)

    if not trades_h4 or not trades_h2:
        print("ERROR: one of the strategies produced zero trades — cannot proceed.")
        print(f"  h4 trades: {len(trades_h4)}")
        print(f"  h2 trades: {len(trades_h2)}")
        return 2

    # Build a common daily date range spanning every exit across both strategies.
    all_exits = [t["exit_ts"].normalize() for t in trades_h4 + trades_h2]
    start = min(all_exits)
    end = max(all_exits)
    date_range = pd.date_range(start, end, freq="D", tz="UTC")

    daily_h4 = aggregate_daily_pnl(trades_h4, date_range)
    daily_h2 = aggregate_daily_pnl(trades_h2, date_range)

    h4_stats = _strategy_stats_from_trades(trades_h4, RANK_HOLDING_HOURS)
    h2_stats = _strategy_stats_from_trades(trades_h2, RANK_HOLDING_HOURS)
    strategy_stats = {"h4": h4_stats, "h2": h2_stats}

    test1 = compute_correlation_test(daily_h4, daily_h2)
    test2_h4 = compute_rolling_sharpe_test(daily_h4)
    test2_h2 = compute_rolling_sharpe_test(daily_h2)
    test3 = compute_combined_portfolio_test(daily_h4, daily_h2, capital=DEFAULT_CAPITAL_USD)

    verdict, reasoning = recommend(test1, test2_h4, test2_h2, test3)

    print(format_report(strategy_stats, test1, test2_h4, test2_h2, test3, verdict, reasoning))
    return 0


if __name__ == "__main__":
    sys.exit(main())
