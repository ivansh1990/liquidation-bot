#!/usr/bin/env python3
"""
L14 Phase 1: Offline tests for research_breadth.py.

No DB, no API, no network for blocks 1-3. Block 4 is an optional live DB
smoke test that gracefully skips when the database is unreachable.

Usage:
    .venv/bin/python scripts/test_research_breadth.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)

from backtest_market_flush_multitf import MARKET_FLUSH_FILTERS  # noqa: E402

from research_breadth import (  # noqa: E402
    build_breadth_filters,
    compute_trading_days_distribution,
    evaluate_breadth_verdict,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(y: int, m: int, d: int, h: int = 12) -> pd.Timestamp:
    return pd.Timestamp(datetime(y, m, d, h, tzinfo=timezone.utc))


def _trade(coin: str, entry: pd.Timestamp, pnl_pct: float, holding_h: int = 8) -> dict:
    return {
        "coin": coin,
        "entry_ts": entry,
        "exit_ts": entry + pd.Timedelta(hours=holding_h),
        "pnl_pct": pnl_pct,
    }


# ---------------------------------------------------------------------------
# Block 1 — filter construction (3)
# ---------------------------------------------------------------------------

def test_block1_filters() -> None:
    print("\n--- Block 1: filter construction ---")

    # 1. K=0 drops the breadth tuple entirely → only z_self filter remains.
    got = build_breadth_filters(0)
    report(
        "K=0 returns [(long_vol_zscore, >, 1.0)] (breadth filter dropped)",
        got == [("long_vol_zscore", ">", 1.0)],
        f"got={got}",
    )

    # 2. K=4 matches locked MARKET_FLUSH_FILTERS structurally.
    got4 = build_breadth_filters(4)
    expected4 = [("long_vol_zscore", ">", 1.0), ("n_coins_flushing", ">=", 4)]
    report(
        "K=4 reproduces locked MARKET_FLUSH_FILTERS structure",
        got4 == expected4 and got4 == list(MARKET_FLUSH_FILTERS),
        f"got={got4}",
    )

    # 3. Out-of-range K raises ValueError.
    raised_neg = False
    raised_high = False
    try:
        build_breadth_filters(-1)
    except ValueError:
        raised_neg = True
    try:
        build_breadth_filters(11)
    except ValueError:
        raised_high = True
    report(
        "K out of [0,10] raises ValueError (tested -1 and 11)",
        raised_neg and raised_high,
        f"neg={raised_neg} high={raised_high}",
    )


# ---------------------------------------------------------------------------
# Block 2 — trading days distribution (4)
# ---------------------------------------------------------------------------

def test_block2_trading_days() -> None:
    print("\n--- Block 2: trading days distribution ---")

    # 4. 5 distinct dates spanning 7 calendar days.
    trades = [
        _trade("BTC", _utc(2025, 10, 1), 1.0),
        _trade("BTC", _utc(2025, 10, 1, 20), -0.5),  # same date as first
        _trade("ETH", _utc(2025, 10, 2), 0.5),
        _trade("BTC", _utc(2025, 10, 4), 0.2),
        _trade("SOL", _utc(2025, 10, 5), -0.3),
        _trade("ETH", _utc(2025, 10, 6), 0.8),
        _trade("BTC", _utc(2025, 10, 7), 0.1),
    ]
    stats = compute_trading_days_distribution(trades)
    # 5 unique dates (1,2,4,5,6,7 → 6 unique). Let me recount: Oct 1, 2, 4, 5, 6, 7 = 6 unique.
    # Calendar span Oct 1 - Oct 7 = 7 days.
    ok_count = stats["total_trading_days"] == 6 and stats["calendar_span_days"] == 7
    report(
        "6 unique trading dates over 7 calendar days",
        ok_count,
        f"total={stats['total_trading_days']} span={stats['calendar_span_days']}",
    )

    # 5. Span < 30 days → rolling-window metrics are zero (no window to slide).
    report(
        "span < 30 days → rolling-window metrics zero, no crash",
        stats["n_rolling_windows"] == 0
        and stats["min_30d_td"] == 0
        and stats["median_30d_td"] == 0.0
        and stats["max_30d_td"] == 0
        and stats["pct_windows_ge14"] == 0.0,
        f"n_windows={stats['n_rolling_windows']}",
    )

    # 6. Dense daily trading over 60 days → every 30d window has ≥14 TD.
    # 50 trading days across Oct 1 - Nov 19 (50 consecutive calendar days).
    dense_trades = [
        _trade("BTC", _utc(2025, 10, 1) + pd.Timedelta(days=i), 0.1)
        for i in range(50)
    ]
    dense = compute_trading_days_distribution(dense_trades)
    report(
        "50 consecutive trading days → 100% of 30d windows have >=14 TD",
        dense["pct_windows_ge14"] == 100.0
        and dense["min_30d_td"] >= 14
        and dense["median_30d_td"] >= 14,
        f"pct={dense['pct_windows_ge14']} min={dense['min_30d_td']} "
        f"median={dense['median_30d_td']}",
    )

    # 7. Empty trade list → all zeros.
    empty = compute_trading_days_distribution([])
    ok_empty = (
        empty["total_trading_days"] == 0
        and empty["calendar_span_days"] == 0
        and empty["pct_active_days"] == 0.0
        and empty["n_rolling_windows"] == 0
        and empty["min_30d_td"] == 0
        and empty["median_30d_td"] == 0.0
        and empty["max_30d_td"] == 0
        and empty["pct_windows_ge14"] == 0.0
    )
    report("empty trade list → all metrics zero (no crash)", ok_empty)


# ---------------------------------------------------------------------------
# Block 3 — verdict tree (4)
# ---------------------------------------------------------------------------

def _mk_wf(
    skipped: bool = False,
    sharpe_oos: float | None = 3.0,
    oos_positive: int = 3,
    oos_total: int = 3,
) -> dict:
    if skipped:
        return {
            "skipped": True, "reason": "N<30",
            "folds": [], "oos_positive": 0, "oos_total": oos_total,
            "pooled_oos": None, "pass_flag": False,
        }
    return {
        "skipped": False, "folds": [],
        "oos_positive": oos_positive, "oos_total": oos_total,
        "pooled_oos": {"n": 150, "win_pct": 60.0, "avg_ret": 0.2, "sharpe": sharpe_oos},
        "pass_flag": True,
    }


def _mk_variant(n: int = 150, win_pct: float = 60.0, sharpe: float = 3.0) -> dict:
    return {
        "per_coin": [],
        "pooled": {"n": n, "win_pct": win_pct, "avg_ret": 0.2, "sharpe": sharpe},
        "days_span": 120,
        "trades_per_day": 1.25,
    }


def _mk_td_stats(min_td: int, median_td: float) -> dict:
    return {
        "total_trading_days": 60,
        "calendar_span_days": 120,
        "pct_active_days": 50.0,
        "n_rolling_windows": 91,
        "min_30d_td": min_td,
        "median_30d_td": median_td,
        "max_30d_td": max(min_td, int(median_td)),
        "pct_windows_ge14": 50.0,
    }


def test_block3_verdict() -> None:
    print("\n--- Block 3: verdict tree ---")

    # 8. walk-forward skipped → FAIL regardless of other fields.
    variant = _mk_variant()
    wf_skip = _mk_wf(skipped=True)
    td = _mk_td_stats(min_td=20, median_td=20)
    report(
        "wf.skipped=True → FAIL",
        evaluate_breadth_verdict(variant, wf_skip, td) == "FAIL",
    )

    # 9. Primary + strict-5 (min>=14) + strict-6 (median>=14) → STRONG_PASS.
    wf_ok = _mk_wf(sharpe_oos=3.0, oos_positive=3, oos_total=3)
    td_strong = _mk_td_stats(min_td=15, median_td=17)
    report(
        "primary + strict-5 + strict-6 met → STRONG_PASS",
        evaluate_breadth_verdict(variant, wf_ok, td_strong) == "STRONG_PASS",
    )

    # 10. Primary + strict-5 only (median fails) → PASS.
    td_pass = _mk_td_stats(min_td=14, median_td=12)
    report(
        "primary + strict-5 (median fails) → PASS (not STRONG_PASS)",
        evaluate_breadth_verdict(variant, wf_ok, td_pass) == "PASS",
    )

    # 11. Primary met, strict-5 fails (min<14) → MARGINAL.
    td_marg = _mk_td_stats(min_td=10, median_td=12)
    report(
        "primary met + strict-5 fails → MARGINAL",
        evaluate_breadth_verdict(variant, wf_ok, td_marg) == "MARGINAL",
    )

    # 12. Primary fails (N<100) → FAIL regardless of strict.
    variant_low = _mk_variant(n=50)
    wf_low = _mk_wf(sharpe_oos=3.0, oos_positive=3)
    # pooled_oos also needs low N for the evaluator to reject.
    wf_low["pooled_oos"]["n"] = 50
    report(
        "N<100 → FAIL (primary violated)",
        evaluate_breadth_verdict(variant_low, wf_low, td_strong) == "FAIL",
    )

    # 13. Sharpe > SUSPICIOUS (8.0) but primary met → MARGINAL (look-ahead guard).
    wf_suspect = _mk_wf(sharpe_oos=9.5, oos_positive=3)
    report(
        "Sharpe > 8.0 → MARGINAL (look-ahead guard)",
        evaluate_breadth_verdict(variant, wf_suspect, td_strong) == "MARGINAL",
    )


# ---------------------------------------------------------------------------
# Block 4 — optional DB smoke (1, skipped without DB)
# ---------------------------------------------------------------------------

def test_block4_db_smoke() -> None:
    print("\n--- Block 4: DB smoke (optional) ---")
    if os.getenv("LIQ_SKIP_DB_TESTS"):
        print("  [SKIP] live DB test — LIQ_SKIP_DB_TESTS set")
        return
    try:
        from collectors.config import get_config  # noqa
        from collectors.db import init_pool  # noqa
        from research_breadth import _load_coins_for_interval  # noqa

        init_pool(get_config())
        dfs = _load_coins_for_interval("h4")
    except Exception as e:
        print(f"  [SKIP] live DB test — {type(e).__name__}: {str(e)[:80]}")
        return
    from backtest_combo import apply_combo  # noqa
    filters = build_breadth_filters(2)
    ok = isinstance(dfs, dict) and len(dfs) > 0
    if ok:
        first_nonempty = next((df for df in dfs.values() if not df.empty), None)
        if first_nonempty is not None:
            mask = apply_combo(first_nonempty, filters)
            ok = ok and len(mask) == len(first_nonempty)
    report(f"live DB: built {len(dfs)} coin frames and applied K=2 filter", ok)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 64)
    print("L14 Phase 1 — research_breadth tests")
    print("=" * 64)
    test_block1_filters()
    test_block2_trading_days()
    test_block3_verdict()
    test_block4_db_smoke()
    print()
    print("=" * 64)
    print(f"RESULT: PASS={passed}  FAIL={failed}")
    print("=" * 64)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
