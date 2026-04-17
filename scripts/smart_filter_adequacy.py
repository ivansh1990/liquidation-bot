#!/usr/bin/env python3
"""
L13 Phase 3c — Smart Filter adequacy test.

Reformulates L13 Phase 3b's ALARM verdict against the actual Binance Copy
Trading Smart Filter rules rather than the abstract rolling-Sharpe gate:

  1. Trading days >= 14 in the last 30 days.
  2. PnL positive on 30d / 60d / 90d rolling windows.
  3. Win days ratio >= 65% (30d/60d) or 60% (90d), computed on TRADING
     days rather than calendar days.
  4. Max drawdown within the window <= 20%.

For each of {h4 baseline, H5_z2.5_h2, 50/50 combined} the script slides
rolling 30/60/90d windows across the per-day pnl history and counts how
many windows satisfy all four gates. A recommendation tree maps the 30d
pass rates to one of six outcomes (STRONG_GO / H4_ONLY / H5_ONLY /
COMBINED_ONLY / H4_DONT_ADD / REJECT_BOTH).

Read-only: reuses run_h4_baseline() and run_h5_z25_h2() from Phase 3b
(both module-level, no modification needed) plus DEFAULT_CAPITAL_USD from
Phase 2b. Does NOT touch bot/, exchange/, collectors/, or any locked
research script.

Usage:
    .venv/bin/python scripts/smart_filter_adequacy.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)


# ---------------------------------------------------------------------------
# Smart Filter window configurations (actual Binance rules)
# ---------------------------------------------------------------------------

SMART_FILTER_CONFIGS = [
    {
        "window_days": 30, "min_trading_days": 14,
        "win_days_threshold": 0.65, "mdd_threshold": 20.0,
    },
    {
        "window_days": 60, "min_trading_days": 28,
        "win_days_threshold": 0.65, "mdd_threshold": 20.0,
    },
    {
        "window_days": 90, "min_trading_days": 42,
        "win_days_threshold": 0.60, "mdd_threshold": 20.0,
    },
]

ADEQUACY_PASS_RATE_PCT = 60.0  # A strategy is "Smart-Filter adequate" when
                               # >= this fraction of 30d windows pass all
                               # four gates.


# ---------------------------------------------------------------------------
# Pure functions (test surface)
# ---------------------------------------------------------------------------


def compute_daily_metrics(
    trades: "list[dict]",
    date_range: pd.DatetimeIndex,
    capital_usd: float = 1000.0,
) -> pd.DataFrame:
    """
    Per-day metrics from a flat trade list.

    Columns:
      pnl_pct      — sum of trade pnl_pct for trades exiting this day
      pnl_usd      — pnl_pct / 100 * capital_usd
      trade_count  — number of trades exiting this day
      is_active    — trade_count > 0
      is_winning   — pnl_pct > 0 (only meaningful when is_active)
      equity_usd   — capital_usd + cumsum(pnl_usd)

    `exit_ts.normalize()` determines which day a trade lands on. Trades
    whose normalized exit date is outside `date_range` are silently ignored
    (mirrors `aggregate_daily_pnl` behavior).
    """
    idx = pd.DatetimeIndex(date_range)
    df = pd.DataFrame(
        {
            "pnl_pct": 0.0,
            "trade_count": 0,
        },
        index=idx,
    )
    df["trade_count"] = df["trade_count"].astype(int)

    for t in trades:
        day = t["exit_ts"].normalize()
        if day in df.index:
            df.at[day, "pnl_pct"] += float(t["pnl_pct"])
            df.at[day, "trade_count"] += 1

    df["pnl_usd"] = df["pnl_pct"] / 100.0 * capital_usd
    df["is_active"] = df["trade_count"] > 0
    df["is_winning"] = df["pnl_pct"] > 0
    df["equity_usd"] = capital_usd + df["pnl_usd"].cumsum()
    # Reorder columns for determinism.
    return df[[
        "pnl_pct", "pnl_usd", "trade_count",
        "is_active", "is_winning", "equity_usd",
    ]]


def simulate_smart_filter_windows(
    daily_df: pd.DataFrame,
    window_days: int,
    min_trading_days: int,
    win_days_threshold: float,
    mdd_threshold: float,
) -> pd.DataFrame:
    """
    Slide a window of length `window_days` across `daily_df` and evaluate
    the four Smart Filter gates at each terminal date.

    Returned DataFrame has one row per window (index = window end date):
      trading_days_in_window
      pnl_sum_usd
      win_days_ratio
      mdd_in_window_pct
      g_trading_days, g_pnl_positive, g_win_days, g_mdd  (bool gates)
      passed                                             (all four)
    """
    n = len(daily_df)
    if n < window_days:
        return pd.DataFrame(
            columns=[
                "trading_days_in_window", "pnl_sum_usd",
                "win_days_ratio", "mdd_in_window_pct",
                "g_trading_days", "g_pnl_positive",
                "g_win_days", "g_mdd", "passed",
            ],
        )

    rows = []
    ends = []
    is_active = daily_df["is_active"].to_numpy()
    is_winning = daily_df["is_winning"].to_numpy()
    pnl_usd = daily_df["pnl_usd"].to_numpy()
    equity = daily_df["equity_usd"].to_numpy()

    for i in range(window_days - 1, n):
        sl = slice(i - window_days + 1, i + 1)
        active_mask = is_active[sl]
        winning_mask = is_winning[sl]

        trading_days = int(active_mask.sum())
        pnl_sum = float(pnl_usd[sl].sum())
        winning_days = int((active_mask & winning_mask).sum())
        win_ratio = (
            float(winning_days) / trading_days if trading_days > 0 else float("nan")
        )

        eq_slice = equity[sl]
        # Intra-window drawdown: align to the window start.
        anchor = eq_slice[0] - pnl_usd[sl][0]
        # Running max of equity from the start of the window. Use the
        # pre-window equity (equity at start minus first-day pnl) as the
        # baseline so a drop on day 1 still registers.
        running_eq = eq_slice
        running_max = pd.Series(running_eq).cummax().to_numpy()
        running_max = [max(anchor, m) for m in running_max]
        dd_pct = [
            (running_eq[j] - running_max[j]) / running_max[j] * 100.0
            if running_max[j] > 0 else 0.0
            for j in range(len(running_eq))
        ]
        mdd_pct = float(min(dd_pct)) if dd_pct else 0.0

        g_trading = trading_days >= min_trading_days
        g_pnl_pos = pnl_sum > 0
        g_win_days = (
            win_ratio >= win_days_threshold
            if trading_days > 0
            else False
        )
        g_mdd = abs(mdd_pct) <= mdd_threshold
        ok = bool(g_trading and g_pnl_pos and g_win_days and g_mdd)

        rows.append({
            "trading_days_in_window": trading_days,
            "pnl_sum_usd": pnl_sum,
            "win_days_ratio": win_ratio,
            "mdd_in_window_pct": mdd_pct,
            "g_trading_days": g_trading,
            "g_pnl_positive": g_pnl_pos,
            "g_win_days": g_win_days,
            "g_mdd": g_mdd,
            "passed": ok,
        })
        ends.append(daily_df.index[i])

    return pd.DataFrame(rows, index=pd.DatetimeIndex(ends, name="window_end"))


def summarize_smart_filter_results(window_df: pd.DataFrame, label: str) -> dict:
    """Aggregate per-window pass statistics + per-criterion breakdown."""
    total = len(window_df)
    if total == 0:
        return {
            "label": label,
            "total_windows": 0,
            "passed_windows": 0,
            "pass_rate_pct": 0.0,
            "criterion_pass_rates": {
                "trading_days": 0.0,
                "pnl_positive": 0.0,
                "win_days": 0.0,
                "mdd": 0.0,
            },
        }
    passed_n = int(window_df["passed"].sum())
    rate = passed_n / total * 100.0
    crit = {
        "trading_days": float(window_df["g_trading_days"].mean() * 100.0),
        "pnl_positive": float(window_df["g_pnl_positive"].mean() * 100.0),
        "win_days": float(window_df["g_win_days"].mean() * 100.0),
        "mdd": float(window_df["g_mdd"].mean() * 100.0),
    }
    return {
        "label": label,
        "total_windows": total,
        "passed_windows": passed_n,
        "pass_rate_pct": rate,
        "criterion_pass_rates": crit,
    }


def recommend(
    h4_30: dict,
    h5_30: dict,
    combined_30: dict,
) -> "tuple[str, str, str]":
    """
    Map 30d pass rates to one of six outcomes.

    Adequacy threshold: >= ADEQUACY_PASS_RATE_PCT (60%) of 30d windows
    pass all four Smart Filter gates.
    """
    h4_ok = h4_30["pass_rate_pct"] >= ADEQUACY_PASS_RATE_PCT
    h5_ok = h5_30["pass_rate_pct"] >= ADEQUACY_PASS_RATE_PCT
    combined_ok = combined_30["pass_rate_pct"] >= ADEQUACY_PASS_RATE_PCT

    if combined_ok:
        if h4_ok and h5_ok:
            return (
                "STRONG_GO",
                "All three (h4, H5, combined) pass Smart Filter adequacy on "
                "30d windows. Both strategies are monthly-stable against "
                "actual Binance Copy Trading rules, and the combined "
                "portfolio inherits that stability.",
                "Phase 4 integration + paper deploy for combined (h4+H5).",
            )
        if h4_ok and not h5_ok:
            return (
                "H4_ONLY",
                "h4 baseline passes Smart Filter adequacy solo; H5_z2.5_h2 "
                "does not pass solo but the combined portfolio still "
                "clears the bar. Safer to deploy h4 alone and keep H5 "
                "paper-only until it clears solo.",
                "Deploy h4 solo, paper H5 for future consideration.",
            )
        if h5_ok and not h4_ok:
            return (
                "H5_ONLY",
                "H5_z2.5_h2 passes Smart Filter adequacy solo AND combined "
                "passes, but h4 baseline does NOT — H5 is pulling the "
                "combined portfolio over the bar. Unusual outcome: verify "
                "manually before taking action.",
                "Deploy H5 solo (unusual outcome, verify manually).",
            )
        # not h4_ok and not h5_ok and combined_ok
        return (
            "COMBINED_ONLY",
            "Neither strategy alone passes Smart Filter adequacy but the "
            "50/50 combined portfolio does — synergy is required. Deploying "
            "either solo would fail the Smart Filter; only the combined "
            "configuration qualifies.",
            "Deploy h4+H5 combined only, not solo.",
        )

    # combined does not clear the bar
    if h4_ok:
        return (
            "H4_DONT_ADD",
            "h4 baseline passes Smart Filter adequacy solo, but adding "
            "H5_z2.5_h2 into a 50/50 combined portfolio drags the pass "
            "rate below the bar. H5 is a net negative for Smart Filter "
            "purposes despite any correlation/Sharpe improvements.",
            "Deploy h4 solo, skip H5, move to L11 SHORT research.",
        )
    return (
        "REJECT_BOTH",
        "Neither h4 baseline nor the combined portfolio passes Smart "
        "Filter adequacy on 30d windows. The live deployment strategy "
        "needs fundamental review — a signal the locked production "
        "baseline cannot satisfy actual Binance rules.",
        "Fundamental issue — review live deployment strategy.",
    )


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def _fmt_pct(v: float) -> str:
    return f"{v:5.1f}%"


def _strategy_block(
    label: str,
    s30: dict,
    s60: dict,
    s90: dict,
) -> "list[str]":
    lines = []
    lines.append("-" * 67)
    cfg30 = SMART_FILTER_CONFIGS[0]
    cfg60 = SMART_FILTER_CONFIGS[1]
    cfg90 = SMART_FILTER_CONFIGS[2]
    lines.append(f"{label} — Smart Filter pass rates")
    lines.append("-" * 67)
    for cfg, s in ((cfg30, s30), (cfg60, s60), (cfg90, s90)):
        lines.append(
            f"  {cfg['window_days']}d windows "
            f"(rule: >={cfg['min_trading_days']} trading days, pnl>0, "
            f"win_days>={int(cfg['win_days_threshold']*100)}%, "
            f"mdd<={int(cfg['mdd_threshold'])}%):"
        )
        lines.append(
            f"    Total: {s['total_windows']:4d}    "
            f"Passed: {s['passed_windows']:4d}  "
            f"({_fmt_pct(s['pass_rate_pct'])})"
        )
        crit = s["criterion_pass_rates"]
        lines.append(
            f"    Trading days gate:    {_fmt_pct(crit['trading_days'])}"
        )
        lines.append(
            f"    PnL positive gate:    {_fmt_pct(crit['pnl_positive'])}"
        )
        lines.append(
            f"    Win days gate:        {_fmt_pct(crit['win_days'])}"
        )
        lines.append(
            f"    MDD gate:             {_fmt_pct(crit['mdd'])}"
        )
        lines.append("")
    return lines


def format_report(
    strategy_stats: dict,
    h4_summaries: "tuple[dict, dict, dict]",
    h5_summaries: "tuple[dict, dict, dict]",
    combined_summaries: "tuple[dict, dict, dict]",
    verdict: str,
    reasoning: str,
    next_action: str,
) -> str:
    lines = []
    W = 67
    lines.append("=" * W)
    lines.append("L13 PHASE 3C — SMART FILTER ADEQUACY REPORT")
    lines.append("=" * W)
    lines.append("")
    lines.append("STRATEGIES:")
    h4s = strategy_stats.get("h4", {})
    h5s = strategy_stats.get("h5", {})
    lines.append(
        f"  h4 baseline:  N={h4s.get('n', '?')}  "
        f"Win%={h4s.get('win_pct', '?')}  "
        f"trades/day={h4s.get('trades_per_day', '?')}"
    )
    lines.append(
        f"  H5_z2.5_h2:   N={h5s.get('n', '?')}  "
        f"Win%={h5s.get('win_pct', '?')}  "
        f"trades/day={h5s.get('trades_per_day', '?')}"
    )
    lines.append("")
    lines.extend(_strategy_block("h4 BASELINE", *h4_summaries))
    lines.extend(_strategy_block("H5_z2.5_h2", *h5_summaries))
    lines.extend(_strategy_block("COMBINED 50/50", *combined_summaries))
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


def _strategy_header_stats(trades: "list[dict]", days_span: float) -> dict:
    if not trades:
        return {"n": 0, "win_pct": None, "trades_per_day": None}
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl_pct"] > 0)
    win_pct = round(wins / n * 100.0, 1)
    tpd = round(n / days_span, 3) if days_span > 0 else None
    return {"n": n, "win_pct": win_pct, "trades_per_day": tpd}


def main() -> int:
    from collectors.config import get_config
    from collectors.db import init_pool
    from validate_h5_z25_h2 import run_h4_baseline, run_h5_z25_h2
    from validate_h1_z15_h2 import DEFAULT_CAPITAL_USD

    init_pool(get_config())

    h4_summary, trades_h4 = run_h4_baseline()
    h5_summary, trades_h5 = run_h5_z25_h2()

    if not trades_h4 or not trades_h5:
        print("ERROR: one of the strategies produced zero trades.")
        print(f"  h4 trades: {len(trades_h4)}")
        print(f"  h5 trades: {len(trades_h5)}")
        print(f"  h4 summary pooled: {h4_summary.get('pooled')}")
        print(f"  h5 summary pooled: {h5_summary.get('pooled')}")
        return 2

    all_exits = [t["exit_ts"].normalize() for t in trades_h4 + trades_h5]
    start = min(all_exits)
    end = max(all_exits)
    date_range = pd.date_range(start, end, freq="D", tz="UTC")
    days_span = (end - start).days + 1

    h4_daily = compute_daily_metrics(
        trades_h4, date_range, capital_usd=DEFAULT_CAPITAL_USD,
    )
    h5_daily = compute_daily_metrics(
        trades_h5, date_range, capital_usd=DEFAULT_CAPITAL_USD,
    )

    # Combined 50/50: split capital half-half. Each half uses its own pnl_pct
    # (strategies are independent) and is scaled to 0.5 * capital. is_active
    # of combined = active in either strategy.
    half_cap = DEFAULT_CAPITAL_USD * 0.5
    combined_pnl_usd = (
        h4_daily["pnl_pct"] / 100.0 * half_cap
        + h5_daily["pnl_pct"] / 100.0 * half_cap
    )
    combined_pnl_pct = combined_pnl_usd / DEFAULT_CAPITAL_USD * 100.0
    combined_trade_count = (
        h4_daily["trade_count"] + h5_daily["trade_count"]
    ).astype(int)
    combined_is_active = combined_trade_count > 0
    combined_is_winning = combined_pnl_pct > 0
    combined_equity = DEFAULT_CAPITAL_USD + combined_pnl_usd.cumsum()
    combined_daily = pd.DataFrame(
        {
            "pnl_pct": combined_pnl_pct,
            "pnl_usd": combined_pnl_usd,
            "trade_count": combined_trade_count,
            "is_active": combined_is_active,
            "is_winning": combined_is_winning,
            "equity_usd": combined_equity,
        },
        index=date_range,
    )

    h4_summaries = []
    h5_summaries = []
    combined_summaries = []
    for cfg in SMART_FILTER_CONFIGS:
        h4_w = simulate_smart_filter_windows(
            h4_daily,
            window_days=cfg["window_days"],
            min_trading_days=cfg["min_trading_days"],
            win_days_threshold=cfg["win_days_threshold"],
            mdd_threshold=cfg["mdd_threshold"],
        )
        h5_w = simulate_smart_filter_windows(
            h5_daily,
            window_days=cfg["window_days"],
            min_trading_days=cfg["min_trading_days"],
            win_days_threshold=cfg["win_days_threshold"],
            mdd_threshold=cfg["mdd_threshold"],
        )
        comb_w = simulate_smart_filter_windows(
            combined_daily,
            window_days=cfg["window_days"],
            min_trading_days=cfg["min_trading_days"],
            win_days_threshold=cfg["win_days_threshold"],
            mdd_threshold=cfg["mdd_threshold"],
        )
        h4_summaries.append(summarize_smart_filter_results(h4_w, "h4"))
        h5_summaries.append(summarize_smart_filter_results(h5_w, "H5"))
        combined_summaries.append(
            summarize_smart_filter_results(comb_w, "combined")
        )

    strategy_stats = {
        "h4": _strategy_header_stats(trades_h4, days_span),
        "h5": _strategy_header_stats(trades_h5, days_span),
    }

    verdict, reasoning, next_action = recommend(
        h4_summaries[0], h5_summaries[0], combined_summaries[0],
    )

    print(format_report(
        strategy_stats,
        tuple(h4_summaries),
        tuple(h5_summaries),
        tuple(combined_summaries),
        verdict, reasoning, next_action,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
