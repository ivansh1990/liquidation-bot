#!/usr/bin/env python3
"""
Jensen's Alpha regression for the locked `market_flush` h4 strategy.

Pre-deployment gate: regress daily strategy P&L on daily BTC spot returns
(CAPM-style) to determine whether the L8 edge is genuine alpha or simply
leveraged BTC beta. Verdict labels drive the deployment decision.

Reuses (read-only):
  - scripts/validate_h1_z15_h2.py:extract_trade_records — per-trade list
  - scripts/smart_filter_adequacy.py:compute_daily_metrics — daily aggregation
  - scripts/research_netposition.py:_load_coins_for_interval — h4 substrate
  - scripts/backtest_market_flush_multitf.py:MARKET_FLUSH_FILTERS,
        RANK_HOLDING_HOURS, fetch_klines_ohlcv — locked signal & ccxt fetcher

Writes: analysis/jensen_report_<UTC-timestamp>.md (gitignored).

Run: .venv/bin/python analysis/jensen_alpha.py
Tests: .venv/bin/python scripts/test_jensen_alpha.py
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
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "scripts"))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_N_FOR_REGRESSION = 30
HAC_LAGS = 5
DEFAULT_CAPITAL_USD = 1000.0
TRADING_DAYS_PER_YEAR = 365  # crypto is 24/7

# Verdict thresholds (from spec)
GENUINE_BETA_MAX = 0.5
GENUINE_R2_MAX = 0.3
LEVERAGED_BETA_MIN = 0.7
LEVERAGED_R2_MIN = 0.4
WEAK_BETA_RANGE = (0.3, 0.7)
WEAK_R2_RANGE = (0.2, 0.4)
SIGNIFICANCE_PVALUE = 0.05

# Pre-flight unit-consistency guard: median |x| ratio must be within this band.
UNIT_RATIO_MIN = 0.01
UNIT_RATIO_MAX = 100.0


# ---------------------------------------------------------------------------
# Pre-flight unit guard (T1)
# ---------------------------------------------------------------------------

def assert_unit_consistency(
    strategy_returns: pd.Series, btc_returns: pd.Series,
) -> None:
    """
    Both series must be in the same unit (percentage points). Raises
    ValueError if median absolute magnitudes differ by more than 100x in
    either direction — a strong signal that one series is in decimals while
    the other is in pp.
    """
    s_med = float(np.median(np.abs(strategy_returns.dropna())))
    b_med = float(np.median(np.abs(btc_returns.dropna())))
    if s_med == 0 or b_med == 0:
        # Degenerate — let downstream handle it (e.g. INCONCLUSIVE on N=0).
        return
    ratio = s_med / b_med
    if ratio < UNIT_RATIO_MIN or ratio > UNIT_RATIO_MAX:
        offender = "strategy_returns" if ratio > 1.0 else "btc_returns"
        raise ValueError(
            f"Unit-magnitude mismatch detected: median |strategy|={s_med:.6g}, "
            f"median |btc|={b_med:.6g}, ratio={ratio:.3g} "
            f"(allowed [{UNIT_RATIO_MIN}, {UNIT_RATIO_MAX}]). "
            f"Likely cause: {offender} is in decimals while the other is in "
            f"percentage points. Convert both to pp (e.g. .pct_change(1) * 100) "
            f"before regression."
        )


# ---------------------------------------------------------------------------
# Annualization (T2): compound formula on 365-day crypto calendar
# ---------------------------------------------------------------------------

def annualize_alpha(alpha_daily_pct: float) -> float:
    """alpha_daily in pp → annualized in pp via compound (1+r)^365 - 1."""
    return ((1.0 + alpha_daily_pct / 100.0) ** TRADING_DAYS_PER_YEAR - 1.0) * 100.0


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------

def _run_ols(
    strategy_returns: pd.Series, btc_returns: pd.Series, hac_lags: int,
) -> Optional[dict]:
    """Plain OLS + HAC robust covariance. Returns regression result dict, or
    None if the inputs are too short / degenerate."""
    import statsmodels.api as sm  # deferred for fast --help / imports

    n = len(strategy_returns)
    if n < MIN_N_FOR_REGRESSION:
        return None
    X = sm.add_constant(btc_returns.values.astype(float))
    y = strategy_returns.values.astype(float)
    naive = sm.OLS(y, X).fit()
    robust = naive.get_robustcov_results(cov_type="HAC", maxlags=hac_lags)
    alpha = float(robust.params[0])
    beta = float(robust.params[1])
    alpha_se_hac = float(robust.bse[0])
    beta_se_hac = float(robust.bse[1])
    alpha_pvalue = float(robust.pvalues[0])
    beta_pvalue = float(robust.pvalues[1])
    ci = robust.conf_int()
    alpha_ci_low = float(ci[0][0])
    alpha_ci_high = float(ci[0][1])
    return {
        "n": int(n),
        "alpha": alpha,
        "beta": beta,
        "r_squared": float(naive.rsquared),
        "alpha_se_naive": float(naive.bse[0]),
        "alpha_se_hac": alpha_se_hac,
        "beta_se_naive": float(naive.bse[1]),
        "beta_se_hac": beta_se_hac,
        "alpha_pvalue": alpha_pvalue,
        "alpha_pvalue_naive": float(naive.pvalues[0]),
        "beta_pvalue": beta_pvalue,
        "alpha_ci_low": alpha_ci_low,
        "alpha_ci_high": alpha_ci_high,
    }


def resolve_verdict(reg: dict) -> str:
    """Map regression dict → one verdict label. Order matters."""
    n = reg.get("n", 0)
    if n < MIN_N_FOR_REGRESSION:
        return "INCONCLUSIVE"
    p = reg["alpha_pvalue"]
    a = reg["alpha"]
    b = reg["beta"]
    r2 = reg["r_squared"]
    if p < SIGNIFICANCE_PVALUE and a < 0:
        return "NEGATIVE_ALPHA"
    if p < SIGNIFICANCE_PVALUE and a > 0:
        if abs(b) < GENUINE_BETA_MAX and r2 < GENUINE_R2_MAX:
            return "GENUINE_ALPHA"
        return "MIXED_ALPHA_BETA"
    # p >= significance: no provable independent edge.
    if abs(b) > LEVERAGED_BETA_MIN and r2 > LEVERAGED_R2_MIN:
        return "LEVERAGED_BETA"
    if (
        WEAK_BETA_RANGE[0] <= abs(b) <= WEAK_BETA_RANGE[1]
        or WEAK_R2_RANGE[0] <= r2 <= WEAK_R2_RANGE[1]
    ):
        return "WEAK_SIGNAL"
    return "INCONCLUSIVE"


def run_jensen_test(
    strategy_returns: pd.Series, btc_returns: pd.Series, hac_lags: int = HAC_LAGS,
) -> dict:
    """Full pipeline: unit guard → OLS+HAC → verdict. Always returns a dict
    with `verdict` populated; on N<30 returns minimal `{n, verdict}`."""
    assert_unit_consistency(strategy_returns, btc_returns)
    reg = _run_ols(strategy_returns, btc_returns, hac_lags)
    if reg is None:
        n = len(strategy_returns)
        return {"n": int(n), "verdict": "INCONCLUSIVE"}
    reg["verdict"] = resolve_verdict(reg)
    return reg


# ---------------------------------------------------------------------------
# Clustering metrics (Smart-Filter-aware diagnostic block)
# ---------------------------------------------------------------------------

def compute_clustering_metrics(daily_metrics: pd.DataFrame) -> dict:
    """Trade-day clustering profile from compute_daily_metrics output. Pure."""
    if daily_metrics.empty:
        return {
            "total_calendar_days": 0,
            "total_trade_days": 0,
            "active_rate_pct": 0.0,
            "max_gap_days": 0,
            "gap_p50": 0,
            "gap_p90": 0,
            "gap_max": 0,
            "median_30d_trading_days": 0.0,
        }
    is_active = daily_metrics["is_active"].astype(bool).to_numpy()
    total_days = int(len(is_active))
    total_active = int(is_active.sum())
    active_rate = (total_active / total_days * 100.0) if total_days else 0.0

    # Inter-trade-day gaps: count consecutive False runs between True days.
    gaps: list[int] = []
    run = 0
    for active in is_active:
        if active:
            if run > 0:
                gaps.append(run)
            run = 0
        else:
            run += 1
    # Trailing inactive run is excluded — it is a partial gap, not bounded.
    if gaps:
        gp50 = int(np.percentile(gaps, 50))
        gp90 = int(np.percentile(gaps, 90))
        gmax = int(max(gaps))
    else:
        gp50 = gp90 = gmax = 0

    rolling = (
        daily_metrics["is_active"]
        .astype(int)
        .rolling(window=30, min_periods=30)
        .sum()
        .dropna()
    )
    median_30d = float(rolling.median()) if len(rolling) else 0.0

    return {
        "total_calendar_days": total_days,
        "total_trade_days": total_active,
        "active_rate_pct": active_rate,
        "max_gap_days": gmax,
        "gap_p50": gp50,
        "gap_p90": gp90,
        "gap_max": gmax,
        "median_30d_trading_days": median_30d,
    }


# ---------------------------------------------------------------------------
# Real-data loaders (DB + ccxt)
# ---------------------------------------------------------------------------

def _load_h4_substrate() -> dict[str, pd.DataFrame]:
    """
    Local h4 substrate loader. Mirrors `_load_coins_for_interval("h4")` from
    research_netposition.py minus the NetPos attach (irrelevant for
    market_flush) and uses `_interval_to_ccxt_timeframe` to build a valid
    ccxt timeframe string. The L16 `bar_minutes` refactor turned
    `_interval_to_bar_hours("h4")` into the float `4.0`, so the original
    `f"{bar_hours}h"` construction in research_netposition.py now produces
    the invalid `"4.0h"` (rejected by Binance with -1120). We bypass it.
    """
    from collectors.config import COINS, binance_ccxt_symbol
    from backtest_market_flush_multitf import (
        _interval_to_bar_hours,
        _interval_to_ccxt_timeframe,
        build_features_tf,
        fetch_klines_ohlcv,
        load_funding,
        load_liquidations_tf,
        load_oi_tf,
        CROSS_COIN_FLUSH_Z,
    )
    from backtest_combo import (
        _try_load_with_pepe_fallback,
        compute_cross_coin_features,
    )

    bar_hours = _interval_to_bar_hours("h4")
    timeframe = _interval_to_ccxt_timeframe("h4")

    per_coin: dict[str, pd.DataFrame] = {}
    for coin in COINS:
        liq_sym, liq_df = _try_load_with_pepe_fallback(
            coin, lambda s: load_liquidations_tf(s, "h4"),
        )
        _, oi_df = _try_load_with_pepe_fallback(
            coin, lambda s: load_oi_tf(s, "h4"),
        )
        _, fund_df = _try_load_with_pepe_fallback(coin, load_funding)

        if liq_df.empty:
            print(f"  {coin:<5}: no liquidation data — skipped")
            per_coin[coin] = pd.DataFrame()
            continue
        ccxt_sym = binance_ccxt_symbol(coin)
        since_ms = int(liq_df["timestamp"].min().timestamp() * 1000)
        try:
            ohlcv_df = fetch_klines_ohlcv(ccxt_sym, since_ms, timeframe)
        except Exception as e:
            print(f"  {coin:<5}: ccxt fetch failed ({e}) — skipped")
            per_coin[coin] = pd.DataFrame()
            continue

        feat = build_features_tf(
            coin, liq_sym, liq_df, oi_df, fund_df, ohlcv_df, bar_hours,
        )
        per_coin[coin] = feat
        print(f"  {coin:<5}: rows={len(feat)}")

    return compute_cross_coin_features(per_coin, flush_z=CROSS_COIN_FLUSH_Z)


def load_market_flush_daily_pnl() -> tuple[pd.Series, pd.DataFrame, list[dict]]:
    """
    Reconstruct the locked h4 `market_flush` trade list from DB substrate and
    aggregate to daily P&L.

    Returns
    -------
    daily_pnl_pct : pd.Series
        Daily strategy P&L in percentage points, indexed by UTC date.
    daily_metrics : pd.DataFrame
        Full compute_daily_metrics output (columns include is_active).
    trades : list[dict]
        Raw trade records for downstream parity checks.
    """
    from collectors.config import get_config
    from collectors.db import init_pool
    from backtest_market_flush_multitf import (
        MARKET_FLUSH_FILTERS, RANK_HOLDING_HOURS,
    )
    from validate_h1_z15_h2 import extract_trade_records
    from smart_filter_adequacy import compute_daily_metrics

    init_pool(get_config())
    print("Loading h4 coin dataframes (substrate for market_flush) ...")
    h4_dfs = _load_h4_substrate()
    trades = extract_trade_records(
        h4_dfs, list(MARKET_FLUSH_FILTERS), RANK_HOLDING_HOURS,
    )
    if not trades:
        raise RuntimeError("market_flush produced 0 trades — DB likely empty.")
    exits = [t["exit_ts"].normalize() for t in trades]
    start = min(exits)
    end = max(exits)
    date_range = pd.date_range(start, end, freq="D", tz="UTC")
    daily = compute_daily_metrics(trades, date_range, capital_usd=DEFAULT_CAPITAL_USD)
    return daily["pnl_pct"], daily, trades


def load_btc_daily_returns(start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """Daily BTC perp close-to-close returns in percentage points, indexed by
    UTC date (00:00 anchor). Inclusive of `end`."""
    from backtest_market_flush_multitf import fetch_klines_ohlcv

    since_ms = int(pd.Timestamp(start).tz_convert("UTC").timestamp() * 1000)
    df = fetch_klines_ohlcv("BTC/USDT:USDT", since_ms, "1d")
    if df.empty:
        raise RuntimeError("ccxt returned no BTC daily klines.")
    end_utc = pd.Timestamp(end).tz_convert("UTC") if end.tzinfo else pd.Timestamp(end, tz="UTC")
    df = df[df.index <= end_utc + pd.Timedelta(days=1)]
    closes = df["close"].astype(float)
    returns_pp = closes.pct_change(1) * 100.0
    returns_pp.index = returns_pp.index.normalize()
    return returns_pp.dropna()


# ---------------------------------------------------------------------------
# Subsample stability (robustness check)
# ---------------------------------------------------------------------------

def run_subsample_stability(
    strategy_returns: pd.Series, btc_returns: pd.Series,
) -> dict:
    """Rerun OLS+HAC on first half and second half. Used as a stability hint
    in the report; not a verdict gate."""
    n = len(strategy_returns)
    mid = n // 2
    s1 = strategy_returns.iloc[:mid]
    b1 = btc_returns.iloc[:mid]
    s2 = strategy_returns.iloc[mid:]
    b2 = btc_returns.iloc[mid:]
    out: dict = {}
    for label, s, b in [("first_half", s1, b1), ("second_half", s2, b2)]:
        reg = _run_ols(s, b, HAC_LAGS) if len(s) >= MIN_N_FOR_REGRESSION else None
        if reg is None:
            out[label] = None
        else:
            out[label] = {
                "alpha": reg["alpha"], "beta": reg["beta"],
                "r_squared": reg["r_squared"],
                "alpha_pvalue": reg["alpha_pvalue"], "n": reg["n"],
            }
    if out["first_half"] and out["second_half"]:
        out["sign_flip"] = (out["first_half"]["alpha"] * out["second_half"]["alpha"]) < 0
    else:
        out["sign_flip"] = None
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

_VERDICT_RECOMMENDATIONS = {
    "GENUINE_ALPHA": (
        "**Proceed with Binance lead trader deployment.** α is statistically "
        "significant, positive, and largely independent of BTC (β<0.5, R²<0.3). "
        "The strategy carries genuine independent edge. Continue paper trading "
        "in parallel as ongoing validation."
    ),
    "MIXED_ALPHA_BETA": (
        "**Deploy with explicit beta awareness.** α is significant and positive, "
        "but a meaningful share of returns comes from BTC exposure. Expected "
        "P&L will track BTC by approximately the β coefficient — Smart Filter "
        "MDD risk rises sharply in a BTC drawdown. Consider position sizing "
        "that accounts for the latent beta exposure."
    ),
    "LEVERAGED_BETA": (
        "**DO NOT deploy as independent alpha.** Strategy is statistically "
        "indistinguishable from leveraged BTC long (β>0.7, R²>0.4, α not "
        "significant). The 2024-2026 bull market makes this look profitable, "
        "but the next BTC correction will reveal it. Reassess thesis or only "
        "deploy a β-hedged variant."
    ),
    "NEGATIVE_ALPHA": (
        "**Stop paper trading.** α is significantly negative — friction "
        "(fees, slippage, timing) exceeds any latent edge. Implementation is "
        "net-negative independent of BTC direction. Reassess strategy from "
        "scratch."
    ),
    "WEAK_SIGNAL": (
        "**Treat as latent BTC exposure with no proven alpha.** β or R² sit "
        "in the partial-exposure zone but α fails the significance gate. Do "
        "NOT deploy as standalone. Either extend the sample size to firm up "
        "the α estimate, or reassess the underlying thesis."
    ),
    "INCONCLUSIVE": (
        "**Cannot decide from current data.** Either N<30 or the regression "
        "outputs landed outside every defined zone. Augment data: extend "
        "backfill window, run live paper for ≥30 days, or examine the trade "
        "list for anomalies before retrying."
    ),
}


def _fmt(v, spec: str = ".4f") -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "n/a"
    try:
        return format(v, spec)
    except Exception:
        return str(v)


def format_report(
    *,
    reg: dict,
    clustering: dict,
    subsample: dict,
    daily_strategy: pd.Series,
    daily_btc: pd.Series,
    sample_head: pd.DataFrame,
    aligned_n: int,
    date_start: pd.Timestamp,
    date_end: pd.Timestamp,
    n_trades: int,
) -> str:
    """Render the Markdown report. Pure."""
    verdict = reg.get("verdict", "INCONCLUSIVE")
    lines: list[str] = []

    def hr() -> None:
        lines.append("")

    lines.append(f"# Jensen's Alpha — `market_flush` h4")
    lines.append("")
    lines.append(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}._")
    hr()

    lines.append("## Verdict")
    lines.append("")
    lines.append(f"**`{verdict}`**")
    lines.append("")
    lines.append(_VERDICT_RECOMMENDATIONS.get(verdict, "(no recommendation defined)"))
    hr()

    lines.append("## Dataset summary")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Data source | backtest only (paper state empty) |")
    lines.append(f"| N trades | {n_trades} |")
    lines.append(f"| Aligned N daily observations | {aligned_n} |")
    lines.append(f"| Date range | {date_start.date()} → {date_end.date()} |")
    if not daily_strategy.empty:
        total_strat = float(daily_strategy.sum())
        total_btc = float(daily_btc.sum())
        lines.append(f"| Strategy total return (sum of daily pp) | {total_strat:+.2f} pp |")
        lines.append(f"| BTC total return (sum of daily pp) | {total_btc:+.2f} pp |")
    hr()

    lines.append("## Pre-flight unit sanity check")
    lines.append("")
    lines.append("First 5 aligned rows (both columns in percentage points):")
    lines.append("")
    lines.append("```")
    lines.append(sample_head.to_string())
    lines.append("```")
    s_med = float(np.median(np.abs(daily_strategy))) if len(daily_strategy) else 0.0
    b_med = float(np.median(np.abs(daily_btc))) if len(daily_btc) else 0.0
    lines.append("")
    lines.append(
        f"Median |strategy| = {s_med:.4f} pp · Median |BTC| = {b_med:.4f} pp · "
        f"ratio = {(s_med / b_med if b_med else float('inf')):.3g}  "
        f"(allowed [{UNIT_RATIO_MIN}, {UNIT_RATIO_MAX}]) — **PASS**"
    )
    hr()

    lines.append("## Regression (CAPM, OLS + Newey-West HAC)")
    lines.append("")
    if reg.get("n", 0) < MIN_N_FOR_REGRESSION:
        lines.append(f"_N={reg.get('n', 0)} < {MIN_N_FOR_REGRESSION} — regression skipped._")
    else:
        a = reg["alpha"]
        a_ann = annualize_alpha(a)
        a_p_one = (
            reg["alpha_pvalue"] / 2.0 if a > 0 else 1.0 - reg["alpha_pvalue"] / 2.0
        )
        lines.append("| Statistic | Value |")
        lines.append("|---|---|")
        lines.append(f"| N (daily observations) | {reg['n']} |")
        lines.append(
            f"| α (daily) | {_fmt(a, '+.4f')} pp/day "
            f"(`= mean daily independent return`) |"
        )
        lines.append(
            f"| α (annualized) | {_fmt(a_ann, '+.3f')} pp/year "
            f"(`= (1 + α_daily/100)^365 - 1`, compound, 24/7 calendar) |"
        )
        lines.append(f"| β | {_fmt(reg['beta'], '+.4f')} |")
        lines.append(f"| R² | {_fmt(reg['r_squared'], '.4f')} |")
        lines.append(
            f"| α p-value (HAC, two-sided) | {_fmt(reg['alpha_pvalue'], '.4g')} |"
        )
        lines.append(
            f"| α p-value (HAC, one-sided) | {_fmt(a_p_one, '.4g')} |"
        )
        lines.append(
            f"| α 95% CI (HAC) | [{_fmt(reg['alpha_ci_low'], '+.4f')}, "
            f"{_fmt(reg['alpha_ci_high'], '+.4f')}] pp/day |"
        )
        lines.append(
            f"| α SE — naive vs HAC | naive {_fmt(reg['alpha_se_naive'], '.4f')} · "
            f"HAC {_fmt(reg['alpha_se_hac'], '.4f')} "
            f"({reg['alpha_se_hac'] / reg['alpha_se_naive']:.2f}x) |"
        )
        lines.append(
            f"| β SE — naive vs HAC | naive {_fmt(reg['beta_se_naive'], '.4f')} · "
            f"HAC {_fmt(reg['beta_se_hac'], '.4f')} "
            f"({reg['beta_se_hac'] / reg['beta_se_naive']:.2f}x) |"
        )
        lines.append(
            f"| β p-value (HAC) | {_fmt(reg['beta_pvalue'], '.4g')} |"
        )
        # Manual flags
        flags: list[str] = []
        if reg["alpha_pvalue"] < 0.001 and reg["r_squared"] < 0.05:
            flags.append(
                "α highly significant with very low R² — inspect trade list "
                "before trusting (rare but real)."
            )
        if abs(reg["beta"]) > 1.5:
            flags.append("|β| > 1.5 — effective leverage on BTC factor exceeds nominal.")
        if flags:
            lines.append("")
            lines.append("**Sanity flags:**")
            for f in flags:
                lines.append(f"- {f}")
    hr()

    lines.append("## Clustering profile")
    lines.append("")
    lines.append("(Diagnostic — explains _why_ a high-Sharpe strategy may still fail Smart Filter adequacy.)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Calendar days | {clustering['total_calendar_days']} |")
    lines.append(f"| Trading days | {clustering['total_trade_days']} |")
    lines.append(f"| Active rate | {clustering['active_rate_pct']:.1f}% |")
    lines.append(f"| Max gap (consecutive non-trading days) | {clustering['max_gap_days']} |")
    lines.append(
        f"| Gap distribution (p50 / p90 / max) | "
        f"{clustering['gap_p50']} / {clustering['gap_p90']} / {clustering['gap_max']} |"
    )
    lines.append(
        f"| Median 30d rolling trading days | {clustering['median_30d_trading_days']:.1f} "
        f"(Smart Filter requires ≥14) |"
    )
    hr()

    lines.append("## Subsample stability")
    lines.append("")
    if subsample.get("first_half") and subsample.get("second_half"):
        f, s = subsample["first_half"], subsample["second_half"]
        lines.append("| Half | N | α | β | R² | α p (HAC) |")
        lines.append("|---|---|---|---|---|---|")
        lines.append(
            f"| First | {f['n']} | {_fmt(f['alpha'], '+.4f')} | "
            f"{_fmt(f['beta'], '+.4f')} | {_fmt(f['r_squared'], '.4f')} | "
            f"{_fmt(f['alpha_pvalue'], '.3g')} |"
        )
        lines.append(
            f"| Second | {s['n']} | {_fmt(s['alpha'], '+.4f')} | "
            f"{_fmt(s['beta'], '+.4f')} | {_fmt(s['r_squared'], '.4f')} | "
            f"{_fmt(s['alpha_pvalue'], '.3g')} |"
        )
        if subsample["sign_flip"]:
            lines.append("")
            lines.append("**⚠ α sign flips between halves — α estimate is unstable.**")
    else:
        lines.append(
            "_Subsample regression skipped — at least one half has N < 30._"
        )
    hr()

    lines.append("## Methodology notes")
    lines.append("")
    lines.append(
        "- Strategy P&L source: `extract_trade_records(h4_dfs, MARKET_FLUSH_FILTERS, "
        "RANK_HOLDING_HOURS=8)` from `scripts/validate_h1_z15_h2.py`, replaying "
        "the locked L8 baseline (Sharpe 5.87, Win 61.0%, N=428) byte-for-byte."
    )
    lines.append(
        "- Daily aggregation: `compute_daily_metrics(trades, date_range)` from "
        "`scripts/smart_filter_adequacy.py`. Non-trading days carry 0.0 pp."
    )
    lines.append(
        "- BTC factor: Binance USDM perpetual `BTC/USDT:USDT`, daily OHLCV "
        "via ccxt, `pct_change(1) * 100` close-to-close, UTC 00:00 anchor."
    )
    lines.append(
        "- Inference: OLS + Newey-West HAC standard errors, maxlags=5. The "
        "naive SE under-states uncertainty when residuals are autocorrelated "
        "(common in clustered-trading-day strategies); HAC is the correct "
        "default. Both shown for audit."
    )
    lines.append(
        "- Annualization: compound `(1 + α_daily/100)^365 − 1` for the "
        "crypto 24/7 calendar. Linear `α × 365` would understate compounding."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    daily_strategy_pp, daily_metrics, trades = load_market_flush_daily_pnl()
    if daily_strategy_pp.empty:
        print("ERROR: zero strategy P&L — abort.")
        return 2
    print(f"Loaded {len(trades)} trades over {len(daily_strategy_pp)} calendar days.")

    start_date = daily_strategy_pp.index.min()
    end_date = daily_strategy_pp.index.max()
    print(f"Fetching BTC daily klines {start_date.date()} → {end_date.date()} ...")
    btc_pp = load_btc_daily_returns(start_date, end_date)

    aligned = pd.DataFrame({
        "strategy": daily_strategy_pp,
        "btc": btc_pp,
    }).dropna()

    n_aligned = len(aligned)
    print(f"\nAligned N = {n_aligned} daily observations.")
    sample_head = aligned.head(5).round(4)
    print("\nSample (first 5 rows, both in pp):")
    print(sample_head.to_string())
    print()

    # Pre-flight unit guard (raises ValueError on mismatch).
    assert_unit_consistency(aligned["strategy"], aligned["btc"])

    reg = run_jensen_test(aligned["strategy"], aligned["btc"], hac_lags=HAC_LAGS)
    clustering = compute_clustering_metrics(daily_metrics)
    subsample = run_subsample_stability(aligned["strategy"], aligned["btc"])

    print(f"\nVerdict: {reg.get('verdict', 'INCONCLUSIVE')}")
    if reg.get("n", 0) >= MIN_N_FOR_REGRESSION:
        print(
            f"  α = {reg['alpha']:+.4f} pp/day "
            f"(annualized {annualize_alpha(reg['alpha']):+.3f} pp)"
        )
        print(f"  β = {reg['beta']:+.4f}")
        print(f"  R² = {reg['r_squared']:.4f}")
        print(f"  α p-value (HAC) = {reg['alpha_pvalue']:.4g}")

    report = format_report(
        reg=reg,
        clustering=clustering,
        subsample=subsample,
        daily_strategy=aligned["strategy"],
        daily_btc=aligned["btc"],
        sample_head=sample_head,
        aligned_n=n_aligned,
        date_start=start_date,
        date_end=end_date,
        n_trades=len(trades),
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(_THIS_DIR, f"jensen_report_{timestamp}.md")
    with open(out_path, "w") as f:
        f.write(report)
    print(f"\nReport written to: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
