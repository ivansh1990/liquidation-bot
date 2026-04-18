#!/usr/bin/env python3
"""
Session A — FLUSH_BOT TP-at-Cluster research.

Replays the locked L8 `market_flush` h4 baseline (~428 trades) and for each
trade substitutes the 8h time exit with a cluster-targeted take-profit at
configurable fraction-of-distance-to-short-cluster levels. Aggregates win%,
Sharpe, PF, MDD, Jensen α/β, and Smart Filter 30d adequacy per TP level,
then ranks, checks plateau stability, and emits a recommendation label.

Pure analysis — no changes to bot/, exchange/, telegram_bot/, collectors/,
or any locked L8 / L-jensen / L10–L16 script. Imports only.

Output: analysis/flush_tp_research_<UTC-timestamp>.md (gitignored).

Run:
    .venv/bin/python scripts/test_flush_tp_research.py    # offline
    .venv/bin/python scripts/flush_tp_research.py         # real data
"""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timezone
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

TP_LEVELS: list[float] = [0.50, 0.75, 0.80, 0.90, 0.99, 0.995, 1.00, 1.05, 1.10]
MAX_HOLD_HOURS: int = 8
CLUSTER_Z_THRESHOLD: float = 2.0
FRICTION_PP: float = 0.15  # 0.15 pp round-trip, matches L-jensen convention
DEFAULT_CAPITAL_USD: float = 1000.0
TRADING_DAYS_PER_YEAR: int = 365
ALPHA_COINS: list[str] = ["SOL", "ETH", "DOGE"]  # L-jensen-percoin subset

SF_MEDIAN_TD_THRESHOLD: float = 14.0
SF_MEDIAN_WR_THRESHOLD: float = 0.65
SF_MAX_ABS_MDD_THRESHOLD: float = 20.0

HIGH_FALLBACK_RATE_WARN_PCT: float = 30.0


# ---------------------------------------------------------------------------
# Cluster identification
# ---------------------------------------------------------------------------

def find_short_cluster_above(
    feat_df: pd.DataFrame,
    entry_ts: pd.Timestamp,
    entry_price: float,
    window_hours: int = MAX_HOLD_HOURS,
    z_threshold: float = CLUSTER_Z_THRESHOLD,
) -> dict:
    """
    Find the earliest forward h4 bar in the next `window_hours` whose
    `short_vol_zscore` exceeds `z_threshold` AND whose close price is above
    `entry_price`. Returns the bar's close as the cluster-price anchor.

    Returns: {"found": bool, "ts": Timestamp|None, "price": float|None}
    """
    if feat_df is None or feat_df.empty:
        return {"found": False, "ts": None, "price": None}
    window_end = entry_ts + pd.Timedelta(hours=window_hours)
    # Strictly forward: timestamp > entry_ts AND <= entry_ts + window.
    mask = (feat_df.index > entry_ts) & (feat_df.index <= window_end)
    sub = feat_df.loc[mask]
    if sub.empty:
        return {"found": False, "ts": None, "price": None}
    qualifying = sub[
        (sub["short_vol_zscore"] > z_threshold)
        & (sub["price"] > entry_price)
    ]
    if qualifying.empty:
        return {"found": False, "ts": None, "price": None}
    # Earliest qualifying bar.
    first_ts = qualifying.index[0]
    return {
        "found": True,
        "ts": first_ts,
        "price": float(qualifying.loc[first_ts, "price"]),
    }


# ---------------------------------------------------------------------------
# TP-exit simulation
# ---------------------------------------------------------------------------

def simulate_tp_exit(
    trade: dict,
    tp_level: float,
    cluster: dict,
    feat_df: pd.DataFrame,
    ohlc_df: pd.DataFrame,
    max_hold_hours: int = MAX_HOLD_HOURS,
) -> dict:
    """
    Simulate cluster-based exit for a single trade at one TP level.

    If cluster not found → 8h time exit at (entry + 8h) close.
    Otherwise:
      target = entry + (cluster - entry) * tp_level
      Walk forward h4 bars up to entry+max_hold_hours. If any bar high
      reaches target → fill at target at that bar's timestamp.
      Else fall back to (entry + max_hold_hours) close.

    Returns a dict including exit_price, gross_return_pct, net_return_pct,
    is_win, duration_hours, exit_reason, no_cluster_found.
    """
    entry_ts: pd.Timestamp = trade["entry_ts"]
    entry_price: float = float(trade["entry_price"])
    deadline = entry_ts + pd.Timedelta(hours=max_hold_hours)

    if not cluster["found"]:
        exit_price = _fallback_timeout_close(ohlc_df, deadline)
        return _build_exit_result(
            entry_ts, entry_price, exit_price, deadline,
            reason="timeout_no_cluster",
            no_cluster_found=True,
        )

    cluster_price = float(cluster["price"])
    # Long fade → target is ABOVE entry, cluster should be above entry.
    target = entry_price + (cluster_price - entry_price) * tp_level

    # Forward bars from entry_ts+1 (exclusive) to deadline (inclusive).
    fwd_mask = (ohlc_df.index > entry_ts) & (ohlc_df.index <= deadline)
    fwd = ohlc_df.loc[fwd_mask]
    if fwd.empty:
        exit_price = _fallback_timeout_close(ohlc_df, deadline)
        return _build_exit_result(
            entry_ts, entry_price, exit_price, deadline,
            reason="timeout", no_cluster_found=False,
        )

    hit_rows = fwd[fwd["high"] >= target]
    if hit_rows.empty:
        exit_price = float(fwd["close"].iloc[-1])
        # Last bar within window — its timestamp is the effective exit.
        exit_ts = fwd.index[-1]
        return _build_exit_result(
            entry_ts, entry_price, exit_price, exit_ts,
            reason="timeout", no_cluster_found=False,
        )
    exit_ts = hit_rows.index[0]
    return _build_exit_result(
        entry_ts, entry_price, target, exit_ts,
        reason="tp_hit", no_cluster_found=False,
    )


