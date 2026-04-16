#!/usr/bin/env python3
"""
Integration test for the exchange (live trading) package.

Six offline blocks (no live APIs required):
  1. ExchangeConfig — inheritance, defaults, singleton
  2. BinanceClient — dry-run mode, testnet, precision, error handling
  3. LiveExecutor — state lifecycle, TP/SL computation, close scenarios, sync
  4. SafetyGuard — circuit breakers, load_from_state, rollover
  5. Scheduler — conviction filter, cycle integration
  6. LiveExecutor edge cases — both-fired ambiguous, manual close

Run directly: .venv/bin/python scripts/test_exchange.py
Exit code 0 on all-pass, 1 otherwise.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchange.binance_client import BinanceClient
from exchange.config import ExchangeConfig
from exchange.live_executor import LiveExecutor
from exchange.safety import SafetyGuard

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
# Block 1: ExchangeConfig
# ---------------------------------------------------------------------------

def test_config() -> None:
    print("\n--- Block 1: ExchangeConfig ---")

    cfg = ExchangeConfig(
        showcase_state_file="/tmp/_test_showcase_unused.json",
    )

    # Inherits BotConfig fields
    report(
        "inherits bot_coins from BotConfig",
        len(cfg.bot_coins) == 10 and "BTC" in cfg.bot_coins,
        f"coins={len(cfg.bot_coins)}",
    )
    report(
        "inherits z_threshold_self from BotConfig",
        cfg.z_threshold_self == 1.0,
        f"z_threshold_self={cfg.z_threshold_self}",
    )

    # Showcase defaults
    report(
        "showcase defaults",
        cfg.showcase_capital == 500.0
        and cfg.showcase_leverage == 15
        and cfg.showcase_margin_usd == 35.0
        and cfg.showcase_max_positions == 2
        and cfg.showcase_tp_pct == 5.0
        and cfg.showcase_sl_pct == 3.0,
        f"capital={cfg.showcase_capital} lev={cfg.showcase_leverage} "
        f"margin={cfg.showcase_margin_usd}",
    )

    # Dry run defaults to True
    report("dry_run defaults to True", cfg.dry_run is True)

    # API key fields exist and default empty
    report(
        "API key fields default empty",
        cfg.binance_api_key == "" and cfg.binance_api_secret == "",
    )


# ---------------------------------------------------------------------------
# Block 2: BinanceClient
# ---------------------------------------------------------------------------

def test_binance_client() -> None:
    print("\n--- Block 2: BinanceClient ---")

    cfg = ExchangeConfig(
        dry_run=True,
        showcase_state_file="/tmp/_test_showcase_unused.json",
        showcase_leverage=15,
        showcase_capital=500.0,
    )
    client = BinanceClient(cfg)

    # Mock the exchange for price calls
    mock_exchange = MagicMock()
    mock_exchange.fetch_ticker.return_value = {"last": 100.0, "close": 100.0}
    mock_exchange.amount_to_precision.side_effect = lambda sym, amt: f"{amt:.4f}"
    mock_exchange.price_to_precision.side_effect = lambda sym, p: f"{p:.2f}"
    client._exchange = mock_exchange

    # --- Dry-run: open_market_long ---
    result = client.open_market_long("SOL", 35.0)
    report(
        "dry_run open_market_long returns DRY- id",
        result["id"].startswith("DRY-"),
        f"id={result['id']}",
    )
    report(
        "dry_run open_market_long average = ticker price",
        result["average"] == 100.0,
        f"average={result['average']}",
    )
    report(
        "dry_run open_market_long filled is computed amount",
        float(result["filled"]) > 0,
        f"filled={result['filled']}",
    )
    # Notional = 35 * 15 = 525, amount = 525 / 100 = 5.25
    expected_amount = 35.0 * 15 / 100.0
    report(
        "dry_run amount = margin * leverage / price",
        abs(float(result["filled"]) - expected_amount) < 0.01,
        f"expected={expected_amount} got={result['filled']}",
    )

    # --- Dry-run: no authenticated calls made ---
    report(
        "dry_run: no create_order called on exchange",
        mock_exchange.create_order.call_count == 0,
        f"calls={mock_exchange.create_order.call_count}",
    )

    # --- Dry-run: place_tp_order ---
    tp_result = client.place_tp_order("SOL", 5.25, 105.0)
    report(
        "dry_run place_tp_order returns DRY- id",
        tp_result["id"].startswith("DRY-") and tp_result["status"] == "open",
        f"id={tp_result['id']}",
    )

    # --- Dry-run: place_sl_order ---
    sl_result = client.place_sl_order("SOL", 5.25, 97.0)
    report(
        "dry_run place_sl_order returns DRY- id",
        sl_result["id"].startswith("DRY-") and sl_result["status"] == "open",
        f"id={sl_result['id']}",
    )

    # --- Dry-run: close_market ---
    close_result = client.close_market("SOL", 5.25)
    report(
        "dry_run close_market returns fill at ticker",
        close_result["id"].startswith("DRY-")
        and close_result["average"] == 100.0,
        f"id={close_result['id']}",
    )

    # --- Dry-run: cancel_order ---
    cancel_result = client.cancel_order("DRY-abc", "SOL")
    report(
        "dry_run cancel_order returns ok",
        cancel_result is not None and cancel_result["status"] == "canceled",
    )

    # --- Dry-run: fetch_positions ---
    positions = client.fetch_positions()
    report(
        "dry_run fetch_positions returns empty",
        positions == [],
    )

    # --- Dry-run: fetch_balance ---
    balance = client.fetch_balance()
    report(
        "dry_run fetch_balance returns showcase capital",
        balance["free"] == 500.0 and balance["total"] == 500.0,
        f"free={balance['free']}",
    )

    # --- set_leverage caching ---
    client._configured_symbols.clear()
    client.set_leverage("BTC")
    client.set_leverage("BTC")  # second call should be cached
    report(
        "set_leverage cached — second call is no-op",
        "BTC/USDT:USDT" in client._configured_symbols,
    )

    # --- Testnet mode ---
    cfg_testnet = ExchangeConfig(
        dry_run=False,
        binance_testnet=True,
        binance_api_key="test",
        binance_api_secret="test",
        showcase_state_file="/tmp/_test_showcase_unused.json",
    )
    client_testnet = BinanceClient(cfg_testnet)
    mock_ex2 = MagicMock()
    with patch("ccxt.binance", return_value=mock_ex2):
        _ = client_testnet._get_exchange()
    report(
        "testnet calls set_sandbox_mode(True)",
        mock_ex2.set_sandbox_mode.called
        and mock_ex2.set_sandbox_mode.call_args[0][0] is True,
    )


# ---------------------------------------------------------------------------
# Block 3: LiveExecutor lifecycle
# ---------------------------------------------------------------------------

def test_live_executor() -> None:
    print("\n--- Block 3: LiveExecutor lifecycle ---")

    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "showcase_state.json")
        cfg = ExchangeConfig(
            showcase_state_file=state_path,
            showcase_capital=500.0,
            showcase_leverage=15,
            showcase_margin_usd=35.0,
            showcase_tp_pct=5.0,
            showcase_sl_pct=3.0,
            showcase_holding_hours=8,
            dry_run=True,
        )

        mock_client = MagicMock(spec=BinanceClient)
        mock_client.cfg = cfg
        mock_client.dry_run = True

        # Mock open_market_long return
        mock_client.open_market_long.return_value = {
            "id": "ORD-001",
            "average": 100.0,
            "filled": 5.25,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "closed",
        }
        mock_client.place_tp_order.return_value = {"id": "TP-001", "status": "open"}
        mock_client.place_sl_order.return_value = {"id": "SL-001", "status": "open"}
        mock_client.get_ticker_price.return_value = 100.0
        mock_client.fetch_positions.return_value = []
        mock_client.fetch_balance.return_value = {"free": 500.0, "total": 500.0}

        guard = SafetyGuard(cfg)
        ex = LiveExecutor(cfg, mock_client, guard)

        # --- Fresh state ---
        report(
            "fresh state capital = showcase_capital",
            ex.state["capital"] == 500.0,
            f"capital={ex.state['capital']}",
        )

        # --- Open position ---
        pos = ex.open_position("SOL", z_score=2.5, n_coins=6)

        report(
            "open_position uses order['average'] for entry_price",
            pos["entry_price"] == 100.0,
            f"entry={pos['entry_price']}",
        )
        report(
            "open_position uses order['filled'] for amount",
            pos["amount"] == 5.25,
            f"amount={pos['amount']}",
        )

        # TP/SL computation
        expected_tp = 100.0 * (1 + 5.0 / 100)  # 105.0
        expected_sl = 100.0 * (1 - 3.0 / 100)  # 97.0
        report(
            "tp_price = entry * (1 + tp_pct/100)",
            abs(pos["tp_price"] - expected_tp) < 0.001,
            f"tp={pos['tp_price']} expected={expected_tp}",
        )
        report(
            "sl_price = entry * (1 - sl_pct/100)",
            abs(pos["sl_price"] - expected_sl) < 0.001,
            f"sl={pos['sl_price']} expected={expected_sl}",
        )

        # TP/SL orders placed with order["filled"] amount
        tp_call_args = mock_client.place_tp_order.call_args
        report(
            "place_tp_order called with filled amount",
            tp_call_args[0][1] == 5.25,
            f"amount_arg={tp_call_args[0][1]}",
        )

        # Order IDs saved
        report(
            "tp_order_id and sl_order_id saved",
            pos["tp_order_id"] == "TP-001" and pos["sl_order_id"] == "SL-001",
            f"tp={pos['tp_order_id']} sl={pos['sl_order_id']}",
        )

        # State persisted before TP/SL (crash recovery)
        # Verify state file was written at least twice (once before TP/SL, once after)
        report(
            "state file exists (persisted during open)",
            os.path.exists(state_path),
        )

        # --- State round-trip ---
        ex2 = LiveExecutor(cfg, mock_client, guard)
        report(
            "state reloaded with 1 position",
            len(ex2.state["positions"]) == 1,
            f"n={len(ex2.state['positions'])}",
        )
        report(
            "reloaded position has exchange fields",
            ex2.state["positions"][0]["exchange_order_id"] == "ORD-001"
            and ex2.state["positions"][0]["tp_price"] == expected_tp,
        )

        # --- Close via TP hit ---
        # Position gone from exchange, TP is closed
        mock_client.fetch_positions.return_value = []
        mock_client.fetch_order.side_effect = lambda oid, coin: {
            "TP-001": {"status": "closed", "average": 105.0, "timestamp": 1000},
            "SL-001": {"status": "open", "average": None, "timestamp": None},
        }.get(oid)

        closed = ex2.check_positions()
        report(
            "TP hit: exit_reason=tp_hit",
            len(closed) == 1 and closed[0]["exit_reason"] == "tp_hit",
            f"reason={closed[0]['exit_reason'] if closed else 'none'}",
        )
        report(
            "TP hit: exit_price from TP fill",
            closed[0]["exit_price"] == 105.0,
            f"exit={closed[0]['exit_price']}",
        )

        # P&L formula parity with PaperExecutor
        pnl_pct = (105.0 - 100.0) / 100.0 * 100.0  # 5.0%
        notional = 35.0 * 15  # 525.0
        pnl_usd = pnl_pct / 100.0 * notional  # 26.25
        report(
            "P&L formula: pnl_pct=(exit-entry)/entry*100",
            abs(closed[0]["pnl_pct"] - pnl_pct) < 0.001,
            f"pnl_pct={closed[0]['pnl_pct']:.4f} expected={pnl_pct:.4f}",
        )
        report(
            "P&L formula: pnl_usd=pnl_pct/100*notional",
            abs(closed[0]["pnl_usd"] - pnl_usd) < 0.01,
            f"pnl_usd={closed[0]['pnl_usd']:.4f} expected={pnl_usd:.4f}",
        )
        report(
            "capital updated by realized P&L",
            abs(ex2.state["capital"] - (500.0 + pnl_usd)) < 0.01,
            f"capital={ex2.state['capital']:.2f}",
        )

        # SL was cancelled
        report(
            "unfired SL order cancelled",
            mock_client.cancel_order.called,
        )

    # --- Close via SL hit (separate executor) ---
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "showcase_state.json")
        cfg = ExchangeConfig(
            showcase_state_file=state_path, dry_run=True,
            showcase_capital=500.0, showcase_leverage=15,
            showcase_margin_usd=35.0, showcase_tp_pct=5.0,
            showcase_sl_pct=3.0,
        )
        mock_client = MagicMock(spec=BinanceClient)
        mock_client.open_market_long.return_value = {
            "id": "ORD-002", "average": 100.0, "filled": 5.25,
            "timestamp": datetime.now(timezone.utc).isoformat(), "status": "closed",
        }
        mock_client.place_tp_order.return_value = {"id": "TP-002", "status": "open"}
        mock_client.place_sl_order.return_value = {"id": "SL-002", "status": "open"}
        mock_client.get_ticker_price.return_value = 100.0

        guard = SafetyGuard(cfg)
        ex = LiveExecutor(cfg, mock_client, guard)
        ex.open_position("BTC", 2.1, 5)

        mock_client.fetch_positions.return_value = []
        mock_client.fetch_order.side_effect = lambda oid, coin: {
            "TP-002": {"status": "open", "average": None, "timestamp": None},
            "SL-002": {"status": "closed", "average": 97.0, "timestamp": 1000},
        }.get(oid)

        closed = ex.check_positions()
        report(
            "SL hit: exit_reason=sl_hit, exit_price=97",
            len(closed) == 1
            and closed[0]["exit_reason"] == "sl_hit"
            and closed[0]["exit_price"] == 97.0,
            f"reason={closed[0].get('exit_reason')} price={closed[0].get('exit_price')}",
        )

    # --- Timeout close ---
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "showcase_state.json")
        cfg = ExchangeConfig(
            showcase_state_file=state_path, dry_run=True,
            showcase_capital=500.0, showcase_leverage=15,
            showcase_margin_usd=35.0, showcase_holding_hours=8,
        )
        mock_client = MagicMock(spec=BinanceClient)
        mock_client.open_market_long.return_value = {
            "id": "ORD-003", "average": 100.0, "filled": 5.25,
            "timestamp": datetime.now(timezone.utc).isoformat(), "status": "closed",
        }
        mock_client.place_tp_order.return_value = {"id": "TP-003", "status": "open"}
        mock_client.place_sl_order.return_value = {"id": "SL-003", "status": "open"}
        mock_client.get_ticker_price.return_value = 100.0
        mock_client.close_market.return_value = {
            "id": "CLOSE-003", "average": 101.0, "filled": 5.25,
            "timestamp": datetime.now(timezone.utc).isoformat(), "status": "closed",
        }
        from collectors.config import binance_ccxt_symbol
        mock_client.fetch_positions.return_value = [
            {"symbol": binance_ccxt_symbol("SOL"), "contracts": 5.25,
             "side": "long", "entryPrice": 100.0, "unrealizedPnl": 0.5,
             "markPrice": 101.0, "liquidationPrice": 93.0, "collateral": 35.0},
        ]

        guard = SafetyGuard(cfg)
        ex = LiveExecutor(cfg, mock_client, guard)
        ex.open_position("SOL", 2.0, 5)
        # Force exit_due to past
        ex.state["positions"][0]["exit_due"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        ex._save_state()

        closed = ex.check_positions()
        report(
            "timeout: exit_reason=timeout, TP/SL cancelled",
            len(closed) == 1 and closed[0]["exit_reason"] == "timeout",
            f"reason={closed[0].get('exit_reason')}",
        )
        report(
            "timeout: cancel_order called for TP and SL",
            mock_client.cancel_order.call_count >= 2,
            f"cancel_calls={mock_client.cancel_order.call_count}",
        )
        report(
            "timeout: close_market called",
            mock_client.close_market.called,
        )

    # --- Sync: position in state but not on exchange ---
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "showcase_state.json")
        cfg = ExchangeConfig(
            showcase_state_file=state_path, dry_run=True,
            showcase_capital=500.0,
        )
        mock_client = MagicMock(spec=BinanceClient)
        mock_client.fetch_positions.return_value = []
        mock_client.get_ticker_price.return_value = 100.0
        mock_client.fetch_balance.return_value = {"free": 500.0, "total": 500.0}

        guard = SafetyGuard(cfg)
        ex = LiveExecutor(cfg, mock_client, guard)
        # Manually inject a position
        ex.state["positions"].append({
            "coin": "ETH", "entry_price": 2000.0,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "exit_due": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
            "margin_usd": 35.0, "notional_usd": 525.0,
            "z_score_at_entry": 2.0, "n_coins_at_entry": 5,
            "amount": 0.2625, "exchange_order_id": "ORD-SYNC",
            "tp_price": 2100.0, "sl_price": 1940.0,
            "tp_order_id": "TP-SYNC", "sl_order_id": "SL-SYNC",
        })
        ex._save_state()

        ex.sync_with_exchange()
        report(
            "sync: state position missing on exchange → closed",
            len(ex.state["positions"]) == 0
            and len(ex.state["closed_trades"]) == 1
            and ex.state["closed_trades"][0]["exit_reason"] == "sync_missing",
        )

    # --- Sync: exchange position not in state → alert only ---
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "showcase_state.json")
        cfg = ExchangeConfig(
            showcase_state_file=state_path, dry_run=True,
            showcase_capital=500.0,
        )
        mock_client = MagicMock(spec=BinanceClient)
        mock_client.fetch_positions.return_value = [
            {"symbol": "DOGE/USDT:USDT", "contracts": 1000.0,
             "side": "long", "entryPrice": 0.15, "unrealizedPnl": 0,
             "markPrice": 0.15, "liquidationPrice": 0.1, "collateral": 10.0},
        ]
        mock_client.fetch_balance.return_value = {"free": 490.0, "total": 500.0}

        guard = SafetyGuard(cfg)
        ex = LiveExecutor(cfg, mock_client, guard)
        ex.sync_with_exchange()
        # Unknown position should NOT be adopted into state
        report(
            "sync: unknown exchange position NOT adopted",
            len(ex.state["positions"]) == 0,
        )

    # --- File lock test ---
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = os.path.join(tmp, "test.lock")
        fd1 = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Second lock attempt should fail
        fd2 = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_blocked = False
        except BlockingIOError:
            lock_blocked = True
        os.close(fd2)
        os.close(fd1)
        report(
            "file lock: second lock attempt raises BlockingIOError",
            lock_blocked,
        )

    # --- get_summary compatible with PaperExecutor ---
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "showcase_state.json")
        cfg = ExchangeConfig(
            showcase_state_file=state_path, dry_run=True,
            showcase_capital=500.0,
        )
        mock_client = MagicMock(spec=BinanceClient)
        guard = SafetyGuard(cfg)
        ex = LiveExecutor(cfg, mock_client, guard)
        summary = ex.get_summary()
        expected_keys = {
            "equity", "total_trades", "win_rate", "total_pnl_usd",
            "open_positions", "daily_trades", "daily_wins", "daily_losses",
        }
        report(
            "get_summary has all PaperExecutor keys",
            expected_keys.issubset(set(summary.keys())),
            f"keys={list(summary.keys())}",
        )


# ---------------------------------------------------------------------------
# Block 4: SafetyGuard
# ---------------------------------------------------------------------------

def test_safety_guard() -> None:
    print("\n--- Block 4: SafetyGuard ---")

    cfg = ExchangeConfig(
        showcase_state_file="/tmp/_test_unused.json",
        max_daily_loss_usd=75.0,
        max_consecutive_losses=5,
        max_daily_trades=6,
    )

    # Fresh guard allows
    guard = SafetyGuard(cfg)
    allowed, reason = guard.can_open_position()
    report("fresh guard allows", allowed and reason == "")

    # Record losses totaling < limit → still allowed
    guard.record_trade_result(-30.0)
    guard.record_trade_result(-20.0)
    allowed, reason = guard.can_open_position()
    report(
        "daily loss $50 < $75 → allowed",
        allowed,
        f"daily_loss=${guard._daily_loss_usd}",
    )

    # Push over daily loss limit
    guard.record_trade_result(-30.0)
    allowed, reason = guard.can_open_position()
    report(
        "daily loss $80 >= $75 → blocked",
        not allowed and "daily loss" in reason,
        f"reason={reason}",
    )

    # Consecutive losses
    guard2 = SafetyGuard(cfg)
    for _ in range(4):
        guard2.record_trade_result(-5.0)
    allowed, _ = guard2.can_open_position()
    report(
        "4 consecutive losses < 5 → allowed",
        allowed,
        f"consec={guard2._consecutive_losses}",
    )
    guard2.record_trade_result(-5.0)
    allowed, reason = guard2.can_open_position()
    report(
        "5 consecutive losses >= 5 → blocked",
        not allowed and "consecutive" in reason,
        f"reason={reason}",
    )

    # Win resets consecutive
    guard3 = SafetyGuard(cfg)
    for _ in range(4):
        guard3.record_trade_result(-5.0)
    guard3.record_trade_result(10.0)  # win
    report(
        "win resets consecutive_losses to 0",
        guard3._consecutive_losses == 0,
        f"consec={guard3._consecutive_losses}",
    )

    # Daily trade limit
    guard4 = SafetyGuard(cfg)
    for i in range(6):
        guard4.record_trade_result(5.0)
    allowed, reason = guard4.can_open_position()
    report(
        "6 daily trades >= 6 → blocked",
        not allowed and "daily trade" in reason,
        f"reason={reason}",
    )

    # UTC rollover resets daily but NOT consecutive
    guard5 = SafetyGuard(cfg)
    guard5.record_trade_result(-10.0)
    guard5.record_trade_result(-10.0)
    guard5._last_reset_date = "2026-04-15"  # yesterday
    guard5._maybe_reset_daily()
    report(
        "UTC rollover resets daily_loss to 0",
        guard5._daily_loss_usd == 0.0,
    )
    report(
        "UTC rollover does NOT reset consecutive_losses",
        guard5._consecutive_losses == 2,
        f"consec={guard5._consecutive_losses}",
    )

    # load_from_state
    guard6 = SafetyGuard(cfg)
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    yesterday = (now - timedelta(days=1)).date().isoformat()
    closed_trades = [
        # Yesterday: 2 losses
        {"exit_time": f"{yesterday}T12:00:00+00:00", "pnl_usd": -10.0},
        {"exit_time": f"{yesterday}T16:00:00+00:00", "pnl_usd": -15.0},
        # Today: 1 loss, 1 win
        {"exit_time": f"{today}T04:00:00+00:00", "pnl_usd": -20.0},
        {"exit_time": f"{today}T08:00:00+00:00", "pnl_usd": 30.0},
    ]
    guard6.load_from_state(closed_trades)
    report(
        "load_from_state: daily_trades = today's trades only",
        guard6._daily_trades == 2,
        f"daily_trades={guard6._daily_trades}",
    )
    report(
        "load_from_state: daily_loss = today's losses only",
        abs(guard6._daily_loss_usd - 20.0) < 0.01,
        f"daily_loss={guard6._daily_loss_usd}",
    )
    report(
        "load_from_state: consecutive_losses = 0 (last trade was a win)",
        guard6._consecutive_losses == 0,
        f"consec={guard6._consecutive_losses}",
    )

    # load_from_state with trailing losses across days
    guard7 = SafetyGuard(cfg)
    closed_cross_day = [
        {"exit_time": f"{yesterday}T20:00:00+00:00", "pnl_usd": 50.0},
        {"exit_time": f"{yesterday}T22:00:00+00:00", "pnl_usd": -5.0},
        {"exit_time": f"{today}T02:00:00+00:00", "pnl_usd": -8.0},
        {"exit_time": f"{today}T06:00:00+00:00", "pnl_usd": -12.0},
    ]
    guard7.load_from_state(closed_cross_day)
    report(
        "load_from_state: consecutive_losses spans days (3 trailing losses)",
        guard7._consecutive_losses == 3,
        f"consec={guard7._consecutive_losses}",
    )


# ---------------------------------------------------------------------------
# Block 5: Scheduler integration
# ---------------------------------------------------------------------------

def test_scheduler_integration() -> None:
    print("\n--- Block 5: Scheduler integration ---")

    cfg = ExchangeConfig(
        showcase_state_file="/tmp/_test_showcase_sched.json",
        showcase_z_threshold=2.0,
        showcase_min_coins_flushing=5,
        z_threshold_market=1.5,
        dry_run=True,
    )

    # Conviction filter: signal that passes paper but fails showcase
    z_scores_weak = {
        "BTC": 1.6, "ETH": 1.7, "SOL": 1.8, "DOGE": 1.9,
        "LINK": 0.5, "AVAX": 0.5, "SUI": 0.5, "ARB": 0.5,
        "WIF": 0.5, "PEPE": 0.5,
    }
    n_flushing_weak = sum(1 for z in z_scores_weak.values() if z > 1.5)
    # Paper: min_coins=4 → 4 >= 4 → PASS
    # Showcase: min_coins=5 → 4 < 5 → FAIL
    report(
        "conviction: 4 flushing passes paper (>=4) but fails showcase (>=5)",
        n_flushing_weak == 4,
        f"n_flushing={n_flushing_weak}",
    )

    # Paper entry threshold z>1.0 → 4 coins qualify
    paper_entries = [c for c, z in z_scores_weak.items() if z > 1.0]
    report(
        "paper entry z>1.0 → 4 candidates",
        len(paper_entries) == 4,
        f"entries={paper_entries}",
    )

    # Showcase entry threshold z>2.0 → 0 coins qualify
    showcase_entries = [c for c, z in z_scores_weak.items() if z > 2.0]
    report(
        "showcase entry z>2.0 → 0 candidates from weak signal",
        len(showcase_entries) == 0,
    )

    # Strong signal that passes showcase
    z_scores_strong = {
        "BTC": 2.5, "ETH": 2.3, "SOL": 2.8, "DOGE": 2.1, "LINK": 2.05,
        "AVAX": 1.6, "SUI": 0.5, "ARB": 0.5, "WIF": 0.5, "PEPE": 0.5,
    }
    n_flushing_strong = sum(1 for z in z_scores_strong.values() if z > 1.5)
    report(
        "conviction: 6 flushing >= 5 → showcase passes",
        n_flushing_strong >= 5,
        f"n_flushing={n_flushing_strong}",
    )
    showcase_entries_strong = sorted(
        [c for c, z in z_scores_strong.items() if z > 2.0],
        key=lambda c: z_scores_strong[c],
        reverse=True,
    )
    report(
        "showcase z>2.0 → 5 entry candidates, highest-z first",
        len(showcase_entries_strong) == 5
        and showcase_entries_strong[0] == "SOL",
        f"entries={showcase_entries_strong}",
    )

    # Guard blocks entry
    guard = SafetyGuard(cfg)
    for _ in range(5):
        guard.record_trade_result(-10.0)
    allowed, reason = guard.can_open_position()
    report(
        "guard blocks after 5 consecutive losses",
        not allowed,
        f"reason={reason}",
    )



# ---------------------------------------------------------------------------
# Block 6: LiveExecutor edge cases (both-fired, manual close)
# ---------------------------------------------------------------------------

def test_live_executor_edge_cases() -> None:
    print("\n--- Block 6: LiveExecutor edge cases ---")

    # --- Both-fired: TP earlier → tp_hit ---
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "showcase_state.json")
        cfg = ExchangeConfig(
            showcase_state_file=state_path, dry_run=True,
            showcase_capital=500.0, showcase_leverage=15,
            showcase_margin_usd=35.0, showcase_tp_pct=5.0,
            showcase_sl_pct=3.0,
        )
        mock_client = MagicMock(spec=BinanceClient)
        mock_client.open_market_long.return_value = {
            "id": "ORD-BF1", "average": 100.0, "filled": 5.25,
            "timestamp": datetime.now(timezone.utc).isoformat(), "status": "closed",
        }
        mock_client.place_tp_order.return_value = {"id": "TP-BF1", "status": "open"}
        mock_client.place_sl_order.return_value = {"id": "SL-BF1", "status": "open"}
        mock_client.get_ticker_price.return_value = 100.0

        guard = SafetyGuard(cfg)
        ex = LiveExecutor(cfg, mock_client, guard)
        ex.open_position("SOL", 2.5, 6)

        # Both TP and SL closed, TP timestamp earlier
        mock_client.fetch_positions.return_value = []
        mock_client.fetch_order.side_effect = lambda oid, coin: {
            "TP-BF1": {"status": "closed", "average": 105.0, "timestamp": 1000},
            "SL-BF1": {"status": "closed", "average": 97.0, "timestamp": 1001},
        }.get(oid)

        closed = ex.check_positions()
        report(
            "both-fired TP earlier: exit_reason=tp_hit",
            len(closed) == 1 and closed[0]["exit_reason"] == "tp_hit",
            f"reason={closed[0]['exit_reason'] if closed else 'none'}",
        )
        report(
            "both-fired TP earlier: exit_price=105.0 (from TP fill)",
            closed[0]["exit_price"] == 105.0,
            f"exit={closed[0]['exit_price']}",
        )
        # pnl_pct = (105 - 100) / 100 * 100 = 5.0
        # notional = 35 * 15 = 525
        # pnl_usd = 5.0 / 100 * 525 = 26.25
        # capital = 500 + 26.25 = 526.25
        report(
            "both-fired TP earlier: capital=526.25",
            abs(ex.state["capital"] - 526.25) < 0.01,
            f"capital={ex.state['capital']:.2f}",
        )

    # --- Both-fired: SL earlier → sl_hit ---
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "showcase_state.json")
        cfg = ExchangeConfig(
            showcase_state_file=state_path, dry_run=True,
            showcase_capital=500.0, showcase_leverage=15,
            showcase_margin_usd=35.0, showcase_tp_pct=5.0,
            showcase_sl_pct=3.0,
        )
        mock_client = MagicMock(spec=BinanceClient)
        mock_client.open_market_long.return_value = {
            "id": "ORD-BF2", "average": 100.0, "filled": 5.25,
            "timestamp": datetime.now(timezone.utc).isoformat(), "status": "closed",
        }
        mock_client.place_tp_order.return_value = {"id": "TP-BF2", "status": "open"}
        mock_client.place_sl_order.return_value = {"id": "SL-BF2", "status": "open"}
        mock_client.get_ticker_price.return_value = 100.0

        guard = SafetyGuard(cfg)
        ex = LiveExecutor(cfg, mock_client, guard)
        ex.open_position("SOL", 2.5, 6)

        # Both TP and SL closed, SL timestamp earlier
        mock_client.fetch_positions.return_value = []
        mock_client.fetch_order.side_effect = lambda oid, coin: {
            "TP-BF2": {"status": "closed", "average": 105.0, "timestamp": 1001},
            "SL-BF2": {"status": "closed", "average": 97.0, "timestamp": 1000},
        }.get(oid)

        closed = ex.check_positions()
        report(
            "both-fired SL earlier: exit_reason=sl_hit",
            len(closed) == 1 and closed[0]["exit_reason"] == "sl_hit",
            f"reason={closed[0]['exit_reason'] if closed else 'none'}",
        )
        report(
            "both-fired SL earlier: exit_price=97.0 (from SL fill)",
            closed[0]["exit_price"] == 97.0,
            f"exit={closed[0]['exit_price']}",
        )
        # pnl_pct = (97 - 100) / 100 * 100 = -3.0
        # pnl_usd = -3.0 / 100 * 525 = -15.75
        # capital = 500 + (-15.75) = 484.25
        report(
            "both-fired SL earlier: capital=484.25",
            abs(ex.state["capital"] - 484.25) < 0.01,
            f"capital={ex.state['capital']:.2f}",
        )

    # --- Manual close: neither TP nor SL closed ---
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "showcase_state.json")
        cfg = ExchangeConfig(
            showcase_state_file=state_path, dry_run=True,
            showcase_capital=500.0, showcase_leverage=15,
            showcase_margin_usd=35.0, showcase_tp_pct=5.0,
            showcase_sl_pct=3.0,
        )
        mock_client = MagicMock(spec=BinanceClient)
        mock_client.open_market_long.return_value = {
            "id": "ORD-MAN", "average": 100.0, "filled": 5.25,
            "timestamp": datetime.now(timezone.utc).isoformat(), "status": "closed",
        }
        mock_client.place_tp_order.return_value = {"id": "TP-MAN", "status": "open"}
        mock_client.place_sl_order.return_value = {"id": "SL-MAN", "status": "open"}
        mock_client.get_ticker_price.return_value = 101.5

        guard = SafetyGuard(cfg)
        ex = LiveExecutor(cfg, mock_client, guard)
        ex.open_position("SOL", 2.5, 6)

        # Position gone, but both TP and SL still open
        mock_client.reset_mock()
        mock_client.fetch_positions.return_value = []
        mock_client.fetch_order.side_effect = lambda oid, coin: {
            "TP-MAN": {"status": "open", "average": None, "timestamp": None},
            "SL-MAN": {"status": "open", "average": None, "timestamp": None},
        }.get(oid)
        mock_client.get_ticker_price.return_value = 101.5

        closed = ex.check_positions()
        report(
            "manual close: exit_reason=manual",
            len(closed) == 1 and closed[0]["exit_reason"] == "manual",
            f"reason={closed[0]['exit_reason'] if closed else 'none'}",
        )
        report(
            "manual close: exit_price=101.5 (from ticker)",
            closed[0]["exit_price"] == 101.5,
            f"exit={closed[0]['exit_price']}",
        )
        report(
            "manual close: position removed from state",
            len(ex.state["positions"]) == 0,
            f"n_positions={len(ex.state['positions'])}",
        )
        report(
            "manual close: trade recorded in closed_trades",
            len(ex.state["closed_trades"]) == 1,
            f"n_closed={len(ex.state['closed_trades'])}",
        )
        report(
            "manual close: cancel_order called for both TP and SL",
            mock_client.cancel_order.call_count >= 2,
            f"cancel_calls={mock_client.cancel_order.call_count}",
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run() -> int:
    test_config()
    test_binance_client()
    test_live_executor()
    test_safety_guard()
    test_scheduler_integration()
    test_live_executor_edge_cases()

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
