"""
Read-only aggregations over a PaperExecutor's state dict.

All functions take raw lists/values, NOT the executor itself, so they are
trivially testable and cannot mutate state.

IMPORTANT: P&L conventions mirror `bot/paper_executor.py` exactly. Do not
introduce new formulas here — we only reshape values for display.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any


def _parse_iso(ts: str) -> datetime:
    """Robust ISO-8601 parse. Forces UTC tzinfo if naive."""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def pnl_today(
    closed_trades: list[dict[str, Any]],
    initial_capital: float,
    *,
    now: datetime | None = None,
) -> tuple[float, float]:
    """
    Sum of pnl_usd across trades whose exit_time falls on today's UTC date.
    Percent is vs initial_capital (stable denominator, matches daily summary).
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    total = 0.0
    for t in closed_trades:
        try:
            ts = _parse_iso(t["exit_time"]).astimezone(timezone.utc).date()
        except (KeyError, ValueError):
            continue
        if ts == today:
            total += float(t.get("pnl_usd", 0.0))
    pct = total / initial_capital * 100.0 if initial_capital else 0.0
    return total, pct


def pnl_total(
    equity: float,
    initial_capital: float,
) -> tuple[float, float]:
    """All-time $ and % P&L: (equity - initial, pct_of_initial)."""
    diff = equity - initial_capital
    pct = diff / initial_capital * 100.0 if initial_capital else 0.0
    return diff, pct


def equity_by_day(
    equity_history: list[dict[str, Any]],
    initial_capital: float,
    days: int = 7,
    *,
    now: datetime | None = None,
) -> list[tuple[date, float]]:
    """
    Return one (date, end_of_day equity) tuple per calendar day for the last
    `days` days. For days before the first equity_history entry we backfill
    with `initial_capital`. Within a day, we take the LAST recorded equity.
    Days with no equity change reuse the previous day's end-of-day value.
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    day_list: list[date] = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]

    # Bucket equity_history entries by UTC date, keep the last value per day.
    by_day: dict[date, float] = {}
    for rec in equity_history:
        try:
            ts = _parse_iso(rec["time"]).astimezone(timezone.utc).date()
            eq = float(rec["equity"])
        except (KeyError, ValueError, TypeError):
            continue
        by_day[ts] = eq  # later entry overwrites earlier → ends up as last

    out: list[tuple[date, float]] = []
    # Prior-day value: we need the last equity BEFORE the first day in our window.
    prior_values = [(d, v) for d, v in by_day.items() if d < day_list[0]]
    if prior_values:
        prior_values.sort()
        running = prior_values[-1][1]
    else:
        running = initial_capital

    for d in day_list:
        if d in by_day:
            running = by_day[d]
        out.append((d, running))
    return out


def best_worst_trade(
    closed_trades: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (best, worst) by pnl_pct. (None, None) for empty input."""
    if not closed_trades:
        return None, None
    by_pct = sorted(closed_trades, key=lambda t: float(t.get("pnl_pct", 0.0)))
    return by_pct[-1], by_pct[0]


def win_rate(closed_trades: list[dict[str, Any]]) -> float:
    """Percent of trades with pnl_pct > 0. 0.0 on empty input."""
    if not closed_trades:
        return 0.0
    wins = sum(1 for t in closed_trades if float(t.get("pnl_pct", 0.0)) > 0)
    return wins / len(closed_trades) * 100.0


def sharpe_ratio(
    closed_trades: list[dict[str, Any]],
    holding_hours: int,
    *,
    min_trades: int = 10,
) -> float | None:
    """
    Annualized Sharpe from per-trade pnl_pct. Matches
    scripts/backtest_liquidation_flush.py:backtest_signal semantics so the
    value printed in /pnl is directly comparable to the backtest.

    Returns None when N < min_trades (noise floor).
    """
    if len(closed_trades) < min_trades or holding_hours <= 0:
        return None
    returns = [float(t.get("pnl_pct", 0.0)) / 100.0 for t in closed_trades]
    mean = sum(returns) / len(returns)
    # Sample std (ddof=1) to match pandas default used in the backtest.
    if len(returns) < 2:
        return None
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    periods_per_year = (365 * 24) / holding_hours
    return mean / std * math.sqrt(periods_per_year)
