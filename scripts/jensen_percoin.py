#!/usr/bin/env python3
"""
L-jensen-percoin — per-coin alpha/beta decomposition of `market_flush` h4.

Decomposes the portfolio-level L-jensen INCONCLUSIVE verdict into per-coin
regressions, then constructs a maximal coin subset whose combined portfolio
passes both Jensen (α p<0.05, |β|<0.7, R²<0.4) and Smart Filter adequacy
(median 30d trading days >=14, median win-day ratio >=65%, max |MDD| <=20%).

Reuse (read-only):
  - analysis/jensen_alpha.py         (helpers, loaders, constants)
  - scripts/validate_h1_z15_h2.py    (extract_trade_records)
  - scripts/smart_filter_adequacy.py (compute_daily_metrics,
                                      simulate_smart_filter_windows)

Writes: analysis/jensen_percoin_report_<UTC-timestamp>.md (gitignored).
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
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "analysis"))

from jensen_alpha import (
    DEFAULT_CAPITAL_USD,
    GENUINE_BETA_MAX,
    GENUINE_R2_MAX,
    HAC_LAGS,
    LEVERAGED_BETA_MIN,
    LEVERAGED_R2_MIN,
    MIN_N_FOR_REGRESSION,
    SIGNIFICANCE_PVALUE,
    TRADING_DAYS_PER_YEAR,
    _run_ols,
    annualize_alpha,
    assert_unit_consistency,
    load_btc_daily_returns,
    load_market_flush_daily_pnl,
    resolve_verdict,
    run_jensen_test,
    run_subsample_stability,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DESCRIPTIVE_ONLY_MIN_N = 15    # n_trades < this: descriptive only, no regression
REGRESSION_LOW_N_WARNING = 30  # n_trades < this but >=15: flagged low_n_warning

# Combined portfolio Jensen gates
COMBINED_BETA_MAX = 0.7
COMBINED_R2_MAX = 0.4
COMBINED_ALPHA_PVALUE_MAX = SIGNIFICANCE_PVALUE  # 0.05

# Combined Smart Filter gates (median over 30d rolling windows)
SF_WINDOW_DAYS = 30
SF_MIN_TRADING_DAYS = 14
SF_MIN_WIN_DAYS_RATIO = 0.65
SF_MAX_ABS_MDD_PCT = 20.0

# Stability categorization (CI1)
STABLE_BETA_RATIO_MAX = 1.5
PARTIAL_BETA_RATIO_MAX = 3.0
NEAR_ZERO_BETA_ABS = 0.2       # either half |β| < this → use abs-diff fallback
STABLE_ABS_BETA_DIFF_MAX = 0.5 # fallback threshold when either half β is near zero

# Overall stability flip threshold
UNSTABLE_SUBSET_RATIO = 0.5

# Eligible per-coin verdicts for subset membership
SUBSET_ELIGIBLE_VERDICTS = {"GENUINE_ALPHA", "MIXED_ALPHA_BETA"}


# ---------------------------------------------------------------------------
# Per-coin daily P&L
# ---------------------------------------------------------------------------

def per_coin_daily_pnl(
    trades: "list[dict]",
    date_range: pd.DatetimeIndex,
    capital_usd: float = DEFAULT_CAPITAL_USD,
) -> "dict[str, pd.DataFrame]":
    """Group trades by coin; return per-coin `compute_daily_metrics` DF."""
    from smart_filter_adequacy import compute_daily_metrics

    by_coin: dict[str, list[dict]] = {}
    for t in trades:
        by_coin.setdefault(t["coin"], []).append(t)
    out: dict[str, pd.DataFrame] = {}
    for coin, coin_trades in by_coin.items():
        out[coin] = compute_daily_metrics(coin_trades, date_range, capital_usd)
    return out


def per_coin_descriptive_stats(
    coin_daily: "dict[str, pd.DataFrame]",
    by_coin_trades: "dict[str, list[dict]]",
) -> pd.DataFrame:
    """Per-coin summary table: n_trades, total_return_pp, win_rate, etc."""
    rows = []
    for coin, df in coin_daily.items():
        trades = by_coin_trades.get(coin, [])
        n = len(trades)
        active_days = int(df["is_active"].sum())
        total_days = int(len(df))
        wins = sum(1 for t in trades if t["pnl_pct"] > 0)
        rows.append({
            "coin": coin,
            "n_trades": n,
            "total_return_pp": float(df["pnl_pct"].sum()),
            "win_rate": (wins / n) if n > 0 else 0.0,
            "mean_daily_pp": float(df["pnl_pct"].mean()),
            "std_daily_pp": float(df["pnl_pct"].std(ddof=1)) if total_days > 1 else 0.0,
            "active_days": active_days,
            "active_rate": (active_days / total_days) if total_days > 0 else 0.0,
        })
    return pd.DataFrame(rows).sort_values("n_trades", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Per-coin regression
# ---------------------------------------------------------------------------

def per_coin_regression(
    coin_daily: "dict[str, pd.DataFrame]",
    btc_pp: pd.Series,
) -> "dict[str, dict]":
    """
    For each coin: align daily pnl_pct with btc_pp, run Jensen test,
    return dict keyed by coin with regression output + verdict.

    Skips regression for coins with n_trades < DESCRIPTIVE_ONLY_MIN_N (15),
    returning {"verdict": "INSUFFICIENT_FOR_REGRESSION", "n": n_trades}.
    Flags low_n_warning when n_trades < REGRESSION_LOW_N_WARNING (30).
    """
    out: dict[str, dict] = {}
    for coin, df in coin_daily.items():
        n_trades = int(df["is_active"].sum())
        strategy_pp = df["pnl_pct"]
        aligned = pd.DataFrame({
            "strategy": strategy_pp,
            "btc": btc_pp,
        }).dropna()
        if n_trades < DESCRIPTIVE_ONLY_MIN_N:
            out[coin] = {
                "verdict": "INSUFFICIENT_FOR_REGRESSION",
                "n_trades": n_trades,
                "n": int(len(aligned)),
                "low_n_warning": True,
            }
            continue
        try:
            reg = run_jensen_test(aligned["strategy"], aligned["btc"], HAC_LAGS)
        except Exception as exc:
            out[coin] = {
                "verdict": "ERROR",
                "n_trades": n_trades,
                "n": int(len(aligned)),
                "error": str(exc),
                "low_n_warning": n_trades < REGRESSION_LOW_N_WARNING,
            }
            continue
        reg["n_trades"] = n_trades
        reg["low_n_warning"] = n_trades < REGRESSION_LOW_N_WARNING
        out[coin] = reg
    return out


# ---------------------------------------------------------------------------
# Combined portfolio gates
# ---------------------------------------------------------------------------

def combined_portfolio_daily_pnl(
    subset_coins: "list[str]",
    coin_daily: "dict[str, pd.DataFrame]",
) -> pd.Series:
    """Sum per-coin daily pnl_pct across subset. Index = shared date_range."""
    if not subset_coins:
        return pd.Series(dtype=float)
    first = coin_daily[subset_coins[0]]
    total = pd.Series(0.0, index=first.index)
    for coin in subset_coins:
        total = total.add(coin_daily[coin]["pnl_pct"], fill_value=0.0)
    return total


def combined_jensen(
    combined_pp: pd.Series, btc_pp: pd.Series,
) -> Optional[dict]:
    """Run Jensen regression on combined portfolio. Returns None if N<30."""
    aligned = pd.DataFrame({
        "strategy": combined_pp,
        "btc": btc_pp,
    }).dropna()
    if len(aligned) < MIN_N_FOR_REGRESSION:
        return None
    return run_jensen_test(aligned["strategy"], aligned["btc"], HAC_LAGS)


def combined_passes_jensen(reg: Optional[dict]) -> "tuple[bool, list[str]]":
    """Evaluate combined Jensen gate. Returns (pass, failing_criteria)."""
    if reg is None or "alpha" not in reg:
        return False, ["regression_skipped_low_n"]
    fails: list[str] = []
    if reg["alpha_pvalue"] >= COMBINED_ALPHA_PVALUE_MAX:
        fails.append(f"alpha_pvalue {reg['alpha_pvalue']:.4g} >= {COMBINED_ALPHA_PVALUE_MAX}")
    if reg["alpha"] <= 0:
        fails.append(f"alpha {reg['alpha']:+.4f} <= 0")
    if abs(reg["beta"]) >= COMBINED_BETA_MAX:
        fails.append(f"|beta| {abs(reg['beta']):.4f} >= {COMBINED_BETA_MAX}")
    if reg["r_squared"] >= COMBINED_R2_MAX:
        fails.append(f"R² {reg['r_squared']:.4f} >= {COMBINED_R2_MAX}")
    return (len(fails) == 0), fails


def combined_smart_filter(
    combined_pp: pd.Series,
    capital_usd: float = DEFAULT_CAPITAL_USD,
) -> dict:
    """
    Compute Smart Filter adequacy for a combined-portfolio daily pnl_pct
    series. Returns median-over-windows metrics AND pass flags.
    """
    from smart_filter_adequacy import simulate_smart_filter_windows

    if combined_pp.empty:
        return {
            "n_windows": 0,
            "median_trading_days": 0.0,
            "median_win_days_ratio": 0.0,
            "max_abs_mdd_pct": 0.0,
            "g_trading_days": False,
            "g_win_days": False,
            "g_mdd": False,
            "passed": False,
        }

    idx = combined_pp.index
    df = pd.DataFrame({
        "pnl_pct": combined_pp.values,
        "trade_count": (combined_pp != 0).astype(int).values,
    }, index=idx)
    df["pnl_usd"] = df["pnl_pct"] / 100.0 * capital_usd
    df["is_active"] = df["trade_count"] > 0
    df["is_winning"] = df["pnl_pct"] > 0
    df["equity_usd"] = capital_usd + df["pnl_usd"].cumsum()

    win_df = simulate_smart_filter_windows(
        df, SF_WINDOW_DAYS, SF_MIN_TRADING_DAYS,
        SF_MIN_WIN_DAYS_RATIO, SF_MAX_ABS_MDD_PCT,
    )
    if win_df.empty:
        return {
            "n_windows": 0,
            "median_trading_days": 0.0,
            "median_win_days_ratio": 0.0,
            "max_abs_mdd_pct": 0.0,
            "g_trading_days": False,
            "g_win_days": False,
            "g_mdd": False,
            "passed": False,
        }

    median_td = float(win_df["trading_days_in_window"].median())
    win_ratios = win_df["win_days_ratio"].dropna()
    median_wd = float(win_ratios.median()) if len(win_ratios) else 0.0
    max_abs_mdd = float(win_df["mdd_in_window_pct"].abs().max())

    g_td = median_td >= SF_MIN_TRADING_DAYS
    g_wd = median_wd >= SF_MIN_WIN_DAYS_RATIO
    g_mdd = max_abs_mdd <= SF_MAX_ABS_MDD_PCT

    return {
        "n_windows": int(len(win_df)),
        "median_trading_days": median_td,
        "median_win_days_ratio": median_wd,
        "max_abs_mdd_pct": max_abs_mdd,
        "g_trading_days": g_td,
        "g_win_days": g_wd,
        "g_mdd": g_mdd,
        "passed": bool(g_td and g_wd and g_mdd),
    }


def combined_sf_failing_criteria(sf: dict) -> "list[str]":
    """Return human-readable list of failing SF criteria."""
    fails: list[str] = []
    if sf["n_windows"] == 0:
        fails.append("no_windows_available")
        return fails
    if not sf["g_trading_days"]:
        fails.append(
            f"median_trading_days {sf['median_trading_days']:.1f} < {SF_MIN_TRADING_DAYS}"
        )
    if not sf["g_win_days"]:
        fails.append(
            f"median_win_days_ratio {sf['median_win_days_ratio']:.3f} < {SF_MIN_WIN_DAYS_RATIO}"
        )
    if not sf["g_mdd"]:
        fails.append(
            f"max_abs_mdd_pct {sf['max_abs_mdd_pct']:.2f} > {SF_MAX_ABS_MDD_PCT}"
        )
    return fails


# ---------------------------------------------------------------------------
# Subset construction (greedy + SF1 fallback)
# ---------------------------------------------------------------------------

def build_subset(
    regs: "dict[str, dict]",
    coin_daily: "dict[str, pd.DataFrame]",
    btc_pp: pd.Series,
) -> dict:
    """
    Greedy subset construction with SF1 fallback.

    Steps:
      1. Seed = all coins with verdict GENUINE_ALPHA.
      2. If seed empty: add MIXED coins one by one (ascending alpha_pvalue)
         until gates pass or MIXED exhausted.
      3. If seed non-empty but fails gates: remove highest |β_contribution|
         one by one until passes, or N=1.
      4. After seed passes: extend with MIXED coins (ascending α p-value)
         until a gate fails.

    Returns dict with keys:
      subset, trace (list of (action, coin, pass, combined_reg, combined_sf)),
      combined_reg, combined_sf, combined_jensen_pass, combined_sf_pass,
      combined_jensen_fails, combined_sf_fails.
    """
    genuine = [c for c, r in regs.items() if r.get("verdict") == "GENUINE_ALPHA"]
    mixed = [
        c for c, r in regs.items() if r.get("verdict") == "MIXED_ALPHA_BETA"
    ]
    mixed.sort(key=lambda c: regs[c].get("alpha_pvalue", 1.0))

    trace: list[dict] = []

    def evaluate(subset: "list[str]") -> dict:
        combined_pp = combined_portfolio_daily_pnl(subset, coin_daily)
        reg = combined_jensen(combined_pp, btc_pp)
        jp, jfails = combined_passes_jensen(reg)
        sf = combined_smart_filter(combined_pp)
        sfp = sf["passed"]
        sffails = combined_sf_failing_criteria(sf)
        return {
            "subset": list(subset),
            "combined_reg": reg,
            "combined_sf": sf,
            "combined_jensen_pass": jp,
            "combined_sf_pass": sfp,
            "combined_jensen_fails": jfails,
            "combined_sf_fails": sffails,
            "both_pass": jp and sfp,
        }

    if not genuine and not mixed:
        return {
            "subset": [],
            "trace": [],
            "combined_reg": None,
            "combined_sf": {},
            "combined_jensen_pass": False,
            "combined_sf_pass": False,
            "combined_jensen_fails": ["no_eligible_coins"],
            "combined_sf_fails": ["no_eligible_coins"],
            "no_subset_reason": "no coin has GENUINE_ALPHA or MIXED_ALPHA_BETA verdict",
        }

    # Seed phase
    seed = list(genuine)
    best: Optional[dict] = None

    if seed:
        current = evaluate(seed)
        trace.append({"action": "seed_genuine", "subset": list(seed), **current})
        if current["both_pass"]:
            best = current
        else:
            # SF1: remove highest |beta_contribution| until passes or N=1.
            while len(seed) > 1 and not current["both_pass"]:
                beta_contrib = _beta_contributions(seed, regs)
                drop = max(beta_contrib.items(), key=lambda kv: abs(kv[1]))[0]
                seed.remove(drop)
                current = evaluate(seed)
                trace.append({
                    "action": f"remove_high_beta[{drop}]",
                    "subset": list(seed), **current,
                })
                if current["both_pass"]:
                    best = current
                    break
            if best is None and current["both_pass"]:
                best = current
    else:
        current = None

    # If seed exhausted without pass, try each MIXED coin solo or seed+MIXED
    if best is None:
        # Try adding MIXED coins one at a time to the empty/remaining seed
        working = list(seed) if seed and current and current["both_pass"] else []
        for coin in mixed:
            candidate = working + [coin]
            current = evaluate(candidate)
            trace.append({"action": f"try_add_mixed[{coin}]", "subset": list(candidate), **current})
            if current["both_pass"]:
                working = candidate
                best = current
                break
        # If still no pass, try each solo (including GENUINE solos, for SF1 N=1 fallback)
        if best is None and genuine:
            # Try each genuine coin solo
            for coin in genuine:
                current = evaluate([coin])
                trace.append({"action": f"try_solo_genuine[{coin}]", "subset": [coin], **current})
                if current["both_pass"]:
                    best = current
                    break

    if best is None:
        return {
            "subset": [],
            "trace": trace,
            "combined_reg": None,
            "combined_sf": {},
            "combined_jensen_pass": False,
            "combined_sf_pass": False,
            "combined_jensen_fails": current["combined_jensen_fails"] if current else ["exhausted"],
            "combined_sf_fails": current["combined_sf_fails"] if current else ["exhausted"],
            "no_subset_reason": "exhausted all coin combinations without passing both gates",
        }

    # Extension phase: try to add more MIXED coins to best subset
    current = best
    for coin in mixed:
        if coin in current["subset"]:
            continue
        candidate = current["subset"] + [coin]
        cand_eval = evaluate(candidate)
        trace.append({"action": f"extend_mixed[{coin}]", "subset": list(candidate), **cand_eval})
        if cand_eval["both_pass"]:
            current = cand_eval

    return {
        "subset": list(current["subset"]),
        "trace": trace,
        "combined_reg": current["combined_reg"],
        "combined_sf": current["combined_sf"],
        "combined_jensen_pass": current["combined_jensen_pass"],
        "combined_sf_pass": current["combined_sf_pass"],
        "combined_jensen_fails": current["combined_jensen_fails"],
        "combined_sf_fails": current["combined_sf_fails"],
    }


def _beta_contributions(
    subset: "list[str]", regs: "dict[str, dict]",
) -> "dict[str, float]":
    """β_contribution[c] = β_c * (n_trades_c / sum(n_trades))."""
    total_n = sum(regs[c].get("n_trades", 0) for c in subset)
    if total_n == 0:
        return {c: 0.0 for c in subset}
    return {
        c: regs[c].get("beta", 0.0) * (regs[c].get("n_trades", 0) / total_n)
        for c in subset
    }


# ---------------------------------------------------------------------------
# Stability categorization (CI1)
# ---------------------------------------------------------------------------

def categorize_stability(subsample: dict) -> "tuple[str, dict]":
    """
    Categorize per-coin regime stability: STABLE / PARTIAL / UNSTABLE.

    Returns (category, detail_dict).
    detail_dict has: first_half, second_half, beta_ratio_or_diff, method.
    """
    first = subsample.get("first_half")
    second = subsample.get("second_half")
    if not first or not second:
        return "UNKNOWN", {"reason": "first or second half N<30"}

    a1 = first["alpha"]
    a2 = second["alpha"]
    b1 = first["beta"]
    b2 = second["beta"]

    sign_flip = (a1 * a2) < 0
    if sign_flip:
        return "UNSTABLE", {
            "reason": "alpha sign flip",
            "first_alpha": a1, "second_alpha": a2,
            "first_beta": b1, "second_beta": b2,
        }

    # β ratio or abs-diff (fallback near zero)
    near_zero = abs(b1) < NEAR_ZERO_BETA_ABS or abs(b2) < NEAR_ZERO_BETA_ABS
    if near_zero:
        diff = abs(b2 - b1)
        if diff <= STABLE_ABS_BETA_DIFF_MAX:
            return "STABLE", {
                "method": "abs_diff",
                "beta_abs_diff": diff,
                "first_beta": b1, "second_beta": b2,
            }
        elif diff <= STABLE_ABS_BETA_DIFF_MAX * 2:
            return "PARTIAL", {
                "method": "abs_diff",
                "beta_abs_diff": diff,
                "first_beta": b1, "second_beta": b2,
            }
        else:
            return "UNSTABLE", {
                "reason": "beta abs-diff out of range",
                "method": "abs_diff",
                "beta_abs_diff": diff,
                "first_beta": b1, "second_beta": b2,
            }

    ratio = abs(b2 / b1)
    inv_ratio = 1.0 / ratio if ratio > 0 else float("inf")
    effective = max(ratio, inv_ratio)
    if effective <= STABLE_BETA_RATIO_MAX:
        category = "STABLE"
    elif effective <= PARTIAL_BETA_RATIO_MAX:
        category = "PARTIAL"
    else:
        category = "UNSTABLE"
    return category, {
        "method": "ratio",
        "beta_ratio": effective,
        "first_beta": b1, "second_beta": b2,
        "first_alpha": a1, "second_alpha": a2,
    }


def subset_stability_summary(
    subset: "list[str]",
    coin_daily: "dict[str, pd.DataFrame]",
    btc_pp: pd.Series,
) -> dict:
    """Run subsample stability per coin; aggregate category counts."""
    per_coin: dict[str, dict] = {}
    counts = {"STABLE": 0, "PARTIAL": 0, "UNSTABLE": 0, "UNKNOWN": 0}
    for coin in subset:
        df = coin_daily[coin]
        aligned = pd.DataFrame({
            "strategy": df["pnl_pct"],
            "btc": btc_pp,
        }).dropna()
        subsample = run_subsample_stability(aligned["strategy"], aligned["btc"])
        category, detail = categorize_stability(subsample)
        per_coin[coin] = {
            "category": category,
            "subsample": subsample,
            "detail": detail,
        }
        counts[category] = counts.get(category, 0) + 1

    total = len(subset)
    unstable_ratio = (counts["UNSTABLE"] / total) if total > 0 else 0.0
    is_regime_dependent = unstable_ratio >= UNSTABLE_SUBSET_RATIO and total > 0

    return {
        "per_coin": per_coin,
        "counts": counts,
        "unstable_ratio": unstable_ratio,
        "is_regime_dependent": is_regime_dependent,
    }


# ---------------------------------------------------------------------------
# Recent-window check (SF2)
# ---------------------------------------------------------------------------

def recent_window_check(
    combined_pp: pd.Series,
    window_days: int = SF_WINDOW_DAYS,
    capital_usd: float = DEFAULT_CAPITAL_USD,
) -> dict:
    """
    Evaluate the last `window_days` of the combined portfolio against the
    three SF gates (trading days, win ratio, |MDD|). Non-blocking — returns
    a flag dict.
    """
    if combined_pp.empty or len(combined_pp) < window_days:
        return {
            "checked": False,
            "reason": f"series shorter than {window_days} days",
            "failed": False,
            "fails": [],
        }

    last = combined_pp.iloc[-window_days:]
    sf = combined_smart_filter(last, capital_usd)
    fails = combined_sf_failing_criteria(sf)
    return {
        "checked": True,
        "window_days": window_days,
        "passed": sf["passed"],
        "failed": (not sf["passed"]),
        "fails": fails,
        "median_trading_days": sf["median_trading_days"],
        "median_win_days_ratio": sf["median_win_days_ratio"],
        "max_abs_mdd_pct": sf["max_abs_mdd_pct"],
        "n_windows": sf["n_windows"],
    }


# ---------------------------------------------------------------------------
# Expected vs actual commentary (NTH)
# ---------------------------------------------------------------------------

# Prior expectation for β: large-cap majors track BTC more tightly (higher
# β), lower-cap / meme coins have more idiosyncratic moves (lower β).
_MAJORS = {"BTC", "ETH"}
_MID_CAPS = {"SOL", "AVAX", "LINK", "ARB", "SUI"}
_LOW_CAPS = {"DOGE", "WIF", "PEPE"}


def expected_vs_actual_commentary(
    subset: "list[str]", regs: "dict[str, dict]",
) -> str:
    """One short paragraph on subset composition vs naive prior."""
    if not subset:
        return ""
    majors = [c for c in subset if c in _MAJORS]
    mids = [c for c in subset if c in _MID_CAPS]
    lows = [c for c in subset if c in _LOW_CAPS]

    prior = (
        "Prior expectation: majors (BTC/ETH) track BTC more tightly (higher β, "
        "less room for independent α); mid-caps (SOL/AVAX/LINK/ARB/SUI) and "
        "low-caps (DOGE/WIF/PEPE) have more idiosyncratic flow and are more "
        "likely to carry independent α."
    )
    composition = (
        f"Subset composition: {len(majors)} major ({', '.join(majors) or '—'}), "
        f"{len(mids)} mid-cap ({', '.join(mids) or '—'}), "
        f"{len(lows)} low-cap ({', '.join(lows) or '—'})."
    )

    if majors and not (mids or lows):
        verdict = (
            "⚠ Counter-intuitive — subset is all majors. Majors should carry "
            "the highest β and least idiosyncratic α, yet the regression put "
            "them on the clean-α side. This may be a sample artifact; verify "
            "by re-running after more data, or inspect for outlier trades."
        )
    elif (mids or lows) and not majors:
        verdict = (
            "Expected — subset excludes majors, consistent with the prior "
            "that BTC/ETH carry dominant β on this substrate. Finding is "
            "intuitive; still requires paper-trade validation but is not a "
            "pure-stats fluke."
        )
    else:
        verdict = (
            "Partially expected — subset mixes majors with mid-/low-caps. "
            "Check the per-coin β column: majors with |β| below "
            f"{GENUINE_BETA_MAX} support the finding; majors with higher β "
            "inside the subset indicate the MIXED_ALPHA_BETA branch admitted "
            "them despite partial BTC exposure."
        )
    return f"{prior} {composition} {verdict}"


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

def recommend(
    regs: "dict[str, dict]",
    subset_result: dict,
    stability: dict,
) -> str:
    """Map subset + stability to one of 5 recommendation labels."""
    # Check for INSUFFICIENT_DATA_ALL_COINS (all coins n_trades < 15)
    if all(
        r.get("verdict") == "INSUFFICIENT_FOR_REGRESSION"
        for r in regs.values()
    ):
        return "INSUFFICIENT_DATA_ALL_COINS"

    subset = subset_result.get("subset", [])
    jp = subset_result.get("combined_jensen_pass", False)
    sfp = subset_result.get("combined_sf_pass", False)

    # No subset found
    if not subset:
        eligible = [
            c for c, r in regs.items()
            if r.get("verdict") in SUBSET_ELIGIBLE_VERDICTS
        ]
        if not eligible:
            return "NO_SUBSET_FOUND"
        # Eligible coins exist but couldn't pass combined gates
        return "PARTIAL_SUBSET" if (jp or sfp) else "NO_SUBSET_FOUND"

    # Subset exists; check stability override
    if stability.get("is_regime_dependent"):
        return "REGIME_DEPENDENT"

    if jp and sfp:
        return "DEPLOY_SUBSET"
    if jp or sfp:
        return "PARTIAL_SUBSET"
    return "NO_SUBSET_FOUND"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

_RECOMMENDATION_TEXTS = {
    "DEPLOY_SUBSET": (
        "**Narrow-universe deployment candidate identified.** Combined subset "
        "passes both Jensen (α significant, |β|<0.7, R²<0.4) and Smart Filter "
        "adequacy on the full L-jensen window. Proceed with paper trading "
        "validation on the subset universe in parallel to L6b."
    ),
    "PARTIAL_SUBSET": (
        "**Subset passes one gate but not both.** Per-coin structure exists "
        "but the combined portfolio cannot clear Jensen + Smart Filter "
        "simultaneously. Options: (a) extend sample, (b) combine with "
        "another orthogonal signal, (c) revisit holding period per subset coin."
    ),
    "NO_SUBSET_FOUND": (
        "**Confirmed: market_flush INCONCLUSIVE is structural, not an "
        "averaging artifact.** No per-coin subset carries independent α. "
        "Pivot to next parallel signal (L6b or H1 liquidation velocity)."
    ),
    "REGIME_DEPENDENT": (
        "**Subset exists but is regime-dependent.** ≥50% of subset coins "
        "show unstable β between first and second half of sample. Deployment "
        "is fragile — the edge was different across the 148-day window. "
        "Consider a regime-filtered variant as a separate research session."
    ),
    "INSUFFICIENT_DATA_ALL_COINS": (
        "**Cannot run per-coin regression.** All 10 coins have n_trades < "
        f"{DESCRIPTIVE_ONLY_MIN_N}. Descriptive stats only — no regression. "
        "Wait for more trades to accumulate, then re-run."
    ),
}


def _fmt(v, spec: str = ".4f") -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "n/a"
    try:
        return format(v, spec)
    except Exception:
        return str(v)


def format_percoin_report(
    *,
    regs: "dict[str, dict]",
    descriptive: pd.DataFrame,
    subset_result: dict,
    stability: dict,
    recent_window: dict,
    commentary: str,
    recommendation: str,
    n_trades_total: int,
    aligned_n: int,
    date_start: pd.Timestamp,
    date_end: pd.Timestamp,
) -> str:
    """Render the Markdown report."""
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines.append(f"# Jensen's Alpha per-coin — `market_flush` h4")
    lines.append("")
    lines.append(f"_Generated {now}._")
    lines.append("")

    # --- 1. Summary
    lines.append("## 1. Summary")
    lines.append("")
    lines.append(f"**Recommendation: `{recommendation}`**")
    lines.append("")
    lines.append(_RECOMMENDATION_TEXTS.get(recommendation, "(no recommendation text defined)"))
    lines.append("")
    lines.append("**Portfolio-level recap (L-jensen):** INCONCLUSIVE with "
                 "α=+1.36 pp/day (two-sided HAC p=0.0555), β=+1.65 (p=0.15), "
                 "R²=0.124, subsample β unstable (0.24 → 2.23).")
    lines.append("")
    lines.append(f"Source trade list: N={n_trades_total} trades over "
                 f"{aligned_n} aligned daily observations "
                 f"({date_start.date()} → {date_end.date()}).")
    lines.append("")

    # --- 2. Descriptive stats
    lines.append("## 2. Per-coin descriptive stats")
    lines.append("")
    lines.append("| Coin | N trades | Total pp | Win% | Mean daily pp | Active days | Active rate |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for _, row in descriptive.iterrows():
        lines.append(
            f"| {row['coin']} | {int(row['n_trades'])} | "
            f"{row['total_return_pp']:+.2f} | {row['win_rate']:.1%} | "
            f"{row['mean_daily_pp']:+.4f} | {int(row['active_days'])} | "
            f"{row['active_rate']:.1%} |"
        )
    lines.append("")

    # --- 3. Per-coin regression
    lines.append("## 3. Per-coin regression (CAPM + HAC)")
    lines.append("")
    lines.append("| Coin | N trades | N days | α (daily pp) | β | R² | α p (HAC 2s) | β p | Verdict | Low-N |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|---|")
    for coin in [d["coin"] for _, d in descriptive.iterrows()]:
        r = regs.get(coin, {})
        verdict = r.get("verdict", "—")
        nt = r.get("n_trades", 0)
        nd = r.get("n", 0)
        if "alpha" in r:
            lines.append(
                f"| {coin} | {nt} | {nd} | "
                f"{_fmt(r['alpha'], '+.4f')} | {_fmt(r['beta'], '+.4f')} | "
                f"{_fmt(r['r_squared'], '.4f')} | {_fmt(r['alpha_pvalue'], '.4g')} | "
                f"{_fmt(r.get('beta_pvalue'), '.4g')} | {verdict} | "
                f"{'⚠' if r.get('low_n_warning') else ''} |"
            )
        else:
            lines.append(
                f"| {coin} | {nt} | {nd} | — | — | — | — | — | {verdict} | "
                f"{'⚠' if r.get('low_n_warning') else ''} |"
            )
    lines.append("")

    # --- 4. Subset construction trace
    lines.append("## 4. Subset construction trace")
    lines.append("")
    trace = subset_result.get("trace", [])
    if not trace:
        lines.append("_No trace — no eligible coins._")
    else:
        lines.append("| # | Action | Subset | Combined Jensen | Combined SF | Both pass? |")
        lines.append("|---:|---|---|---|---|:---:|")
        for i, step in enumerate(trace, 1):
            sub_str = ",".join(step.get("subset", [])) or "∅"
            reg = step.get("combined_reg") or {}
            jp = step.get("combined_jensen_pass")
            jp_icon = "✓" if jp else "✗"
            if reg and "alpha" in reg:
                j_str = (
                    f"α={reg['alpha']:+.3f} p={reg['alpha_pvalue']:.3g} "
                    f"β={reg['beta']:+.2f} R²={reg['r_squared']:.2f} {jp_icon}"
                )
            else:
                j_str = f"N<{MIN_N_FOR_REGRESSION} {jp_icon}"
            sf = step.get("combined_sf") or {}
            sfp = step.get("combined_sf_pass")
            sfp_icon = "✓" if sfp else "✗"
            if sf.get("n_windows", 0) > 0:
                sf_str = (
                    f"TD={sf['median_trading_days']:.1f} "
                    f"WR={sf['median_win_days_ratio']:.2f} "
                    f"MDD={sf['max_abs_mdd_pct']:.1f}% {sfp_icon}"
                )
            else:
                sf_str = f"n/a {sfp_icon}"
            both = "✓" if step.get("both_pass") else "—"
            lines.append(
                f"| {i} | {step['action']} | `{sub_str}` | {j_str} | {sf_str} | {both} |"
            )
    lines.append("")

    # --- 5. Final subset
    lines.append("## 5. Final candidate subset")
    lines.append("")
    subset = subset_result.get("subset", [])
    if not subset:
        lines.append(f"_No subset selected. Reason: {subset_result.get('no_subset_reason', 'gates not met')}._")
        if subset_result.get("combined_jensen_fails"):
            lines.append("")
            lines.append(f"Jensen fails: {', '.join(subset_result['combined_jensen_fails'])}")
        if subset_result.get("combined_sf_fails"):
            lines.append(f"SF fails: {', '.join(subset_result['combined_sf_fails'])}")
    else:
        lines.append(f"**Coins:** {', '.join(subset)} (N={len(subset)})")
        if len(subset) == 1:
            lines.append("")
            lines.append("⚠ **N=1 fragile candidate** — single-coin portfolio lacks "
                         "diversification; treat as provisional.")
        reg = subset_result.get("combined_reg") or {}
        if "alpha" in reg:
            lines.append("")
            lines.append("### Combined Jensen")
            lines.append("")
            lines.append("| Statistic | Value |")
            lines.append("|---|---|")
            lines.append(f"| N days | {reg['n']} |")
            lines.append(
                f"| α (daily) | {_fmt(reg['alpha'], '+.4f')} pp "
                f"(annualized {annualize_alpha(reg['alpha']):+.2f} pp) |"
            )
            lines.append(f"| β | {_fmt(reg['beta'], '+.4f')} |")
            lines.append(f"| R² | {_fmt(reg['r_squared'], '.4f')} |")
            lines.append(f"| α p-value (HAC 2-sided) | {_fmt(reg['alpha_pvalue'], '.4g')} |")
            lines.append(f"| β p-value (HAC) | {_fmt(reg.get('beta_pvalue'), '.4g')} |")
            jp_icon = "✓" if subset_result["combined_jensen_pass"] else "✗"
            lines.append(f"| Combined Jensen gate | {jp_icon} |")
            if subset_result.get("combined_jensen_fails"):
                lines.append(f"| Failing criteria | {', '.join(subset_result['combined_jensen_fails'])} |")
        sf = subset_result.get("combined_sf") or {}
        if sf.get("n_windows"):
            lines.append("")
            lines.append("### Combined Smart Filter (30d rolling, full window)")
            lines.append("")
            lines.append("| Metric | Value | Gate |")
            lines.append("|---|---:|:---:|")
            lines.append(
                f"| N windows | {sf['n_windows']} | — |"
            )
            lines.append(
                f"| Median trading days | {sf['median_trading_days']:.1f} "
                f"(req ≥ {SF_MIN_TRADING_DAYS}) | "
                f"{'✓' if sf['g_trading_days'] else '✗'} |"
            )
            lines.append(
                f"| Median win-days ratio | {sf['median_win_days_ratio']:.3f} "
                f"(req ≥ {SF_MIN_WIN_DAYS_RATIO}) | "
                f"{'✓' if sf['g_win_days'] else '✗'} |"
            )
            lines.append(
                f"| Max |MDD| (pp) | {sf['max_abs_mdd_pct']:.2f}% "
                f"(req ≤ {SF_MAX_ABS_MDD_PCT}%) | "
                f"{'✓' if sf['g_mdd'] else '✗'} |"
            )
            sfp_icon = "✓" if subset_result["combined_sf_pass"] else "✗"
            lines.append(f"| Combined SF gate | — | {sfp_icon} |")
    lines.append("")

    # --- 6. Subsample stability
    lines.append("## 6. Subsample stability (categorize, do NOT auto-remove)")
    lines.append("")
    if not subset:
        lines.append("_No subset → no stability analysis._")
    else:
        counts = stability.get("counts", {})
        lines.append(
            f"**Counts:** STABLE={counts.get('STABLE', 0)}, "
            f"PARTIAL={counts.get('PARTIAL', 0)}, "
            f"UNSTABLE={counts.get('UNSTABLE', 0)}, "
            f"UNKNOWN={counts.get('UNKNOWN', 0)}"
        )
        lines.append(
            f"**Unstable ratio:** {stability.get('unstable_ratio', 0.0):.1%} "
            f"(threshold for REGIME_DEPENDENT override: ≥ {UNSTABLE_SUBSET_RATIO:.0%})"
        )
        lines.append("")
        lines.append("| Coin | Category | First α | First β | Second α | Second β | Detail |")
        lines.append("|---|---|---:|---:|---:|---:|---|")
        for coin in subset:
            pc = stability["per_coin"].get(coin, {})
            cat = pc.get("category", "—")
            sample = pc.get("subsample", {})
            fh = sample.get("first_half") or {}
            sh = sample.get("second_half") or {}
            detail = pc.get("detail", {})
            method = detail.get("method", "")
            if method == "ratio":
                det_str = f"β ratio={detail.get('beta_ratio', 0):.2f}"
            elif method == "abs_diff":
                det_str = f"β |Δ|={detail.get('beta_abs_diff', 0):.3f}"
            else:
                det_str = detail.get("reason", "—")
            lines.append(
                f"| {coin} | {cat} | "
                f"{_fmt(fh.get('alpha'), '+.4f')} | {_fmt(fh.get('beta'), '+.4f')} | "
                f"{_fmt(sh.get('alpha'), '+.4f')} | {_fmt(sh.get('beta'), '+.4f')} | "
                f"{det_str} |"
            )
    lines.append("")

    # --- 7. Recent-window sanity
    lines.append("## 7. Recent-window sanity (last 30 days)")
    lines.append("")
    if not recent_window.get("checked"):
        lines.append(f"_Skipped — {recent_window.get('reason', 'unavailable')}._")
    else:
        lines.append("| Metric | Value | Gate |")
        lines.append("|---|---:|:---:|")
        lines.append(
            f"| Median trading days | {recent_window['median_trading_days']:.1f} "
            f"(req ≥ {SF_MIN_TRADING_DAYS}) | "
            f"{'✓' if recent_window['median_trading_days'] >= SF_MIN_TRADING_DAYS else '✗'} |"
        )
        lines.append(
            f"| Median win-days ratio | {recent_window['median_win_days_ratio']:.3f} "
            f"(req ≥ {SF_MIN_WIN_DAYS_RATIO}) | "
            f"{'✓' if recent_window['median_win_days_ratio'] >= SF_MIN_WIN_DAYS_RATIO else '✗'} |"
        )
        lines.append(
            f"| Max |MDD| | {recent_window['max_abs_mdd_pct']:.2f}% "
            f"(req ≤ {SF_MAX_ABS_MDD_PCT}%) | "
            f"{'✓' if recent_window['max_abs_mdd_pct'] <= SF_MAX_ABS_MDD_PCT else '✗'} |"
        )
        if recent_window.get("failed"):
            lines.append("")
            lines.append(
                f"**⚠ RECENT_DETERIORATION**: the last {recent_window['window_days']} "
                f"days do NOT pass the SF gate(s): "
                f"{', '.join(recent_window.get('fails', []))}. "
                f"Non-blocking — recommendation unchanged. Flag for architect review."
            )
    lines.append("")

    # --- 8. Expected vs actual
    if commentary:
        lines.append("## 8. Expected vs actual commentary")
        lines.append("")
        lines.append(commentary)
        lines.append("")

    # --- 9. Recommendation recap
    lines.append("## 9. Recommendation")
    lines.append("")
    lines.append(f"**`{recommendation}`**")
    lines.append("")
    lines.append(_RECOMMENDATION_TEXTS.get(recommendation, ""))
    lines.append("")

    # Methodology
    lines.append("---")
    lines.append("")
    lines.append("## Methodology notes")
    lines.append("")
    lines.append(
        "- Per-coin P&L source: `extract_trade_records(h4_dfs, "
        "MARKET_FLUSH_FILTERS, RANK_HOLDING_HOURS=8)` — same locked L8 "
        "baseline as L-jensen, grouped by `coin` field."
    )
    lines.append(
        "- Per-coin regression: OLS + Newey-West HAC (maxlags=5), identical "
        "to `analysis/jensen_alpha.py`. Coins with n_trades < 15 skipped; "
        "coins with 15 ≤ n < 30 flagged `low_n_warning`."
    )
    lines.append(
        "- Subset construction: greedy with SF1 fallback. Seed = all "
        "GENUINE_ALPHA coins; if seed fails combined gates, remove "
        "highest-|β-contribution| one at a time. Then extend by MIXED "
        "coins in ascending α p-value order while gates hold."
    )
    lines.append(
        "- Combined Jensen gate: α p < 0.05 (two-sided HAC), α > 0, "
        f"|β| < {COMBINED_BETA_MAX}, R² < {COMBINED_R2_MAX}."
    )
    lines.append(
        "- Combined Smart Filter gate: median 30d rolling trading days "
        f"≥ {SF_MIN_TRADING_DAYS}, median win-days ratio "
        f"≥ {SF_MIN_WIN_DAYS_RATIO}, max |MDD| ≤ {SF_MAX_ABS_MDD_PCT}%."
    )
    lines.append(
        "- Stability categorization (CI1): STABLE if β ratio within "
        f"[1/{STABLE_BETA_RATIO_MAX}, {STABLE_BETA_RATIO_MAX}], PARTIAL if "
        f"within [1/{PARTIAL_BETA_RATIO_MAX}, {PARTIAL_BETA_RATIO_MAX}], "
        "UNSTABLE otherwise or on α sign flip. Near-zero β (abs < "
        f"{NEAR_ZERO_BETA_ABS}) falls back to absolute |Δβ| thresholds. "
        f"Overall recommendation flips to REGIME_DEPENDENT when "
        f"≥ {UNSTABLE_SUBSET_RATIO:.0%} of subset coins are UNSTABLE."
    )
    lines.append(
        "- Recent-window sanity (SF2): last 30 days of combined portfolio "
        "evaluated against SF gates. Non-blocking — annotates report "
        "but does not change recommendation."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Loading market_flush h4 daily P&L (this runs the L-jensen loader) ...")
    daily_strategy_pp, daily_metrics, trades = load_market_flush_daily_pnl()
    if daily_strategy_pp.empty:
        print("ERROR: zero strategy P&L — abort.")
        return 2

    n_trades_total = len(trades)
    start_date = daily_strategy_pp.index.min()
    end_date = daily_strategy_pp.index.max()
    date_range = pd.date_range(start_date, end_date, freq="D", tz="UTC")

    print(f"Fetching BTC daily klines {start_date.date()} → {end_date.date()} ...")
    btc_pp = load_btc_daily_returns(start_date, end_date)

    # Group trades by coin
    by_coin_trades: dict[str, list[dict]] = {}
    for t in trades:
        by_coin_trades.setdefault(t["coin"], []).append(t)

    coin_daily = per_coin_daily_pnl(trades, date_range)
    descriptive = per_coin_descriptive_stats(coin_daily, by_coin_trades)
    print("\nPer-coin descriptive stats:")
    print(descriptive.to_string(index=False))

    print("\nRunning per-coin Jensen regression ...")
    regs = per_coin_regression(coin_daily, btc_pp)
    for coin, r in regs.items():
        v = r.get("verdict", "—")
        if "alpha" in r:
            print(f"  {coin:<5}: α={r['alpha']:+.4f} β={r['beta']:+.4f} "
                  f"R²={r['r_squared']:.3f} p={r['alpha_pvalue']:.3g} → {v}")
        else:
            print(f"  {coin:<5}: {v} (n_trades={r.get('n_trades', 0)})")

    print("\nBuilding subset (greedy + SF1 fallback) ...")
    subset_result = build_subset(regs, coin_daily, btc_pp)
    print(f"Final subset: {subset_result['subset'] or '∅'}")

    subset_coins = subset_result.get("subset", [])
    stability = subset_stability_summary(subset_coins, coin_daily, btc_pp)
    if subset_coins:
        print(f"Stability counts: {stability['counts']} "
              f"(unstable ratio {stability['unstable_ratio']:.1%})")

    combined_pp = combined_portfolio_daily_pnl(subset_coins, coin_daily)
    recent = recent_window_check(combined_pp)
    commentary = expected_vs_actual_commentary(subset_coins, regs)

    rec = recommend(regs, subset_result, stability)
    print(f"\nRecommendation: {rec}")

    aligned = pd.DataFrame({
        "strategy": daily_strategy_pp,
        "btc": btc_pp,
    }).dropna()

    report = format_percoin_report(
        regs=regs,
        descriptive=descriptive,
        subset_result=subset_result,
        stability=stability,
        recent_window=recent,
        commentary=commentary,
        recommendation=rec,
        n_trades_total=n_trades_total,
        aligned_n=int(len(aligned)),
        date_start=start_date,
        date_end=end_date,
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = os.path.join(_PROJECT_ROOT, "analysis")
    out_path = os.path.join(out_dir, f"jensen_percoin_report_{timestamp}.md")
    with open(out_path, "w") as f:
        f.write(report)
    print(f"\nReport written to: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
