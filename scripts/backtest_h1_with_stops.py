#!/usr/bin/env python3
"""
ATR-based TP/SL backtest for the H1 "Long Flush → BUY" signal.

Replaces L2's fixed forward-return measurement with realistic exits:
  - Entry: close of a bar where long_vol_zscore > DEFAULT_THRESHOLDS[coin]
  - TP    = entry + tp_mult × ATR(14)
  - SL    = entry − sl_mult × ATR(14)
  - Walk forward bar-by-bar up to `max_hold`; otherwise exit at close of the
    last in-window bar (reason=timeout).

Same-bar TP+SL resolution: PESSIMISTIC (SL assumed to hit first).
Gap-through-SL: if bar opens below SL, filled at bar.open (worse than SL).
Gap-through-TP (favorable): if bar opens above TP, filled at bar.open.

ATR shifted by 1 bar so the signal bar uses ATR from strictly prior bars.

Entry thresholds are hardcoded from L2 winners; update after walk-forward.

Usage:
    .venv/bin/python scripts/backtest_h1_with_stops.py
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from itertools import product

import ccxt
import numpy as np
import pandas as pd

# Path setup (see walkforward_h1_flush.py for rationale).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)

from collectors.config import binance_ccxt_symbol, get_config  # noqa: E402
from collectors.db import init_pool  # noqa: E402

from backtest_liquidation_flush import compute_signals, load_liquidations  # noqa: E402


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Entry z-thresholds per coin — seeded from L2 in-sample winners.
# Update by hand after running scripts/walkforward_h1_flush.py.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "SOL": 2.0,
    "DOGE": 2.5,
    "LINK": 2.0,
    "AVAX": 2.0,
    "SUI": 2.0,
    "ARB": 2.0,
}

TP_MULTS: list[float] = [1.0, 1.5, 2.0, 2.5]
SL_MULTS: list[float] = [0.5, 0.75, 1.0, 1.5]
MAX_HOLDS: list[int] = [2, 3, 4, 6]   # bars of 4H → 8h, 12h, 16h, 24h
ATR_PERIOD: int = 14
MIN_TRADES: int = 10                  # configs with fewer are flagged


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def fetch_klines_4h_ohlc(ccxt_symbol: str, since_ms: int) -> pd.DataFrame:
    """
    Fetch full 4H OHLC candles from Binance futures via ccxt.

    Mirrors the paginated loop in backtest_liquidation_flush.fetch_klines_4h
    but keeps open/high/low/close columns (that helper keeps only close).
    """
    exchange = ccxt.binance({"options": {"defaultType": "swap"}})
    all_rows: list[list] = []
    cursor = since_ms
    while True:
        batch = exchange.fetch_ohlcv(
            ccxt_symbol, timeframe="4h", since=cursor, limit=1000
        )
        if not batch:
            break
        all_rows.extend(batch)
        cursor = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    if not all_rows:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "vol"])
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("timestamp")[["open", "high", "low", "close"]]


def add_atr(ohlc: pd.DataFrame, period: int = ATR_PERIOD) -> pd.DataFrame:
    """
    Append an `atr` column. True Range = max(H-L, |H-prevC|, |prevC-L|);
    ATR = rolling mean over `period`. Shifted by 1 bar so a signal on bar t
    uses ATR computed from bars strictly before t (no look-ahead).
    """
    prev_close = ohlc["close"].shift(1)
    tr = pd.concat(
        [
            ohlc["high"] - ohlc["low"],
            (ohlc["high"] - prev_close).abs(),
            (prev_close - ohlc["low"]).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period).mean().shift(1)
    return ohlc.assign(atr=atr)


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------


@dataclass
class Trade:
    entry_ts: pd.Timestamp
    entry_price: float
    exit_ts: pd.Timestamp
    exit_price: float
    reason: str
    bars: int

    @property
    def pnl_pct(self) -> float:
        return (self.exit_price - self.entry_price) / self.entry_price * 100


def simulate_trade(
    ohlc: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    atr: float,
    tp_mult: float,
    sl_mult: float,
    max_hold: int,
) -> Trade | None:
    """
    Walk bars forward from entry_idx+1 until TP, SL, or timeout.

    Pessimistic same-bar resolution: if high≥TP and low≤SL on the same bar,
    SL is considered hit first. Gap-through handled explicitly at bar.open.
    Returns None if there aren't at least max_hold bars remaining to simulate.
    """
    tp = entry_price + tp_mult * atr
    sl = entry_price - sl_mult * atr
    last_idx = min(entry_idx + max_hold, len(ohlc) - 1)
    if last_idx <= entry_idx:
        return None

    for i in range(entry_idx + 1, last_idx + 1):
        bar = ohlc.iloc[i]
        bar_ts = ohlc.index[i]

        # Gap-through-SL: opened below stop → filled at open (worse than sl).
        if bar["open"] <= sl:
            return Trade(
                entry_ts=ohlc.index[entry_idx],
                entry_price=entry_price,
                exit_ts=bar_ts,
                exit_price=float(bar["open"]),
                reason="SL_gap",
                bars=i - entry_idx,
            )
        # Gap-through-TP (favorable).
        if bar["open"] >= tp:
            return Trade(
                entry_ts=ohlc.index[entry_idx],
                entry_price=entry_price,
                exit_ts=bar_ts,
                exit_price=float(bar["open"]),
                reason="TP_gap",
                bars=i - entry_idx,
            )
        # Pessimistic intrabar: SL before TP.
        if bar["low"] <= sl:
            return Trade(
                entry_ts=ohlc.index[entry_idx],
                entry_price=entry_price,
                exit_ts=bar_ts,
                exit_price=float(sl),
                reason="SL",
                bars=i - entry_idx,
            )
        if bar["high"] >= tp:
            return Trade(
                entry_ts=ohlc.index[entry_idx],
                entry_price=entry_price,
                exit_ts=bar_ts,
                exit_price=float(tp),
                reason="TP",
                bars=i - entry_idx,
            )

    # Timeout: close of last bar in window.
    return Trade(
        entry_ts=ohlc.index[entry_idx],
        entry_price=entry_price,
        exit_ts=ohlc.index[last_idx],
        exit_price=float(ohlc.iloc[last_idx]["close"]),
        reason="timeout",
        bars=last_idx - entry_idx,
    )


def compute_metrics(trades: list[Trade]) -> dict | None:
    """Return aggregate metrics for a list of trades, or None if empty."""
    if not trades:
        return None
    pnl = np.array([t.pnl_pct for t in trades])
    n = len(trades)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    win_pct = float((pnl > 0).mean() * 100)
    avg_pct = float(pnl.mean())

    # Profit factor — abs(sum wins) / abs(sum losses)
    sum_win = wins.sum() if wins.size else 0.0
    sum_loss = abs(losses.sum()) if losses.size else 0.0
    if sum_loss > 0:
        pf = sum_win / sum_loss
    else:
        pf = float("inf") if sum_win > 0 else 0.0

    # Max drawdown on cumulative equity (sum of per-trade pct returns; simple
    # single-unit compounding not used — this is a percentage-points curve).
    equity = np.cumsum(pnl)
    peak = np.maximum.accumulate(equity)
    dd = equity - peak
    max_dd = float(dd.min()) if dd.size else 0.0

    # Sharpe — annualize using the actual mean holding hours.
    mean_bars = float(np.mean([t.bars for t in trades]))
    mean_hours = max(mean_bars * 4.0, 1.0)
    std = float(pnl.std())
    if std > 0:
        periods_per_year = 365 * 24 / mean_hours
        sharpe = (avg_pct / std) * np.sqrt(periods_per_year)
    else:
        sharpe = 0.0

    reasons = {r: 0 for r in ("TP", "SL", "TP_gap", "SL_gap", "timeout")}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1

    return {
        "n": n,
        "win_pct": round(win_pct, 1),
        "avg_pct": round(avg_pct, 2),
        "pf": round(pf, 2) if np.isfinite(pf) else pf,
        "max_dd": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "mean_hold_bars": round(mean_bars, 2),
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Per-coin backtest
# ---------------------------------------------------------------------------


def backtest_coin(coin: str, z_threshold: float) -> None:
    print(f"\n{'=' * 78}")
    print(f"ATR-BASED TP/SL: {coin}  (entry: long_vol_zscore > {z_threshold})")
    print(f"{'=' * 78}")

    liq_df = load_liquidations(coin)
    if liq_df.empty:
        print(f"  No liquidation data for {coin} — skipped.")
        return

    since_ms = int(liq_df["timestamp"].min().timestamp() * 1000)
    ccxt_symbol = binance_ccxt_symbol(coin)
    try:
        ohlc = fetch_klines_4h_ohlc(ccxt_symbol, since_ms)
    except Exception as e:
        print(f"  Failed to fetch klines ({ccxt_symbol}): {e}")
        return
    if ohlc.empty:
        print(f"  No price data for {coin} — skipped.")
        return

    # Add ATR (shifted) to OHLC frame.
    ohlc = add_atr(ohlc, ATR_PERIOD)

    # Build signal DataFrame using only close for compute_signals.
    price_df = ohlc[["close"]].rename(columns={"close": "price"})
    sig = compute_signals(liq_df, price_df)
    if sig.empty:
        print(f"  No aligned data for {coin} — skipped.")
        return

    # Align indexes — sig index is a subset of ohlc index (both 4H UTC).
    # Use the ohlc frame for trade simulation; it has high/low/atr per bar.
    # Find signal bars where zscore > threshold AND atr is known.
    entry_mask = (sig["long_vol_zscore"] > z_threshold)
    entry_ts = sig.index[entry_mask]
    # Only keep entries that exist in ohlc with a non-NaN atr.
    common = entry_ts.intersection(ohlc.index)
    entries = [ts for ts in common if not pd.isna(ohlc.at[ts, "atr"])]

    print(
        f"  Data: OHLC {len(ohlc)} bars, {ohlc.index.min().date()} → "
        f"{ohlc.index.max().date()}"
    )
    print(f"  Entries triggered: {len(entries)} (z > {z_threshold}, ATR available)")
    if len(entries) < MIN_TRADES:
        print(f"  Fewer than MIN_TRADES={MIN_TRADES} entries — skipping grid.")
        return

    # Precompute integer positions for entries.
    ts_to_pos = {ts: i for i, ts in enumerate(ohlc.index)}
    entry_positions = [ts_to_pos[ts] for ts in entries]

    # Grid search over TP×SL×hold.
    all_results: list[tuple[tuple[float, float, int], dict]] = []
    for tp_mult, sl_mult, max_hold in product(TP_MULTS, SL_MULTS, MAX_HOLDS):
        trades: list[Trade] = []
        for pos in entry_positions:
            entry_price = float(ohlc.iloc[pos]["close"])
            atr = float(ohlc.iloc[pos]["atr"])
            t = simulate_trade(
                ohlc, pos, entry_price, atr, tp_mult, sl_mult, max_hold
            )
            if t is not None:
                trades.append(t)
        metrics = compute_metrics(trades)
        if metrics is None or metrics["n"] < MIN_TRADES:
            continue
        all_results.append(((tp_mult, sl_mult, max_hold), metrics))

    if not all_results:
        print("  No configs produced enough trades — skipping.")
        return

    # Sort by Sharpe desc; print top 10.
    all_results.sort(key=lambda x: x[1]["sharpe"], reverse=True)
    top = all_results[:10]

    print()
    print("  Top 10 configs by Sharpe:")
    print(
        f"  {'TP×ATR':>7} {'SL×ATR':>7} {'Hold':>5} {'N':>4} "
        f"{'Win%':>6} {'Avg%':>7} {'PF':>6} {'MaxDD%':>8} {'Sharpe':>7}"
    )
    print("  " + "-" * 70)
    for (tp, sl, hold), m in top:
        pf_disp = f"{m['pf']:.2f}" if np.isfinite(m["pf"]) else "inf"
        print(
            f"  {tp:>7.2f} {sl:>7.2f} {hold * 4:>3}h "
            f"{m['n']:>4} {m['win_pct']:>5.1f}% {m['avg_pct']:>6.2f}% "
            f"{pf_disp:>6} {m['max_dd']:>7.2f}% {m['sharpe']:>7.2f}"
        )

    # Best by Sharpe — summary line + reason breakdown.
    (best_tp, best_sl, best_hold), best_m = top[0]
    print()
    print(
        f"  Best config (recommended for paper trade): "
        f"TP={best_tp}×ATR, SL={best_sl}×ATR, timeout={best_hold * 4}h"
    )
    r = best_m["reasons"]
    print(
        f"  Exit-reason breakdown: TP={r.get('TP', 0)}  SL={r.get('SL', 0)}  "
        f"TP_gap={r.get('TP_gap', 0)}  SL_gap={r.get('SL_gap', 0)}  "
        f"timeout={r.get('timeout', 0)}   "
        f"mean_hold={best_m['mean_hold_bars']} bars ({best_m['mean_hold_bars'] * 4:.1f}h)"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    init_pool(get_config())

    print("H1 FLUSH — ATR-BASED TP/SL BACKTEST")
    print("=" * 78)
    print("Data: CoinGlass 4H liquidations + Binance 4H OHLC (ccxt)")
    print(
        f"Grid: TP×ATR ∈ {TP_MULTS}   SL×ATR ∈ {SL_MULTS}   "
        f"max_hold ∈ {[h * 4 for h in MAX_HOLDS]}h (→ {len(TP_MULTS)*len(SL_MULTS)*len(MAX_HOLDS)} configs/coin)"
    )
    print(f"ATR: {ATR_PERIOD}-period, shifted +1 bar (no look-ahead)")
    print("Same-bar TP+SL → pessimistic (SL first). Gap-through → fill at bar.open.")
    print(
        "Entry thresholds (update after walk-forward):\n    "
        + "  ".join(f"{c}={z}" for c, z in DEFAULT_THRESHOLDS.items())
    )

    for coin, z in DEFAULT_THRESHOLDS.items():
        try:
            backtest_coin(coin, z)
        except Exception as e:
            print(f"\n  Error processing {coin}: {e}")


if __name__ == "__main__":
    main()
