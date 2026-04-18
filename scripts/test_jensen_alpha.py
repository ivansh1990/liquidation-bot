#!/usr/bin/env python3
"""
Offline tests for analysis/jensen_alpha.py — synthetic-data scenarios that
exercise the verdict ladder and the pre-flight unit-consistency guard.

Run directly: .venv/bin/python scripts/test_jensen_alpha.py
Exit code 0 on all-pass, 1 otherwise.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.jensen_alpha import (
    assert_unit_consistency,
    annualize_alpha,
    resolve_verdict,
    run_jensen_test,
)


passed = 0
failed = 0


def report(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")


def _make_btc(n: int, seed: int) -> pd.Series:
    """BTC daily returns in percentage points. Independent rng per seed."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-10-01", periods=n, freq="D", tz="UTC")
    return pd.Series(rng.normal(0.0, 2.0, n), index=idx)


def _noise(n: int, seed: int, sd: float) -> np.ndarray:
    """Idiosyncratic noise. Independent rng so it does NOT correlate with BTC."""
    return np.random.default_rng(seed).normal(0.0, sd, n)


# ---------------------------------------------------------------------------
# Block 1: Verdict ladder (synthetic DGPs with known α, β)
# ---------------------------------------------------------------------------

def test_pure_alpha() -> None:
    print("\n--- Test 1: pure alpha (no BTC exposure) ---")
    n = 200
    btc = _make_btc(n, seed=100)
    # α = 0.1 pp daily (~10 bps), β = 0, idiosyncratic noise small enough
    # to make α highly significant. Independent seed so noise ⊥ btc.
    strategy = pd.Series(0.1 + _noise(n, seed=200, sd=0.2), index=btc.index)

    result = run_jensen_test(strategy, btc, hac_lags=5)

    report("verdict = GENUINE_ALPHA", result["verdict"] == "GENUINE_ALPHA",
           f"got {result['verdict']}")
    report("α positive and ≈ 0.1 pp", abs(result["alpha"] - 0.1) < 0.05,
           f"α={result['alpha']:.4f} pp")
    report("β ≈ 0 (< 0.5)", abs(result["beta"]) < 0.5,
           f"β={result['beta']:.4f}")
    report("R² < 0.3", result["r_squared"] < 0.3,
           f"R²={result['r_squared']:.4f}")
    report("α p-value < 0.05 (HAC)", result["alpha_pvalue"] < 0.05,
           f"p={result['alpha_pvalue']:.4f}")


def test_pure_beta() -> None:
    print("\n--- Test 2: pure beta (leveraged BTC) ---")
    n = 200
    btc = _make_btc(n, seed=101)
    # α = 0, β = 1.2, low idiosyncratic noise → R² high.
    strategy = pd.Series(
        1.2 * btc.values + _noise(n, seed=201, sd=0.5), index=btc.index,
    )

    result = run_jensen_test(strategy, btc, hac_lags=5)

    report("verdict = LEVERAGED_BETA", result["verdict"] == "LEVERAGED_BETA",
           f"got {result['verdict']}")
    report("β ≈ 1.2 (> 0.7)", abs(result["beta"] - 1.2) < 0.1,
           f"β={result['beta']:.4f}")
    report("R² > 0.4", result["r_squared"] > 0.4,
           f"R²={result['r_squared']:.4f}")
    report("α p-value ≥ 0.05 (HAC)", result["alpha_pvalue"] >= 0.05,
           f"p={result['alpha_pvalue']:.4f}")


def test_mixed_alpha_beta() -> None:
    print("\n--- Test 3: mixed alpha + beta ---")
    n = 200
    btc = _make_btc(n, seed=102)
    # α = 0.05 pp daily, β = 0.6, noise small enough for α to be significant.
    strategy = pd.Series(
        0.05 + 0.6 * btc.values + _noise(n, seed=202, sd=0.2),
        index=btc.index,
    )

    result = run_jensen_test(strategy, btc, hac_lags=5)

    report("verdict = MIXED_ALPHA_BETA",
           result["verdict"] == "MIXED_ALPHA_BETA",
           f"got {result['verdict']}")
    report("α positive ≈ 0.05 pp", abs(result["alpha"] - 0.05) < 0.05,
           f"α={result['alpha']:.4f}")
    report("α p-value < 0.05", result["alpha_pvalue"] < 0.05,
           f"p={result['alpha_pvalue']:.4f}")
    report("β ≥ 0.5 OR R² ≥ 0.3",
           result["beta"] >= 0.5 or result["r_squared"] >= 0.3,
           f"β={result['beta']:.4f} R²={result['r_squared']:.4f}")


