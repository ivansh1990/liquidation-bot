#!/usr/bin/env python3
"""
Integration test for the paper trading bot.

Three offline blocks (no live APIs required):
  1. Z-score parity   — bot/signal.SignalComputer.compute_z_scores must
                         match the L2 backtest formula byte-for-byte.
  2. State round-trip — PaperExecutor save → reload → close → reload.
  3. Signal end-to-end — mocked fetch, verifies market_flush logic,
                         fresh-bar gate, and entry_coins selection.

Run directly: .venv/bin/python scripts/test_paper_bot.py
Exit code 0 on all-pass, 1 otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import BotConfig
from bot.paper_executor import PaperExecutor
from bot.signal import SignalComputer, _floor_4h

logging.basicConfig(
    level=logging.WARNING,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
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
# Block 1: Z-score parity with L2 backtest
# ---------------------------------------------------------------------------

def test_zscore_parity() -> None:
    print("\n--- Block 1: Z-score parity ---")
    rng = np.random.default_rng(42)
    values = rng.lognormal(mean=15, sigma=1, size=120)
    ts = pd.date_range("2026-01-01", periods=120, freq="4h", tz="UTC")
    df = pd.DataFrame({"long_vol_usd": values}, index=ts)

    cfg = BotConfig(state_file="/tmp/_paper_state_unused.json")
    sig = SignalComputer(cfg)
    out = sig.compute_z_scores(df)

    # Re-compute inline with the same formula as
    # scripts/backtest_liquidation_flush.py:116-119.
    rm = df["long_vol_usd"].rolling(90).mean()
    rs = df["long_vol_usd"].rolling(90).std()
    expected = (df["long_vol_usd"] - rm) / rs

    try:
        pd.testing.assert_series_equal(
            out["long_vol_zscore"], expected, check_names=False,
        )
        report("z-score matches L2 formula element-wise", True,
               f"{out['long_vol_zscore'].notna().sum()} non-NaN rows")
    except AssertionError as e:
        report("z-score matches L2 formula element-wise", False, str(e)[:200])

    # Sanity: last value should be finite when we have >= 90 bars.
    last = out["long_vol_zscore"].iloc[-1]
    report("last z-score is finite", bool(np.isfinite(last)),
           f"z[-1]={last:.4f}")


# ---------------------------------------------------------------------------
# Block 2: PaperExecutor state round-trip (no live ccxt)
# ---------------------------------------------------------------------------

def test_state_roundtrip() -> None:
    print("\n--- Block 2: State round-trip ---")
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "paper_state.json")
        cfg = BotConfig(state_file=state_path)

        # Patch get_current_price so we don't hit live ccxt. Each call
        # returns a coin-specific mock price — entry 100, exit 102 (= +2%).
        prices = {"SOL": 100.0, "BTC": 50000.0}

        with patch.object(
            PaperExecutor, "get_current_price",
            side_effect=lambda self, coin: prices[coin],
            autospec=True,
        ):
            ex = PaperExecutor(cfg)
            starting_capital = ex.state["capital"]

            pos_sol = ex.open_position("SOL", z_score=2.5, n_coins=6)
            pos_btc = ex.open_position("BTC", z_score=1.5, n_coins=6)
            ex._save_state()

            report(
                "state file was created",
                os.path.exists(state_path),
                state_path,
            )

            # Reload into a fresh instance.
            ex2 = PaperExecutor(cfg)
            report(
                "reloaded 2 open positions",
                len(ex2.state["positions"]) == 2,
                f"n={len(ex2.state['positions'])}",
            )
            report(
                "capital preserved across reload",
                ex2.state["capital"] == starting_capital,
                f"${ex2.state['capital']:.2f}",
            )

            # Close SOL by bumping its price +2% and forcing exit_due into past.
            prices["SOL"] = 102.0
            ex2.state["positions"][0]["exit_due"] = (
                datetime.now(timezone.utc) - timedelta(minutes=1)
            ).isoformat()
            closed = ex2.check_positions()
            report(
                "SOL closed on timeout",
                len(closed) == 1
                and closed[0]["coin"] == "SOL"
                and closed[0]["exit_reason"] == "timeout",
                f"closed={len(closed)}",
            )

            # +2% price on $300 notional = +$6 P&L.
            expected_pnl = 2.0 / 100.0 * pos_sol["notional_usd"]
            actual_pnl = closed[0]["pnl_usd"]
            report(
                "P&L formula: pnl_pct/100 * notional_usd",
                abs(actual_pnl - expected_pnl) < 1e-6,
                f"got ${actual_pnl:.4f}, expected ${expected_pnl:.4f}",
            )
            report(
                "capital incremented by realized P&L",
                abs(ex2.state["capital"] - (starting_capital + expected_pnl))
                < 1e-6,
                f"${ex2.state['capital']:.2f}",
            )

            # Trigger catastrophe SL on BTC: drop price -6%.
            prices["BTC"] = 50000.0 * 0.94
            closed2 = ex2.check_positions()
            report(
                "BTC closed on sl_hit (−6% price drop)",
                len(closed2) == 1
                and closed2[0]["exit_reason"] == "sl_hit"
                and closed2[0]["pnl_pct"] < -cfg.max_loss_pct,
                f"pnl_pct={closed2[0]['pnl_pct']:.2f}%",
            )

            ex2._save_state()

            # Final reload: all 2 closed, 0 open.
            ex3 = PaperExecutor(cfg)
            report(
                "after both close: 0 open, 2 closed",
                len(ex3.state["positions"]) == 0
                and len(ex3.state["closed_trades"]) == 2,
                f"open={len(ex3.state['positions'])}, "
                f"closed={len(ex3.state['closed_trades'])}",
            )


# ---------------------------------------------------------------------------
# Block 3: Signal end-to-end with mocked fetch
# ---------------------------------------------------------------------------

async def test_signal_end_to_end() -> None:
    print("\n--- Block 3: Signal end-to-end ---")
    cfg = BotConfig(state_file="/tmp/_paper_state_unused.json")
    sig = SignalComputer(cfg)

    # Build a canned DataFrame whose last bar is aligned to floor_4h(now)-4h,
    # and whose z-scores are crafted per coin to produce the target pattern.
    expected_bar = _floor_4h(datetime.now(timezone.utc)) - timedelta(hours=4)
    idx = pd.date_range(end=expected_bar, periods=95, freq="4h", tz="UTC")
    rng = np.random.default_rng(0)
    base = rng.lognormal(mean=15, sigma=0.3, size=95)

    def _df_with_spike_size(spike_sigmas: float) -> pd.DataFrame:
        """Return a 95-bar DF whose last value is `spike_sigmas` std above the
        rolling-90 mean of the preceding bars."""
        arr = base.copy()
        # First 94 values untouched; last value = mean(prev90) + k*std(prev90).
        prev90 = arr[-91:-1]
        arr[-1] = prev90.mean() + spike_sigmas * prev90.std()
        df = pd.DataFrame(
            {"long_vol_usd": arr, "short_vol_usd": arr * 0.8},
            index=idx,
        )
        return df

    # Design:
    #   4 coins have z ≈ 2.0 (> 1.5 → flushing, > 1.0 → entry candidates)
    #   1 coin has z ≈ 1.2 (> 1.0 but NOT > 1.5)  → entry candidate, not counted
    #   5 coins have z ≈ 0.3 (neither threshold)
    # Expected:
    #   n_coins_flushing = 4 (the 4 strong ones)
    #   is_market_flush  = True (4 >= min_coins_flushing=4)
    #   entry_coins      = those 4 + the 1 borderline = 5 total
    coin_plan = {
        "BTC":  2.1, "ETH":  2.0, "SOL":  2.2, "DOGE": 2.3,  # 4 flushing
        "LINK": 1.2,                                           # entry only
        "AVAX": 0.3, "SUI":  0.3, "ARB": 0.3, "WIF": 0.3, "PEPE": 0.3,
    }
    canned = {c: _df_with_spike_size(z) for c, z in coin_plan.items()}

    expected_flushing = sum(1 for z in coin_plan.values() if z > 1.5)
    expected_entries = sorted(c for c, z in coin_plan.items() if z > 1.0)

    call_count = {"n": 0}

    async def mock_fetch(self, session, coin, n_bars=100):
        call_count["n"] += 1
        return canned[coin]

    with patch.object(
        SignalComputer, "fetch_recent_liquidations",
        new=mock_fetch,
    ), patch("asyncio.sleep", new=lambda *a, **kw: _noop_awaitable()):
        res = await sig.check_market_flush(session=None)

    report(
        "all 10 coins fetched",
        call_count["n"] == 10,
        f"calls={call_count['n']}",
    )
    report(
        f"n_coins_flushing == {expected_flushing}",
        res["n_coins_flushing"] == expected_flushing,
        f"got {res['n_coins_flushing']}",
    )
    report(
        "is_market_flush is True",
        res["is_market_flush"] is True,
        f"got {res['is_market_flush']}",
    )
    report(
        "fetch_failed is False",
        res["fetch_failed"] is False,
        f"got {res['fetch_failed']}",
    )
    actual_entries = sorted(res["entry_coins"])
    report(
        f"entry_coins = {expected_entries}",
        actual_entries == expected_entries,
        f"got {actual_entries}",
    )
    report(
        "timestamp is expected bar",
        res["timestamp"] == expected_bar,
        f"got {res['timestamp']}",
    )

    # --- Now test the stale-bar gate: last bar one step too old. ---
    stale_idx = pd.date_range(
        end=expected_bar - timedelta(hours=4),
        periods=95, freq="4h", tz="UTC",
    )
    stale = {
        c: canned[c].set_axis(stale_idx).rename_axis(canned[c].index.name)
        for c in coin_plan
    }

    async def mock_stale_fetch(self, session, coin, n_bars=100):
        return stale[coin]

    with patch.object(
        SignalComputer, "fetch_recent_liquidations",
        new=mock_stale_fetch,
    ), patch("asyncio.sleep", new=lambda *a, **kw: _noop_awaitable()):
        res_stale = await sig.check_market_flush(session=None)

    report(
        "stale last-bar → fetch_failed=True",
        res_stale["fetch_failed"] is True,
        f"got {res_stale['fetch_failed']}",
    )
    report(
        "stale last-bar → is_market_flush=False",
        res_stale["is_market_flush"] is False,
        f"got {res_stale['is_market_flush']}",
    )


async def _noop_awaitable():
    return None


# ---------------------------------------------------------------------------
# Optional block 4: live CoinGlass smoke test (skipped if no API key)
# ---------------------------------------------------------------------------

async def test_live_coinglass_smoke() -> None:
    print("\n--- Optional: live CoinGlass smoke test ---")
    cfg = BotConfig(state_file="/tmp/_paper_state_unused.json")
    if not cfg.coinglass_api_key:
        report("skipped (no LIQ_COINGLASS_API_KEY in env)", True, "")
        return

    import aiohttp as _aio
    from collectors.db import init_pool
    init_pool(cfg)

    sig = SignalComputer(cfg)
    async with _aio.ClientSession() as session:
        df = await sig.fetch_recent_liquidations(session, "BTC", n_bars=10)

    ok = df is not None and not df.empty
    detail = f"{len(df) if df is not None else 0} rows" if ok else "empty"
    report("live CoinGlass fetch for BTC", ok, detail)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run() -> int:
    test_zscore_parity()
    test_state_roundtrip()
    await test_signal_end_to_end()
    await test_live_coinglass_smoke()

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
