#!/usr/bin/env python3
"""
L3b-2: Combo signal backtest.

Tests combined filters (flush + OI drop + price drawdown + cross-coin breadth +
normalized-to-OI scale + funding + volume) to see if any combo survives where
individual signals failed.

Data sources (all 4H unless noted, ~167-day horizon):
  - coinglass_liquidations  (long/short liquidated USD)
  - coinglass_oi            (aggregated open interest OHLC)
  - coinglass_funding       (8H funding rate, ffill'd to 4H)
  - Binance 4H klines via ccxt (OHLCV)

Reuses locked L2 helpers (load_liquidations, compute_signals) and the L3
split_folds helper; only OI/funding loaders, cross-coin features, and the
combo filter engine are new.

Usage:
    .venv/bin/python scripts/backtest_combo.py | tee analysis/combo_L3b.txt
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import ccxt
import numpy as np
import pandas as pd

# Path setup so sibling scripts and the `collectors` package both import.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)

from collectors.config import COINS, binance_ccxt_symbol, get_config  # noqa: E402
from collectors.db import get_conn, init_pool  # noqa: E402

from backtest_liquidation_flush import (  # noqa: E402
    compute_signals,
    load_liquidations,
)
from walkforward_h1_flush import split_folds  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOLDING_HOURS: list[int] = [4, 8, 12, 24]
ZSCORE_WINDOW: int = 90
ATR_PERIOD: int = 14
CROSS_COIN_FLUSH_Z: float = 1.5   # threshold used to count "flushing" coins
WF_FOLDS: int = 4
WF_MIN_TRADES: int = 30
RANK_HOLDING_HOURS: int = 8

# PEPE may be stored as "PEPE" or "1000PEPE" in coinglass_* tables
# (backfill tried primary first, fell back to the latter).
PEPE_ALIASES: list[str] = ["PEPE", "1000PEPE"]


# ---------------------------------------------------------------------------
# Combo definitions (fixed thresholds — no in-sample tuning per L3b-2 spec)
# ---------------------------------------------------------------------------

COMBOS: dict[str, dict] = {
    "baseline_flush": {
        "filters": [("long_vol_zscore", ">", 2.0)],
        "description": "Simple flush z>2.0 (L2 baseline)",
    },
    "capitulation": {
        "filters": [
            ("long_vol_zscore", ">", 1.5),
            ("oi_change_6", "<", -3.0),
            ("drawdown_24h", "<", -3.0),
        ],
        "description": "Flush + OI dropping + price dropping = real capitulation",
    },
    "normalized_flush": {
        "filters": [
            ("liq_oi_zscore", ">", 2.0),
        ],
        "description": "Liquidation volume abnormally large relative to OI",
    },
    "market_flush": {
        "filters": [
            ("long_vol_zscore", ">", 1.0),
            ("n_coins_flushing", ">=", 4),
        ],
        "description": "This coin flushing + 4+ other coins flushing = deleveraging",
    },
    "double_flush": {
        "filters": [
            ("long_vol_zscore", ">", 1.5),
            ("long_vol_zscore_prev", ">", 1.0),
        ],
        "description": "Two consecutive flushes = deeper capitulation",
    },
    "flush_extreme_funding": {
        "filters": [
            ("long_vol_zscore", ">", 1.5),
            ("funding_rate", "<", -0.0003),
        ],
        "description": "Flush when funding extremely negative = max bearish sentiment",
    },
    "full_capitulation": {
        "filters": [
            ("long_vol_zscore", ">", 1.5),
            ("oi_change_6", "<", -2.0),
            ("drawdown_24h", "<", -2.0),
            ("n_coins_flushing", ">=", 3),
        ],
        "description": "Full market capitulation — all signals aligned",
    },
    "normalized_market": {
        "filters": [
            ("liq_oi_zscore", ">", 1.5),
            ("n_coins_flushing", ">=", 3),
        ],
        "description": "Large relative flush across multiple coins",
    },
    "flush_volume_spike": {
        "filters": [
            ("long_vol_zscore", ">", 1.5),
            ("volume_zscore", ">", 1.5),
        ],
        "description": "Flush accompanied by trading volume spike = real panic",
    },
}


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _try_load_with_pepe_fallback(coin: str, loader) -> tuple[str, pd.DataFrame]:
    """
    For PEPE, try the canonical symbol first, then the 1000PEPE fallback.
    Returns (used_symbol, df). For other coins, uses the coin name directly.
    """
    if coin == "PEPE":
        for alias in PEPE_ALIASES:
            df = loader(alias)
            if not df.empty:
                return alias, df
        return PEPE_ALIASES[0], df  # empty
    df = loader(coin)
    return coin, df


def load_oi(symbol: str) -> pd.DataFrame:
    """Load coinglass_oi for one symbol; DF indexed by UTC timestamp."""
    with get_conn() as conn:
        df = pd.read_sql(
            """
            SELECT timestamp, open_interest, oi_high, oi_low
            FROM coinglass_oi
            WHERE symbol = %s
            ORDER BY timestamp
            """,
            conn,
            params=(symbol,),
            parse_dates=["timestamp"],
        )
    if df.empty:
        return df
    df = df.set_index("timestamp").sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def load_funding(symbol: str) -> pd.DataFrame:
    """Load coinglass_funding for one symbol; DF indexed by UTC timestamp."""
    with get_conn() as conn:
        df = pd.read_sql(
            """
            SELECT timestamp, funding_rate
            FROM coinglass_funding
            WHERE symbol = %s
            ORDER BY timestamp
            """,
            conn,
            params=(symbol,),
            parse_dates=["timestamp"],
        )
    if df.empty:
        return df
    df = df.set_index("timestamp").sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def fetch_klines_4h_ohlcv(ccxt_symbol: str, since_ms: int) -> pd.DataFrame:
    """
    Fetch paginated 4H OHLCV (with volume) from Binance futures.

    Mirrors fetch_klines_4h_ohlc in backtest_h1_with_stops.py but keeps volume.
    Returns DF indexed by UTC timestamp with [open, high, low, close, volume].
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
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------------
# Feature helpers
# ---------------------------------------------------------------------------

