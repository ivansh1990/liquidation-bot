"""
SafetyGuard — circuit breakers checked before every position open.

Counters are in-memory only, rebuilt from state on startup via load_from_state().

Reset rules:
  - daily_loss_usd, daily_trades: reset on UTC date rollover.
  - consecutive_losses: NOT reset on rollover. Counts across days.
    4 losses yesterday + 1 today = 5 consecutive = blocked.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from exchange.config import ExchangeConfig

log = logging.getLogger(__name__)


class SafetyGuard:
    def __init__(self, cfg: ExchangeConfig):
        self.cfg = cfg
        self._daily_loss_usd: float = 0.0
        self._daily_trades: int = 0
        self._consecutive_losses: int = 0
        self._last_reset_date: str | None = None

    def _today_utc(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _maybe_reset_daily(self) -> None:
        """Reset daily counters if UTC date changed."""
        today = self._today_utc()
        if self._last_reset_date != today:
            log.info(
                "daily reset: loss $%.2f→$0 trades %d→0 (date %s→%s)",
                self._daily_loss_usd, self._daily_trades,
                self._last_reset_date, today,
            )
            self._daily_loss_usd = 0.0
            self._daily_trades = 0
            self._last_reset_date = today

    def record_trade_result(self, pnl_usd: float) -> None:
        """Update counters after a trade closes."""
        self._maybe_reset_daily()
        self._daily_trades += 1
        if pnl_usd < 0:
            self._daily_loss_usd += abs(pnl_usd)
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0
        log.info(
            "guard updated: daily_loss=$%.2f daily_trades=%d consec_losses=%d",
            self._daily_loss_usd, self._daily_trades, self._consecutive_losses,
        )

    def can_open_position(self) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        Checks daily loss, consecutive losses, and daily trade count.
        """
        self._maybe_reset_daily()

        if self._daily_loss_usd >= self.cfg.max_daily_loss_usd:
            reason = (
                f"daily loss limit reached: ${self._daily_loss_usd:.2f} "
                f">= ${self.cfg.max_daily_loss_usd:.2f}"
            )
            log.warning("guard BLOCKED: %s", reason)
            return False, reason

        if self._consecutive_losses >= self.cfg.max_consecutive_losses:
            reason = (
                f"consecutive loss limit reached: {self._consecutive_losses} "
                f">= {self.cfg.max_consecutive_losses}"
            )
            log.warning("guard BLOCKED: %s", reason)
            return False, reason

        if self._daily_trades >= self.cfg.max_daily_trades:
            reason = (
                f"daily trade limit reached: {self._daily_trades} "
                f">= {self.cfg.max_daily_trades}"
            )
            log.warning("guard BLOCKED: %s", reason)
            return False, reason

        return True, ""

    def load_from_state(self, closed_trades: list[dict]) -> None:
        """
        Reconstruct counters from closed_trades on startup.
        - daily_loss_usd, daily_trades: from today's UTC trades only.
        - consecutive_losses: from tail of ALL trades (not just today).
        """
        today = self._today_utc()
        self._last_reset_date = today
        self._daily_loss_usd = 0.0
        self._daily_trades = 0
        self._consecutive_losses = 0

        # Daily counters: only today's trades
        for t in closed_trades:
            exit_time_str = t.get("exit_time", "")
            if not exit_time_str:
                continue
            try:
                exit_date = datetime.fromisoformat(exit_time_str).date().isoformat()
            except (ValueError, TypeError):
                continue
            if exit_date == today:
                self._daily_trades += 1
                pnl = t.get("pnl_usd", 0)
                if pnl < 0:
                    self._daily_loss_usd += abs(pnl)

        # Consecutive losses: scan from tail of ALL trades
        for t in reversed(closed_trades):
            pnl = t.get("pnl_usd", 0)
            if pnl < 0:
                self._consecutive_losses += 1
            else:
                break

        log.info(
            "guard loaded from state: daily_loss=$%.2f daily_trades=%d "
            "consec_losses=%d",
            self._daily_loss_usd, self._daily_trades, self._consecutive_losses,
        )
