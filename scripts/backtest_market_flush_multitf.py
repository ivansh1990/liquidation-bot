#!/usr/bin/env python3
"""
L8: Multi-timeframe market_flush backtest.

Tests the market_flush combo signal (L3b-2) on 1H, 2H, and 4H CoinGlass
liquidation data.  The 4H run serves as a reference baseline that should
reproduce L3b-2 numbers (Sharpe ~5.60, win ~60.7%, N ~422).

Data sources:
  - coinglass_liquidations_{h1,h2} or coinglass_liquidations (h4)
  - coinglass_oi_{h1,h2} or coinglass_oi (h4)
  - coinglass_funding  (h8, forward-filled to any bar grid)
  - Binance klines via ccxt (1h/2h/4h)

Usage:
    .venv/bin/python scripts/backtest_market_flush_multitf.py --interval h4
    .venv/bin/python scripts/backtest_market_flush_multitf.py --interval h2
    .venv/bin/python scripts/backtest_market_flush_multitf.py --interval h1
"""
from __future__ import annotations

import argparse
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

from backtest_combo import (  # noqa: E402
    apply_combo,
    _metrics_for_trades,
    compute_cross_coin_features,
    _try_load_with_pepe_fallback,
)
from walkforward_h1_flush import split_folds  # noqa: E402
from smart_filter_adequacy import (  # noqa: E402  — L16 strict gates
    SMART_FILTER_CONFIGS,
    compute_daily_metrics,
    simulate_smart_filter_windows,
    summarize_smart_filter_results,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Holding periods in HOURS, keyed by bar_minutes. All include 8h for the
# cross-interval ranking anchor (RANK_HOLDING_HOURS). bar_minutes is the
# canonical internal TF representation (L16) — sub-hour intervals like 30m
# cannot be keyed by integer bar_hours. `HOLDING_HOURS_MAP` below is a
# backward-compat view derived from this dict.
HOLDING_HOURS_BY_MINUTES: dict[int, list[int]] = {
    30:  [4, 8, 16, 48],   # h30m: fine-grained, mirrors h1 holding set
    60:  [4, 8, 16, 48],   # h1
    120: [8, 16, 32, 48],  # h2
    240: [4, 8, 12, 24],   # h4: L3b-2 baseline
}

# Backward-compat dict keyed by integer bar_hours. Omits sub-hour intervals
# (30m) since they cannot be represented as an integer number of hours.
# Existing L8/L15 tests import and index this via HOLDING_HOURS_MAP[1/2/4].
HOLDING_HOURS_MAP: dict[int, list[int]] = {
    m // 60: v for m, v in HOLDING_HOURS_BY_MINUTES.items() if m % 60 == 0
}

# Z-score window in bars at 4H that equals 15 calendar days.
_BASE_Z_WINDOW_4H = 90

CROSS_COIN_FLUSH_Z: float = 1.5   # threshold for counting "flushing" coins
RANK_HOLDING_HOURS: int = 8       # cross-interval ranking at 8h

WF_FOLDS: int = 4
WF_MIN_TRADES: int = 30

# market_flush combo — locked from L3b-2.  Do NOT change thresholds.
MARKET_FLUSH_FILTERS: list[tuple] = [
    ("long_vol_zscore", ">", 1.0),
    ("n_coins_flushing", ">=", 4),
]

# PEPE may be stored as "PEPE" or "1000PEPE" in coinglass_* tables.
PEPE_ALIASES: list[str] = ["PEPE", "1000PEPE"]

ATR_PERIOD: int = 14


# ---------------------------------------------------------------------------
# Interval helpers
# ---------------------------------------------------------------------------

# Canonical per-interval bar duration in minutes (L16).  Sub-hour intervals
# force float bar_hours, so bar_minutes is preferred everywhere internally.
INTERVAL_TO_BAR_MINUTES: dict[str, int] = {
    "30m": 30, "h1": 60, "h2": 120, "h4": 240,
}


def _interval_to_bar_minutes(interval: str) -> int:
    return INTERVAL_TO_BAR_MINUTES[interval]


def _interval_to_bar_hours(interval: str) -> float:
    """Returns a float so 30m → 0.5 resolves cleanly.  Integer intervals
    still compare correctly (e.g. h4 → 4.0 == 4)."""
    return INTERVAL_TO_BAR_MINUTES[interval] / 60.0


def _interval_to_ccxt_timeframe(interval: str) -> str:
    """Map internal interval name to ccxt's timeframe string.  ccxt's Binance
    driver supports '30m', '1h', '2h', '4h' natively."""
    m = INTERVAL_TO_BAR_MINUTES[interval]
    return f"{m}m" if m < 60 else f"{m // 60}h"


def _z_window(bar_hours: float) -> int:
    """Scale z-score window to maintain the 15-day calendar window across
    intervals (h4→90, h2→180, h1→360, 30m→720)."""
    return int(_BASE_Z_WINDOW_4H * 4 / bar_hours)


def _lookback_24h(bar_hours: float) -> int:
    """Number of bars in 24 hours (h4→6, h2→12, h1→24, 30m→48)."""
    return int(24 / bar_hours)


def _forward_periods(holding_hours: int, bar_hours: float) -> int:
    """Number of forward bars to shift for a holding-period return.  Always
    returns >= 1 to guard against accidental zero-shift at extreme TFs."""
    return max(1, int(round(holding_hours / bar_hours)))


# ---------------------------------------------------------------------------
# Data loaders (multi-TF aware)
# ---------------------------------------------------------------------------

def load_liquidations_tf(symbol: str, interval: str) -> pd.DataFrame:
    """Load CoinGlass liquidations from the interval-specific table.

    Naming convention: h4 uses the base table `coinglass_liquidations`;
    other intervals use `coinglass_liquidations_{interval}` (30m/h1/h2)."""
    table = "coinglass_liquidations" if interval == "h4" else f"coinglass_liquidations_{interval}"
    with get_conn() as conn:
        df = pd.read_sql(
            f"SELECT timestamp, long_vol_usd, short_vol_usd "
            f"FROM {table} WHERE symbol = %s ORDER BY timestamp",
            conn,
            params=(symbol,),
            parse_dates=["timestamp"],
        )
    return df


def load_oi_tf(symbol: str, interval: str) -> pd.DataFrame:
    """Load CoinGlass OI from the interval-specific table."""
    table = "coinglass_oi" if interval == "h4" else f"coinglass_oi_{interval}"
    with get_conn() as conn:
        df = pd.read_sql(
            f"SELECT timestamp, open_interest, oi_high, oi_low "
            f"FROM {table} WHERE symbol = %s ORDER BY timestamp",
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
    """Load coinglass_funding (h8, shared across all intervals)."""
    with get_conn() as conn:
        df = pd.read_sql(
            "SELECT timestamp, funding_rate FROM coinglass_funding "
            "WHERE symbol = %s ORDER BY timestamp",
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


def fetch_klines_ohlcv(
    ccxt_symbol: str, since_ms: int, timeframe: str,
) -> pd.DataFrame:
    """
    Fetch paginated OHLCV from Binance futures via ccxt.
    Returns DF indexed by UTC timestamp with [open, high, low, close, volume].
    """
    exchange = ccxt.binance({"options": {"defaultType": "swap"}})
    all_rows: list[list] = []
    cursor = since_ms
    while True:
        batch = exchange.fetch_ohlcv(
            ccxt_symbol, timeframe=timeframe, since=cursor, limit=1000,
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
# compute_signals_tf  — parameterized mirror of locked compute_signals
# ---------------------------------------------------------------------------

def compute_signals_tf(
    liq_df: pd.DataFrame,
    price_df: pd.DataFrame,
    bar_hours: float,
    holding_hours: list[int] | None = None,
) -> pd.DataFrame:
    """
    Mirrors backtest_liquidation_flush.compute_signals (L2, locked) but scales
    rolling windows and forward returns by bar_hours.

    At bar_hours=4 this produces byte-for-byte identical output to the original.
    bar_hours may be a float (e.g. 0.5 for 30m); forward-return shifts are
    computed via _forward_periods to ensure integer shift counts.
    """
    z_window = _z_window(bar_hours)
    lookback = _lookback_24h(bar_hours)
    if holding_hours is None:
        bar_minutes = int(round(bar_hours * 60))
        holding_hours = HOLDING_HOURS_BY_MINUTES.get(bar_minutes, [4, 8, 12, 24])

    df = liq_df.copy()
    df = df.set_index("timestamp").sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    df["total_vol"] = df["long_vol_usd"] + df["short_vol_usd"]
    df["long_short_ratio"] = df["long_vol_usd"] / df["short_vol_usd"].replace(0, np.nan)

    df["long_vol_24h"] = df["long_vol_usd"].rolling(lookback).sum()
    df["short_vol_24h"] = df["short_vol_usd"].rolling(lookback).sum()
    df["ratio_24h"] = df["long_vol_24h"] / df["short_vol_24h"].replace(0, np.nan)

    df["long_vol_zscore"] = (
        (df["long_vol_usd"] - df["long_vol_usd"].rolling(z_window).mean())
        / df["long_vol_usd"].rolling(z_window).std()
    )
    df["short_vol_zscore"] = (
        (df["short_vol_usd"] - df["short_vol_usd"].rolling(z_window).mean())
        / df["short_vol_usd"].rolling(z_window).std()
    )

    # Join prices.
    prices = price_df.sort_index()
    df["price"] = prices["price"].reindex(df.index, method="ffill")

    # Forward returns for each holding period.  _forward_periods ensures
    # integer shifts and handles sub-hour intervals correctly.
    for hours in holding_hours:
        periods = _forward_periods(hours, bar_hours)
        df[f"return_{hours}h"] = df["price"].pct_change(periods).shift(-periods) * 100

    return df.dropna(subset=["price"])


# ---------------------------------------------------------------------------
# Feature helpers
# ---------------------------------------------------------------------------

def _zscore_tf(series: pd.Series, bar_hours: float) -> pd.Series:
    window = _z_window(bar_hours)
    return (series - series.rolling(window).mean()) / series.rolling(window).std()


def _add_atr(ohlc: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
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
# Per-coin feature assembly (multi-TF)
# ---------------------------------------------------------------------------

def build_features_tf(
    coin: str,
    liq_used_symbol: str,
    liq_df: pd.DataFrame,
    oi_df: pd.DataFrame,
    funding_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
    bar_hours: float,
) -> pd.DataFrame:
    """
    Merge all inputs into a feature DataFrame for one coin.
    Mirrors backtest_combo.build_features but scales all period-dependent
    computations by bar_hours.  bar_hours may be a float (e.g. 0.5 for 30m).
    """
    if liq_df.empty or ohlcv_df.empty:
        return pd.DataFrame()

    bar_minutes = int(round(bar_hours * 60))
    holding_hours = HOLDING_HOURS_BY_MINUTES.get(bar_minutes, [4, 8, 12, 24])

    price_df = ohlcv_df[["close"]].rename(columns={"close": "price"})
    df = compute_signals_tf(liq_df, price_df, bar_hours, holding_hours)

    # OHLC for ATR / drawdown / volume.
    ohlc = ohlcv_df.reindex(df.index, method="nearest")
    df["atr"] = _add_atr(ohlc)
    df["volume"] = ohlc["volume"]
    df["volume_zscore"] = _zscore_tf(df["volume"], bar_hours)

    # Drawdown: cumulative 24h pct change (past-looking).
    bars_24h = _lookback_24h(bar_hours)
    df["drawdown_24h"] = df["price"].pct_change(bars_24h) * 100

    # Open interest.
    if not oi_df.empty:
        oi_aligned = oi_df.reindex(df.index, method="ffill")
        df["oi"] = oi_aligned["open_interest"]
        df["oi_change_1"] = df["oi"].pct_change(1) * 100
        df["oi_change_24h"] = df["oi"].pct_change(bars_24h) * 100
        df["liq_oi_ratio"] = df["total_vol"] / df["oi"].replace(0, np.nan)
        df["liq_oi_zscore"] = _zscore_tf(df["liq_oi_ratio"], bar_hours)
    else:
        for col in ("oi", "oi_change_1", "oi_change_24h", "liq_oi_ratio", "liq_oi_zscore"):
            df[col] = np.nan

    # Funding: h8 → forward-fill to any bar grid.
    if not funding_df.empty:
        fund_aligned = funding_df.reindex(df.index, method="ffill")
        df["funding_rate"] = fund_aligned["funding_rate"]
        df["funding_extreme"] = df["funding_rate"].abs() > 0.0005
    else:
        df["funding_rate"] = np.nan
        df["funding_extreme"] = False

    df["long_vol_zscore_prev"] = df["long_vol_zscore"].shift(1)
    df["coin"] = coin

    return df.dropna(subset=["price"])


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

def _fmt_cell(metrics: Optional[dict]) -> str:
    if metrics is None:
        return "         -        "
    return f"{metrics['n']:>3} {metrics['win_pct']:>4.1f}% {metrics['avg_ret']:>+5.2f}%"


def print_coin_table(
    coin: str,
    df: pd.DataFrame,
    result: dict,
    holding_hours: list[int],
) -> None:
    if df.empty:
        print(f"  {coin:<5}: no data")
        return
    cells = "    ".join(_fmt_cell(result.get(f"h{h}")) for h in holding_hours)
    print(f"  {coin:<5} {result['n_signals']:>8}  |  {cells}")


def test_combo_tf(
    df: pd.DataFrame,
    filters: list[tuple],
    holding_hours: list[int],
) -> dict:
    """Apply market_flush combo and compute metrics for each holding period."""
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
# Portfolio aggregation
# ---------------------------------------------------------------------------

def aggregate_across_coins(
    all_coin_dfs: dict[str, pd.DataFrame],
    combo_filters: list[tuple],
    holding_hours: int = RANK_HOLDING_HOURS,
) -> dict:
    """Apply market_flush to every coin, return pooled + per-coin metrics."""
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
                "coin": coin, "n": 0,
                "win_pct": None, "avg_ret": None, "sharpe": None,
            })
            continue
        m = _metrics_for_trades(trades, holding_hours)
        if m is None:
            per_coin.append({
                "coin": coin, "n": int(len(trades)),
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


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------

def walk_forward(
    all_coin_dfs: dict[str, pd.DataFrame],
    combo_filters: list[tuple],
    n_folds: int = WF_FOLDS,
    holding_hours: int = RANK_HOLDING_HOURS,
) -> None:
    """4-fold walk-forward with fixed thresholds (no grid tuning)."""
    print()
    print("=" * 72)
    print(f"WALK-FORWARD — market_flush  ({n_folds}-fold, fixed thresholds)")
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
        if i == len(fold_bounds) - 1:
            in_fold = trade_df.loc[
                (trade_df.index >= start) & (trade_df.index <= end), "ret"
            ]
        else:
            in_fold = trade_df.loc[
                (trade_df.index >= start) & (trade_df.index < end), "ret"
            ]
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

    # Verdict
    positive_folds = sum(1 for s in oos_sharpes if s > 0)
    total_oos = max(1, n_folds - 1)
    pooled_m = (
        _metrics_for_trades(pd.Series(oos_pooled_trades), holding_hours)
        if oos_pooled_trades else None
    )
    pooled_sharpe = pooled_m["sharpe"] if pooled_m else None
    print()
    if pooled_m is not None and pooled_sharpe is not None:
        print(
            f"  Pooled OOS: N={pooled_m['n']}  "
            f"Win%={pooled_m['win_pct']:.1f}  "
            f"Avg%={pooled_m['avg_ret']:+.2f}  "
            f"Sharpe={pooled_sharpe:+.2f}"
        )
    else:
        print("  Pooled OOS: insufficient trades to compute Sharpe.")

    wf_passed = (
        pooled_sharpe is not None
        and pooled_sharpe > 1.0
        and positive_folds >= 2
        and (n_folds - 1) == 3
    )
    verdict = "PASS" if wf_passed else "FAIL"
    print(
        f"  OOS positive folds: {positive_folds}/{total_oos}  "
        f"pooled Sharpe > 1.0: {pooled_sharpe is not None and pooled_sharpe > 1.0}  "
        f"→ {verdict}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="L8: Multi-timeframe market_flush backtest."
    )
    parser.add_argument(
        "--interval", type=str, default="h4", choices=["30m", "h1", "h2", "h4"],
        help="CoinGlass interval (default: h4).  30m is L16.",
    )
    args = parser.parse_args()

    interval = args.interval
    bar_minutes = _interval_to_bar_minutes(interval)
    bar_hours = _interval_to_bar_hours(interval)
    holding_hours = HOLDING_HOURS_BY_MINUTES[bar_minutes]
    z_window = _z_window(bar_hours)
    lookback = _lookback_24h(bar_hours)
    timeframe = _interval_to_ccxt_timeframe(interval)

    init_pool(get_config())

    print("=" * 72)
    print(f"L8 MULTI-TF MARKET_FLUSH BACKTEST — interval={interval}")
    print("=" * 72)
    print(f"  bar_hours={bar_hours}  z_window={z_window}  lookback_24h={lookback}")
    print(f"  holding_hours={holding_hours}  ranking_at=h{RANK_HOLDING_HOURS}")
    print(f"  market_flush filters: z_self>1.0, n_coins>=4")
    print()

    # ---- Load per-coin data ----
    print("=" * 72)
    print("DATA LOAD SUMMARY")
    print("=" * 72)
    per_coin_features: dict[str, pd.DataFrame] = {}

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
            per_coin_features[coin] = pd.DataFrame()
            continue
        since_ms = int(liq_df["timestamp"].min().timestamp() * 1000)
        try:
            ohlcv_df = fetch_klines_ohlcv(ccxt_sym, since_ms, timeframe)
        except Exception as e:
            print(f"  {coin:<5}: ccxt fetch failed ({e}) — skipped")
            per_coin_features[coin] = pd.DataFrame()
            continue

        features = build_features_tf(
            coin, liq_sym, liq_df, oi_df, fund_df, ohlcv_df, bar_hours,
        )
        per_coin_features[coin] = features
        print(
            f"  {coin:<5}: liq {len(liq_df):>5} (sym={liq_sym})  "
            f"oi {len(oi_df):>5}  funding {len(fund_df):>4}  "
            f"price {len(ohlcv_df):>5}  features {len(features):>5}"
        )

    # ---- Cross-coin features ----
    per_coin_features = compute_cross_coin_features(per_coin_features)

    # Sanity: n_coins_flushing range.
    sample_coin = next(
        (c for c, d in per_coin_features.items() if not d.empty), None,
    )
    if sample_coin is not None:
        ncf = per_coin_features[sample_coin]["n_coins_flushing"]
        print()
        print(
            f"  n_coins_flushing range: min={int(ncf.min())} max={int(ncf.max())} "
            f"mean={ncf.mean():.2f}  (expect 0..10)"
        )

    # ---- Per-coin combo test ----
    print()
    print("=" * 72)
    print(f"PER-COIN MARKET_FLUSH — interval={interval}")
    print(f"  market_flush signal (z_self > 1.0, n_coins >= 4, market z > 1.5)")
    print("=" * 72)

    header_h = "    ".join(f"→{h}h" for h in holding_hours)
    print()
    print(f"  {'Coin':<5} {'Signals':>8}  |  " + "    ".join(
        f"  {'N':>3} {'Win%':>5} {'Avg%':>6}" for _ in holding_hours
    ))

    all_coin_results: dict[str, dict] = {}
    for coin, df in per_coin_features.items():
        result = test_combo_tf(df, MARKET_FLUSH_FILTERS, holding_hours)
        all_coin_results[coin] = result
        print_coin_table(coin, df, result, holding_hours)

    # ---- Portfolio summary at h=8 ----
    agg = aggregate_across_coins(
        per_coin_features, MARKET_FLUSH_FILTERS, RANK_HOLDING_HOURS,
    )

    print()
    print("=" * 72)
    print(f"PORTFOLIO SUMMARY — market_flush @ h={RANK_HOLDING_HOURS}, interval={interval}")
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
    if pooled is None or pooled.get("n", 0) == 0:
        print("  TOTAL — no trades")
        print()
        print("No trades produced — skipping walk-forward and verdict.")
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

    # ---- Walk-forward ----
    walk_forward(per_coin_features, MARKET_FLUSH_FILTERS)

    # ---- PASS/FAIL verdict ----
    print()
    print("=" * 72)
    print(f"VERDICT — interval={interval}")
    print("=" * 72)
    print("  PASS criteria (L3b-2 parity):")
    print("    1. Pooled portfolio Sharpe > 2.0")
    print("    2. Win rate > 55%")
    print("    3. N trades >= 100 (statistical significance)")
    print("    4. >= 2/3 OOS folds positive Sharpe")
    print("    5. Pooled OOS Sharpe > 1.0")
    print()

    # Recompute walk-forward stats for the final verdict.
    ret_col = f"return_{RANK_HOLDING_HOURS}h"
    all_trades: list[float] = []
    for df in per_coin_features.values():
        if df.empty or ret_col not in df.columns:
            continue
        mask = apply_combo(df, MARKET_FLUSH_FILTERS)
        trades = df.loc[mask, ret_col].dropna()
        all_trades.extend(trades.tolist())

    all_trades_s = pd.Series(all_trades) if all_trades else pd.Series(dtype=float)
    total_n = len(all_trades_s)
    total_win_pct = (all_trades_s > 0).mean() * 100 if total_n > 0 else 0
    total_sharpe = pooled["sharpe"] if pooled and pooled.get("sharpe") is not None else None

    # Walk-forward OOS stats (recompute from pooled trades).
    all_index: list[pd.Timestamp] = []
    wf_rows: list[tuple] = []
    for df in per_coin_features.values():
        if df.empty or ret_col not in df.columns:
            continue
        all_index.extend(df.index.tolist())
        mask = apply_combo(df, MARKET_FLUSH_FILTERS)
        trades = df.loc[mask, ret_col].dropna()
        for ts, r in trades.items():
            wf_rows.append((ts, float(r)))

    oos_sharpe = None
    oos_positive_folds = 0
    if len(wf_rows) >= WF_MIN_TRADES and all_index:
        idx_sorted = pd.DatetimeIndex(sorted(set(all_index)))
        fold_bounds = split_folds(idx_sorted, WF_FOLDS)
        trade_df = pd.DataFrame(wf_rows, columns=["ts", "ret"]).sort_values("ts").set_index("ts")
        oos_trades_all: list[float] = []
        oos_sharpes_list: list[float] = []
        for i, (start, end) in enumerate(fold_bounds):
            if i == 0:
                continue  # skip train
            if i == len(fold_bounds) - 1:
                in_fold = trade_df.loc[
                    (trade_df.index >= start) & (trade_df.index <= end), "ret"
                ]
            else:
                in_fold = trade_df.loc[
                    (trade_df.index >= start) & (trade_df.index < end), "ret"
                ]
            oos_trades_all.extend(in_fold.tolist())
            m = _metrics_for_trades(in_fold, RANK_HOLDING_HOURS)
            if m is not None:
                oos_sharpes_list.append(m["sharpe"])
        oos_positive_folds = sum(1 for s in oos_sharpes_list if s > 0)
        if oos_trades_all:
            oos_m = _metrics_for_trades(pd.Series(oos_trades_all), RANK_HOLDING_HOURS)
            oos_sharpe = oos_m["sharpe"] if oos_m else None

    c1 = total_sharpe is not None and total_sharpe > 2.0
    c2 = total_win_pct > 55
    c3 = total_n >= 100
    c4 = oos_positive_folds >= 2
    c5 = oos_sharpe is not None and oos_sharpe > 1.0
    primary_ok = c1 and c2 and c3 and c4 and c5

    print(f"  1. Pooled Sharpe > 2.0:       {'PASS' if c1 else 'FAIL'}  ({total_sharpe})")
    print(f"  2. Win rate > 55%:            {'PASS' if c2 else 'FAIL'}  ({total_win_pct:.1f}%)")
    print(f"  3. N trades >= 100:           {'PASS' if c3 else 'FAIL'}  ({total_n})")
    print(f"  4. >= 2/3 OOS folds positive: {'PASS' if c4 else 'FAIL'}  ({oos_positive_folds}/3)")
    print(f"  5. Pooled OOS Sharpe > 1.0:   {'PASS' if c5 else 'FAIL'}  ({oos_sharpe})")
    print()

    # -----------------------------------------------------------------
    # L16: Smart Filter adequacy (strict track)
    # -----------------------------------------------------------------
    print("=" * 72)
    print("SMART FILTER ADEQUACY — 30d rolling window (Binance lead-trader)")
    print("=" * 72)

    # Build per-trade records: exit_ts = entry_ts + 8h, pnl_pct = return_8h.
    sf_trades: list[dict] = []
    for df in per_coin_features.values():
        if df.empty or ret_col not in df.columns:
            continue
        mask = apply_combo(df, MARKET_FLUSH_FILTERS)
        trades_s = df.loc[mask, ret_col].dropna()
        for ts, r in trades_s.items():
            sf_trades.append({
                "exit_ts": ts + pd.Timedelta(hours=RANK_HOLDING_HOURS),
                "pnl_pct": float(r),
            })

    strict_verdict_info: dict = {
        "min_30d_td": None, "median_30d_td": None,
        "median_30d_win_days": None, "max_30d_mdd_abs": None,
        "strict_ok": False,
    }
    sample_days = 0

    if not sf_trades:
        print("  No trades — Smart Filter adequacy cannot be computed.")
    else:
        min_exit = min(t["exit_ts"] for t in sf_trades)
        max_exit = max(t["exit_ts"] for t in sf_trades)
        date_range = pd.date_range(
            start=min_exit.normalize(), end=max_exit.normalize(),
            freq="D", tz="UTC",
        )
        sample_days = len(date_range)
        daily = compute_daily_metrics(sf_trades, date_range)

        # Run 30/60/90d windows for diagnostics; strict gates use 30d.
        sf_30d: pd.DataFrame | None = None
        for cfg in SMART_FILTER_CONFIGS:
            wdf = simulate_smart_filter_windows(
                daily,
                window_days=cfg["window_days"],
                min_trading_days=cfg["min_trading_days"],
                win_days_threshold=cfg["win_days_threshold"],
                mdd_threshold=cfg["mdd_threshold"],
            )
            label = f"{cfg['window_days']}d"
            summary = summarize_smart_filter_results(wdf, label)
            if cfg["window_days"] == 30:
                sf_30d = wdf
            print(
                f"  {label:<4} windows: {summary['passed_windows']}/{summary['total_windows']} "
                f"passed  ({summary['pass_rate_pct']:.1f}%)  "
                f"[TD={summary['criterion_pass_rates']['trading_days']:.0f}%  "
                f"PnL+={summary['criterion_pass_rates']['pnl_positive']:.0f}%  "
                f"Win={summary['criterion_pass_rates']['win_days']:.0f}%  "
                f"MDD={summary['criterion_pass_rates']['mdd']:.0f}%]"
            )

        # Strict gates on 30d window.
        if sf_30d is not None and len(sf_30d) > 0:
            td = sf_30d["trading_days_in_window"]
            wr = sf_30d["win_days_ratio"].dropna()
            mdd = sf_30d["mdd_in_window_pct"].abs()
            strict_verdict_info.update({
                "min_30d_td": int(td.min()),
                "median_30d_td": float(td.median()),
                "median_30d_win_days": float(wr.median()) if len(wr) else None,
                "max_30d_mdd_abs": float(mdd.max()),
            })
            c6 = strict_verdict_info["min_30d_td"] >= 14
            c7 = strict_verdict_info["median_30d_td"] >= 14
            c8 = (
                strict_verdict_info["median_30d_win_days"] is not None
                and strict_verdict_info["median_30d_win_days"] >= 0.65
            )
            c9 = strict_verdict_info["max_30d_mdd_abs"] <= 20.0
            strict_verdict_info["strict_ok"] = c6 and c7 and c8 and c9
            print()
            print(f"  6. min 30d TD >= 14:          "
                  f"{'PASS' if c6 else 'FAIL'}  "
                  f"({strict_verdict_info['min_30d_td']})")
            print(f"  7. median 30d TD >= 14:       "
                  f"{'PASS' if c7 else 'FAIL'}  "
                  f"({strict_verdict_info['median_30d_td']:.1f})")
            mw = strict_verdict_info["median_30d_win_days"]
            mw_str = f"{mw:.2f}" if mw is not None else "N/A"
            print(f"  8. median 30d win-days >= 65%:"
                  f" {'PASS' if c8 else 'FAIL'}  ({mw_str})")
            print(f"  9. max 30d |MDD| <= 20%:      "
                  f"{'PASS' if c9 else 'FAIL'}  "
                  f"({strict_verdict_info['max_30d_mdd_abs']:.1f}%)")
        else:
            print("  Insufficient daily coverage for 30d rolling windows "
                  "(sample shorter than 30 days after trade exits).")

    # -----------------------------------------------------------------
    # Sample adequacy + dual-track verdict
    # -----------------------------------------------------------------
    print()
    print(f"  Sample span (trade days): {sample_days}")
    if 0 < sample_days < 180:
        print(f"  ⚠️  Sample < 180d (L8 reference) — statistical confidence "
              f"reduced; borderline PASS should be re-run on extended data.")

    suspicious_sharpe = (
        total_sharpe is not None and total_sharpe > 8.0
    )
    strict_ok = strict_verdict_info["strict_ok"]
    if suspicious_sharpe:
        verdict = "MARGINAL"
        verdict_note = (
            f"pooled Sharpe {total_sharpe:.2f} > 8.0 suggests look-ahead or "
            "outlier-driven edge — manual review required"
        )
    elif primary_ok and strict_ok:
        verdict = "PASS"
        verdict_note = "all 9 criteria met AND pooled Sharpe <= 8.0"
    elif primary_ok and not strict_ok:
        verdict = "MARGINAL"
        verdict_note = "primary 5 met; strict (6-9) partial — Smart Filter adequacy not fully cleared"
    else:
        verdict = "FAIL"
        verdict_note = "one or more primary criteria not met"

    print()
    print(f"  Result {interval}: {verdict}  — {verdict_note}")
    print()
    if verdict == "PASS":
        print(f"  Recommended action: promote {interval} to paper-trade "
              "parallel to h4; 14-day paper window before live.")
    elif verdict == "MARGINAL":
        print(f"  Recommended action: extend sample / investigate strict gate "
              "failures before any deployment decision.")
    else:
        print(f"  Recommended action: do NOT deploy {interval} strategy")

    # h4 sanity check
    if interval == "h4":
        print()
        print("=" * 72)
        print("SANITY CHECK — h4 should match L3b-2 reference")
        print("=" * 72)
        print(f"  L3b-2 reference: N=422, Win%=60.7, Sharpe=5.60")
        print(f"  This run:        N={total_n}, Win%={total_win_pct:.1f}, Sharpe={total_sharpe}")
        if total_n > 0 and abs(total_n - 422) < 50:
            print("  ✅ Trade count in expected range")
        elif total_n > 0:
            print("  ⚠️  Trade count differs significantly — investigate")


if __name__ == "__main__":
    main()
