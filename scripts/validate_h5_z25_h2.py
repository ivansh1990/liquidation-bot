#!/usr/bin/env python3
"""
L13 Phase 3b: H5_z2.5_h2 validation script.

Three additional tests on top of L13 Phase 3 results to resolve the
"deploy vs reject" decision for the `H5_z2.5_h2` CVD exhaustion candidate
(the single qualified MARGINAL: N=137, Win% 56.2, pooled OOS Sharpe 6.20,
3/3 OOS folds positive, failed only on trades/day absolute floor):

  Test 1: Daily-return Pearson correlation with h4 baseline (diversification).
  Test 2: Rolling 30-day Sharpe stability (Smart Filter simulation) — for
          both the h4 baseline and H5_z2.5_h2 separately.
  Test 3: Combined 50/50 portfolio simulation (synergy).

Read-only: mirrors L10 Phase 2b structure and reuses its helpers (they are
all module-level importable — no refactor needed). Does NOT modify any
locked research script.

PARITY NOTE: `extract_trade_records` replicates `run_variant`'s internal
mask logic while preserving per-trade metadata (run_variant returns only
pooled aggregates). If the replicated mask ever drifts from run_variant's
internal path, the divergence is visible in the report header — pooled
(N, Win%, Sharpe) is printed once from run_variant and again from the
extracted trade list.

Usage:
    .venv/bin/python scripts/validate_h5_z25_h2.py
"""
from __future__ import annotations

import math
import os
import sys

import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)

