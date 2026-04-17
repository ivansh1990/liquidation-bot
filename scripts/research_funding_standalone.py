#!/usr/bin/env python3
"""
L15 Phase 1: Funding rate z-score standalone research.

After L14 Phase 1 proved the market_flush architecture is structurally
incompatible with Binance Smart Filter (edge and temporal dispersion are
mutually exclusive), this script tests funding rate extremes as a new
continuous-by-design signal class on h4.

Matrix: 2 hypotheses × 3 z-thresholds × 1 timeframe (h4) = 6 variants +
baseline reference row. Funding is h8 at source; z-score computed on the
h8 series with a 45-bar window (15 calendar days) then ffilled to h4.

Hypotheses:
  H1 Contrarian SHORT:  funding_zscore > z   (crowded longs → flush down)
  H2 Contrarian LONG:   funding_zscore < -z  (crowded shorts → squeeze up)

PASS criteria (all 9):
  Primary (L8 parity):
    1) Pooled OOS Sharpe > 2.0
    2) Win% > 55
    3) N >= 100
    4) >= 2/3 OOS folds positive
    5) Pooled OOS Sharpe > 1.0 (redundant, kept per spec)
  Strict (Smart Filter adequacy, L13 Phase 3c / L14 Phase 1):
    6) Min 30d rolling trading days >= 14
    7) Median 30d rolling trading days >= 14
    8) Median 30d rolling win days ratio >= 65%
    9) Max 30d rolling absolute MDD <= 20%

Any pooled OOS Sharpe > SUSPICIOUS_SHARPE (8.0) auto-demotes to MARGINAL
(look-ahead guard).

Usage:
    .venv/bin/python scripts/research_funding_standalone.py
    .venv/bin/python scripts/research_funding_standalone.py \
        --hypotheses H1 --thresholds 2.0
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

import numpy as np
import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)

from collectors.config import COINS, binance_ccxt_symbol, get_config  # noqa: E402
from collectors.db import init_pool  # noqa: E402

from backtest_combo import _try_load_with_pepe_fallback, apply_combo  # noqa: E402
from backtest_market_flush_multitf import (  # noqa: E402
    WF_FOLDS,
    WF_MIN_TRADES,
    fetch_klines_ohlcv,
    load_funding,
)
from research_netposition import (  # noqa: E402
    SUSPICIOUS_SHARPE,
    _fmt_num,
    format_final_ranking,
    run_variant,
    run_walkforward,
)
from smart_filter_adequacy import (  # noqa: E402
    compute_daily_metrics,
    simulate_smart_filter_windows,
    summarize_smart_filter_results,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 15 calendar days at h8 cadence = 3 bars/day × 15 = 45 bars. Matches the
# 15-day window baked into market_flush (90 × h4 bars).
Z_WINDOW_FUNDING: int = 45

# Fixed 8h holding, time-based exit. Matches L3b-2 baseline convention.
HOLDING_HOURS: int = 8
BAR_HOURS_H4: int = 4

# Defensive floor for rolling std — avoids inf when the window is constant.
STD_EPS: float = 1e-12

DEFAULT_CAPITAL_USD: float = 1000.0

HYPOTHESES: tuple[str, ...] = ("H1", "H2")
DEFAULT_THRESHOLDS: tuple[float, ...] = (1.5, 2.0, 2.5)

# 30-day Smart Filter gate parameters (L13 Phase 3c canonical values).
SF_30D_WINDOW_DAYS: int = 30
SF_30D_MIN_TRADING_DAYS: int = 14
SF_30D_WIN_DAYS_THRESHOLD: float = 0.65
SF_30D_MDD_THRESHOLD_PCT: float = 20.0

# Binance klines history to fetch (matches CoinGlass Startup backfill).
FETCH_DAYS: int = 180


# ---------------------------------------------------------------------------
# Pure functions (test surface)
# ---------------------------------------------------------------------------

def compute_funding_zscore(
    series: pd.Series, window: int = Z_WINDOW_FUNDING,
) -> pd.Series:
    """
    Rolling per-coin z-score of the funding-rate series at the source h8
    cadence. First (window - 1) rows are NaN (strict min_periods=window —
    no warm-up partials). Zero-stddev periods return NaN rather than inf.
    """
    rolling_mean = series.rolling(window, min_periods=window).mean()
    rolling_std = series.rolling(window, min_periods=window).std()
    zscore = (series - rolling_mean) / rolling_std
    zscore = zscore.where(rolling_std >= STD_EPS, other=np.nan)
    return zscore


def build_funding_filters(
    hypothesis: str, z_fund: float,
) -> "tuple[list[tuple], str]":
    """
    Return (filter_list, direction) for a hypothesis + threshold. Filter is
    fed to apply_combo; direction ('long' | 'short') decides how returns
    are signed when trades are extracted.
    """
    if hypothesis == "H1":
        return [("funding_zscore", ">", z_fund)], "short"
    if hypothesis == "H2":
        return [("funding_zscore", "<", -z_fund)], "long"
    raise ValueError(f"Unknown hypothesis: {hypothesis!r} (valid: {HYPOTHESES})")


def apply_direction(
    df: pd.DataFrame, direction: str, holding_hours: int = HOLDING_HOURS,
) -> pd.DataFrame:
    """
    Return a copy of `df` with the `return_{holding_hours}h` column flipped
    when direction == 'short'. Long returns pass through unchanged.
    Downstream run_variant / run_walkforward read this column directly, so
    pooled metrics come out direction-consistent without any helper changes.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"Unknown direction: {direction!r}")
    out = df.copy()
    col = f"return_{holding_hours}h"
    if direction == "short" and col in out.columns:
        out[col] = -out[col]
    return out


