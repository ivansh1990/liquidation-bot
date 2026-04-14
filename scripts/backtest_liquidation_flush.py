#!/usr/bin/env python3
"""
Backtest: does extreme liquidation asymmetry predict price reversal?

Hypotheses:
  H1: After heavy LONG liquidations (flush) → price bounces (BUY signal)
  H2: After heavy SHORT liquidations (squeeze) → price drops (SELL signal)
  H3: Extreme long/short liquidation ratio → contrarian trade

Data:
  - Liquidations: coinglass_liquidations (4H)
  - Prices: Binance futures 4H klines fetched on-the-fly via ccxt

Usage:
    .venv/bin/python scripts/backtest_liquidation_flush.py
"""
from __future__ import annotations

import os
import sys
from datetime import timezone

import ccxt
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import binance_ccxt_symbol, get_config
from collectors.db import get_conn, init_pool


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def fetch_klines_4h(ccxt_symbol: str, since_ms: int) -> pd.DataFrame:
    """
    Fetch 4H OHLCV from Binance futures via ccxt.

    Returns a DataFrame indexed by UTC timestamp with a single `price`
    column holding the close price. Paginated in batches of 1000.
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
        return pd.DataFrame(columns=["price"])
    df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "vol"])
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("timestamp")[["close"]].rename(columns={"close": "price"})


def load_liquidations(symbol: str) -> pd.DataFrame:
    """Load 4H CoinGlass liquidations for a symbol."""
    with get_conn() as conn:
        liq_df = pd.read_sql(
            """
            SELECT timestamp, long_vol_usd, short_vol_usd
            FROM coinglass_liquidations
            WHERE symbol = %s
            ORDER BY timestamp
            """,
            conn,
            params=(symbol,),
            parse_dates=["timestamp"],
        )
    return liq_df


# ---------------------------------------------------------------------------
# Signal engineering
# ---------------------------------------------------------------------------

def compute_signals(
    liq_df: pd.DataFrame,
    price_df: pd.DataFrame,
    lookback_periods: int = 6,
) -> pd.DataFrame:
    """
    Compute signal columns from the liquidation data + forward returns.

    Columns added:
      - total_vol           sum of long + short liquidated USD this period
      - long_short_ratio    long_vol / short_vol (this 4H period)
      - long_vol_24h        rolling 6×4H = 24H long liquidation sum
      - short_vol_24h       rolling 6×4H = 24H short liquidation sum
      - ratio_24h           long_vol_24h / short_vol_24h
      - long_vol_zscore     z-score of long_vol_usd over 90-period window
      - short_vol_zscore    z-score of short_vol_usd over 90-period window
      - return_{N}h         forward N-hour pct return (N ∈ {4, 8, 12, 24})
    """
    df = liq_df.copy()
    df = df.set_index("timestamp").sort_index()
    # Normalize timezone to UTC to match klines.
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    df["total_vol"] = df["long_vol_usd"] + df["short_vol_usd"]
    df["long_short_ratio"] = df["long_vol_usd"] / df["short_vol_usd"].replace(0, np.nan)

    df["long_vol_24h"] = df["long_vol_usd"].rolling(lookback_periods).sum()
    df["short_vol_24h"] = df["short_vol_usd"].rolling(lookback_periods).sum()
    df["ratio_24h"] = df["long_vol_24h"] / df["short_vol_24h"].replace(0, np.nan)

    df["long_vol_zscore"] = (
        (df["long_vol_usd"] - df["long_vol_usd"].rolling(90).mean())
        / df["long_vol_usd"].rolling(90).std()
    )
    df["short_vol_zscore"] = (
        (df["short_vol_usd"] - df["short_vol_usd"].rolling(90).mean())
        / df["short_vol_usd"].rolling(90).std()
    )

    # Join prices. Klines are already 4H-aligned; use asof merge to tolerate
    # minor misalignment and fill forward.
    prices = price_df.sort_index()
    df["price"] = prices["price"].reindex(df.index, method="ffill")

    # Forward returns at 4, 8, 12, 24 hours (1, 2, 3, 6 periods)
    for hours in [4, 8, 12, 24]:
        periods = hours // 4
        df[f"return_{hours}h"] = df["price"].pct_change(periods).shift(-periods) * 100

    return df.dropna(subset=["price"])


# ---------------------------------------------------------------------------
# Backtest core
# ---------------------------------------------------------------------------

def backtest_signal(
    df: pd.DataFrame,
    signal_col: str,
    threshold: float,
    direction: str,
    forward_hours: int = 12,
) -> dict | None:
    """
    Test a single signal configuration.

    Args:
      signal_col:    column name holding the signal value
      threshold:     entry threshold
      direction:     'above' (enter when signal > threshold) or 'below'
      forward_hours: holding period in hours (must match a return_{N}h col)

    Returns None if there are fewer than 5 valid trades.
    """
    return_col = f"return_{forward_hours}h"

    if direction == "above":
        mask = df[signal_col] > threshold
    else:
        mask = df[signal_col] < threshold

    trades = df.loc[mask, return_col].dropna()
    if len(trades) < 5:
        return None

    win_rate = (trades > 0).mean() * 100
    avg_return = trades.mean()
    n_trades = len(trades)

    # Annualized Sharpe (period count per year = 365*24 / holding hours)
    periods_per_year = 365 * 24 / forward_hours
    sharpe = (
        (trades.mean() / trades.std()) * np.sqrt(periods_per_year)
        if trades.std() > 0 else 0
    )

    return {
        "n_trades": n_trades,
        "win_rate": round(win_rate, 1),
        "avg_return": round(avg_return, 2),
        "median_return": round(trades.median(), 2),
        "max_loss": round(trades.min(), 2),
        "max_win": round(trades.max(), 2),
        "sharpe": round(sharpe, 2),
    }


def run_backtest(symbol: str) -> None:
    """Run H1/H2/H3 tests and print result tables for one symbol."""
    print(f"\n{'='*60}")
    print(f"  BACKTEST: {symbol}")
    print(f"{'='*60}")

    liq_df = load_liquidations(symbol)
    if liq_df.empty:
        print(f"  No liquidation data for {symbol}")
        return

    since_ms = int(liq_df["timestamp"].min().timestamp() * 1000)
    ccxt_symbol = binance_ccxt_symbol(symbol)
    try:
        price_df = fetch_klines_4h(ccxt_symbol, since_ms)
    except Exception as e:
        print(f"  Failed to fetch klines for {symbol} ({ccxt_symbol}): {e}")
        return

    if price_df.empty:
        print(f"  No price data for {symbol}")
        return

    df = compute_signals(liq_df, price_df)
    if df.empty:
        print(f"  No aligned data for {symbol}")
        return

    print(f"  Data: {len(df)} periods, {df.index.min().date()} → {df.index.max().date()}")
    print(f"  Price range: ${df['price'].min():.4f} → ${df['price'].max():.4f}")

    # === H1: Long flush → BUY ===
    print(f"\n  H1: Long Liquidation Flush → BUY")
    print(f"  (heavy long liquidations = sellers exhausted, expect bounce)")
    print(f"  {'Threshold':<15} {'N':>5} {'Win%':>6} {'Avg%':>7} {'Med%':>7} {'Sharpe':>7}")
    print(f"  {'-'*48}")
    for zscore_thresh in [1.0, 1.5, 2.0, 2.5]:
        for hours in [4, 8, 12, 24]:
            result = backtest_signal(df, "long_vol_zscore", zscore_thresh, "above", hours)
            if result and result["n_trades"] >= 5:
                label = f"z>{zscore_thresh} →{hours}h"
                print(
                    f"  {label:<15} {result['n_trades']:>5} {result['win_rate']:>5.1f}% "
                    f"{result['avg_return']:>6.2f}% {result['median_return']:>6.2f}% "
                    f"{result['sharpe']:>6.2f}"
                )

    # === H2: Short squeeze → SELL ===
    print(f"\n  H2: Short Liquidation Squeeze → SELL")
    print(f"  (heavy short liquidations = buyers exhausted, expect drop)")
    print(f"  {'Threshold':<15} {'N':>5} {'Win%':>6} {'Avg%':>7} {'Med%':>7} {'Sharpe':>7}")
    print(f"  {'-'*48}")
    for zscore_thresh in [1.0, 1.5, 2.0, 2.5]:
        for hours in [4, 8, 12, 24]:
            result_long = backtest_signal(
                df, "short_vol_zscore", zscore_thresh, "above", hours
            )
            if result_long and result_long["n_trades"] >= 5:
                # Invert for SHORT: positive price move = loss for a short.
                label = f"z>{zscore_thresh} →{hours}h"
                print(
                    f"  {label:<15} {result_long['n_trades']:>5} "
                    f"{100 - result_long['win_rate']:>5.1f}% "
                    f"{-result_long['avg_return']:>6.2f}% "
                    f"{-result_long['median_return']:>6.2f}% "
                    f"{-result_long['sharpe']:>6.2f}"
                )

    # === H3: Extreme 24H ratio → contrarian ===
    print(f"\n  H3: Extreme Long/Short Ratio (24H) → Contrarian")
    print(f"  (ratio > X means longs liquidated much more → expect bounce)")
    print(f"  {'Threshold':<15} {'N':>5} {'Win%':>6} {'Avg%':>7} {'Med%':>7} {'Sharpe':>7}")
    print(f"  {'-'*48}")
    for ratio_thresh in [2.0, 3.0, 5.0, 10.0]:
        for hours in [4, 8, 12, 24]:
            result = backtest_signal(df, "ratio_24h", ratio_thresh, "above", hours)
            if result and result["n_trades"] >= 5:
                label = f"r>{ratio_thresh} →{hours}h"
                print(
                    f"  {label:<15} {result['n_trades']:>5} {result['win_rate']:>5.1f}% "
                    f"{result['avg_return']:>6.2f}% {result['median_return']:>6.2f}% "
                    f"{result['sharpe']:>6.2f}"
                )


def main() -> None:
    cfg = get_config()
    init_pool(cfg)

    print("LIQUIDATION FLUSH BACKTEST")
    print("=" * 60)
    print("Data: CoinGlass aggregated liquidations (4H)")
    print("Prices: Binance futures 4H klines (ccxt)")

    for symbol in ["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX", "SUI", "ARB"]:
        try:
            run_backtest(symbol)
        except Exception as e:
            print(f"  Error for {symbol}: {e}")

    print(f"\n{'='*60}")
    print("CONCLUSION")
    print("=" * 60)
    print("Look for: Sharpe > 0.8, Win Rate > 55%, N trades > 20")
    print("If found → signal is viable, move to Session L3 integration with heatmap")
    print("If not → liquidation flush alone has no edge")


if __name__ == "__main__":
    main()