def _fallback_timeout_close(
    ohlc_df: pd.DataFrame, deadline: pd.Timestamp,
) -> float:
    """Close of the bar at or just before deadline."""
    mask = ohlc_df.index <= deadline
    sub = ohlc_df.loc[mask]
    if sub.empty:
        # Best-effort: fall back to whichever close we have.
        return float(ohlc_df["close"].iloc[-1]) if len(ohlc_df) else float("nan")
    return float(sub["close"].iloc[-1])


def _build_exit_result(
    entry_ts: pd.Timestamp,
    entry_price: float,
    exit_price: float,
    exit_ts: pd.Timestamp,
    reason: str,
    no_cluster_found: bool,
) -> dict:
    gross_ret_pct = (exit_price / entry_price - 1.0) * 100.0
    net_ret_pct = gross_ret_pct - FRICTION_PP
    duration_hours = (exit_ts - entry_ts).total_seconds() / 3600.0
    return {
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "gross_return_pct": gross_ret_pct,
        "net_return_pct": net_ret_pct,
        "is_win": net_ret_pct > 0,
        "duration_hours": duration_hours,
        "exit_reason": reason,
        "no_cluster_found": no_cluster_found,
    }


# ---------------------------------------------------------------------------
# Sweep + aggregate metrics
# ---------------------------------------------------------------------------

def _aggregate_metrics(
    per_trade: list[dict], holding_hours_anchor: int = MAX_HOLD_HOURS,
) -> dict:
    """Pooled metrics across a list of per-trade exit dicts."""
    n = len(per_trade)
    if n == 0:
        return {
            "n_trades": 0, "n_wins": 0, "win_pct": 0.0,
            "total_return_pp": 0.0, "avg_trade_pp": 0.0,
            "sharpe": 0.0, "profit_factor": 0.0, "max_dd_pct": 0.0,
            "avg_duration_hours": 0.0, "n_no_cluster_fallback": 0,
        }
    rets = np.array([t["net_return_pct"] for t in per_trade], dtype=float)
    durs = np.array([t["duration_hours"] for t in per_trade], dtype=float)
    n_wins = int((rets > 0).sum())
    n_no_cluster = sum(1 for t in per_trade if t["no_cluster_found"])
    total = float(rets.sum())
    avg = float(rets.mean())
    std = float(rets.std(ddof=1)) if n >= 2 else 0.0
    bars_per_year = TRADING_DAYS_PER_YEAR * 24.0 / holding_hours_anchor
    sharpe = (avg / std) * math.sqrt(bars_per_year) if std > 0 else 0.0
    pos = rets[rets > 0].sum()
    neg = -rets[rets < 0].sum()
    pf = float(pos / neg) if neg > 0 else (float("inf") if pos > 0 else 0.0)
    # Equity curve from pp series: cumsum → running max → drawdown.
    eq = rets.cumsum()
    run_max = np.maximum.accumulate(eq)
    dd = eq - run_max
    mdd = float(dd.min()) if len(dd) else 0.0
    return {
        "n_trades": n,
        "n_wins": n_wins,
        "win_pct": 100.0 * n_wins / n,
        "total_return_pp": total,
        "avg_trade_pp": avg,
        "sharpe": sharpe,
        "profit_factor": pf,
        "max_dd_pct": mdd,
        "avg_duration_hours": float(durs.mean()),
        "n_no_cluster_fallback": n_no_cluster,
    }


