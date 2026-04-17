#!/usr/bin/env python3
"""
L13 Phase 3c: Offline tests for smart_filter_adequacy.py.

Covers the three pure functions of the adequacy script:
  - compute_daily_metrics
  - simulate_smart_filter_windows
  - recommend

No DB / API / network for the required blocks. Block 4 optionally exercises
run_h4_baseline() against a live DB and skips cleanly without one.

Usage:
    .venv/bin/python scripts/test_smart_filter_adequacy.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)

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


def _mk_trade(exit_day: str, pnl_pct: float, coin: str = "BTC") -> dict:
    exit_ts = pd.Timestamp(exit_day, tz="UTC")
    return {
        "coin": coin,
        "entry_ts": exit_ts - pd.Timedelta(hours=2),
        "exit_ts": exit_ts,
        "pnl_pct": float(pnl_pct),
    }


# ---------------------------------------------------------------------------
# Block 1 — compute_daily_metrics (3 assertions)
# ---------------------------------------------------------------------------


def test_block1_compute_daily_metrics() -> None:
    print("\n--- Block 1: compute_daily_metrics ---")
    from smart_filter_adequacy import compute_daily_metrics

    trades = [
        _mk_trade("2026-01-01 12:00", 1.0),
        _mk_trade("2026-01-01 18:00", -0.5),
        _mk_trade("2026-01-03 06:00", 2.0),
    ]
    date_range = pd.date_range(
        "2026-01-01", "2026-01-05", freq="D", tz="UTC"
    )
    df = compute_daily_metrics(trades, date_range, capital_usd=1000.0)

    # 1. Columns + index shape.
    expected_cols = {
        "pnl_pct", "pnl_usd", "trade_count",
        "is_active", "is_winning", "equity_usd",
    }
    ok_cols = (
        set(df.columns) == expected_cols
        and isinstance(df.index, pd.DatetimeIndex)
        and df.index.tz is not None
        and len(df) == 5
    )
    report(
        "columns + index shape",
        ok_cols,
        f"cols={sorted(df.columns)} tz={df.index.tz}",
    )

    # 2. Zero-trade days carry zeros + False flags.
    d2 = pd.Timestamp("2026-01-02", tz="UTC")
    r2 = df.loc[d2]
    ok_zero = (
        r2["pnl_pct"] == 0.0
        and r2["trade_count"] == 0
        and bool(r2["is_active"]) is False
        and bool(r2["is_winning"]) is False
    )
    report("zero-trade day is all zeros", ok_zero, str(r2.to_dict()))

    # 3. equity_usd invariants: starts at capital + day1_pnl, ends at
    #    capital + total_pnl.
    total_pnl_pct = 1.0 + -0.5 + 2.0  # 2.5
    total_pnl_usd = total_pnl_pct / 100 * 1000.0  # 25.0
    day1_pnl_usd = (1.0 + -0.5) / 100 * 1000.0  # 5.0
    ok_equity = (
        abs(df["equity_usd"].iloc[0] - (1000.0 + day1_pnl_usd)) < 1e-6
        and abs(df["equity_usd"].iloc[-1] - (1000.0 + total_pnl_usd)) < 1e-6
    )
    report(
        "equity starts at capital+day1, ends at capital+total",
        ok_equity,
        f"first={df['equity_usd'].iloc[0]}  last={df['equity_usd'].iloc[-1]}",
    )


# ---------------------------------------------------------------------------
# Block 2 — simulate_smart_filter_windows (4 assertions)
# ---------------------------------------------------------------------------


def _build_daily_df(
    days: int,
    pnl_per_active_day: float,
    active_days: list[int],
    capital_usd: float = 1000.0,
) -> pd.DataFrame:
    """Construct a daily DataFrame with activity on the given day indices."""
    idx = pd.date_range("2026-01-01", periods=days, freq="D", tz="UTC")
    rows = []
    equity = capital_usd
    for i, _d in enumerate(idx):
        if i in active_days:
            pnl_pct = pnl_per_active_day
            pnl_usd = pnl_pct / 100 * capital_usd
            equity += pnl_usd
            rows.append({
                "pnl_pct": pnl_pct,
                "pnl_usd": pnl_usd,
                "trade_count": 1,
                "is_active": True,
                "is_winning": pnl_pct > 0,
                "equity_usd": equity,
            })
        else:
            rows.append({
                "pnl_pct": 0.0,
                "pnl_usd": 0.0,
                "trade_count": 0,
                "is_active": False,
                "is_winning": False,
                "equity_usd": equity,
            })
    return pd.DataFrame(rows, index=idx)


def test_block2_simulate_windows() -> None:
    print("\n--- Block 2: simulate_smart_filter_windows ---")
    from smart_filter_adequacy import simulate_smart_filter_windows

    # 4. Length invariant: n_windows = days - window_days + 1.
    df_good = _build_daily_df(
        days=90, pnl_per_active_day=1.0, active_days=list(range(0, 90, 2)),
    )
    win_df = simulate_smart_filter_windows(
        df_good, window_days=30, min_trading_days=14,
        win_days_threshold=0.65, mdd_threshold=20.0,
    )
    ok_len = len(win_df) == 90 - 30 + 1
    report("window count = days - window + 1", ok_len, f"got={len(win_df)}")

    # 5. "Good" window passes all four gates.
    #    45 active days over 90, all winning, small MDD, pnl positive.
    ok_any_pass = bool(win_df["passed"].any())
    report(
        "some windows PASS when daily_df is healthy",
        ok_any_pass,
        f"passed={int(win_df['passed'].sum())}/{len(win_df)}",
    )

    # 6. Low-activity window: too few trading days (only 3 active in first 30).
    df_sparse = _build_daily_df(
        days=90, pnl_per_active_day=1.0, active_days=[0, 1, 2],
    )
    win_sparse = simulate_smart_filter_windows(
        df_sparse, window_days=30, min_trading_days=14,
        win_days_threshold=0.65, mdd_threshold=20.0,
    )
    # The first window (ending at day 30) has 3 active days (< 14) -> FAIL.
    first = win_sparse.iloc[0]
    ok_low_act = (
        bool(first["passed"]) is False
        and bool(first["g_trading_days"]) is False
    )
    report(
        "<min_trading_days -> FAIL via g_trading_days",
        ok_low_act,
        f"first={first.to_dict()}",
    )

    # 7. Negative-pnl window: g_pnl_positive False -> passed False.
    #    14 active days all losing.
    df_loss = _build_daily_df(
        days=90, pnl_per_active_day=-1.0, active_days=list(range(0, 14)),
    )
    win_loss = simulate_smart_filter_windows(
        df_loss, window_days=30, min_trading_days=14,
        win_days_threshold=0.65, mdd_threshold=20.0,
    )
    first_loss = win_loss.iloc[0]
    ok_loss = (
        bool(first_loss["passed"]) is False
        and bool(first_loss["g_pnl_positive"]) is False
    )
    report(
        "negative pnl -> FAIL via g_pnl_positive",
        ok_loss,
        f"pnl_sum_usd={first_loss['pnl_sum_usd']}",
    )


# ---------------------------------------------------------------------------
# Block 3 — recommend() verdict tree (4 assertions)
# ---------------------------------------------------------------------------


def _s(pass_rate: float) -> dict:
    return {"pass_rate_pct": float(pass_rate)}


def test_block3_recommend() -> None:
    print("\n--- Block 3: recommend() verdict tree ---")
    from smart_filter_adequacy import recommend

    # 8. All three >= 60% -> STRONG_GO.
    v, _, _ = recommend(_s(72), _s(65), _s(78))
    report("all adequate -> STRONG_GO", v == "STRONG_GO", f"got={v}")

    # 9. h4 adequate, combined below 60% -> H4_DONT_ADD.
    v, _, _ = recommend(_s(70), _s(40), _s(55))
    report("h4 OK, combined FAIL -> H4_DONT_ADD", v == "H4_DONT_ADD", f"got={v}")

    # 10. Neither adequate, combined below 60% -> REJECT_BOTH.
    v, _, _ = recommend(_s(40), _s(30), _s(45))
    report("all FAIL -> REJECT_BOTH", v == "REJECT_BOTH", f"got={v}")

    # 11. h4 FAIL, H5 PASS, combined PASS -> H5_ONLY (new outcome).
    v, _, _ = recommend(_s(40), _s(70), _s(75))
    report(
        "h4 FAIL, H5 OK, combined OK -> H5_ONLY",
        v == "H5_ONLY",
        f"got={v}",
    )


# ---------------------------------------------------------------------------
# Block 4 — optional DB smoke (1 assertion, skipped without DB)
# ---------------------------------------------------------------------------


def test_block4_db_smoke_optional() -> None:
    print("\n--- Block 4: optional DB smoke ---")
    try:
        from collectors.config import get_config
        from collectors.db import init_pool
        init_pool(get_config())
    except Exception as exc:
        print(f"  [SKIP] DB unavailable — {type(exc).__name__}")
        return

    try:
        from validate_h5_z25_h2 import run_h4_baseline
        _summary, trades = run_h4_baseline()
    except Exception as exc:
        report("run_h4_baseline executes", False, f"raised {exc!r}")
        return

    ok_shape = isinstance(trades, list) and len(trades) > 0
    report(
        "h4 baseline returns non-empty trade list",
        ok_shape,
        f"n={len(trades)}",
    )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 60)
    print("L13 Phase 3c — smart_filter_adequacy offline tests")
    print("=" * 60)
    test_block1_compute_daily_metrics()
    test_block2_simulate_windows()
    test_block3_recommend()
    test_block4_db_smoke_optional()
    print()
    print("=" * 60)
    print(f"RESULT: PASS={passed}  FAIL={failed}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