# Reuse Phase 2b helpers — all module-level importable.
from validate_h1_z15_h2 import (  # noqa: E402
    DEFAULT_CAPITAL_USD,
    STD_EPS,  # noqa: F401  re-exported for convenience
    TRADING_DAYS_PER_YEAR,
    aggregate_daily_pnl,
    compute_combined_portfolio_test,
    compute_correlation_test,
    compute_rolling_sharpe_test,
    extract_trade_records,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

H5_Z_THRESHOLD = 2.5
H5_INTERVAL = "h2"
H4_INTERVAL = "h4"


# ---------------------------------------------------------------------------
# Data loading wrappers
# ---------------------------------------------------------------------------


def run_h4_baseline() -> "tuple[dict, list[dict]]":
    """
    Load h4 coin dataframes, run the locked `market_flush` baseline, and
    return both the pooled summary (from run_variant) and the per-trade
    list (from extract_trade_records).
    """
    # Deferred imports so that tests can import this module without DB.
    from research_cvd_standalone import _load_coins_for_interval
    from research_netposition import run_variant
    from backtest_market_flush_multitf import (
        MARKET_FLUSH_FILTERS,
        RANK_HOLDING_HOURS,
    )

    print(f"Loading {H4_INTERVAL} coin dataframes ...")
    dfs = _load_coins_for_interval(H4_INTERVAL)
    print()

    filters = list(MARKET_FLUSH_FILTERS)
    summary = run_variant(dfs, filters, RANK_HOLDING_HOURS)
    trades = extract_trade_records(dfs, filters, RANK_HOLDING_HOURS)
    return summary, trades


def run_h5_z25_h2() -> "tuple[dict, list[dict]]":
    """
    Load h2 coin dataframes (with CVD + exhaustion + divergence features),
    run H5 with threshold 2.5, and return both the pooled summary and the
    per-trade list.
    """
    from research_cvd_standalone import (
        _load_coins_for_interval,
        build_hypothesis_filters,
    )
    from research_netposition import run_variant
    from backtest_market_flush_multitf import RANK_HOLDING_HOURS

    print(f"Loading {H5_INTERVAL} coin dataframes (with CVD features) ...")
    dfs = _load_coins_for_interval(H5_INTERVAL)
    print()

    filters = build_hypothesis_filters("H5", H5_Z_THRESHOLD)
    summary = run_variant(dfs, filters, RANK_HOLDING_HOURS)
    trades = extract_trade_records(dfs, filters, RANK_HOLDING_HOURS)
    return summary, trades


def _strategy_stats_from_trades(
    trades: "list[dict]", holding_hours: int
) -> dict:
    """Pooled (N, Win%, Sharpe) parity check from the flat trade list."""
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
    return {
        "n": n,
        "win_pct": round(win_pct, 1),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
    }


# ---------------------------------------------------------------------------
# Recommendation tree (mirror of L10 Phase 2b)
# ---------------------------------------------------------------------------


def recommend(
    test1: dict,
    test2_h4: dict,
    test2_h5: dict,
    test3: dict,
) -> "tuple[str, str, str]":
    """
    Decision tree. Returns (verdict, reasoning paragraph, next action).
    """
    if not test2_h4["passed"]:
        return (
            "ALARM",
            "h4 baseline fails rolling 30-day Sharpe stability test. Even the "
            "locked production signal cannot consistently satisfy Smart Filter "
            "conditions in a rolling window. Pause Phase 3b and re-examine the "
            "baseline before layering standalone signals alongside it.",
            "Pause Phase 3b; reinvestigate h4 baseline rolling stability.",
        )
    if not test1["passed"] or not test3["passed"]:
        return (
            "REJECT",
            "H5_z2.5_h2 either duplicates h4 baseline (test 1 fail) or fails "
            "to improve a combined portfolio (test 3 fail). CVD exhaustion "
            "does not earn a slot — proceed to L11 (SHORT-side research) "
            "instead of Phase 4 integration.",
            "Skip to L11 SHORT-side research.",
        )
    if test2_h5["passed"]:
        return (
            "STRONG_GO",
            "All tests pass: H5_z2.5_h2 diversifies with the h4 baseline, is "
            "monthly-stable on its own, and improves the combined portfolio. "
            "Proceed to Phase 4 integration and paper trading parallel to h4.",
            "Phase 4 integration + paper deploy parallel to h4.",
        )
    return (
        "WEAK_GO",
        "Diversification (test 1) and synergy (test 3) hold, but H5_z2.5_h2 "
        "fails monthly Sharpe stability on its own. Paper trading before live "
        "deployment is mandatory; do NOT skip the paper gate.",
        "Paper trading parallel to h4 (min 30 days); no live until rolling "
        "stability passes.",
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
    test2_h5: dict,
    test3: dict,
    verdict: str,
    reasoning: str,
    next_action: str,
) -> str:
    """Pure string builder for the validation report."""
    lines: "list[str]" = []
    W = 67
    lines.append("=" * W)
    lines.append("L13 PHASE 3B — H5_z2.5_h2 VALIDATION REPORT")
    lines.append("=" * W)
    lines.append("")
    lines.append("STRATEGIES:")
    h4s = strategy_stats["h4"]
    h5s = strategy_stats["h5"]
    lines.append(
        f"  h4 baseline:   N={h4s['n']}  Win%={_fmt(h4s['win_pct'])}  "
        f"pooled Sharpe={_fmt(h4s['sharpe'])}"
    )
    lines.append(
        f"  H5_z2.5_h2:    N={h5s['n']}  Win%={_fmt(h5s['win_pct'])}  "
        f"pooled Sharpe={_fmt(h5s['sharpe'])}"
    )
    lines.append("")
    lines.append("-" * W)
    lines.append("TEST 1: DAILY RETURN CORRELATION")
    lines.append("-" * W)
    lines.append(
        f"  Pearson correlation (h4 vs h5):   "
        f"{_fmt(test1['correlation'], '.4f')}"
    )
    lines.append(
        f"  Active days h4:     {_fmt(test1['active_h4_pct'], '.1f')}% "
        f"of {test1['n_days']} days"
    )
    lines.append(
        f"  Active days h5:     {_fmt(test1['active_h2_pct'], '.1f')}%"
    )
    lines.append(
        f"  Both active days:   {_fmt(test1['both_active_pct'], '.1f')}%"
    )
    lines.append("  Overlap breakdown on common active days:")
    lines.append(
        f"    Both winning:        {_fmt(test1['both_win_pct'], '.1f')}%"
    )
    lines.append(
        f"    Both losing:         {_fmt(test1['both_lose_pct'], '.1f')}%"
    )
    lines.append(
        f"    h4 win, h5 lose:     "
        f"{_fmt(test1['h4_win_h2_lose_pct'], '.1f')}%  <- diversification"
    )
    lines.append(
        f"    h4 lose, h5 win:     "
        f"{_fmt(test1['h4_lose_h2_win_pct'], '.1f')}%  <- diversification"
    )
    lines.append("")
    lines.append("  PASS criteria:")
    lines.append(
        f"    [{_ok(test1['correlation'] < 0.5)}] correlation < 0.5   "
        f"({_fmt(test1['correlation'], '.4f')})"
    )
    lines.append(
        f"    [{_ok(test1['mixed_pct'] >= 15.0)}] mixed days >= 15%    "
        f"({_fmt(test1['mixed_pct'], '.1f')}%)"
    )
    lines.append("")
    lines.append(f"  VERDICT TEST 1: {'PASS' if test1['passed'] else 'FAIL'}")
    lines.append("")
    lines.append("-" * W)
    lines.append("TEST 2: ROLLING 30-DAY SHARPE STABILITY")
    lines.append("-" * W)
    for label, r in (("h4 baseline", test2_h4), ("H5_z2.5_h2", test2_h5)):
        lines.append(
            f"  [{label}] used={r['n_windows']}/"
            f"{r.get('n_total_windows', '?')} windows  "
            f"(skipped low-activity={r.get('n_dropped_low_activity', 0)}, "
            f"zero-std={r.get('n_dropped_zero_std', 0)})"
        )
        drop_pct = r.get("dropped_low_activity_pct", 0.0)
        warn = (
            "  [!! WARN: drop >30% — reconsider min_trading_days]"
            if drop_pct > 30
            else ""
        )
        lines.append(
            f"    Low-activity drop:    {_fmt(drop_pct, '.1f')}%{warn}"
        )
        lines.append(f"    Min rolling Sharpe:   {_fmt(r['min'])}")
        lines.append(
            f"    P05 / Median / P95:   {_fmt(r['p05'])} / "
            f"{_fmt(r['median'])} / {_fmt(r['p95'])}"
        )
        lines.append(f"    Max:                  {_fmt(r['max'])}")
        lines.append(
            f"    % windows Sharpe > 2: {_fmt(r['pct_above_2'], '.1f')}%"
        )
        lines.append(
            f"    % windows win days >= 65%: "
            f"{_fmt(r['pct_win_days_above_65'], '.1f')}%"
        )
        lines.append("")
    lines.append("  PASS criteria (each strategy):")
    lines.append("    min > 0  AND  median > 2.0  AND  >= 60% windows Sharpe > 2  AND")
    lines.append("    >= 50% windows win days >= 65%")
    lines.append("")
    lines.append(
        f"  VERDICT TEST 2 (h4): {'PASS' if test2_h4['passed'] else 'FAIL'}"
    )
    lines.append(
        f"  VERDICT TEST 2 (h5): {'PASS' if test2_h5['passed'] else 'FAIL'}"
    )
    lines.append("")
    lines.append("-" * W)
    lines.append("TEST 3: COMBINED PORTFOLIO (50/50)")
    lines.append("-" * W)
    # compute_combined_portfolio_test returns the partner strategy's metrics
    # under "h2_solo_*" keys — they correspond to our h5 strategy here.
    h5_solo_sharpe = test3["h2_solo_sharpe"]
    h5_solo_mdd = test3["h2_solo_mdd"]
    h5_solo_win_days = test3["h2_solo_win_days"]
    h5_solo_tpd = test3["h2_solo_trades_per_day"]
    lines.append(
        f"  Combined Sharpe:      "
        f"{_fmt(test3['combined_sharpe'], '+.3f')}   "
        f"(h4 solo: {_fmt(test3['h4_solo_sharpe'], '+.3f')}, "
        f"h5 solo: {_fmt(h5_solo_sharpe, '+.3f')})"
    )
    lines.append(
        f"  Combined MDD:         "
        f"{_fmt(test3['combined_mdd'], '.2f')} USD   "
        f"(h4 solo: {_fmt(test3['h4_solo_mdd'], '.2f')}, "
        f"h5 solo: {_fmt(h5_solo_mdd, '.2f')})"
    )
    lines.append(
        f"  Combined win days:    "
        f"{_fmt(test3['combined_win_days'], '.1f')}%   "
        f"(h4 solo: {_fmt(test3['h4_solo_win_days'], '.1f')}%, "
        f"h5 solo: {_fmt(h5_solo_win_days, '.1f')}%)"
    )
    lines.append(
        f"  Combined trades/day:  "
        f"{_fmt(test3['combined_trades_per_day'], '.3f')}   "
        f"(h4: {_fmt(test3['h4_solo_trades_per_day'], '.3f')}, "
        f"h5: {_fmt(h5_solo_tpd, '.3f')})"
    )
    lines.append("")
    max_solo = max(test3["h4_solo_sharpe"], h5_solo_sharpe)
    lines.append("  PASS criteria:")
    lines.append(
        f"    [{_ok(test3['combined_sharpe'] > max_solo)}] combined Sharpe > "
        f"max(h4, h5)  ({_fmt(test3['combined_sharpe'], '.3f')} vs "
        f"{_fmt(max_solo, '.3f')})"
    )
    lines.append(
        f"    [{_ok(test3['combined_mdd'] > test3['h4_solo_mdd'])}] combined "
        f"MDD less severe than h4 solo"
    )
    lines.append(
        f"    [{_ok(test3['combined_win_days'] >= test3['h4_solo_win_days'])}] "
        f"combined win days >= h4 solo"
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
    lines.append("")
    lines.append("  Next action: " + next_action)
    lines.append("=" * W)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    from collectors.config import get_config
    from collectors.db import init_pool
    from backtest_market_flush_multitf import RANK_HOLDING_HOURS

    init_pool(get_config())

    h4_summary, trades_h4 = run_h4_baseline()
    h5_summary, trades_h5 = run_h5_z25_h2()

    if not trades_h4 or not trades_h5:
        print("ERROR: one of the strategies produced zero trades — cannot proceed.")
        print(f"  h4 trades: {len(trades_h4)}")
        print(f"  h5 trades: {len(trades_h5)}")
        # run_variant summaries printed for debug.
        print(f"  h4 summary: {h4_summary.get('pooled')}")
        print(f"  h5 summary: {h5_summary.get('pooled')}")
        return 2

    all_exits = [t["exit_ts"].normalize() for t in trades_h4 + trades_h5]
    start = min(all_exits)
    end = max(all_exits)
    date_range = pd.date_range(start, end, freq="D", tz="UTC")

    daily_h4 = aggregate_daily_pnl(trades_h4, date_range)
    daily_h5 = aggregate_daily_pnl(trades_h5, date_range)

    h4_stats = _strategy_stats_from_trades(trades_h4, RANK_HOLDING_HOURS)
    h5_stats = _strategy_stats_from_trades(trades_h5, RANK_HOLDING_HOURS)
    strategy_stats = {"h4": h4_stats, "h5": h5_stats}

    test1 = compute_correlation_test(daily_h4, daily_h5)
    test2_h4 = compute_rolling_sharpe_test(daily_h4)
    test2_h5 = compute_rolling_sharpe_test(daily_h5)
    test3 = compute_combined_portfolio_test(
        daily_h4, daily_h5, capital=DEFAULT_CAPITAL_USD,
    )

    verdict, reasoning, next_action = recommend(
        test1, test2_h4, test2_h5, test3,
    )

    print(format_report(
        strategy_stats, test1, test2_h4, test2_h5, test3,
        verdict, reasoning, next_action,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