def _run_jensen_per_tp(
    per_trade: list[dict],
    trades_meta: list[dict],
    btc_daily_pp: pd.Series,
) -> dict:
    """Daily-aggregate per_trade net pnl across coins, regress on BTC."""
    from analysis.jensen_alpha import run_jensen_test

    if not per_trade or btc_daily_pp is None or btc_daily_pp.empty:
        return {"verdict": "INCONCLUSIVE", "n": 0}
    # Build a flat list of {exit_ts, pnl_pct} (pp), then aggregate per UTC day.
    pairs = [(t["exit_ts"], t["net_return_pct"]) for t in per_trade]
    s = pd.Series(
        [p for _, p in pairs],
        index=pd.DatetimeIndex([ts for ts, _ in pairs], tz="UTC"),
    )
    daily = s.groupby(s.index.normalize()).sum()
    daily.index = daily.index.tz_convert("UTC") if daily.index.tz else daily.index.tz_localize("UTC")
    # Reindex onto BTC's index so non-trading days get 0.
    btc = btc_daily_pp.copy()
    if btc.index.tz is None:
        btc.index = btc.index.tz_localize("UTC")
    common = btc.index.intersection(daily.index.union(btc.index))
    strategy = daily.reindex(common, fill_value=0.0)
    btc_aligned = btc.reindex(common).dropna()
    aligned = pd.DataFrame({"strategy": strategy, "btc": btc_aligned}).dropna()
    if len(aligned) < 30:
        return {"verdict": "INCONCLUSIVE", "n": int(len(aligned))}
    try:
        reg = run_jensen_test(aligned["strategy"], aligned["btc"], hac_lags=5)
    except Exception as e:  # unit-consistency guard or regression failure
        return {"verdict": "INCONCLUSIVE", "n": int(len(aligned)), "error": str(e)}
    return {
        "verdict": reg.get("verdict", "INCONCLUSIVE"),
        "n": reg.get("n", 0),
        "alpha": reg.get("alpha"),
        "beta": reg.get("beta"),
        "r_squared": reg.get("r_squared"),
        "alpha_pvalue": reg.get("alpha_pvalue"),
    }


def _run_smart_filter_per_tp(per_trade: list[dict]) -> dict:
    """30d Smart Filter adequacy on the per-TP trade set."""
    from smart_filter_adequacy import (
        compute_daily_metrics, simulate_smart_filter_windows,
        summarize_smart_filter_results, SMART_FILTER_CONFIGS,
    )
    if not per_trade:
        return {
            "median_30d_td": 0.0, "median_30d_wr": 0.0,
            "max_30d_abs_mdd": 0.0, "pass_rate_pct": 0.0, "sf_pass": False,
        }
    trades_adapted = [
        {"exit_ts": t["exit_ts"], "pnl_pct": t["net_return_pct"]}
        for t in per_trade
    ]
    exits = [t["exit_ts"].normalize() for t in trades_adapted]
    start, end = min(exits), max(exits)
    # Ensure UTC tz for range generator.
    if start.tz is None:
        start = start.tz_localize("UTC")
        end = end.tz_localize("UTC")
    date_range = pd.date_range(start, end, freq="D", tz="UTC")
    daily = compute_daily_metrics(trades_adapted, date_range, DEFAULT_CAPITAL_USD)
    cfg30 = SMART_FILTER_CONFIGS[0]
    windows = simulate_smart_filter_windows(
        daily,
        window_days=cfg30["window_days"],
        min_trading_days=cfg30["min_trading_days"],
        win_days_threshold=cfg30["win_days_threshold"],
        mdd_threshold=cfg30["mdd_threshold"],
    )
    summary = summarize_smart_filter_results(windows, "per_tp")
    if windows.empty:
        return {
            "median_30d_td": 0.0, "median_30d_wr": 0.0,
            "max_30d_abs_mdd": 0.0, "pass_rate_pct": 0.0, "sf_pass": False,
            "total_windows": 0,
        }
    median_td = float(windows["trading_days_in_window"].median())
    wr = windows["win_days_ratio"].dropna()
    median_wr = float(wr.median()) if len(wr) else 0.0
    max_abs_mdd = float(windows["mdd_in_window_pct"].abs().max())
    pass_rate = summary["pass_rate_pct"]
    sf_pass = (
        median_td >= SF_MEDIAN_TD_THRESHOLD
        and median_wr >= SF_MEDIAN_WR_THRESHOLD
        and max_abs_mdd <= SF_MAX_ABS_MDD_THRESHOLD
    )
    return {
        "median_30d_td": median_td,
        "median_30d_wr": median_wr,
        "max_30d_abs_mdd": max_abs_mdd,
        "pass_rate_pct": pass_rate,
        "sf_pass": sf_pass,
        "total_windows": int(summary["total_windows"]),
    }