def test_negative_alpha() -> None:
    print("\n--- Test 4: negative alpha (cost > edge) ---")
    n = 200
    btc = _make_btc(n, seed=103)
    # α = -0.1 pp daily, β = 0.1, noise small enough for α to be significant.
    strategy = pd.Series(
        -0.1 + 0.1 * btc.values + _noise(n, seed=203, sd=0.2),
        index=btc.index,
    )

    result = run_jensen_test(strategy, btc, hac_lags=5)

    report("verdict = NEGATIVE_ALPHA", result["verdict"] == "NEGATIVE_ALPHA",
           f"got {result['verdict']}")
    report("α negative ≈ -0.1 pp", abs(result["alpha"] + 0.1) < 0.05,
           f"α={result['alpha']:.4f}")
    report("α p-value < 0.05", result["alpha_pvalue"] < 0.05,
           f"p={result['alpha_pvalue']:.4f}")


def test_inconclusive_low_n() -> None:
    print("\n--- Test 5: inconclusive (N < 30) ---")
    n = 20
    btc = _make_btc(n, seed=104)
    strategy = pd.Series(0.1 + _noise(n, seed=204, sd=0.2), index=btc.index)

    result = run_jensen_test(strategy, btc, hac_lags=5)

    report("verdict = INCONCLUSIVE", result["verdict"] == "INCONCLUSIVE",
           f"got {result['verdict']}")
    report("n recorded", result["n"] == n, f"n={result['n']}")


def test_weak_signal() -> None:
    print("\n--- Test 6: weak signal (partial β, no significant α) ---")
    n = 200
    btc = _make_btc(n, seed=105)
    # β = 0.5 (mid-range), tiny α drowned in large noise → p ≥ 0.05.
    # Var(0.5·btc) = 1.0 pp²; Var(noise) = 2.25 pp² → R² ≈ 0.31 (in [0.2, 0.4]).
    strategy = pd.Series(
        0.01 + 0.5 * btc.values + _noise(n, seed=205, sd=1.5),
        index=btc.index,
    )

    result = run_jensen_test(strategy, btc, hac_lags=5)

    report("verdict = WEAK_SIGNAL", result["verdict"] == "WEAK_SIGNAL",
           f"got {result['verdict']}")
    report("α p-value ≥ 0.05 (not significant)",
           result["alpha_pvalue"] >= 0.05,
           f"p={result['alpha_pvalue']:.4f}")
    report("β in [0.3, 0.7] OR R² in [0.2, 0.4]",
           (0.3 <= result["beta"] <= 0.7) or (0.2 <= result["r_squared"] <= 0.4),
           f"β={result['beta']:.4f} R²={result['r_squared']:.4f}")
    report("β ≤ 0.7 (not LEVERAGED_BETA)", result["beta"] <= 0.7,
           f"β={result['beta']:.4f}")


# ---------------------------------------------------------------------------
# Block 2: Pre-flight unit-consistency guard
# ---------------------------------------------------------------------------

def test_unit_mismatch_raises() -> None:
    print("\n--- Test 7: unit-mismatch guard halts before regression ---")
    n = 100
    idx = pd.date_range("2025-10-01", periods=n, freq="D", tz="UTC")
    # strategy in percentage points (median |x| ≈ 1.4 pp)
    strategy = pd.Series(np.random.default_rng(50).normal(0.0, 2.0, n), index=idx)
    # btc in DECIMALS, scaled small enough to push ratio firmly past 100x
    # (median |x| ≈ 0.0034 = 0.34% expressed as decimal). Independent rng.
    btc = pd.Series(np.random.default_rng(150).normal(0.0, 0.005, n), index=idx)

    raised = False
    try:
        assert_unit_consistency(strategy, btc)
    except ValueError as e:
        raised = "median" in str(e).lower() or "magnitude" in str(e).lower()
    report("ValueError raised for >100x median ratio", raised)

    # Same units (both pp) → no raise.
    btc_pp = btc * 100  # convert decimals → pp
    no_raise = True
    try:
        assert_unit_consistency(strategy, btc_pp)
    except ValueError:
        no_raise = False
    report("no raise when both series in same units", no_raise)