def extract_trade_records(
    df: pd.DataFrame,
    mask: pd.Series,
    direction: str,
    holding_hours: int = HOLDING_HOURS,
) -> list[dict]:
    """
    Pull per-trade records from a direction-adjusted per-coin DF. The
    return column at this point already carries the correct sign (see
    apply_direction); we just copy it out as pnl_pct alongside timestamp
    metadata for Smart Filter daily aggregation.
    """
    col = f"return_{holding_hours}h"
    if col not in df.columns:
        return []
    trades = df.loc[mask, col].dropna()
    if trades.empty:
        return []
    coin = str(df["coin"].iloc[0]) if "coin" in df.columns else "?"
    delta = pd.Timedelta(hours=holding_hours)
    return [
        {
            "coin": coin,
            "entry_ts": ts,
            "exit_ts": ts + delta,
            "pnl_pct": float(pnl),
            "direction": direction,
        }
        for ts, pnl in trades.items()
    ]


def evaluate_verdict(
    variant: dict,
    wf: dict,
    sf_30d_window_df: pd.DataFrame,
) -> str:
    """
    Dual-track verdict. Primary criteria gated on pooled OOS Sharpe/Win%/N
    and fold positivity; strict criteria gated on 30d Smart Filter rolling
    windows (trading-day floors + win-days ratio + MDD). A Sharpe above
    SUSPICIOUS_SHARPE demotes to MARGINAL regardless of strict status.
    """
    pooled = variant.get("pooled") or {}
    pooled_oos = wf.get("pooled_oos") or {}
    if wf.get("skipped") or not pooled or not pooled_oos:
        return "FAIL"

    sharpe_oos = pooled_oos.get("sharpe")
    win_pct = pooled.get("win_pct")
    n_trades = pooled.get("n", 0)

    primary_ok = (
        sharpe_oos is not None
        and sharpe_oos > 2.0
        and sharpe_oos > 1.0  # formally redundant, kept per spec
        and win_pct is not None
        and win_pct > 55.0
        and n_trades >= 100
        and wf.get("oos_positive", 0) >= 2
    )
    if not primary_ok:
        return "FAIL"

    if sharpe_oos > SUSPICIOUS_SHARPE:
        return "MARGINAL"

    if sf_30d_window_df is None or len(sf_30d_window_df) == 0:
        return "MARGINAL"

    td_series = sf_30d_window_df["trading_days_in_window"]
    wdr_series = sf_30d_window_df["win_days_ratio"].dropna()
    mdd_series = sf_30d_window_df["mdd_in_window_pct"]

    min_td = int(td_series.min())
    median_td = float(td_series.median())
    median_wdr = float(wdr_series.median()) if not wdr_series.empty else 0.0
    # mdd_in_window_pct is <= 0; worst = most negative = .min()
    max_abs_mdd = abs(float(mdd_series.min())) if len(mdd_series) else 0.0

    strict_ok = (
        min_td >= SF_30D_MIN_TRADING_DAYS
        and median_td >= SF_30D_MIN_TRADING_DAYS
        and median_wdr >= SF_30D_WIN_DAYS_THRESHOLD
        and max_abs_mdd <= SF_30D_MDD_THRESHOLD_PCT
    )
    return "PASS" if strict_ok else "MARGINAL"


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_funding_features_h4(
    symbol: str, h4_index: pd.DatetimeIndex,
) -> pd.DataFrame:
    """
    Return a DataFrame indexed on `h4_index` with two columns:
    `funding_rate` and `funding_zscore`. The z-score is computed at the
    source h8 cadence (so the 45-bar window = 15 calendar days) and then
    ffilled to h4 — never recomputed on the ffilled series, which would
    give a different answer because of the 2× bar duplication.
    """
    df_h8 = load_funding(symbol)
    if df_h8.empty:
        return pd.DataFrame(
            {"funding_rate": np.nan, "funding_zscore": np.nan},
            index=h4_index,
        )
    df_h8 = df_h8.sort_index()
    zscore_h8 = compute_funding_zscore(df_h8["funding_rate"])
    h8_features = pd.DataFrame(
        {
            "funding_rate": df_h8["funding_rate"],
            "funding_zscore": zscore_h8,
        },
        index=df_h8.index,
    )
    return h8_features.reindex(h4_index, method="ffill")