def sweep_tp_levels(
    trades: list[dict],
    tp_levels: list[float],
    data_cache: dict[str, dict[str, pd.DataFrame]],
    max_hold_hours: int = MAX_HOLD_HOURS,
    btc_daily_pp: Optional[pd.Series] = None,
) -> list[dict]:
    """
    For each TP level, simulate all trades and compute aggregate metrics.
    btc_daily_pp is optional — when supplied, Jensen regression is added.
    Smart Filter metrics included when possible (≥ 30-day span).
    """
    per_coin_clusters: dict[str, dict[pd.Timestamp, dict]] = {}
    for coin in set(t["coin"] for t in trades):
        per_coin_clusters[coin] = {}
        feat = data_cache.get(coin, {}).get("feat")
        if feat is None or feat.empty:
            continue
    # Pre-compute clusters per trade (independent of TP level).
    for t in trades:
        coin = t["coin"]
        feat = data_cache.get(coin, {}).get("feat")
        if feat is None or feat.empty:
            per_coin_clusters.setdefault(coin, {})[t["entry_ts"]] = {
                "found": False, "ts": None, "price": None,
            }
            continue
        per_coin_clusters[coin][t["entry_ts"]] = find_short_cluster_above(
            feat, t["entry_ts"], t["entry_price"],
            window_hours=max_hold_hours,
            z_threshold=CLUSTER_Z_THRESHOLD,
        )

    out: list[dict] = []
    for tp in tp_levels:
        per_trade: list[dict] = []
        for t in trades:
            coin = t["coin"]
            feat = data_cache.get(coin, {}).get("feat")
            ohlc = data_cache.get(coin, {}).get("ohlc")
            if feat is None or ohlc is None:
                continue
            cluster = per_coin_clusters[coin][t["entry_ts"]]
            exit_info = simulate_tp_exit(
                t, tp, cluster, feat, ohlc, max_hold_hours=max_hold_hours,
            )
            exit_info["coin"] = coin
            per_trade.append(exit_info)

        metrics = _aggregate_metrics(per_trade)
        metrics["tp_level"] = tp
        metrics["per_trade"] = per_trade  # retained for per-coin / per-TP drills
        metrics["jensen"] = (
            _run_jensen_per_tp(per_trade, trades, btc_daily_pp)
            if btc_daily_pp is not None else
            {"verdict": "INCONCLUSIVE", "n": 0}
        )
        try:
            metrics["smart_filter"] = _run_smart_filter_per_tp(per_trade)
        except Exception as e:
            metrics["smart_filter"] = {
                "median_30d_td": 0.0, "median_30d_wr": 0.0,
                "max_30d_abs_mdd": 0.0, "pass_rate_pct": 0.0,
                "sf_pass": False, "error": str(e),
            }
        out.append(metrics)
    return out


# ---------------------------------------------------------------------------
# Plateau + recommendation
# ---------------------------------------------------------------------------

def plateau_check(sweep_results: list[dict]) -> dict:
    """
    Rank TP levels by Sharpe descending. Confirm plateau when the top TP's
    immediate neighbors (by tp_level ordering) are both in the top-3.
    Edge-case rule: top at the array boundary only has ONE neighbor; that one
    must be in top-3.
    """
    if len(sweep_results) < 3:
        return {"confirmed": False, "reason": "PLATEAU_UNKNOWN (need ≥3 levels)"}
    ordered = sorted(sweep_results, key=lambda r: r["tp_level"])
    tp_index = {r["tp_level"]: i for i, r in enumerate(ordered)}
    # Rank by Sharpe descending; skip NaN Sharpe.
    ranked = sorted(
        sweep_results,
        key=lambda r: (r.get("sharpe") if r.get("sharpe") is not None else -math.inf),
        reverse=True,
    )
    top = ranked[0]
    top_idx = tp_index[top["tp_level"]]
    top3_tps = {r["tp_level"] for r in ranked[:3]}
    neighbors = []
    if top_idx > 0:
        neighbors.append(ordered[top_idx - 1]["tp_level"])
    if top_idx < len(ordered) - 1:
        neighbors.append(ordered[top_idx + 1]["tp_level"])
    if not neighbors:
        return {"confirmed": False, "reason": "top has no neighbors"}
    confirmed = all(n in top3_tps for n in neighbors)
    return {
        "confirmed": confirmed,
        "top_tp": top["tp_level"],
        "top_sharpe": top["sharpe"],
        "top3_tps": sorted(top3_tps),
        "neighbors_checked": neighbors,
    }


def build_recommendation(
    sweep_results: list[dict],
    baseline_result: dict,
) -> dict:
    """Map sweep + baseline → one of 5 recommendation labels."""
    if not sweep_results:
        return {"label": "NO_IMPROVEMENT", "reasoning": "sweep empty"}

    verdicts = [r["jensen"]["verdict"] for r in sweep_results]
    if verdicts and all(v == "LEVERAGED_BETA" for v in verdicts):
        return {
            "label": "STRATEGY_QUESTIONABLE",
            "reasoning": (
                "All TP levels classify as LEVERAGED_BETA — strategy appears "
                "indistinguishable from leveraged BTC exposure regardless of "
                "exit logic. Halt and review thesis."
            ),
        }

    baseline_sharpe = baseline_result.get("sharpe", float("-inf"))
    best = max(sweep_results, key=lambda r: r.get("sharpe", float("-inf")))
    sharpe_beats_baseline = best["sharpe"] > baseline_sharpe

    if not sharpe_beats_baseline:
        return {
            "label": "NO_IMPROVEMENT",
            "reasoning": (
                f"Best TP-level Sharpe ({best['sharpe']:.3f}) does not exceed "
                f"baseline 8h-time-exit Sharpe ({baseline_sharpe:.3f}). Keep "
                "time exit; cluster TP adds no value."
            ),
            "best_tp": best["tp_level"],
        }

    plateau = plateau_check(sweep_results)
    jensen_ok = best["jensen"]["verdict"] != "LEVERAGED_BETA"
    sf_pass = best["smart_filter"].get("sf_pass", False)

    if sf_pass and plateau["confirmed"] and sharpe_beats_baseline and jensen_ok:
        return {
            "label": "DEPLOY_V2",
            "reasoning": (
                f"Best TP={best['tp_level']} clears Smart Filter, plateau is "
                "confirmed, Sharpe beats baseline, and Jensen verdict is "
                f"{best['jensen']['verdict']}. Proceed with paper-bot exit-"
                "logic update (Session A.1)."
            ),
            "best_tp": best["tp_level"],
            "plateau": plateau,
        }
    if sf_pass and (not plateau["confirmed"] or not sharpe_beats_baseline):
        return {
            "label": "SF_PASS_MARGINAL",
            "reasoning": (
                "Best TP clears Smart Filter but either plateau is unstable "
                "(possible overfit) or Sharpe doesn't exceed baseline. "
                "Discuss tradeoffs before any deploy."
            ),
            "best_tp": best["tp_level"],
            "plateau": plateau,
        }
    if sharpe_beats_baseline and jensen_ok and plateau["confirmed"] and not sf_pass:
        return {
            "label": "IMPROVEMENT_NO_SF",
            "reasoning": (
                f"Best TP={best['tp_level']} improves Sharpe and passes "
                "Jensen but doesn't clear Smart Filter adequacy. FLUSH_BOT v2 "
                "viable as a diversifier alongside MAGNET_BOT (Session Б)."
            ),
            "best_tp": best["tp_level"],
            "plateau": plateau,
        }
    # Catch-all: improvement exists but nothing clean lands.
    return {
        "label": "NO_IMPROVEMENT",
        "reasoning": (
            "Best TP beats baseline Sharpe but fails the remaining gates "
            "(Jensen LEVERAGED_BETA or unstable plateau). Treat as noise."
        ),
        "best_tp": best["tp_level"],
        "plateau": plateau,
    }


