"""
LiveExecutor — real Binance Futures order execution with exchange-side TP/SL.

State schema extends PaperExecutor with exchange-specific fields.
Same P&L formula as PaperExecutor (backtest parity):
  pnl_pct = (exit - entry) / entry * 100      (unleveraged)
  pnl_usd = pnl_pct / 100 * notional_usd      (leverage applied)

Crash recovery: state is persisted IMMEDIATELY after market fill, BEFORE
TP/SL placement. If the process dies between fill and TP/SL, the next
startup's sync_with_exchange detects the unprotected position.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from collectors.alerts import send_alert
from exchange.binance_client import BinanceClient
from exchange.config import ExchangeConfig
from exchange.safety import SafetyGuard

log = logging.getLogger(__name__)


class LiveExecutor:
    def __init__(
        self,
        cfg: ExchangeConfig,
        client: BinanceClient,
        guard: SafetyGuard,
    ):
        self.cfg = cfg
        self.client = client
        self.guard = guard
        self.state: dict[str, Any] = self._load_state()

    # ------------------------------------------------------------------
    # State I/O (same pattern as PaperExecutor)
    # ------------------------------------------------------------------
    def _default_state(self) -> dict[str, Any]:
        return {
            "capital": float(self.cfg.showcase_capital),
            "positions": [],
            "closed_trades": [],
            "equity_history": [],
            "last_summary_date": None,
        }

    def _load_state(self) -> dict[str, Any]:
        path = self.cfg.showcase_state_file
        if not os.path.exists(path):
            state = self._default_state()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            return state
        try:
            with open(path, "r") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.error("state file corrupted (%s): %s — starting fresh", path, e)
            return self._default_state()
        default = self._default_state()
        for k, v in default.items():
            state.setdefault(k, v)
        return state

    def _save_state(self) -> None:
        """Atomic write: tmp -> os.replace. Survives SIGKILL mid-write."""
        path = self.cfg.showcase_state_file
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2, default=str)
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # Position opening
    # ------------------------------------------------------------------
    def open_position(
        self,
        coin: str,
        z_score: float,
        n_coins: int,
    ) -> dict[str, Any]:
        """
        Full open sequence with crash-safe state persistence:
        1. Set leverage (idempotent, cached)
        2. Market buy
        3. Persist state with tp/sl_order_id=None (crash recovery point)
        4. Place TP and SL orders
        5. Update state with order IDs, save again
        """
        # 1. Setup
        self.client.set_leverage(coin)

        # 2. Market fill
        margin_usd = self.cfg.showcase_margin_usd
        order = self.client.open_market_long(coin, margin_usd)

        entry_price = float(order["average"])
        filled_amount = float(order["filled"])
        notional_usd = margin_usd * self.cfg.showcase_leverage

        # 3. Compute TP/SL prices
        tp_price = entry_price * (1 + self.cfg.showcase_tp_pct / 100.0)
        sl_price = entry_price * (1 - self.cfg.showcase_sl_pct / 100.0)

        entry_time = datetime.now(timezone.utc)
        exit_due = entry_time + timedelta(hours=self.cfg.showcase_holding_hours)

        pos = {
            "coin": coin,
            "entry_price": entry_price,
            "entry_time": entry_time.isoformat(),
            "exit_due": exit_due.isoformat(),
            "margin_usd": margin_usd,
            "notional_usd": notional_usd,
            "z_score_at_entry": float(z_score),
            "n_coins_at_entry": int(n_coins),
            "amount": filled_amount,
            "exchange_order_id": str(order["id"]),
            "tp_price": tp_price,
            "sl_price": sl_price,
            "tp_order_id": None,
            "sl_order_id": None,
        }

        # 4. PERSIST IMMEDIATELY — crash recovery point
        self.state["positions"].append(pos)
        self._save_state()
        log.info(
            "OPENED LONG %s @ %.6f qty=%.8f (state saved, placing TP/SL next)",
            coin, entry_price, filled_amount,
        )

        # 5. Place TP and SL
        tp_order_id, sl_order_id = self._place_protection(
            coin, filled_amount, tp_price, sl_price,
        )
        pos["tp_order_id"] = tp_order_id
        pos["sl_order_id"] = sl_order_id
        self._save_state()

        log.info(
            "OPENED LONG %s complete: entry=%.6f tp=%.6f sl=%.6f "
            "margin=$%.2f notional=$%.2f z=%.2f n=%d tp_id=%s sl_id=%s",
            coin, entry_price, tp_price, sl_price,
            margin_usd, notional_usd, z_score, n_coins,
            tp_order_id, sl_order_id,
        )
        return pos

    def _place_protection(
        self,
        coin: str,
        amount: float,
        tp_price: float,
        sl_price: float,
    ) -> tuple[str | None, str | None]:
        """
        Place TP and SL orders. On failure, retry 3x with backoff.
        If all retries fail, emergency close the position.
        """
        tp_order_id: str | None = None
        sl_order_id: str | None = None

        # Place TP
        for attempt in range(4):
            try:
                tp_result = self.client.place_tp_order(coin, amount, tp_price)
                tp_order_id = str(tp_result["id"])
                break
            except Exception as e:
                if attempt < 3:
                    delay = 2 ** (attempt + 1)
                    log.warning(
                        "TP placement failed for %s (attempt %d): %s, retry in %ds",
                        coin, attempt + 1, e, delay,
                    )
                    time.sleep(delay)
                else:
                    log.error("TP placement FAILED after 4 attempts for %s: %s", coin, e)

        # Place SL
        for attempt in range(4):
            try:
                sl_result = self.client.place_sl_order(coin, amount, sl_price)
                sl_order_id = str(sl_result["id"])
                break
            except Exception as e:
                if attempt < 3:
                    delay = 2 ** (attempt + 1)
                    log.warning(
                        "SL placement failed for %s (attempt %d): %s, retry in %ds",
                        coin, attempt + 1, e, delay,
                    )
                    time.sleep(delay)
                else:
                    log.error("SL placement FAILED after 4 attempts for %s: %s", coin, e)

        # If either failed, position is unprotected
        if tp_order_id is None or sl_order_id is None:
            self._handle_unprotected(coin, amount, tp_order_id, sl_order_id)

        return tp_order_id, sl_order_id

    def _handle_unprotected(
        self,
        coin: str,
        amount: float,
        tp_order_id: str | None,
        sl_order_id: str | None,
    ) -> None:
        """Alert and emergency close if both TP and SL failed."""
        missing = []
        if tp_order_id is None:
            missing.append("TP")
        if sl_order_id is None:
            missing.append("SL")

        msg = (
            f"🚨 <b>UNPROTECTED POSITION</b>: {coin}\n"
            f"Missing: {', '.join(missing)}\n"
            f"Attempting emergency close..."
        )
        log.critical("UNPROTECTED %s: missing %s", coin, missing)
        _sync_alert(self.cfg, msg)

        if tp_order_id is None and sl_order_id is None:
            # No protection at all — emergency close
            try:
                self.client.close_market(coin, amount)
                log.info("emergency close %s succeeded", coin)
                _sync_alert(
                    self.cfg,
                    f"✅ Emergency close {coin} succeeded",
                )
            except Exception as e:
                log.critical(
                    "EMERGENCY CLOSE FAILED for %s: %s — MANUAL INTERVENTION REQUIRED",
                    coin, e,
                )
                _sync_alert(
                    self.cfg,
                    f"🔴 <b>CRITICAL</b>: emergency close {coin} FAILED: {e}\n"
                    f"MANUAL INTERVENTION REQUIRED",
                )

    # ------------------------------------------------------------------
    # Position checking
    # ------------------------------------------------------------------
    def check_positions(self) -> list[dict[str, Any]]:
        """
        Walk all state positions, detect TP/SL fills and timeouts.
        Returns list of newly closed trade dicts.
        """
        if not self.state["positions"]:
            return []

        now = datetime.now(timezone.utc)
        closed: list[dict[str, Any]] = []
        still_open: list[dict[str, Any]] = []

        # Batch-fetch all exchange positions once
        try:
            exchange_positions = self.client.fetch_positions()
        except Exception as e:
            log.warning(
                "fetch_positions failed, leaving all positions as-is: %s", e,
            )
            return []

        exchange_symbols = {p["symbol"] for p in exchange_positions}

        for pos in self.state["positions"]:
            coin = pos["coin"]
            from collectors.config import binance_ccxt_symbol
            symbol = binance_ccxt_symbol(coin)

            if symbol not in exchange_symbols:
                # Position gone from exchange — determine exit reason
                trade = self._resolve_gone_position(pos, now)
                if trade is not None:
                    closed.append(trade)
                else:
                    # Could not determine — leave in state, retry next cycle
                    still_open.append(pos)
            else:
                # Position still on exchange
                exit_due = datetime.fromisoformat(pos["exit_due"])
                if now >= exit_due:
                    # Timeout — close it
                    trade = self._timeout_close(pos)
                    if trade is not None:
                        closed.append(trade)
                    else:
                        still_open.append(pos)
                else:
                    # Still active — update unrealized P&L for display
                    self._update_unrealized(pos, exchange_positions, symbol)
                    still_open.append(pos)

        self.state["positions"] = still_open

        if closed:
            self._save_state()

        return closed

    def _resolve_gone_position(
        self,
        pos: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any] | None:
        """
        Position disappeared from exchange. Check TP/SL order statuses
        to determine exit reason. On API failure, return None (retry next cycle).
        """
        coin = pos["coin"]
        tp_order_id = pos.get("tp_order_id")
        sl_order_id = pos.get("sl_order_id")

        # Fetch order statuses
        tp_status = None
        tp_fill_price = None
        tp_timestamp = None
        sl_status = None
        sl_fill_price = None
        sl_timestamp = None

        if tp_order_id:
            try:
                tp_order = self.client.fetch_order(tp_order_id, coin)
                if tp_order:
                    tp_status = tp_order.get("status")
                    tp_fill_price = tp_order.get("average")
                    tp_timestamp = tp_order.get("timestamp")
            except Exception as e:
                log.warning(
                    "could not determine TP status for %s, will retry next cycle: %s",
                    coin, e,
                )
                return None

        if sl_order_id:
            try:
                sl_order = self.client.fetch_order(sl_order_id, coin)
                if sl_order:
                    sl_status = sl_order.get("status")
                    sl_fill_price = sl_order.get("average")
                    sl_timestamp = sl_order.get("timestamp")
            except Exception as e:
                log.warning(
                    "could not determine SL status for %s, will retry next cycle: %s",
                    coin, e,
                )
                return None

        tp_closed = tp_status == "closed"
        sl_closed = sl_status == "closed"

        if tp_closed and not sl_closed:
            exit_price = float(tp_fill_price) if tp_fill_price else pos["tp_price"]
            reason = "tp_hit"
            # Cancel the SL
            if sl_order_id:
                self.client.cancel_order(sl_order_id, coin)
        elif sl_closed and not tp_closed:
            exit_price = float(sl_fill_price) if sl_fill_price else pos["sl_price"]
            reason = "sl_hit"
            # Cancel the TP
            if tp_order_id:
                self.client.cancel_order(tp_order_id, coin)
        elif tp_closed and sl_closed:
            # Ambiguous — both fired (stress scenario). Use earlier timestamp.
            log.critical(
                "BOTH TP and SL filled for %s — investigate manually", coin,
            )
            _sync_alert(
                self.cfg,
                f"🔴 <b>CRITICAL</b>: both TP and SL filled for {coin}, "
                f"investigate manually",
            )
            if (tp_timestamp or 0) <= (sl_timestamp or 0):
                exit_price = float(tp_fill_price) if tp_fill_price else pos["tp_price"]
                reason = "tp_hit"
            else:
                exit_price = float(sl_fill_price) if sl_fill_price else pos["sl_price"]
                reason = "sl_hit"
        else:
            # Neither closed — manual close or API lag
            log.warning(
                "position gone but no TP/SL triggered for %s", coin,
            )
            _sync_alert(
                self.cfg,
                f"⚠️ Position gone but no TP/SL triggered for {coin}. "
                f"Possible manual close.",
            )
            try:
                exit_price = self.client.get_ticker_price(coin)
            except Exception:
                exit_price = pos["entry_price"]
            reason = "manual"
            # Cancel any remaining orders
            if tp_order_id:
                self.client.cancel_order(tp_order_id, coin)
            if sl_order_id:
                self.client.cancel_order(sl_order_id, coin)

        return self._close_from_exchange(pos, exit_price, reason)

    def _timeout_close(self, pos: dict[str, Any]) -> dict[str, Any] | None:
        """Close position on timeout: cancel TP/SL, market sell."""
        coin = pos["coin"]
        log.info("timeout close %s", coin)

        # Cancel TP/SL
        if pos.get("tp_order_id"):
            self.client.cancel_order(pos["tp_order_id"], coin)
        if pos.get("sl_order_id"):
            self.client.cancel_order(pos["sl_order_id"], coin)

        # Market close
        try:
            close_result = self.client.close_market(coin, pos["amount"])
            exit_price = float(close_result["average"])
        except Exception as e:
            log.error("timeout close_market failed for %s: %s", coin, e)
            _sync_alert(
                self.cfg,
                f"⚠️ Timeout close failed for {coin}: {e}",
            )
            return None

        return self._close_from_exchange(pos, exit_price, "timeout")

    def _close_from_exchange(
        self,
        pos: dict[str, Any],
        exit_price: float,
        reason: str,
    ) -> dict[str, Any]:
        """
        Record trade closure. Explicit order:
        1) Compute P&L
        2) Update state (capital, closed_trades, equity_history)
        3) _save_state()
        4) guard.record_trade_result()
        """
        entry = pos["entry_price"]
        notional = pos["notional_usd"]
        pnl_pct = (exit_price - entry) / entry * 100.0
        pnl_usd = pnl_pct / 100.0 * notional

        exit_time = datetime.now(timezone.utc)
        trade = {
            "coin": pos["coin"],
            "entry_price": entry,
            "exit_price": exit_price,
            "entry_time": pos["entry_time"],
            "exit_time": exit_time.isoformat(),
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "exit_reason": reason,
            "exchange_order_id": pos.get("exchange_order_id"),
        }

        # 2) Update state
        self.state["capital"] += pnl_usd
        self.state["closed_trades"].append(trade)
        self.state["equity_history"].append({
            "time": exit_time.isoformat(),
            "equity": self.state["capital"],
        })

        # 3) Persist
        self._save_state()

        # 4) Guard
        self.guard.record_trade_result(pnl_usd)

        log.info(
            "CLOSED %s: %+.2f%% ($%+.2f) %s entry=%.6f exit=%.6f equity=$%.2f",
            pos["coin"], pnl_pct, pnl_usd, reason,
            entry, exit_price, self.state["capital"],
        )
        return trade

    def _update_unrealized(
        self,
        pos: dict[str, Any],
        exchange_positions: list[dict],
        symbol: str,
    ) -> None:
        """Update unrealized P&L fields for /positions display."""
        for ep in exchange_positions:
            if ep["symbol"] == symbol:
                pos["unrealized_pnl"] = ep.get("unrealizedPnl", 0)
                pos["mark_price"] = ep.get("markPrice", 0)
                break

    # ------------------------------------------------------------------
    # Sync with exchange on startup
    # ------------------------------------------------------------------
    def sync_with_exchange(self) -> None:
        """
        Reconcile state with exchange:
        - State position missing from exchange -> close with reason=sync_missing
        - Exchange position not in state -> alert only, do NOT touch
        - Log balance vs state capital
        """
        log.info("sync_with_exchange starting")

        try:
            exchange_positions = self.client.fetch_positions()
        except Exception as e:
            log.warning("sync: fetch_positions failed: %s", e)
            _sync_alert(
                self.cfg,
                f"⚠️ Sync: could not fetch exchange positions: {e}",
            )
            return

        from collectors.config import binance_ccxt_symbol
        exchange_symbols = {p["symbol"] for p in exchange_positions}

        # Check state positions against exchange
        still_open = []
        for pos in self.state["positions"]:
            symbol = binance_ccxt_symbol(pos["coin"])
            if symbol not in exchange_symbols:
                log.warning(
                    "sync: %s in state but not on exchange — marking closed",
                    pos["coin"],
                )
                try:
                    exit_price = self.client.get_ticker_price(pos["coin"])
                except Exception:
                    exit_price = pos["entry_price"]
                self._close_from_exchange(pos, exit_price, "sync_missing")
                _sync_alert(
                    self.cfg,
                    f"⚠️ Sync: {pos['coin']} was in state but not on exchange. "
                    f"Marked as closed (sync_missing).",
                )
            else:
                # Check if position has TP/SL protection
                if not pos.get("tp_order_id") or not pos.get("sl_order_id"):
                    log.warning(
                        "sync: %s has missing TP/SL protection, re-placing",
                        pos["coin"],
                    )
                    tp_id, sl_id = self._place_protection(
                        pos["coin"], pos["amount"],
                        pos["tp_price"], pos["sl_price"],
                    )
                    if tp_id:
                        pos["tp_order_id"] = tp_id
                    if sl_id:
                        pos["sl_order_id"] = sl_id
                still_open.append(pos)

        self.state["positions"] = still_open

        # Check exchange positions not in state
        state_symbols = {
            binance_ccxt_symbol(p["coin"]) for p in self.state["positions"]
        }
        for ep in exchange_positions:
            if ep["symbol"] not in state_symbols:
                log.warning(
                    "sync: exchange position %s NOT in state — DO NOT TOUCH",
                    ep["symbol"],
                )
                _sync_alert(
                    self.cfg,
                    f"⚠️ <b>UNKNOWN POSITION</b> on exchange: {ep['symbol']} "
                    f"({ep['contracts']} contracts). Not managed by this bot.",
                )

        # Log balance
        try:
            balance = self.client.fetch_balance()
            log.info(
                "sync: exchange USDT free=$%.2f total=$%.2f | "
                "state capital=$%.2f",
                balance["free"], balance["total"], self.state["capital"],
            )
        except Exception as e:
            log.warning("sync: fetch_balance failed: %s", e)

        self._save_state()
        log.info("sync_with_exchange complete")

    # ------------------------------------------------------------------
    # Reporting (compatible with PaperExecutor.get_summary)
    # ------------------------------------------------------------------
    def get_summary(self) -> dict[str, Any]:
        closed = self.state["closed_trades"]
        total = len(closed)
        wins = sum(1 for t in closed if t["pnl_pct"] > 0)
        total_pnl_usd = sum(t["pnl_usd"] for t in closed)

        now = datetime.now(timezone.utc)
        day_cutoff = now - timedelta(hours=24)
        daily = [
            t for t in closed
            if datetime.fromisoformat(t["exit_time"]) >= day_cutoff
        ]
        daily_wins = sum(1 for t in daily if t["pnl_pct"] > 0)

        return {
            "equity": self.state["capital"],
            "total_trades": total,
            "win_rate": (wins / total * 100.0) if total else 0.0,
            "total_pnl_usd": total_pnl_usd,
            "open_positions": len(self.state["positions"]),
            "daily_trades": len(daily),
            "daily_wins": daily_wins,
            "daily_losses": len(daily) - daily_wins,
        }


def _sync_alert(cfg: ExchangeConfig, msg: str) -> None:
    """Best-effort sync Telegram alert (import avoids circular)."""
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(send_alert(cfg, msg))
        else:
            loop.run_until_complete(send_alert(cfg, msg))
    except Exception:
        # Never let alert failure crash execution
        log.debug("alert send failed (non-fatal)")