# ---------------------------------------------------------------------------
# Block 3: Annualization formula (T2)
# ---------------------------------------------------------------------------

def test_annualization_compound() -> None:
    print("\n--- Test 8: alpha annualization (compound formula) ---")
    # 0.0123 pp/day → expected (1.000123)^365 - 1 = 0.04591… → ~4.591 pp/year
    daily_pp = 0.0123
    annual_pp = annualize_alpha(daily_pp)
    expected = ((1 + daily_pp / 100.0) ** 365 - 1) * 100.0
    report("compound formula matches", abs(annual_pp - expected) < 1e-9,
           f"got {annual_pp:.6f} pp, expected {expected:.6f} pp")
    # Negative daily α → negative annual.
    neg = annualize_alpha(-0.05)
    report("negative daily α produces negative annual",
           neg < 0, f"annual={neg:.6f} pp")
    # Zero α → zero.
    zero = annualize_alpha(0.0)
    report("zero α annualizes to zero", abs(zero) < 1e-12,
           f"got {zero}")


# ---------------------------------------------------------------------------
# Block 4: Verdict resolver direct-call edge cases
# ---------------------------------------------------------------------------

def test_resolve_verdict_branches() -> None:
    print("\n--- Test 9: resolve_verdict direct branch coverage ---")

    # GENUINE_ALPHA
    v = resolve_verdict({"alpha": 0.1, "alpha_pvalue": 0.001,
                         "beta": 0.1, "r_squared": 0.05, "n": 200})
    report("GENUINE_ALPHA branch", v == "GENUINE_ALPHA", f"got {v}")

    # MIXED_ALPHA_BETA via β
    v = resolve_verdict({"alpha": 0.05, "alpha_pvalue": 0.01,
                         "beta": 0.6, "r_squared": 0.1, "n": 200})
    report("MIXED branch via β≥0.5", v == "MIXED_ALPHA_BETA", f"got {v}")

    # MIXED_ALPHA_BETA via R²
    v = resolve_verdict({"alpha": 0.05, "alpha_pvalue": 0.01,
                         "beta": 0.2, "r_squared": 0.4, "n": 200})
    report("MIXED branch via R²≥0.3", v == "MIXED_ALPHA_BETA", f"got {v}")

    # NEGATIVE_ALPHA
    v = resolve_verdict({"alpha": -0.1, "alpha_pvalue": 0.001,
                         "beta": 0.1, "r_squared": 0.05, "n": 200})
    report("NEGATIVE_ALPHA branch", v == "NEGATIVE_ALPHA", f"got {v}")

    # LEVERAGED_BETA
    v = resolve_verdict({"alpha": 0.001, "alpha_pvalue": 0.9,
                         "beta": 1.0, "r_squared": 0.7, "n": 200})
    report("LEVERAGED_BETA branch", v == "LEVERAGED_BETA", f"got {v}")

    # WEAK_SIGNAL
    v = resolve_verdict({"alpha": 0.001, "alpha_pvalue": 0.9,
                         "beta": 0.5, "r_squared": 0.3, "n": 200})
    report("WEAK_SIGNAL branch", v == "WEAK_SIGNAL", f"got {v}")

    # INCONCLUSIVE via low N
    v = resolve_verdict({"alpha": 0.1, "alpha_pvalue": 0.001,
                         "beta": 0.1, "r_squared": 0.05, "n": 20})
    report("INCONCLUSIVE via N<30", v == "INCONCLUSIVE", f"got {v}")

    # INCONCLUSIVE via no match (degenerate: p≥0.05, β=0.1, R²=0.05)
    v = resolve_verdict({"alpha": 0.001, "alpha_pvalue": 0.9,
                         "beta": 0.1, "r_squared": 0.05, "n": 200})
    report("INCONCLUSIVE via no-match", v == "INCONCLUSIVE", f"got {v}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    test_pure_alpha()
    test_pure_beta()
    test_mixed_alpha_beta()
    test_negative_alpha()
    test_inconclusive_low_n()
    test_weak_signal()
    test_unit_mismatch_raises()
    test_annualization_compound()
    test_resolve_verdict_branches()
    print(f"\nPASS: {passed} | FAIL: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
