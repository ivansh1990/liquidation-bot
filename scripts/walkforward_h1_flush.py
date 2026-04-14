#!/usr/bin/env python3
"""
Walk-forward validation of H1 "Long Flush → BUY" signal.

Method:
  - Split full timeline into 6 equal folds.
  - Fold 0 = train only. Folds 1..5 = out-of-sample (OOS).
  - For each OOS fold, train on ALL prior folds (expanding window).
  - On train, grid-search (z_threshold × holding_hours) to pick the combo
    with the highest Sharpe (requires N ≥ 5 on train).
  - If no combo meets the min-N gate, fall back to z=2.0, h=8 (L2 consensus).
  - Apply the chosen combo on OOS and record metrics.
  - Report per-fold table + aggregated OOS (pooled returns, single Sharpe).

Coins: SOL, DOGE, LINK, AVAX, SUI, ARB.
Skipped: BTC, ETH (L2 showed no edge).

PASS criterion per coin:
  ≥ 4/5 OOS folds with positive Sharpe AND
  Pooled OOS Sharpe > 0.5 AND
  Pooled OOS win rate > 55%.

Usage:
    .venv/bin/python scripts/walkforward_h1_flush.py
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Add project root so `collectors` is importable, and `scripts/` so we can
# import from the locked L2 baseline module.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)

from collectors.config import binance_ccxt_symbol, get_config  # noqa: E402
from collectors.db import init_pool  # noqa: E402

from backtest_liquidation_flush import (  # noqa: E402
    compute_signals,
    fetch_klines_4h,
    load_liquidations,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COINS: list[str] = ["SOL", "DOGE", "LINK", "AVAX", "SUI", "ARB"]
N_FOLDS: int = 6
Z_GRID: list[float] = [1.0, 1.5, 2.0, 2.5, 3.0]
H_GRID: list[int] = [4, 8, 12]
MIN_TRAIN_N: int = 5
FALLBACK_Z: float = 2.0
FALLBACK_H: int = 8

# PASS thresholds
PASS_POS_FOLDS: int = 4  # out of 5 OOS folds
PASS_POOLED_SHARPE: float = 0.5
PASS_POOLED_WIN_PCT: float = 55.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class Combo:
    z: float
    h: int
    train_sharpe: float | None  # None if fallback was used
    train_n: int
    fallback: bool


@dataclass
class FoldRow:
    fold: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp
    combo: Combo
    oos_n: int
    oos_win_pct: float
    oos_avg_pct: float
    oos_sharpe: float
    oos_trades: pd.Series  # raw returns for pooling


def split_folds(index: pd.DatetimeIndex, n_folds: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Split a time index into n equal-time contiguous folds."""
    ts = index.min()
    te = index.max()
    edges = pd.date_range(ts, te, periods=n_folds + 1)
    return [(edges[i], edges[i + 1]) for i in range(n_folds)]


def _trades_for_combo(df: pd.DataFrame, z: float, h: int) -> pd.Series:
    """Return the raw forward returns for entries where long_vol_zscore > z."""
    col = f"return_{h}h"
    if col not in df.columns:
        return pd.Series(dtype=float)
    mask = df["long_vol_zscore"] > z
    return df.loc[mask, col].dropna()