def _load_coins_h4() -> dict[str, pd.DataFrame]:
    """
    Build per-coin feature frames for all tracked coins. Each frame has:
      coin, price, funding_rate, funding_zscore, return_{HOLDING_HOURS}h.
    Missing or invalid coins produce an empty DataFrame.
    """
    per_coin: dict[str, pd.DataFrame] = {}
    for coin in COINS:
        ccxt_sym = binance_ccxt_symbol(coin)
        since_ms = (
            int(
                (
                    pd.Timestamp.utcnow() - pd.Timedelta(days=FETCH_DAYS)
                ).timestamp() * 1000
            )
        )
        try:
            ohlcv = fetch_klines_ohlcv(ccxt_sym, since_ms, "4h")
        except Exception as e:
            print(f"  {coin:<5}: ccxt fetch failed ({e}) — skipped")
            per_coin[coin] = pd.DataFrame()
            continue
        if ohlcv.empty:
            print(f"  {coin:<5}: no price data — skipped")
            per_coin[coin] = pd.DataFrame()
            continue

        h4_index = ohlcv.index
        _, funding_feat = _try_load_with_pepe_fallback(
            coin, lambda s: _funding_loader_wrapper(s, h4_index),
        )
        if funding_feat is None or funding_feat.empty:
            print(f"  {coin:<5}: no funding data — skipped")
            per_coin[coin] = pd.DataFrame()
            continue

        periods = HOLDING_HOURS // BAR_HOURS_H4
        ret_col = f"return_{HOLDING_HOURS}h"
        fwd_return_pct = ohlcv["close"].pct_change(periods).shift(-periods) * 100

        df = pd.DataFrame(index=h4_index)
        df["coin"] = coin
        df["price"] = ohlcv["close"]
        df["funding_rate"] = funding_feat["funding_rate"]
        df["funding_zscore"] = funding_feat["funding_zscore"]
        df[ret_col] = fwd_return_pct
        df = df.dropna(subset=["price"])

        coverage = int(df["funding_zscore"].notna().mean() * 100) if len(df) else 0
        print(f"  {coin:<5}: rows={len(df)}  funding_zscore_coverage={coverage}%")
        per_coin[coin] = df

    return per_coin


