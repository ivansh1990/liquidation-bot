"""
Per-command business logic.

Handlers return **strings** (MarkdownV2); they never send Telegram messages
directly. The dispatcher in app.py is responsible for all I/O to Telegram.
This keeps handlers pure-enough to unit-test with plain asserts.

Handlers MAY do read I/O (PaperExecutor state load, ccxt ticker fetch,
systemctl, HTTP pings). They MUST NOT mutate any paper-bot state.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bot.config import BotConfig
from bot.paper_executor import PaperExecutor
from telegram_bot import formatters as F
from telegram_bot import health as H
from telegram_bot import pnl as P
from telegram_bot.registry import (
    REGISTRY,
    StrategyEntry,
    find_entry,
    load_executor,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------
@dataclass
class HandlerContext:
    cfg: BotConfig
    chat_id: str
    args: list[str]          # tokens after the command name
    message_id: int | None = None


# ---------------------------------------------------------------------------
# Helpers shared by /status and /pnl
# ---------------------------------------------------------------------------
def _load_strategy(entry: StrategyEntry, cfg: BotConfig) -> PaperExecutor | None:
    """load_executor + swallow loader errors (registry uses PaperExecutor's own
    corrupt-file recovery, so this only fires on catastrophic config errors)."""
    if not entry.is_deployed:
        return None
    try:
        return load_executor(entry, cfg)
    except Exception as e:
        log.warning("load_executor(%s) failed: %s", entry.key, e)
        raise


def _time_in_position(entry_time_iso: str, now: datetime) -> str:
    try:
        dt = datetime.fromisoformat(entry_time_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return "—"
    secs = (now - dt).total_seconds()
    return H._fmt_duration(secs)


def _last_cycle_iso(entry: StrategyEntry) -> str | None:
    """
    Derive last scheduler cycle from the state file's mtime. `_save_state`
    is called every cycle regardless of whether a trade fired, so file
    mtime is a reliable heartbeat.
    """
    if not entry.state_file or not os.path.exists(entry.state_file):
        return None
    try:
        ts = os.path.getmtime(entry.state_file)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------
async def handle_status(ctx: HandlerContext) -> str:
    rows: list[dict[str, Any]] = []
    for entry in REGISTRY:
        row: dict[str, Any] = {"label": entry.label}
        if not entry.is_deployed:
            row["state"] = "not_deployed"
            rows.append(row)
            continue
        try:
            ex = _load_strategy(entry, ctx.cfg)
        except Exception as e:
            row["state"] = "error"
            row["error"] = str(e)
            rows.append(row)
            continue
        if ex is None:
            row["state"] = "not_deployed"
            rows.append(row)
            continue

        try:
            summary = ex.get_summary()
            today_usd, today_pct = P.pnl_today(
                ex.state["closed_trades"], ctx.cfg.initial_capital,
            )
            total_usd, total_pct = P.pnl_total(
                summary["equity"], ctx.cfg.initial_capital,
            )
        except Exception as e:
            row["state"] = "error"
            row["error"] = f"pnl agg: {e}"
            rows.append(row)
            continue

        systemd_info: dict[str, Any] = {}
        if entry.systemd_unit:
            try:
                systemd_info = await asyncio.wait_for(
                    H.check_systemd_unit(entry.systemd_unit),
                    timeout=3.0,
                )
            except Exception as e:
                log.debug("systemctl %s: %s", entry.systemd_unit, e)

        sysd_state = systemd_info.get("state") or "—"
        sysd_uptime = systemd_info.get("uptime") or "—"

        state = "active" if sysd_state == "active" else (
            "stopped" if sysd_state in ("inactive", "failed") else "active"
        )
        # NOTE: treat "unknown" (e.g. Darwin) as active so dev runs render
        # cleanly; in prod systemctl is always present.

        row.update({
            "state": state,
            "equity": summary["equity"],
            "pnl_today_usd": today_usd,
            "pnl_today_pct": today_pct,
            "pnl_total_usd": total_usd,
            "pnl_total_pct": total_pct,
            "open_positions": summary["open_positions"],
            "last_cycle_iso": _last_cycle_iso(entry),
            "systemd_state": sysd_state,
            "systemd_uptime": sysd_uptime,
        })
        rows.append(row)

    return F.format_status(rows)


# ---------------------------------------------------------------------------
# /pnl [4h|2h|1h]
# ---------------------------------------------------------------------------
async def handle_pnl(ctx: HandlerContext) -> str:
    key = ctx.args[0] if ctx.args else "4h"
    entry = find_entry(key) or REGISTRY[0]

    if not entry.is_deployed:
        return F.format_pnl_not_deployed(entry.label)

    try:
        ex = _load_strategy(entry, ctx.cfg)
    except Exception as e:
        return F.format_error(f"pnl load: {e}")
    if ex is None:
        return F.format_pnl_not_deployed(entry.label)

    closed = ex.state["closed_trades"]
    hist = ex.state["equity_history"]

    eq_days = P.equity_by_day(hist, ctx.cfg.initial_capital, days=7)
    wr = P.win_rate(closed)
    best, worst = P.best_worst_trade(closed)
    holding_hours = entry.holding_hours or ctx.cfg.holding_hours
    sharpe = P.sharpe_ratio(closed, holding_hours)

    return F.format_pnl(
        strategy_label=entry.label,
        equity_days=eq_days,
        win_rate_pct=wr,
        best_trade=best,
        worst_trade=worst,
        sharpe=sharpe,
        total_trades=len(closed),
    )


# ---------------------------------------------------------------------------
# /trades [4h|2h|1h] [N]
# ---------------------------------------------------------------------------
async def handle_trades(ctx: HandlerContext) -> str:
    strategy_key = "4h"
    limit = 10
    known_keys = {e.key for e in REGISTRY}

    for tok in ctx.args:
        tl = tok.strip().lower()
        if tl in known_keys or tl.rstrip("h") in {k.rstrip("h") for k in known_keys}:
            strategy_key = tl
        else:
            try:
                n = int(tl)
                limit = max(1, min(50, n))
            except ValueError:
                return F.format_usage_trades()

    entry = find_entry(strategy_key) or REGISTRY[0]
    if not entry.is_deployed:
        return F.format_trades_not_deployed(entry.label)

    try:
        ex = _load_strategy(entry, ctx.cfg)
    except Exception as e:
        return F.format_error(f"trades load: {e}")
    if ex is None:
        return F.format_trades_not_deployed(entry.label)

    return F.format_trades(
        trades=list(ex.state["closed_trades"]),
        strategy_label=entry.label,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# /positions  — all deployed strategies, parallel ccxt fetches w/ timeout
# ---------------------------------------------------------------------------
async def handle_positions(ctx: HandlerContext) -> str:
    now = datetime.now(timezone.utc)

    # Gather (executor, position, label) triples across all deployed strategies.
    open_positions: list[tuple[PaperExecutor, dict[str, Any], str]] = []
    for entry in REGISTRY:
        if not entry.is_deployed:
            continue
        try:
            ex = _load_strategy(entry, ctx.cfg)
        except Exception as e:
            log.warning("positions: load %s failed: %s", entry.key, e)
            continue
        if ex is None:
            continue
        for pos in ex.state["positions"]:
            open_positions.append((ex, pos, entry.label))

    if not open_positions:
        return F.format_positions([])

    async def _fetch(ex: PaperExecutor, coin: str) -> float | None:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(ex.get_current_price, coin),
                timeout=ctx.cfg.position_price_timeout_s
                if hasattr(ctx.cfg, "position_price_timeout_s") else 2.0,
            )
        except Exception as e:
            log.debug("position price fetch for %s: %s", coin, e)
            return None

    prices = await asyncio.gather(
        *[_fetch(ex, pos["coin"]) for ex, pos, _ in open_positions],
        return_exceptions=False,
    )

    display: list[dict[str, Any]] = []
    for (ex, pos, label), current in zip(open_positions, prices):
        entry_px = float(pos["entry_price"])
        leverage = float(ctx.cfg.leverage)
        # LONG liquidation (unleveraged price drop of 1/leverage):
        liq_px = entry_px * (1.0 - 1.0 / leverage) if leverage > 0 else None
        display.append({
            "coin": pos["coin"],
            "strategy_label": label,
            "entry_price": entry_px,
            "current_price": current,
            "liquidation_price": liq_px,
            "notional_usd": float(pos.get("notional_usd", 0.0)),
            "margin_usd": float(pos.get("margin_usd", 0.0)),
            "time_in_position": _time_in_position(pos["entry_time"], now),
        })

    return F.format_positions(display)


# ---------------------------------------------------------------------------
# /config
# ---------------------------------------------------------------------------
async def handle_config(ctx: HandlerContext) -> str:
    return F.format_config(ctx.cfg, REGISTRY)


# ---------------------------------------------------------------------------
# /health  — run all checks in parallel with per-check timeout
# ---------------------------------------------------------------------------
async def handle_health(ctx: HandlerContext) -> str:
    units = [e.systemd_unit for e in REGISTRY if e.systemd_unit]
    # Include the telegram bot's own unit so we can self-report.
    units.append("liq-telegram-bot.service")

    async def _sd(unit: str) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                H.check_systemd_unit(unit), timeout=5.0,
            )
        except Exception as e:
            return {"unit": unit, "state": "unknown", "uptime": None,
                    "raw": f"timeout/error: {e}"}

    sd_task = asyncio.gather(*[_sd(u) for u in units])
    ping_task = asyncio.gather(*[H.ping_all()], return_exceptions=True)

    async def _errs() -> list[str]:
        try:
            return await asyncio.wait_for(
                H.recent_errors("liq-paper-bot.service", hours=1),
                timeout=5.0,
            )
        except Exception:
            return []

    err_task = _errs()

    sd_out, ping_out, errs = await asyncio.gather(sd_task, ping_task, err_task)

    apis: list[dict[str, Any]] = []
    if ping_out and not isinstance(ping_out[0], BaseException):
        apis = list(ping_out[0])

    return F.format_health(
        systemd=list(sd_out),
        host=H.host_stats(),
        apis=apis,
        recent_errors=list(errs),
    )


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
async def handle_help(ctx: HandlerContext) -> str:
    return F.format_help()


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------
COMMANDS: dict[str, Any] = {
    "/status": handle_status,
    "/pnl": handle_pnl,
    "/trades": handle_trades,
    "/positions": handle_positions,
    "/config": handle_config,
    "/health": handle_health,
    "/help": handle_help,
    "/start": handle_help,  # Telegram clients send /start on "Open chat"
}

# Commands where the handler may take >1s; the dispatcher shows ⏳ Loading.
NEEDS_LOADING: set[str] = {"/status", "/pnl", "/positions", "/health", "/trades"}


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """
    Parse `/foo bar 10` → ("/foo", ["bar", "10"]). Returns None if `text`
    isn't a slash-command. Strips the optional `@botname` suffix Telegram
    appends in group chats.
    """
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    tokens = text.split()
    cmd = tokens[0].lower()
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    return cmd, tokens[1:]