def _zscore(series: pd.Series, window: int = ZSCORE_WINDOW) -> pd.Series:
    return (series - series.rolling(window).mean()) / series.rolling(window).std()


def _add_atr(ohlc: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """
    ATR(period), shifted +1 bar (no look-ahead). Same formula as
    backtest_h1_with_stops.add_atr but returns the series directly.
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
    return tr.rolling(period).mean().shift(1)


# ---------------------------------------------------------------------------
# Per-coin feature assembly
# ---------------------------------------------------------------------------

def build_features(
    coin: str,
    liq_used_symbol: str,
    liq_df: pd.DataFrame,
    oi_df: pd.DataFrame,
    funding_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Merge all inputs into a single 4H-indexed feature DataFrame for one coin.
    Adds every column any of the 9 combos references (cross-coin columns
    are injected afterward by compute_cross_coin_features).
    """
    if liq_df.empty or ohlcv_df.empty:
        return pd.DataFrame()

    # L2's compute_signals returns UTC-indexed DF with long/short_vol_usd,
    # long/short_vol_zscore, total_vol, ratio_24h, price, return_{4,8,12,24}h.
    # It expects liq_df as a plain DF (timestamp column) and price_df indexed
    # by timestamp with a `price` column.
    price_df = ohlcv_df[["close"]].rename(columns={"close": "price"})
    df = compute_signals(liq_df, price_df)

    # Bring OHLC columns in on the same index for ATR / drawdown / volume.
    ohlc = ohlcv_df.reindex(df.index, method="nearest")
    df["atr"] = _add_atr(ohlc)
    df["volume"] = ohlc["volume"]
    df["volume_zscore"] = _zscore(df["volume"])

    # Drawdown: cumulative 24h pct change (price over 6 periods, past-looking).
    df["drawdown_24h"] = df["price"].pct_change(6) * 100

    # Open interest: exact 4H match on timestamp (CoinGlass 4H grid).
    if not oi_df.empty:
        oi_aligned = oi_df.reindex(df.index, method="ffill")
        df["oi"] = oi_aligned["open_interest"]
        df["oi_change_1"] = df["oi"].pct_change(1) * 100
        df["oi_change_6"] = df["oi"].pct_change(6) * 100
        # Normalized flush: total liq USD relative to market OI.
        df["liq_oi_ratio"] = df["total_vol"] / df["oi"].replace(0, np.nan)
        df["liq_oi_zscore"] = _zscore(df["liq_oi_ratio"])
    else:
        df["oi"] = np.nan
        df["oi_change_1"] = np.nan
        df["oi_change_6"] = np.nan
        df["liq_oi_ratio"] = np.nan
        df["liq_oi_zscore"] = np.nan

    # Funding: h8 → ffill to 4H grid. Values at 00/08/16 UTC propagate through
    # intermediate 04/12/20 UTC bars until the next funding tick.
    if not funding_df.empty:
        fund_aligned = funding_df.reindex(df.index, method="ffill")
        df["funding_rate"] = fund_aligned["funding_rate"]
        df["funding_extreme"] = df["funding_rate"].abs() > 0.0005
    else:
        df["funding_rate"] = np.nan
        df["funding_extreme"] = False

    # Previous-bar zscore for double_flush combo.
    df["long_vol_zscore_prev"] = df["long_vol_zscore"].shift(1)

    # Sanity: attach coin name for aggregation later.
    df["coin"] = coin

    # Drop rows with no price; everything else can be selectively NaN and the
    # combo filters will just not fire on those rows.
    return df.dropna(subset=["price"])


# ---------------------------------------------------------------------------
# Cross-coin features
# ---------------------------------------------------------------------------

def compute_cross_coin_features(
    all_coin_dfs: dict[str, pd.DataFrame],
    flush_z: float = CROSS_COIN_FLUSH_Z,
) -> dict[str, pd.DataFrame]:
    """
    For each timestamp, count how many coins have long_vol_zscore > flush_z
    and sum total_vol across all coins. Merge back into each per-coin DF.

    Per the spec's sample code, n_coins_flushing is inclusive of the current
    coin (self-counting on the assumption that a market-wide event should
    include this coin too).
    """
    if not all_coin_dfs:
        return all_coin_dfs

    z_wide = pd.DataFrame(
        {c: df["long_vol_zscore"] for c, df in all_coin_dfs.items() if not df.empty}
    )
    liq_wide = pd.DataFrame(
        {c: df["total_vol"] for c, df in all_coin_dfs.items() if not df.empty}
    )

    n_flushing = (z_wide > flush_z).sum(axis=1)
    market_total = liq_wide.sum(axis=1)

    result: dict[str, pd.DataFrame] = {}
    for coin, df in all_coin_dfs.items():
        if df.empty:
            result[coin] = df
            continue
        df = df.copy()
        df["n_coins_flushing"] = n_flushing.reindex(df.index).fillna(0).astype(int)
        df["market_liq_total"] = market_total.reindex(df.index).fillna(0.0)
        result[coin] = df
    return result


# ---------------------------------------------------------------------------
# Combo engine
# ---------------------------------------------------------------------------

def apply_combo(df: pd.DataFrame, filters: list[tuple]) -> pd.Series:
    """Return boolean mask where ALL filter conditions are met."""
    mask = pd.Series(True, index=df.index)
    for col, op, thresh in filters:
        if col not in df.columns:
            return pd.Series(False, index=df.index)
        series = df[col]
        if op == ">":
            cond = series > thresh
        elif op == "<":
            cond = series < thresh
        elif op == ">=":
            cond = series >= thresh
        elif op == "<=":
            cond = series <= thresh
        elif op == "==":
            cond = series == thresh
        else:
            raise ValueError(f"Unsupported operator: {op}")
        # NaN propagates as False; no signal fires on undefined features.
        mask &= cond.fillna(False)
    return mask


def _metrics_for_trades(trades: pd.Series, forward_hours: int) -> Optional[dict]:
    if len(trades) < 5:
        return None
    win_rate = (trades > 0).mean() * 100
    avg_ret = trades.mean()
    std = trades.std()
    sharpe = (
        (avg_ret / std) * np.sqrt(365 * 24 / forward_hours) if std and std > 0 else 0.0
    )
    return {
        "n": int(len(trades)),
        "win_pct": round(win_rate, 1),
        "avg_ret": round(avg_ret, 2),
        "sharpe": round(sharpe, 2),
    }


def test_combo(
    df: pd.DataFrame,
    filters: list[tuple],
    holding_hours: list[int] = HOLDING_HOURS,
) -> dict:
    """
    Apply one combo and compute metrics for each holding period.

    Returns:
        {
            "n_signals": int,
            "h{H}": {n, win_pct, avg_ret, sharpe} | None  for each H
        }
    """
    mask = apply_combo(df, filters)
    result: dict = {"n_signals": int(mask.sum())}
    for h in holding_hours:
        col = f"return_{h}h"
        if col not in df.columns:
            result[f"h{h}"] = None
            continue
        trades = df.loc[mask, col].dropna()
        result[f"h{h}"] = _metrics_for_trades(trades, h)
    return result


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

def _fmt_cell(metrics: Optional[dict]) -> str:
    if metrics is None:
        return "         -        "
    return f"{metrics['n']:>3} {metrics['win_pct']:>4.1f}% {metrics['avg_ret']:>+5.2f}%"


def print_coin_table(coin: str, df: pd.DataFrame, combo_results: dict[str, dict]) -> None:
    print()
    print("=" * 72)
    print(f"COMBO BACKTEST: {coin}")
    print("=" * 72)
    if df.empty:
        print("  No data available.")
        return
    start = df.index.min().strftime("%Y-%m-%d")
    end = df.index.max().strftime("%Y-%m-%d")
    print(f"Data: {len(df)} periods, {start} → {end}")
    print()
    header = f"  {'Combo':<24} {'Signals':>8}  | "
    header += "    →4h              →8h              →12h             →24h"
    print(header)
    print(f"  {'-'*24} {'-'*8}  | " + "    N   Win%   Avg     " * 4)
    for name, result in combo_results.items():
        cells = "    ".join(_fmt_cell(result[f"h{h}"]) for h in HOLDING_HOURS)
        print(f"  {name:<24} {result['n_signals']:>8}  |  {cells}")


# ---------------------------------------------------------------------------
# Portfolio aggregation
# ---------------------------------------------------------------------------

def aggregate_combo_across_coins(
    all_coin_dfs: dict[str, pd.DataFrame],
    combo_filters: list[tuple],
    holding_hours: int = RANK_HOLDING_HOURS,
) -> dict:
    """
    Apply one combo to every coin, concatenate trade returns, return pooled
    metrics + per-coin breakdown (ordered by Sharpe desc).

    Returns:
        {
            "per_coin": [{"coin", "n", "win_pct", "avg_ret", "sharpe"}, ...],
            "pooled": {"n", "win_pct", "avg_ret", "sharpe"} | None,
            "days_span": int,
        }
    """
    ret_col = f"return_{holding_hours}h"
    per_coin: list[dict] = []
    pooled_trades: list[pd.Series] = []
    min_ts: Optional[pd.Timestamp] = None
    max_ts: Optional[pd.Timestamp] = None

    for coin, df in all_coin_dfs.items():
        if df.empty or ret_col not in df.columns:
            continue
        if min_ts is None or df.index.min() < min_ts:
            min_ts = df.index.min()
        if max_ts is None or df.index.max() > max_ts:
            max_ts = df.index.max()
        mask = apply_combo(df, combo_filters)
        trades = df.loc[mask, ret_col].dropna()
        if len(trades) == 0:
            per_coin.append({
                "coin": coin,
                "n": 0, "win_pct": None, "avg_ret": None, "sharpe": None,
            })
            continue
        m = _metrics_for_trades(trades, holding_hours)
        if m is None:
            per_coin.append({
                "coin": coin,
                "n": int(len(trades)),
                "win_pct": round((trades > 0).mean() * 100, 1),
                "avg_ret": round(trades.mean(), 2),
                "sharpe": None,
            })
        else:
            per_coin.append({"coin": coin, **m})
        pooled_trades.append(trades)

    pooled = None
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

    return {"per_coin": per_coin, "pooled": pooled, "days_span": days_span}


def print_portfolio_summary(combo_name: str, agg: dict) -> None:
    print()
    print("=" * 72)
    print(f"PORTFOLIO SUMMARY — best combo: '{combo_name}' @ h={RANK_HOLDING_HOURS}")
    print("=" * 72)
    print(f"  {'Coin':<6} {'N':>4}  {'Win%':>6}  {'Avg%':>6}  {'Sharpe':>7}")
    for row in agg["per_coin"]:
        n = row["n"]
        if n == 0:
            print(f"  {row['coin']:<6} {n:>4}  {'-':>6}  {'-':>6}  {'-':>7}")
            continue
        win = f"{row['win_pct']:.1f}" if row["win_pct"] is not None else "-"
        avg = f"{row['avg_ret']:+.2f}" if row["avg_ret"] is not None else "-"
        sh = f"{row['sharpe']:+.2f}" if row["sharpe"] is not None else "-"
        print(f"  {row['coin']:<6} {n:>4}  {win:>6}  {avg:>6}  {sh:>7}")
    print(f"  {'-'*6} {'-'*4}  {'-'*6}  {'-'*6}  {'-'*7}")
    pooled = agg["pooled"]
    if pooled is None:
        print("  TOTAL — no trades")
        return
    win = f"{pooled['win_pct']:.1f}"
    avg = f"{pooled['avg_ret']:+.2f}"
    sh = f"{pooled['sharpe']:+.2f}" if pooled["sharpe"] is not None else "-"
    print(f"  {'TOTAL':<6} {pooled['n']:>4}  {win:>6}  {avg:>6}  {sh:>7}")

    days = agg["days_span"] or 1
    n = pooled["n"]
    if n > 0:
        every = days / n
        monthly_n = 30 / every if every > 0 else 0
        monthly_edge = monthly_n * pooled["avg_ret"]
        print()
        print(f"  Frequency: {n} trades / {days} days = 1 every {every:.1f} days")
        print(
            f"  Estimated monthly: ~{monthly_n:.1f} trades × "
            f"{pooled['avg_ret']:+.2f}% = {monthly_edge:+.1f}% (before leverage/slippage)"
        )


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------

def walk_forward_best_combo(
    combo_name: str,
    combo_filters: list[tuple],
    all_coin_dfs: dict[str, pd.DataFrame],
    n_folds: int = WF_FOLDS,
    holding_hours: int = RANK_HOLDING_HOURS,
) -> None:
    """
    Apply combo across all coins, pool trades, split into n_folds folds by
    timestamp. Fold 0 = train baseline, folds 1..n-1 = OOS.

    Fixed thresholds (no grid tuning on train per overfitting rule).
    """
    print()
    print("=" * 72)
    print(f"WALK-FORWARD — '{combo_name}'  ({n_folds}-fold, fixed thresholds)")
    print("=" * 72)

    ret_col = f"return_{holding_hours}h"
    rows: list[tuple[pd.Timestamp, float]] = []
    all_index: list[pd.Timestamp] = []
    for df in all_coin_dfs.values():
        if df.empty or ret_col not in df.columns:
            continue
        all_index.extend(df.index.tolist())
        mask = apply_combo(df, combo_filters)
        trades = df.loc[mask, ret_col].dropna()
        for ts, r in trades.items():
            rows.append((ts, float(r)))

    if len(rows) < WF_MIN_TRADES:
        print(
            f"  Total pooled trades N={len(rows)} < {WF_MIN_TRADES} → "
            f"insufficient N for walk-forward."
        )
        return
    if not all_index:
        print("  No data.")
        return

    idx_sorted = pd.DatetimeIndex(sorted(set(all_index)))
    fold_bounds = split_folds(idx_sorted, n_folds)

    print(f"  {'Fold':<7} {'Period':<28} {'N':>4}  {'Win%':>6}  {'Avg%':>6}  {'Sharpe':>7}")
    trade_df = pd.DataFrame(rows, columns=["ts", "ret"]).sort_values("ts")
    trade_df = trade_df.set_index("ts")

    oos_sharpes: list[float] = []
    oos_pooled_trades: list[float] = []

    for i, (start, end) in enumerate(fold_bounds):
        label = "Train" if i == 0 else f"OOS{i}"
        in_fold = trade_df.loc[(trade_df.index >= start) & (trade_df.index < end), "ret"]
        # Include the final timestamp in the last fold (pd.date_range right
        # edge is exclusive in our loc slice).
        if i == len(fold_bounds) - 1:
            in_fold = trade_df.loc[(trade_df.index >= start) & (trade_df.index <= end), "ret"]
        period = f"{start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}"
        if len(in_fold) == 0:
            print(f"  {label:<7} {period:<28} {0:>4}  {'-':>6}  {'-':>6}  {'-':>7}")
            continue
        m = _metrics_for_trades(in_fold, holding_hours)
        if m is None:
            win = (in_fold > 0).mean() * 100
            avg = in_fold.mean()
            print(
                f"  {label:<7} {period:<28} {len(in_fold):>4}  "
                f"{win:>5.1f}%  {avg:>+5.2f}%  {'-':>7}"
            )
            if i > 0:
                oos_pooled_trades.extend(in_fold.tolist())
            continue
        print(
            f"  {label:<7} {period:<28} {m['n']:>4}  "
            f"{m['win_pct']:>5.1f}%  {m['avg_ret']:>+5.2f}%  {m['sharpe']:>+6.2f}"
        )
        if i > 0:
            oos_sharpes.append(m["sharpe"])
            oos_pooled_trades.extend(in_fold.tolist())

    # Verdict on OOS folds only.
    positive_folds = sum(1 for s in oos_sharpes if s > 0)
    total_oos = max(1, n_folds - 1)
    pooled_m = (
        _metrics_for_trades(pd.Series(oos_pooled_trades), holding_hours)
        if oos_pooled_trades else None
    )
    pooled_sharpe = pooled_m["sharpe"] if pooled_m else None
    print()
    if pooled_sharpe is not None:
        print(
            f"  Pooled OOS: N={pooled_m['n']}  "
            f"Win%={pooled_m['win_pct']:.1f}  "
            f"Avg%={pooled_m['avg_ret']:+.2f}  "
            f"Sharpe={pooled_sharpe:+.2f}"
        )
    else:
        print("  Pooled OOS: insufficient trades to compute Sharpe.")
    passed = (
        pooled_sharpe is not None
        and pooled_sharpe > 1.0
        and positive_folds >= 2
        and (n_folds - 1) == 3  # sanity on fold count
    )
    verdict = "PASS" if passed else "FAIL"
    print(
        f"  OOS positive folds: {positive_folds}/{total_oos}  "
        f"pooled Sharpe > 1.0: {pooled_sharpe is not None and pooled_sharpe > 1.0}  "
        f"→ {verdict}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    init_pool(get_config())

    print("=" * 72)
    print("L3b-2 COMBO SIGNAL BACKTEST")
    print("=" * 72)

    # ---- Load per-coin base data ----
    print()
    print("=" * 72)
    print("DATA LOAD SUMMARY")
    print("=" * 72)
    per_coin_features: dict[str, pd.DataFrame] = {}
    load_summary: list[dict] = []

    for coin in COINS:
        liq_sym, liq_df = _try_load_with_pepe_fallback(coin, load_liquidations)
        _, oi_df = _try_load_with_pepe_fallback(coin, load_oi)
        _, fund_df = _try_load_with_pepe_fallback(coin, load_funding)

        ccxt_sym = binance_ccxt_symbol(coin)
        if liq_df.empty:
            print(f"  {coin:<5}: no liquidation data — skipped")
            per_coin_features[coin] = pd.DataFrame()
            load_summary.append({"coin": coin, "skipped": True})
            continue
        since_ms = int(liq_df["timestamp"].min().timestamp() * 1000)
        try:
            ohlcv_df = fetch_klines_4h_ohlcv(ccxt_sym, since_ms)
        except Exception as e:
            print(f"  {coin:<5}: ccxt fetch failed ({e}) — skipped")
            per_coin_features[coin] = pd.DataFrame()
            load_summary.append({"coin": coin, "skipped": True})
            continue

        features = build_features(coin, liq_sym, liq_df, oi_df, fund_df, ohlcv_df)
        per_coin_features[coin] = features
        print(
            f"  {coin:<5}: liq {len(liq_df):>4} (sym={liq_sym})  "
            f"oi {len(oi_df):>4}  funding {len(fund_df):>4}  "
            f"price {len(ohlcv_df):>4}  features {len(features):>4}"
        )
        load_summary.append({
            "coin": coin,
            "liq": len(liq_df),
            "oi": len(oi_df),
            "funding": len(fund_df),
            "price": len(ohlcv_df),
            "features": len(features),
        })

    # ---- Cross-coin features ----
    per_coin_features = compute_cross_coin_features(per_coin_features)

    # Sanity: n_coins_flushing range.
    sample_coin = next(
        (c for c, d in per_coin_features.items() if not d.empty), None
    )
    if sample_coin is not None:
        ncf = per_coin_features[sample_coin]["n_coins_flushing"]
        print()
        print(
            f"  n_coins_flushing range: min={int(ncf.min())} max={int(ncf.max())} "
            f"mean={ncf.mean():.2f}  (expect 0..10)"
        )

    # ---- Per-coin combo tests ----
    all_combo_results: dict[str, dict[str, dict]] = {}  # coin -> combo -> result
    for coin, df in per_coin_features.items():
        coin_results: dict[str, dict] = {}
        for combo_name, combo_def in COMBOS.items():
            coin_results[combo_name] = test_combo(df, combo_def["filters"])
        all_combo_results[coin] = coin_results
        print_coin_table(coin, df, coin_results)

    # ---- Rank combos globally by pooled Sharpe at h=8 ----
    print()
    print("=" * 72)
    print(f"COMBO RANKING — pooled metrics @ h={RANK_HOLDING_HOURS} across all coins")
    print("=" * 72)
    ranking: list[tuple[str, dict]] = []
    for combo_name, combo_def in COMBOS.items():
        agg = aggregate_combo_across_coins(
            per_coin_features, combo_def["filters"], RANK_HOLDING_HOURS
        )
        ranking.append((combo_name, agg))

    def _sort_key(item: tuple[str, dict]) -> tuple:
        pooled = item[1]["pooled"]
        if pooled is None or pooled.get("sharpe") is None:
            return (-1, -float("inf"), 0)
        # Require at least 5 trades to rank.
        if pooled["n"] < 5:
            return (-1, -float("inf"), 0)
        return (1, pooled["sharpe"], pooled["n"])

    ranking.sort(key=_sort_key, reverse=True)
    print(f"  {'Combo':<24} {'N':>5}  {'Win%':>6}  {'Avg%':>6}  {'Sharpe':>7}")
    for combo_name, agg in ranking:
        pooled = agg["pooled"]
        if pooled is None or pooled.get("n", 0) == 0:
            print(f"  {combo_name:<24} {'0':>5}  {'-':>6}  {'-':>6}  {'-':>7}")
            continue
        n = pooled["n"]
        win = f"{pooled['win_pct']:.1f}"
        avg = f"{pooled['avg_ret']:+.2f}"
        sh = f"{pooled['sharpe']:+.2f}" if pooled["sharpe"] is not None else "-"
        print(f"  {combo_name:<24} {n:>5}  {win:>6}  {avg:>6}  {sh:>7}")

    # ---- Portfolio summary for best combo ----
    best_combo_name, best_agg = ranking[0]
    if best_agg["pooled"] is not None and best_agg["pooled"].get("n", 0) > 0:
        print_portfolio_summary(best_combo_name, best_agg)

        # ---- Walk-forward on the best combo ----
        walk_forward_best_combo(
            best_combo_name,
            COMBOS[best_combo_name]["filters"],
            per_coin_features,
        )
    else:
        print()
        print("No combo produced any trades — nothing to rank, skipping portfolio "
              "summary and walk-forward.")

    # ---- Sanity check: baseline_flush vs L2 H1 on SOL at h=8 ----
    print()
    print("=" * 72)
    print("SANITY CHECK — baseline_flush (z>2.0) @ h=8 vs L2 H1 flush numbers")
    print("=" * 72)
    sol_res = all_combo_results.get("SOL", {}).get("baseline_flush")
    if sol_res and sol_res["h8"] is not None:
        m = sol_res["h8"]
        print(
            f"  SOL baseline_flush h=8 → N={m['n']}  Win%={m['win_pct']}  "
            f"Avg%={m['avg_ret']}  Sharpe={m['sharpe']}"
        )
        print("  Compare against scripts/backtest_liquidation_flush.py output.")
    else:
        print("  SOL baseline_flush h=8 not available.")


if __name__ == "__main__":
    main()