def _funding_loader_wrapper(
    symbol: str, h4_index: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Adapter so _try_load_with_pepe_fallback's single-arg loader works."""
    return load_funding_features_h4(symbol, h4_index)


# ---------------------------------------------------------------------------
# Variant execution
# ---------------------------------------------------------------------------

def _apply_direction_to_all(
    per_coin: dict[str, pd.DataFrame], direction: str,
) -> dict[str, pd.DataFrame]:
    return {
        c: (apply_direction(df, direction, HOLDING_HOURS) if not df.empty else df)
        for c, df in per_coin.items()
    }


def _collect_trade_records(
    per_coin_dir: dict[str, pd.DataFrame],
    filters: list[tuple],
    direction: str,
) -> list[dict]:
    records: list[dict] = []
    for df in per_coin_dir.values():
        if df is None or df.empty:
            continue
        mask = apply_combo(df, filters)
        records.extend(
            extract_trade_records(df, mask, direction, HOLDING_HOURS)
        )
    return records


def _simulate_sf_30d(records: list[dict]) -> "tuple[pd.DataFrame, dict]":
    """Daily pnl series → 30d rolling Smart Filter window evaluation."""
    if not records:
        empty = pd.DataFrame(
            columns=[
                "trading_days_in_window", "pnl_sum_usd",
                "win_days_ratio", "mdd_in_window_pct",
                "g_trading_days", "g_pnl_positive",
                "g_win_days", "g_mdd", "passed",
            ],
        )
        return empty, summarize_smart_filter_results(empty, "30d")
    exits = [r["exit_ts"].normalize() for r in records]
    start = min(exits)
    end = max(exits)
    date_range = pd.date_range(start, end, freq="D", tz="UTC")
    daily = compute_daily_metrics(records, date_range, capital_usd=DEFAULT_CAPITAL_USD)
    window_df = simulate_smart_filter_windows(
        daily,
        window_days=SF_30D_WINDOW_DAYS,
        min_trading_days=SF_30D_MIN_TRADING_DAYS,
        win_days_threshold=SF_30D_WIN_DAYS_THRESHOLD,
        mdd_threshold=SF_30D_MDD_THRESHOLD_PCT,
    )
    summary = summarize_smart_filter_results(window_df, "30d")
    return window_df, summary


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
        return "—"
    return f"{float(v) * 100:5.1f}%"


def format_variant_block(
    name: str,
    hypothesis: str,
    z_fund: float,
    direction: str,
    variant: dict,
    wf: dict,
    sf_window_df: pd.DataFrame,
    sf_summary: dict,
    verdict: str,
) -> str:
    lines: list[str] = []
    lines.append("─" * 72)
    desc = "SHORT on + funding" if hypothesis == "H1" else "LONG on − funding"
    lines.append(
        f"VARIANT: {name}   ({desc}, |z|>{z_fund}, direction={direction}, h4)"
    )
    lines.append("─" * 72)

    v_pooled = variant.get("pooled") or {}
    lines.append(
        f"Signal metrics: N={v_pooled.get('n', 0)}  "
        f"Win%={_fmt_num(v_pooled.get('win_pct'))}  "
        f"Sharpe={_fmt_num(v_pooled.get('sharpe'))}  "
        f"trades/day={variant.get('trades_per_day', 0.0):.2f}"
    )
    lines.append("")

    if wf.get("skipped"):
        lines.append(f"Walk-forward: SKIPPED ({wf.get('reason', 'n/a')})")
    else:
        lines.append("Walk-forward:")
        for fold in wf["folds"]:
            sign = ""
            if fold["label"].startswith("OOS") and fold.get("sharpe") is not None:
                sign = "  ← positive" if fold["sharpe"] > 0 else "  ← negative"
            lines.append(
                f"  {fold['label']:<6} N={fold['n']:<4} "
                f"Win={_fmt_num(fold.get('win_pct'))}%  "
                f"Sharpe={_fmt_num(fold.get('sharpe'))}{sign}"
            )
        pooled_oos = wf.get("pooled_oos") or {}
        if pooled_oos:
            lines.append("")
            lines.append(
                f"Pooled OOS: N={pooled_oos.get('n')}  "
                f"Win={_fmt_num(pooled_oos.get('win_pct'))}%  "
                f"Sharpe={_fmt_num(pooled_oos.get('sharpe'))}  "
                f"positive folds {wf.get('oos_positive', 0)}/"
                f"{wf.get('oos_total', 0)}"
            )
    lines.append("")

    pooled_oos = wf.get("pooled_oos") or {}
    sharpe_oos = pooled_oos.get("sharpe")
    win_pct = v_pooled.get("win_pct")
    n_trades = v_pooled.get("n", 0)

    primary_checks = [
        (
            "Pooled OOS Sharpe > 2.0",
            sharpe_oos is not None and sharpe_oos > 2.0,
            _fmt_num(sharpe_oos),
        ),
        (
            "Win% > 55%",
            win_pct is not None and win_pct > 55.0,
            _fmt_num(win_pct),
        ),
        ("N trades >= 100", n_trades >= 100, str(n_trades)),
        (
            ">=2/3 OOS folds positive",
            wf.get("oos_positive", 0) >= 2,
            f"{wf.get('oos_positive', 0)}/{wf.get('oos_total', 0)}",
        ),
        (
            "Pooled OOS Sharpe > 1.0",
            sharpe_oos is not None and sharpe_oos > 1.0,
            _fmt_num(sharpe_oos),
        ),
    ]
    lines.append("Primary criteria (L8 parity, all must hold):")
    for label, ok, val in primary_checks:
        icon = "[OK]" if ok else "[--]"
        lines.append(f"  {icon} {label:<38} ({val})")

    lines.append("")
    lines.append("Strict criteria (Smart Filter 30d rolling windows):")
    if sf_window_df is None or len(sf_window_df) == 0:
        lines.append("  [--] No rolling windows (insufficient active trade days)")
    else:
        td = sf_window_df["trading_days_in_window"]
        wdr = sf_window_df["win_days_ratio"].dropna()
        mdd = sf_window_df["mdd_in_window_pct"]
        min_td = int(td.min())
        median_td = float(td.median())
        median_wdr = float(wdr.median()) if not wdr.empty else 0.0
        max_abs_mdd = abs(float(mdd.min())) if len(mdd) else 0.0

        strict_checks = [
            (
                f"Min 30d trading days >= {SF_30D_MIN_TRADING_DAYS}",
                min_td >= SF_30D_MIN_TRADING_DAYS,
                f"{min_td}",
            ),
            (
                f"Median 30d trading days >= {SF_30D_MIN_TRADING_DAYS}",
                median_td >= SF_30D_MIN_TRADING_DAYS,
                f"{median_td:.1f}",
            ),
            (
                f"Median 30d win days >= {int(SF_30D_WIN_DAYS_THRESHOLD * 100)}%",
                median_wdr >= SF_30D_WIN_DAYS_THRESHOLD,
                _fmt_pct(median_wdr),
            ),
            (
                f"Max 30d |MDD| <= {int(SF_30D_MDD_THRESHOLD_PCT)}%",
                max_abs_mdd <= SF_30D_MDD_THRESHOLD_PCT,
                f"{max_abs_mdd:.1f}%",
            ),
        ]
        for label, ok, val in strict_checks:
            icon = "[OK]" if ok else "[--]"
            lines.append(f"  {icon} {label:<38} ({val})")
        lines.append(
            f"  Windows evaluated: {sf_summary['total_windows']}  "
            f"passed all 4 gates: {sf_summary['passed_windows']}  "
            f"({sf_summary['pass_rate_pct']:.1f}%)"
        )

    if sharpe_oos is not None and sharpe_oos > SUSPICIOUS_SHARPE:
        lines.append("")
        lines.append(
            f"  [!!] suspicious Sharpe {sharpe_oos:.2f} > {SUSPICIOUS_SHARPE} "
            f"— manual review required"
        )

    lines.append("")
    lines.append(f"VERDICT: {verdict}")
    lines.append("─" * 72)
    return "\n".join(lines)


def _recommendation(ranking_rows: list[dict]) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("RECOMMENDATION")
    lines.append("=" * 72)
    variant_rows = [r for r in ranking_rows if r["verdict"] != "REF"]
    pass_rows = [r for r in variant_rows if r["verdict"] == "PASS"]
    marginal_rows = [r for r in variant_rows if r["verdict"] == "MARGINAL"]
    if pass_rows:
        lines.append("  → Phase 2 candidates (extend to h2/h1):")
        for r in pass_rows:
            lines.append(
                f"     • {r['name']}  Sharpe={_fmt_num(r['sharpe_oos'])}  "
                f"trades/d={r['trades_per_day']:.2f}"
            )
    elif marginal_rows:
        lines.append("  → No PASS variants. MARGINAL (review manually):")
        for r in marginal_rows:
            lines.append(
                f"     • {r['name']}  Sharpe={_fmt_num(r['sharpe_oos'])}  "
                f"trades/d={r['trades_per_day']:.2f}"
            )
        lines.append(
            "     Funding-standalone does not clear gates at h4; consider "
            "L15 Phase 2 (OI z-score) before moving to Phase 2 here."
        )
    else:
        lines.append(
            "  → All variants FAIL. Document funding-standalone as "
            "tested-and-rejected; proceed to L15 Phase 2 (OI z-score) "
            "or alternative signal class."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="L15 Phase 1: funding rate z-score standalone research.",
    )
    parser.add_argument(
        "--hypotheses", type=str, default=",".join(HYPOTHESES),
        help="Comma-separated hypotheses (default: H1,H2).",
    )
    parser.add_argument(
        "--thresholds", type=str,
        default=",".join(str(t) for t in DEFAULT_THRESHOLDS),
        help="Comma-separated z_fund thresholds (default: 1.5,2.0,2.5).",
    )
    args = parser.parse_args()

    hypotheses = [x.strip() for x in args.hypotheses.split(",") if x.strip()]
    thresholds = [float(x.strip()) for x in args.thresholds.split(",") if x.strip()]

    for h in hypotheses:
        if h not in HYPOTHESES:
            raise ValueError(f"Unknown hypothesis {h!r} (valid: {HYPOTHESES})")

    init_pool(get_config())

    print("=" * 72)
    print("L15 PHASE 1 — FUNDING RATE Z-SCORE STANDALONE RESEARCH")
    print("=" * 72)
    print(
        f"  hypotheses={hypotheses}  thresholds={thresholds}  "
        f"timeframe=h4  holding={HOLDING_HOURS}h"
    )
    print(
        f"  walk-forward {WF_FOLDS}-fold (min N={WF_MIN_TRADES})  "
        f"Smart Filter 30d gate: min_td>={SF_30D_MIN_TRADING_DAYS}, "
        f"win_days>={int(SF_30D_WIN_DAYS_THRESHOLD * 100)}%, "
        f"|mdd|<={int(SF_30D_MDD_THRESHOLD_PCT)}%"
    )
    print()

    print("=" * 72)
    print("LOADING PER-COIN FEATURES")
    print("=" * 72)
    per_coin = _load_coins_h4()
    print()

    ranking_rows: list[dict] = []

    # Reference row: market_flush-free bag, funding-only signal reference.
    # This is NOT a proper baseline (different signal class) — it just lists
    # the raw trading-grid stats so downstream readers can ground-truth
    # the variant report against per-coin coverage.
    ref_row = {
        "name": "REF_h4",
        "n": sum(int(df[f"return_{HOLDING_HOURS}h"].notna().sum())
                 for df in per_coin.values() if not df.empty),
        "win_pct": None,
        "sharpe_oos": None,
        "oos": "—",
        "trades_per_day": 0.0,
        "verdict": "REF",
    }
    ranking_rows.append(ref_row)

    for hypothesis in hypotheses:
        for z_fund in thresholds:
            name = f"{hypothesis}_z{z_fund}_h4"
            filters, direction = build_funding_filters(hypothesis, z_fund)
            per_coin_dir = _apply_direction_to_all(per_coin, direction)

            variant = run_variant(per_coin_dir, filters, HOLDING_HOURS)
            wf = run_walkforward(per_coin_dir, filters, HOLDING_HOURS)

            records = _collect_trade_records(per_coin_dir, filters, direction)
            sf_window_df, sf_summary = _simulate_sf_30d(records)

            verdict = evaluate_verdict(variant, wf, sf_window_df)

            # Explicit 100%-win OOS fold halt (look-ahead smell).
            if not wf.get("skipped"):
                for fold in wf.get("folds", []):
                    if (
                        fold["label"].startswith("OOS")
                        and fold.get("n", 0) > 0
                        and fold.get("win_pct") == 100.0
                    ):
                        print()
                        print(
                            f"[!!] STOPPING: {name} fold {fold['label']} "
                            f"reports 100% win rate (N={fold.get('n')}). "
                            f"Likely look-ahead in features. Investigate "
                            f"before trusting report."
                        )
                        sys.exit(2)

            print()
            print(format_variant_block(
                name, hypothesis, z_fund, direction,
                variant, wf, sf_window_df, sf_summary, verdict,
            ))

            v_pooled = variant.get("pooled") or {}
            pooled_oos = wf.get("pooled_oos") or {}
            ranking_rows.append({
                "name": name,
                "n": v_pooled.get("n", 0),
                "win_pct": v_pooled.get("win_pct"),
                "sharpe_oos": pooled_oos.get("sharpe"),
                "oos": (
                    f"{wf.get('oos_positive', 0)}/{wf.get('oos_total', 0)}"
                    if not wf.get("skipped") else "—"
                ),
                "trades_per_day": variant.get("trades_per_day", 0.0),
                "verdict": verdict,
            })

    print()
    print(format_final_ranking(ranking_rows))

    print()
    print(_recommendation(ranking_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