# ---------------------------------------------------------------------------
# Real-data loaders
# ---------------------------------------------------------------------------

def _build_data_cache_and_trades() -> tuple[
    list[dict], dict[str, dict[str, pd.DataFrame]], pd.Series,
]:
    """
    Load the locked L8 market_flush trade list and rebuild the per-coin
    feature + OHLCV frames needed for cluster detection and TP simulation.

    Returns (trades, data_cache, btc_daily_pp).
    """
    from collectors.config import COINS, binance_ccxt_symbol, get_config
    from collectors.db import init_pool
    from backtest_market_flush_multitf import (
        MARKET_FLUSH_FILTERS, RANK_HOLDING_HOURS,
        _interval_to_bar_hours, _interval_to_ccxt_timeframe,
        build_features_tf, fetch_klines_ohlcv, load_funding,
        load_liquidations_tf, load_oi_tf, CROSS_COIN_FLUSH_Z,
    )
    from backtest_combo import (
        _try_load_with_pepe_fallback, compute_cross_coin_features,
    )
    from validate_h1_z15_h2 import extract_trade_records
    from analysis.jensen_alpha import load_btc_daily_returns

    init_pool(get_config())

    bar_hours = _interval_to_bar_hours("h4")
    timeframe = _interval_to_ccxt_timeframe("h4")

    print("Loading h4 feature frames + OHLCV cache...")
    per_coin_feat: dict[str, pd.DataFrame] = {}
    per_coin_ohlc: dict[str, pd.DataFrame] = {}
    for coin in COINS:
        liq_sym, liq_df = _try_load_with_pepe_fallback(
            coin, lambda s: load_liquidations_tf(s, "h4"),
        )
        _, oi_df = _try_load_with_pepe_fallback(
            coin, lambda s: load_oi_tf(s, "h4"),
        )
        _, fund_df = _try_load_with_pepe_fallback(coin, load_funding)
        if liq_df.empty:
            print(f"  {coin:<5}: no liquidations — skipped")
            per_coin_feat[coin] = pd.DataFrame()
            continue
        ccxt_sym = binance_ccxt_symbol(coin)
        since_ms = int(liq_df["timestamp"].min().timestamp() * 1000)
        try:
            ohlcv_df = fetch_klines_ohlcv(ccxt_sym, since_ms, timeframe)
        except Exception as e:
            print(f"  {coin:<5}: ccxt fetch failed ({e}) — skipped")
            per_coin_feat[coin] = pd.DataFrame()
            continue
        feat = build_features_tf(
            coin, liq_sym, liq_df, oi_df, fund_df, ohlcv_df, bar_hours,
        )
        per_coin_feat[coin] = feat
        per_coin_ohlc[coin] = ohlcv_df
        print(f"  {coin:<5}: feat rows={len(feat)} ohlc rows={len(ohlcv_df)}")

    all_dfs = compute_cross_coin_features(per_coin_feat, flush_z=CROSS_COIN_FLUSH_Z)
    trades_raw = extract_trade_records(
        all_dfs, list(MARKET_FLUSH_FILTERS), RANK_HOLDING_HOURS,
    )
    if not trades_raw:
        raise RuntimeError("market_flush produced 0 trades — DB substrate empty.")

    # Rehydrate entry_price from the feature frame's `price` column.
    enriched: list[dict] = []
    for t in trades_raw:
        coin = t["coin"]
        feat = all_dfs.get(coin)
        if feat is None or feat.empty or t["entry_ts"] not in feat.index:
            continue
        entry_price = float(feat.loc[t["entry_ts"], "price"])
        enriched.append({
            "coin": coin,
            "entry_ts": t["entry_ts"],
            "exit_ts_baseline": t["exit_ts"],
            "entry_price": entry_price,
            "pnl_pct_baseline": float(t["pnl_pct"]),
        })

    # Build data cache keyed by coin.
    data_cache: dict[str, dict[str, pd.DataFrame]] = {}
    for coin in per_coin_feat:
        if per_coin_feat[coin].empty or coin not in per_coin_ohlc:
            continue
        # feat used for cluster detection uses cross-coin-augmented frame.
        data_cache[coin] = {
            "feat": all_dfs.get(coin, per_coin_feat[coin]),
            "ohlc": per_coin_ohlc[coin],
        }

    # BTC daily returns for Jensen regression.
    exits = [t["exit_ts_baseline"].normalize() for t in enriched]
    start = min(exits)
    end = max(exits)
    print(f"Fetching BTC daily klines for Jensen regression {start.date()}→{end.date()}...")
    try:
        btc_daily_pp = load_btc_daily_returns(start, end)
    except Exception as e:
        print(f"  BTC daily fetch failed ({e}) — Jensen regression will be INCONCLUSIVE")
        btc_daily_pp = pd.Series(dtype=float)

    return enriched, data_cache, btc_daily_pp


