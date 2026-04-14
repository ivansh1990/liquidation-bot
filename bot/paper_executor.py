"""
PaperExecutor — simulates LONG positions, tracks equity, persists state.

State schema (JSON, UTC ISO-8601 timestamps throughout):
  {
    "capital":  1005.97,
    "positions": [
        {
          "coin": "SOL",
          "entry_price": 85.50,
          "entry_time":  "2026-04-14T08:05:12+00:00",
          "exit_due":    "2026-04-14T16:05:12+00:00",
          "margin_usd":  100.0,     # explicit, not inferred
          "notional_usd": 300.0,    # = margin × leverage
          "z_score_at_entry":  2.3,
          "n_coins_at_entry":  6
        }
    ],
    "closed_trades": [
        {
          "coin": "SOL",
          "entry_price": 85.50, "exit_price": 87.22,
          "entry_time":  "...",  "exit_time":  "...",
          "pnl_pct":     1.99,   # unleveraged price move, backtest-parity
          "pnl_usd":     5.97,
          "exit_reason": "timeout" | "sl_hit"
        }
    ],
    "equity_history":    [{"time": "...", "equity": 1005.97}],
    "last_summary_date": "2026-04-14"   # guards daily-summary double-send
  }

P&L semantics (DO NOT change without coordinating with backtest):
  pnl_pct  = (exit - entry) / entry * 100        # unleveraged price move
  pnl_usd  = pnl_pct / 100 * notional_usd         # leverage applied to $
  Capital updates only on close (realized P&L).

Catastrophe SL fires on pnl_pct <= -max_loss_pct  (−5% PRICE drop, not margin).
A 5% price drop at 3× leverage = 15% margin loss — matches backtest max_loss.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import ccxt

from bot.config import BotConfig
from collectors.config import binance_ccxt_symbol

log = logging.getLogger(__name__)


class PaperExecutor:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self._exchange: ccxt.Exchange | None = None
        self.state: dict[str, Any] = self._load_state()

    # ------------------------------------------------------------------
    # State I/O
    # ------------------------------------------------------------------
    def _default_state(self) -> dict[str, Any]:
        return {
            "capital": float(self.cfg.initial_capital),
            "positions": [],
            "closed_trades": [],
            "equity_history": [],
            "last_summary_date": None,
        }

    def _load_state(self) -> dict[str, Any]:
        path = self.cfg.state_file
        if not os.path.exists(path):
            state = self._default_state()
            # Ensure parent dir exists for the eventual save.
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            return state
        try:
            with open(path, "r") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.error("state file corrupted (%s): %s — starting fresh", path, e)
            return self._default_state()
        # Backfill any missing keys from a newer schema version.
        default = self._default_state()
        for k, v in default.items():
            state.setdefault(k, v)
        return state

    def _save_state(self) -> None:
        """Atomic write: tmp → os.replace. Survives SIGKILL mid-write."""
        path = self.cfg.state_file
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2, default=str)
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------
    def _get_exchange(self) -> ccxt.Exchange:
        if self._exchange is None:
            self._exchange = ccxt.binance({"options": {"defaultType": "swap"}})
        return self._exchange

    def get_current_price(self, coin: str) -> float:
        """Live ccxt ticker price (per plan decision #1)."""
        ex = self._get_exchange()
        sym = binance_ccxt_symbol(coin)
        ticker = ex.fetch_ticker(sym)
        last = ticker.get("last") or ticker.get("close")
        if last is None:
            raise RuntimeError(f"ccxt ticker missing last/close for {sym}")
        return float(last)

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------
    def open_position(
        self,
        coin: str,
        z_score: float,
        n_coins: int,
    ) -> dict[str, Any]:
        """
        Open a paper LONG. Entry price is a live ccxt ticker at call time.
        Capital is NOT mutated on open — only on close.
        """
        entry_price = self.get_current_price(coin)
        entry_time = datetime.now(timezone.utc)
        exit_due = entry_time + timedelta(hours=self.cfg.holding_hours)

        margin_usd = self.state["capital"] * self.cfg.position_size_pct / 100.0
        notional_usd = margin_usd * self.cfg.leverage

        pos = {
            "coin": coin,
            "entry_price": entry_price,
            "entry_time": entry_time.isoformat(),
            "exit_due": exit_due.isoformat(),
            "margin_usd": margin_usd,
            "notional_usd": notional_usd,
            "z_score_at_entry": float(z_score),
            "n_coins_at_entry": int(n_coins),
        }
        self.state["positions"].append(pos)
        log.info(
            "OPENED LONG %s @ %.6f  margin=$%.2f notional=$%.2f z=%.2f n=%d",
            coin, entry_price, margin_usd, notional_usd, z_score, n_coins,
        )
        return pos

    def check_positions(self) -> list[dict[str, Any]]:
        """
        Walk all open positions; close any whose exit_due has passed or whose
        unleveraged drawdown has hit -max_loss_pct. Returns newly-closed
        trade dicts (for downstream alerting).
        """
        if not self.state["positions"]:
            return []

        now = datetime.now(timezone.utc)
        closed: list[dict[str, Any]] = []
        still_open: list[dict[str, Any]] = []

        for pos in self.state["positions"]:
            try:
                current = self.get_current_price(pos["coin"])
            except Exception as e:
                log.warning(
                    "price fetch failed for %s; leaving position open: %s",
                    pos["coin"], e,
                )
                still_open.append(pos)
                continue

            entry = pos["entry_price"]
            pnl_pct_price = (current - entry) / entry * 100.0
            exit_due = datetime.fromisoformat(pos["exit_due"])

            if pnl_pct_price <= -self.cfg.max_loss_pct:
                closed.append(self._close(pos, current, "sl_hit"))
            elif now >= exit_due:
                closed.append(self._close(pos, current, "timeout"))
            else:
                still_open.append(pos)

        self.state["positions"] = still_open
        return closed

    def _close(
        self,
        position: dict[str, Any],
        exit_price: float,
        reason: str,
    ) -> dict[str, Any]:
        entry = position["entry_price"]
        notional = position["notional_usd"]
        pnl_pct = (exit_price - entry) / entry * 100.0   # unleveraged
        pnl_usd = pnl_pct / 100.0 * notional              # leverage applied

        exit_time = datetime.now(timezone.utc)
        trade = {
            "coin": position["coin"],
            "entry_price": entry,
            "exit_price": exit_price,
            "entry_time": position["entry_time"],
            "exit_time": exit_time.isoformat(),
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "exit_reason": reason,
        }
        self.state["capital"] += pnl_usd
        self.state["closed_trades"].append(trade)
        self.state["equity_history"].append({
            "time": exit_time.isoformat(),
            "equity": self.state["capital"],
        })
        log.info(
            "CLOSED %s: %+.2f%% ($%+.2f) %s  entry=%.6f exit=%.6f equity=$%.2f",
            position["coin"], pnl_pct, pnl_usd, reason,
            entry, exit_price, self.state["capital"],
        )
        return trade

    # ------------------------------------------------------------------
    # Reporting
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