def _metrics(trades: pd.Series, forward_hours: int) -> dict:
    """Compute n, win%, avg%, annualized Sharpe for a trade return series."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_pct": 0.0, "avg_pct": 0.0, "sharpe": 0.0}
    win_pct = float((trades > 0).mean() * 100)
    avg_pct = float(trades.mean())
    std = float(trades.std())
    if std > 0 and forward_hours > 0:
        periods_per_year = 365 * 24 / forward_hours
        sharpe = (avg_pct / std) * np.sqrt(periods_per_year)
    else:
        sharpe = 0.0
    return {
        "n": n,
        "win_pct": round(win_pct, 1),
        "avg_pct": round(avg_pct, 2),
        "sharpe": round(sharpe, 2),
    }


def grid_search_best(train_df: pd.DataFrame) -> Combo:
    """Find (z, h) with highest Sharpe on train, N ≥ MIN_TRAIN_N.

    Falls back to (FALLBACK_Z, FALLBACK_H) if no combo qualifies.
    """
    best: Combo | None = None
    best_sharpe = -np.inf
    for z in Z_GRID:
        for h in H_GRID:
            trades = _trades_for_combo(train_df, z, h)
            if len(trades) < MIN_TRAIN_N:
                continue
            m = _metrics(trades, h)
            if m["sharpe"] > best_sharpe:
                best_sharpe = m["sharpe"]
                best = Combo(z=z, h=h, train_sharpe=m["sharpe"], train_n=m["n"], fallback=False)
    if best is None:
        return Combo(
            z=FALLBACK_Z,
            h=FALLBACK_H,
            train_sharpe=None,
            train_n=0,
            fallback=True,
        )
    return best


def apply_combo_on_oos(oos_df: pd.DataFrame, combo: Combo) -> tuple[dict, pd.Series]:
    """Return OOS metrics + raw trade-return Series for pooling."""
    trades = _trades_for_combo(oos_df, combo.z, combo.h)
    return _metrics(trades, combo.h), trades


# ---------------------------------------------------------------------------
# Per-coin walk-forward
# ---------------------------------------------------------------------------


def walkforward_symbol(coin: str) -> dict | None:
    """Run 6-fold walk-forward for one coin. Returns summary dict or None."""
    print(f"\n{'=' * 78}")
    print(f"WALK-FORWARD: {coin}")
    print(f"{'=' * 78}")

    liq_df = load_liquidations(coin)
    if liq_df.empty:
        print(f"  No liquidation data for {coin} — skipped.")
        return None

    since_ms = int(liq_df["timestamp"].min().timestamp() * 1000)
    ccxt_symbol = binance_ccxt_symbol(coin)
    try:
        price_df = fetch_klines_4h(ccxt_symbol, since_ms)
    except Exception as e:
        print(f"  Failed to fetch klines ({ccxt_symbol}): {e}")
        return None
    if price_df.empty:
        print(f"  No price data for {coin} — skipped.")
        return None

    df = compute_signals(liq_df, price_df)
    if df.empty:
        print(f"  No aligned data for {coin} — skipped.")
        return None

    print(f"  Data: {len(df)} periods, {df.index.min().date()} → {df.index.max().date()}")

    fold_bounds = split_folds(df.index, N_FOLDS)

    # Header
    print()
    print(
        f"  {'Fold':<5} {'Train period':<25} {'Selected':<12} "
        f"{'TrN':>4} {'OOS period':<25} {'OOS_N':>5} "
        f"{'Win%':>6} {'Avg%':>7} {'Sharpe':>7}  Note"
    )
    print("  " + "-" * 110)

    rows: list[FoldRow] = []
    for k in range(1, N_FOLDS):  # folds 1..5 are OOS
        oos_start, oos_end = fold_bounds[k]
        # expanding-window train = everything before oos_start
        train = df.loc[df.index < oos_start]
        oos = df.loc[(df.index >= oos_start) & (df.index < oos_end)]
        train_start = df.index.min() if not train.empty else oos_start
        train_end = train.index.max() if not train.empty else oos_start

        combo = grid_search_best(train)
        oos_metrics, oos_trades = apply_combo_on_oos(oos, combo)

        row = FoldRow(
            fold=k,
            train_start=train_start,
            train_end=train_end,
            oos_start=oos_start,
            oos_end=oos_end,
            combo=combo,
            oos_n=oos_metrics["n"],
            oos_win_pct=oos_metrics["win_pct"],
            oos_avg_pct=oos_metrics["avg_pct"],
            oos_sharpe=oos_metrics["sharpe"],
            oos_trades=oos_trades,
        )
        rows.append(row)

        sel = f"z>{combo.z}/{combo.h}h"
        note = "fallback" if combo.fallback else ""
        train_period = f"{train_start.date()} → {train_end.date()}"
        oos_period = f"{oos_start.date()} → {oos_end.date()}"
        print(
            f"  {k:<5} {train_period:<25} {sel:<12} "
            f"{combo.train_n:>4} {oos_period:<25} {row.oos_n:>5} "
            f"{row.oos_win_pct:>5.1f}% {row.oos_avg_pct:>6.2f}% {row.oos_sharpe:>7.2f}  {note}"
        )

    # Aggregated (pooled) OOS
    all_trades = pd.concat([r.oos_trades for r in rows]) if rows else pd.Series(dtype=float)
    # For pooled Sharpe, annualize by the *mean* holding hours across folds
    # (weighted by trade count). If all folds used the same h, this is exact.
    if len(all_trades) > 0:
        weighted_h = sum(r.combo.h * r.oos_n for r in rows) / len(all_trades)
    else:
        weighted_h = FALLBACK_H
    pooled = _metrics(all_trades, int(round(weighted_h)))

    positive_folds = sum(1 for r in rows if r.oos_sharpe > 0)

    print()
    print(
        f"  Aggregated OOS (pooled): N={pooled['n']}  "
        f"Win={pooled['win_pct']}%  Avg={pooled['avg_pct']}%  Sharpe={pooled['sharpe']}"
    )
    print(f"  Positive folds: {positive_folds}/{len(rows)}")

    passes = (
        positive_folds >= PASS_POS_FOLDS
        and pooled["sharpe"] > PASS_POOLED_SHARPE
        and pooled["win_pct"] > PASS_POOLED_WIN_PCT
    )
    verdict = "PASS" if passes else "FAIL"
    print(f"  VERDICT: {verdict}")

    return {
        "coin": coin,
        "oos_n": pooled["n"],
        "win_pct": pooled["win_pct"],
        "avg_pct": pooled["avg_pct"],
        "sharpe": pooled["sharpe"],
        "positive_folds": positive_folds,
        "total_folds": len(rows),
        "passes": passes,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def print_portfolio_summary(summaries: list[dict]) -> None:
    print(f"\n{'=' * 78}")
    print("WALK-FORWARD PORTFOLIO SUMMARY")
    print(f"{'=' * 78}")
    print(
        f"  {'Coin':<6} {'OOS_N':>6} {'Win%':>6} {'Avg%':>7} {'Sharpe':>7} "
        f"{'PosFolds':>9}  Verdict"
    )
    print("  " + "-" * 60)
    passing: list[str] = []
    failing: list[str] = []
    for s in summaries:
        verdict = "PASS" if s["passes"] else "FAIL"
        print(
            f"  {s['coin']:<6} {s['oos_n']:>6} {s['win_pct']:>5.1f}% "
            f"{s['avg_pct']:>6.2f}% {s['sharpe']:>7.2f} "
            f"{s['positive_folds']:>4}/{s['total_folds']:<4}  {verdict}"
        )
        (passing if s["passes"] else failing).append(s["coin"])

    print()
    if passing:
        print(f"  Passing coins → proceed to ATR backtest: {', '.join(passing)}")
    if failing:
        print(f"  Failing coins → drop from paper-trade candidates: {', '.join(failing)}")

    print()
    print("  Risk notes:")
    print("    • Grid search on thin train folds is noisy; fallback rows flagged above.")
    print("    • Per-coin Sharpes correlated via crypto beta — pooled-across-coins Sharpe")
    print("      intentionally not computed.")


def main() -> None:
    init_pool(get_config())

    print("H1 FLUSH — WALK-FORWARD VALIDATION")
    print("=" * 78)
    print("Data: CoinGlass 4H liquidations + Binance 4H klines")
    print(
        f"Folds: {N_FOLDS} (fold 0=train only, folds 1..{N_FOLDS - 1}=OOS, expanding window)"
    )
    print(f"Grid:  z ∈ {Z_GRID}   h ∈ {H_GRID}   min N={MIN_TRAIN_N}")
    print(f"Fallback: z={FALLBACK_Z}, h={FALLBACK_H} (L2 consensus)")
    print(
        f"PASS:  ≥{PASS_POS_FOLDS}/{N_FOLDS - 1} positive folds, "
        f"pooled Sharpe>{PASS_POOLED_SHARPE}, pooled win%>{PASS_POOLED_WIN_PCT}"
    )

    summaries: list[dict] = []
    for coin in COINS:
        try:
            s = walkforward_symbol(coin)
        except Exception as e:
            print(f"\n  Error processing {coin}: {e}")
            continue
        if s is not None:
            summaries.append(s)

    if summaries:
        print_portfolio_summary(summaries)


if __name__ == "__main__":
    main()