def _baseline_from_trades(trades: list[dict]) -> dict:
    """Baseline 8h time-exit metrics from the raw trade list (pre-friction)."""
    per_trade = []
    for t in trades:
        gross = float(t["pnl_pct_baseline"])
        net = gross - FRICTION_PP
        per_trade.append({
            "entry_ts": t["entry_ts"],
            "exit_ts": t["exit_ts_baseline"],
            "entry_price": t["entry_price"],
            "exit_price": t["entry_price"] * (1.0 + gross / 100.0),
            "gross_return_pct": gross,
            "net_return_pct": net,
            "is_win": net > 0,
            "duration_hours": 8.0,
            "exit_reason": "baseline_time",
            "no_cluster_found": False,
            "coin": t["coin"],
        })
    m = _aggregate_metrics(per_trade)
    m["tp_level"] = "baseline_8h_time"
    m["per_trade"] = per_trade
    return m


# ---------------------------------------------------------------------------
# Per-coin breakdown
# ---------------------------------------------------------------------------

def per_coin_breakdown(
    sweep_results: list[dict], coins: list[str] = ALPHA_COINS,
) -> dict[str, dict[float, dict]]:
    """For each (coin, TP level) compute the subset metrics."""
    out: dict[str, dict[float, dict]] = {}
    for coin in coins:
        out[coin] = {}
        for r in sweep_results:
            sub = [t for t in r.get("per_trade", []) if t["coin"] == coin]
            metrics = _aggregate_metrics(sub)
            out[coin][r["tp_level"]] = metrics
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt(v, spec: str = ".3f") -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "n/a"
    try:
        return format(v, spec)
    except Exception:
        return str(v)


_RECOMMENDATION_NEXT: dict[str, str] = {
    "DEPLOY_V2": "Session A.1 — paper-bot exit-logic update to cluster TP.",
    "SF_PASS_MARGINAL": "Discuss instability tradeoff; consider MAGNET_BOT instead.",
    "IMPROVEMENT_NO_SF": "Session Б (MAGNET_BOT) — FLUSH_BOT v2 as diversifier.",
    "NO_IMPROVEMENT": "Keep 8h time exit. Proceed directly to Session Б.",
    "STRATEGY_QUESTIONABLE": "HALT — architect review required.",
}


def format_flush_tp_report(
    *,
    sweep_results: list[dict],
    baseline: dict,
    recommendation: dict,
    plateau: dict,
    per_coin: dict[str, dict[float, dict]],
    n_trades: int,
    date_start: pd.Timestamp,
    date_end: pd.Timestamp,
    coin_coverage: dict[str, int],
) -> str:
    lines: list[str] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append("# Session A — FLUSH_BOT TP-at-Cluster Research")
    lines.append("")
    lines.append(f"_Generated {ts}_")
    lines.append("")

    # 1. Summary
    lines.append("## 1. Summary & recommendation")
    lines.append("")
    lines.append(f"**Label:** `{recommendation['label']}`")
    lines.append("")
    lines.append(recommendation["reasoning"])
    lines.append("")
    lines.append(f"**Next action:** {_RECOMMENDATION_NEXT.get(recommendation['label'], '—')}")
    lines.append("")

    # 2. Dataset
    lines.append("## 2. Dataset")
    lines.append("")
    lines.append(f"- Baseline trades (L8 market_flush h4, locked): **{n_trades}**")
    lines.append(f"- Date range: {date_start.date()} → {date_end.date()}")
    lines.append(f"- Coins: {', '.join(f'{c} ({n})' for c, n in coin_coverage.items())}")
    lines.append(f"- Friction: {FRICTION_PP} pp / trade (L-jensen convention)")
    lines.append(f"- Cluster rule: `short_vol_zscore > {CLUSTER_Z_THRESHOLD}` on h4 bar + bar_close > entry_price")
    lines.append(f"- Max hold: {MAX_HOLD_HOURS}h (2 h4 bars forward)")
    # Fallback rate check from baseline: N/A. Use any TP level's fallback count.
    if sweep_results:
        first = sweep_results[0]
        fb_pct = 100.0 * first["n_no_cluster_fallback"] / max(first["n_trades"], 1)
        lines.append(f"- Cluster coverage: {100.0 - fb_pct:.1f}% of trades (fallback rate {fb_pct:.1f}%)")
        if fb_pct > HIGH_FALLBACK_RATE_WARN_PCT:
            lines.append(f"  - ⚠️ Fallback rate >{HIGH_FALLBACK_RATE_WARN_PCT:.0f}% — sweep largely reflects baseline behavior.")
    lines.append("")

    # 3. Sweep table
    lines.append("## 3. Sweep results (per TP level)")
    lines.append("")
    lines.append("| TP | N | No-cluster | Win% | AvgPP | TotalPP | Sharpe | PF | MaxDD | AvgHours | Jensen | SF pass |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|")
    def _row(r):
        j = r["jensen"]
        s = r["smart_filter"]
        return (
            f"| {r['tp_level']} | {r['n_trades']} | {r['n_no_cluster_fallback']} | "
            f"{_fmt(r['win_pct'], '.1f')} | {_fmt(r['avg_trade_pp'], '+.3f')} | "
            f"{_fmt(r['total_return_pp'], '+.2f')} | {_fmt(r['sharpe'], '.2f')} | "
            f"{_fmt(r['profit_factor'], '.2f')} | {_fmt(r['max_dd_pct'], '.2f')} | "
            f"{_fmt(r['avg_duration_hours'], '.2f')} | {j['verdict']} | "
            f"{'YES' if s.get('sf_pass') else 'no'} |"
        )
    # Baseline row
    j_b = baseline.get("jensen", {"verdict": "—"})
    s_b = baseline.get("smart_filter", {"sf_pass": False})
    lines.append(
        f"| **baseline_8h** | {baseline['n_trades']} | — | "
        f"{_fmt(baseline['win_pct'], '.1f')} | {_fmt(baseline['avg_trade_pp'], '+.3f')} | "
        f"{_fmt(baseline['total_return_pp'], '+.2f')} | {_fmt(baseline['sharpe'], '.2f')} | "
        f"{_fmt(baseline['profit_factor'], '.2f')} | {_fmt(baseline['max_dd_pct'], '.2f')} | "
        f"{_fmt(baseline['avg_duration_hours'], '.2f')} | {j_b.get('verdict', '—')} | "
        f"{'YES' if s_b.get('sf_pass') else 'no'} |"
    )
    for r in sweep_results:
        lines.append(_row(r))
    lines.append("")

    # 4. Baseline vs best TP side-by-side
    best = max(sweep_results, key=lambda r: r["sharpe"]) if sweep_results else None
    lines.append("## 4. Baseline vs best-TP")
    lines.append("")
    lines.append("| Metric | Baseline (8h time) | Best TP |")
    lines.append("|---|---:|---:|")
    if best:
        lines.append(f"| TP level | — | {best['tp_level']} |")
        for key, label in [
            ("n_trades", "N"), ("win_pct", "Win%"),
            ("avg_trade_pp", "Avg pp"), ("total_return_pp", "Total pp"),
            ("sharpe", "Sharpe"), ("profit_factor", "PF"),
            ("max_dd_pct", "MaxDD pp"), ("avg_duration_hours", "AvgHours"),
        ]:
            lines.append(
                f"| {label} | {_fmt(baseline[key], '.3f')} | {_fmt(best[key], '.3f')} |"
            )
    else:
        lines.append("| — | — | — |")
    lines.append("")

    # 5. Per-coin breakdown
    lines.append(f"## 5. Per-coin breakdown (α-coins: {', '.join(ALPHA_COINS)})")
    lines.append("")
    for coin, by_tp in per_coin.items():
        lines.append(f"### {coin}")
        lines.append("")
        lines.append("| TP | N | Win% | AvgPP | Sharpe | MaxDD |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for tp in sorted(by_tp.keys()):
            m = by_tp[tp]
            lines.append(
                f"| {tp} | {m['n_trades']} | {_fmt(m['win_pct'], '.1f')} | "
                f"{_fmt(m['avg_trade_pp'], '+.3f')} | {_fmt(m['sharpe'], '.2f')} | "
                f"{_fmt(m['max_dd_pct'], '.2f')} |"
            )
        lines.append("")

    # 6. Plateau analysis
    lines.append("## 6. Plateau analysis")
    lines.append("")
    lines.append(f"- Top TP (by Sharpe): **{plateau.get('top_tp')}** (Sharpe={_fmt(plateau.get('top_sharpe'), '.3f')})")
    lines.append(f"- Top-3 TPs (by Sharpe): {plateau.get('top3_tps')}")
    lines.append(f"- Neighbors checked: {plateau.get('neighbors_checked')}")
    confirmed = plateau.get("confirmed")
    if confirmed is True:
        lines.append("- **Plateau confirmed** — top TP's immediate neighbors are both in top-3.")
    elif confirmed is False:
        lines.append("- **UNSTABLE_PEAK** — top TP neighbors are not both top-3; possible overfit.")
    else:
        lines.append(f"- Plateau undetermined: {plateau.get('reason', '—')}")
    lines.append("")

    # 7. Jensen regression for best TP
    lines.append("## 7. Jensen regression at best TP")
    lines.append("")
    if best and best["jensen"].get("n", 0) >= 30:
        j = best["jensen"]
        lines.append("| Statistic | Value |")
        lines.append("|---|---:|")
        lines.append(f"| N daily obs | {j['n']} |")
        lines.append(f"| α (daily pp) | {_fmt(j.get('alpha'), '+.4f')} |")
        lines.append(f"| β | {_fmt(j.get('beta'), '+.4f')} |")
        lines.append(f"| R² | {_fmt(j.get('r_squared'), '.4f')} |")
        lines.append(f"| α p-value (HAC, two-sided) | {_fmt(j.get('alpha_pvalue'), '.4g')} |")
        lines.append(f"| Verdict | **{j['verdict']}** |")
    else:
        lines.append("_N < 30 — regression skipped or no BTC data._")
    lines.append("")

    # 8. Smart Filter adequacy for best TP
    lines.append("## 8. Smart Filter adequacy at best TP (30d windows)")
    lines.append("")
    if best:
        s = best["smart_filter"]
        lines.append("| Metric | Value | Threshold |")
        lines.append("|---|---:|---:|")
        lines.append(f"| Median 30d trading days | {_fmt(s.get('median_30d_td'), '.1f')} | ≥ {SF_MEDIAN_TD_THRESHOLD:.0f} |")
        lines.append(f"| Median 30d win-day ratio | {_fmt(s.get('median_30d_wr'), '.3f')} | ≥ {SF_MEDIAN_WR_THRESHOLD:.2f} |")
        lines.append(f"| Max 30d |MDD| % | {_fmt(s.get('max_30d_abs_mdd'), '.2f')} | ≤ {SF_MAX_ABS_MDD_THRESHOLD:.1f} |")
        lines.append(f"| Pass rate 30d windows | {_fmt(s.get('pass_rate_pct'), '.1f')} | — |")
        lines.append(f"| **SF pass** | **{'YES' if s.get('sf_pass') else 'no'}** | — |")
    lines.append("")

    # 9. Recommendation recap
    lines.append("## 9. Recommendation recap")
    lines.append("")
    lines.append(f"- **Label:** `{recommendation['label']}`")
    lines.append(f"- **Best TP level:** {recommendation.get('best_tp', '—')}")
    lines.append(f"- **Reasoning:** {recommendation['reasoning']}")
    lines.append(f"- **Next action:** {_RECOMMENDATION_NEXT.get(recommendation['label'], '—')}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    trades, data_cache, btc_daily_pp = _build_data_cache_and_trades()
    print(f"Loaded {len(trades)} L8 baseline trades.")
    if not trades:
        print("ERROR: 0 trades — abort.")
        return 2

    baseline = _baseline_from_trades(trades)
    # Baseline's Jensen + SF run on the same pipeline for parity.
    baseline["jensen"] = _run_jensen_per_tp(baseline["per_trade"], trades, btc_daily_pp)
    try:
        baseline["smart_filter"] = _run_smart_filter_per_tp(baseline["per_trade"])
    except Exception as e:
        baseline["smart_filter"] = {
            "median_30d_td": 0.0, "median_30d_wr": 0.0,
            "max_30d_abs_mdd": 0.0, "pass_rate_pct": 0.0,
            "sf_pass": False, "error": str(e),
        }
    print(
        f"Baseline (8h time exit, post-friction): "
        f"N={baseline['n_trades']} Win%={baseline['win_pct']:.1f} "
        f"Sharpe={baseline['sharpe']:.2f} AvgPP={baseline['avg_trade_pp']:+.3f}"
    )

    print(f"\nRunning TP sweep over {TP_LEVELS} ...")
    sweep_results = sweep_tp_levels(
        trades, TP_LEVELS, data_cache,
        max_hold_hours=MAX_HOLD_HOURS,
        btc_daily_pp=btc_daily_pp if not btc_daily_pp.empty else None,
    )

    plateau = plateau_check(sweep_results)
    recommendation = build_recommendation(sweep_results, baseline)
    per_coin = per_coin_breakdown(sweep_results, ALPHA_COINS)

    # Coverage census from trades.
    coin_coverage: dict[str, int] = {}
    for t in trades:
        coin_coverage[t["coin"]] = coin_coverage.get(t["coin"], 0) + 1

    exits = [t["exit_ts_baseline"].normalize() for t in trades]
    date_start = min(exits)
    date_end = max(exits)

    report = format_flush_tp_report(
        sweep_results=sweep_results,
        baseline=baseline,
        recommendation=recommendation,
        plateau=plateau,
        per_coin=per_coin,
        n_trades=len(trades),
        date_start=date_start,
        date_end=date_end,
        coin_coverage=coin_coverage,
    )

    # Strip the heavy per_trade payloads before considering the sweep "done."
    for r in sweep_results:
        r.pop("per_trade", None)
    baseline.pop("per_trade", None)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = os.path.join(_PROJECT_ROOT, "analysis")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"flush_tp_research_{timestamp}.md")
    with open(out_path, "w") as f:
        f.write(report)

    print(f"\nRecommendation: {recommendation['label']} (best TP={recommendation.get('best_tp')})")
    print(f"Report written to: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
