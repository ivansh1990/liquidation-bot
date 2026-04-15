"""
MarkdownV2 message builders. Pure functions — no I/O, no config loads.

Convention:
  * Every dynamic string runs through `escape_md` before being inserted
    into a message.
  * Literal MarkdownV2 syntax (``**bold**``, fenced code blocks, ``__italic__``)
    is composed AROUND the escaped body, never inside.
  * Tables use a fenced ``` text ... ``` block — inside that block,
    MarkdownV2 does NOT require escaping, so we can use ASCII `|` and `.` freely.
  * Each returned string is ≤ 4096 chars (Telegram's hard limit).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from bot.config import BotConfig
from telegram_bot.registry import StrategyEntry
from telegram_bot.telegram_api import escape_md

_MAX_MSG = 4000  # stay under Telegram's 4096 with headroom for trim marker

# Unicode block chars used for the /pnl sparkline (not MD2-special).
_BLOCKS = "▁▂▃▄▅▆▇█"


# ---------------------------------------------------------------------------
# Price / money helpers (copied from bot/alerts.py intentionally — keeps
# telegram_bot/ free of any `bot/alerts` dependency).
# ---------------------------------------------------------------------------
def _fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def _fmt_price(p: float) -> str:
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:.4f}"
    return f"${p:.8f}"


def _fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def _signed_money(x: float) -> str:
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.2f}"


def _trim_to_limit(body: str) -> str:
    """Trim body if it would exceed _MAX_MSG; preserve valid MD2 by breaking at newline."""
    if len(body) <= _MAX_MSG:
        return body
    head = body[: _MAX_MSG - 40]
    last_nl = head.rfind("\n")
    if last_nl > 0:
        head = head[:last_nl]
    return head + "\n\n" + escape_md("… truncated")


def _state_emoji(state: str) -> str:
    return {
        "active": "✅",
        "stopped": "⛔",
        "not_deployed": "⚪",
        "error": "❌",
    }.get(state, "❔")


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------
def format_status(strategies: list[dict[str, Any]]) -> str:
    """
    Each strategy dict keys:
        label, state, equity, pnl_today_usd, pnl_today_pct,
        pnl_total_usd, pnl_total_pct, open_positions, last_cycle_iso,
        systemd_state, systemd_uptime, error  (error str when state=="error")
    Missing/None → render as `—`.
    """
    header = "*📊 Strategies status*"
    blocks: list[str] = []
    for s in strategies:
        label = escape_md(str(s.get("label", "?")))
        state = str(s.get("state", "?"))
        emoji = _state_emoji(state)
        lines: list[str] = [f"{emoji} *{label}*"]

        if state == "not_deployed":
            lines.append(escape_md("  not deployed"))
        elif state == "error":
            err = escape_md(str(s.get("error") or "unknown"))[:120]
            lines.append(f"  ❌ error: {err}")
        else:
            equity = s.get("equity")
            eq_str = _fmt_money(float(equity)) if equity is not None else "—"
            today_usd = s.get("pnl_today_usd")
            today_pct = s.get("pnl_today_pct")
            today_str = (
                f"{_signed_money(float(today_usd))} ({_fmt_pct(float(today_pct))})"
                if today_usd is not None and today_pct is not None else "—"
            )
            total_usd = s.get("pnl_total_usd")
            total_pct = s.get("pnl_total_pct")
            total_str = (
                f"{_signed_money(float(total_usd))} ({_fmt_pct(float(total_pct))})"
                if total_usd is not None and total_pct is not None else "—"
            )
            open_n = s.get("open_positions")
            last_cycle = s.get("last_cycle_iso")
            sysd_state = s.get("systemd_state") or "—"
            sysd_uptime = s.get("systemd_uptime") or "—"

            lines.append(escape_md(f"  Equity: {eq_str}"))
            lines.append(escape_md(f"  Today: {today_str}"))
            lines.append(escape_md(f"  All-time: {total_str}"))
            lines.append(escape_md(
                f"  Open: {open_n if open_n is not None else '—'} position(s)"
            ))
            lines.append(escape_md(f"  Last cycle: {last_cycle or '—'}"))
            lines.append(escape_md(
                f"  systemd: {sysd_state} ({sysd_uptime})"
            ))
        blocks.append("\n".join(lines))

    body = header + "\n\n" + "\n\n".join(blocks)
    return _trim_to_limit(body)


# ---------------------------------------------------------------------------
# /pnl  — 7-day equity curve + stats
# ---------------------------------------------------------------------------
def _sparkline(values: list[float]) -> str:
    """Map values → 8-level block sparkline. Empty / constant input → flat."""
    if not values:
        return ""
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return _BLOCKS[0] * len(values)
    scale = (len(_BLOCKS) - 1) / (hi - lo)
    out: list[str] = []
    for v in values:
        idx = int((v - lo) * scale)
        idx = max(0, min(len(_BLOCKS) - 1, idx))
        out.append(_BLOCKS[idx])
    return "".join(out)


def format_pnl(
    strategy_label: str,
    equity_days: list[tuple[date, float]],
    win_rate_pct: float,
    best_trade: dict[str, Any] | None,
    worst_trade: dict[str, Any] | None,
    sharpe: float | None,
    total_trades: int,
) -> str:
    label = escape_md(strategy_label)
    lines = [f"*💰 P&L — {label}*", ""]

    # Equity curve (7 days)
    if equity_days:
        sparkline = _sparkline([v for _, v in equity_days])
        curve_rows = [
            f"  {d.isoformat()}  {_fmt_money(v)}" for d, v in equity_days
        ]
        lines.append("Equity \\(last 7d\\):")
        # Fenced code block — no MD2 escaping needed inside
        lines.append("```")
        lines.append(sparkline)
        lines.extend(curve_rows)
        lines.append("```")
    else:
        lines.append(escape_md("Equity curve: no data"))

    lines.append("")
    lines.append(escape_md(f"Total trades: {total_trades}"))
    lines.append(escape_md(f"Win rate: {win_rate_pct:.1f}%"))

    if sharpe is None:
        lines.append(escape_md("Sharpe: — (need ≥10 trades)"))
    else:
        lines.append(escape_md(f"Sharpe (annualized): {sharpe:+.2f}"))

    if best_trade is not None:
        coin = escape_md(str(best_trade.get("coin", "?")))
        pct = float(best_trade.get("pnl_pct", 0.0))
        lines.append(f"Best: {coin} " + escape_md(_fmt_pct(pct)))
    if worst_trade is not None:
        coin = escape_md(str(worst_trade.get("coin", "?")))
        pct = float(worst_trade.get("pnl_pct", 0.0))
        lines.append(f"Worst: {coin} " + escape_md(_fmt_pct(pct)))

    return _trim_to_limit("\n".join(lines))


def format_pnl_not_deployed(strategy_label: str) -> str:
    label = escape_md(strategy_label)
    return f"*💰 P&L — {label}*\n\n" + escape_md("  not deployed")


# ---------------------------------------------------------------------------
# /trades
# ---------------------------------------------------------------------------
def format_trades(
    trades: list[dict[str, Any]],
    strategy_label: str,
    limit: int,
) -> str:
    label = escape_md(strategy_label)
    header = f"*📜 Last trades — {label}*"
    if not trades:
        return header + "\n\n" + escape_md("No trades yet.")

    # Newest first, clamp to limit.
    sorted_trades = sorted(
        trades,
        key=lambda t: t.get("exit_time", ""),
        reverse=True,
    )
    shown = sorted_trades[:limit]
    omitted = len(sorted_trades) - len(shown)

    rows: list[str] = [
        f"{'time':<16} {'coin':<6} {'entry':>10} {'exit':>10} {'pnl%':>8} {'reason':<8}",
        "-" * 64,
    ]
    for t in shown:
        exit_ts = str(t.get("exit_time", ""))[:16]
        coin = str(t.get("coin", "?"))[:6]
        entry = float(t.get("entry_price", 0.0))
        exit_p = float(t.get("exit_price", 0.0))
        pct = float(t.get("pnl_pct", 0.0))
        reason = str(t.get("exit_reason", "?"))[:8]
        entry_s = _compact_price(entry)
        exit_s = _compact_price(exit_p)
        rows.append(
            f"{exit_ts:<16} {coin:<6} {entry_s:>10} {exit_s:>10} {pct:+7.2f}% {reason:<8}"
        )
    body = header + "\n\n```\n" + "\n".join(rows) + "\n```"
    if omitted > 0:
        body += "\n" + escape_md(f"… {omitted} more rows omitted")
    return _trim_to_limit(body)


def format_trades_not_deployed(strategy_label: str) -> str:
    label = escape_md(strategy_label)
    return f"*📜 Last trades — {label}*\n\n" + escape_md("  not deployed")


def _compact_price(p: float) -> str:
    """Table-friendly price string; no $ sign, no commas."""
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.8f}"


# ---------------------------------------------------------------------------
# /positions
# ---------------------------------------------------------------------------
def format_positions(positions: list[dict[str, Any]]) -> str:
    header = "*📌 Open positions*"
    if not positions:
        return header + "\n\n" + escape_md("No open positions.")

    blocks: list[str] = []
    for p in positions:
        coin = escape_md(str(p.get("coin", "?")))
        entry = float(p.get("entry_price", 0.0))
        current = p.get("current_price")
        liq_px = p.get("liquidation_price")
        notional = float(p.get("notional_usd", 0.0))
        margin = float(p.get("margin_usd", 0.0))
        time_in = p.get("time_in_position") or "—"
        label = escape_md(str(p.get("strategy_label", "")))

        if current is None:
            pnl_line = escape_md("  Current: — | Unrealized: —")
        else:
            pnl_pct = (float(current) - entry) / entry * 100.0
            pnl_usd = pnl_pct / 100.0 * notional
            pnl_line = escape_md(
                f"  Current: {_fmt_price(float(current))} | "
                f"Unrealized: {_signed_money(pnl_usd)} ({pnl_pct:+.2f}%)"
            )

        liq_line = (
            escape_md(f"  Liq est: {_fmt_price(float(liq_px))}")
            if liq_px is not None else escape_md("  Liq est: —")
        )

        block = (
            f"📌 *{coin}*" + (f"  _{label}_" if label else "") + "\n"
            + escape_md(
                f"  Entry: {_fmt_price(entry)} | "
                f"Size: ${notional:,.2f} (margin ${margin:,.2f})"
            ) + "\n"
            + pnl_line + "\n"
            + liq_line + "\n"
            + escape_md(f"  Time in: {time_in}")
        )
        blocks.append(block)

    return _trim_to_limit(header + "\n\n" + "\n\n".join(blocks))


# ---------------------------------------------------------------------------
# /config
# ---------------------------------------------------------------------------
def format_config(cfg: BotConfig, entries: list[StrategyEntry]) -> str:
    header = "*⚙️ Config*"
    blocks: list[str] = []

    for e in entries:
        label = escape_md(e.label)
        if not e.is_deployed:
            blocks.append(f"• *{label}*\n" + escape_md("  not deployed"))
            continue
        lines = [
            f"• *{label}*",
            escape_md(f"  state_file: {e.state_file}"),
            escape_md(f"  systemd: {e.systemd_unit}"),
            escape_md(f"  holding: {e.holding_hours}h"),
            escape_md(f"  z_self ≥ {cfg.z_threshold_self}, "
                      f"z_market ≥ {cfg.z_threshold_market}, "
                      f"n_coins ≥ {cfg.min_coins_flushing}"),
            escape_md(f"  z_lookback: {cfg.z_lookback} bars"),
            escape_md(f"  max_loss: {cfg.max_loss_pct}% (price, unleveraged)"),
            escape_md(f"  max_positions: {cfg.max_positions}"),
            escape_md(f"  initial_capital: ${cfg.initial_capital:,.2f}"),
            escape_md(f"  position_size: {cfg.position_size_pct}% × {cfg.leverage}x"),
            escape_md(f"  coins: {', '.join(cfg.bot_coins)}"),
        ]
        blocks.append("\n".join(lines))

    return _trim_to_limit(header + "\n\n" + "\n\n".join(blocks))


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
def format_health(
    systemd: list[dict[str, Any]],
    host: dict[str, Any],
    apis: list[dict[str, Any]],
    recent_errors: list[str],
) -> str:
    """
    systemd: [{unit, state, uptime}]   host: {cpu_pct, mem_used_mb, mem_total_mb, disk_pct}
    apis: [{name, ok, ms}]   recent_errors: list[str]
    """
    header = "*🩺 Health*"
    lines: list[str] = [header, "", "*systemd:*"]
    for s in systemd:
        unit = escape_md(str(s.get("unit", "?")))
        state = str(s.get("state", "?"))
        uptime = escape_md(str(s.get("uptime") or "—"))
        emoji = _state_emoji("active" if state == "active" else
                             "stopped" if state in ("inactive", "failed") else
                             "error")
        lines.append(escape_md("  ") + f"{emoji} {unit} " + escape_md(
            f"({state}, up {uptime})"
        ))

    lines.append("")
    lines.append("*API pings:*")
    for a in apis:
        name = escape_md(str(a.get("name", "?")))
        ok = bool(a.get("ok"))
        ms = a.get("ms")
        emoji = "✅" if ok else "❌"
        tail = escape_md(f"{ms} ms" if ms is not None and ok else "timeout")
        lines.append(f"  {emoji} {name} " + tail)

    lines.append("")
    lines.append("*Host:*")
    if host:
        cpu = host.get("cpu_pct")
        mem_used = host.get("mem_used_mb")
        mem_total = host.get("mem_total_mb")
        disk = host.get("disk_pct")
        cpu_s = f"{cpu:.1f}%" if cpu is not None else "—"
        mem_s = (f"{mem_used:.0f}/{mem_total:.0f} MB"
                 if mem_used is not None and mem_total is not None else "—")
        disk_s = f"{disk:.0f}%" if disk is not None else "—"
        lines.append(escape_md(f"  CPU: {cpu_s}"))
        lines.append(escape_md(f"  RAM: {mem_s}"))
        lines.append(escape_md(f"  Disk: {disk_s} used"))
    else:
        lines.append(escape_md("  (unavailable)"))

    lines.append("")
    lines.append("*Errors last hour:*")
    if recent_errors:
        # Fenced code block avoids escaping quirks in journalctl output.
        lines.append("```")
        for err in recent_errors[:5]:
            lines.append(err[:200])
        lines.append("```")
        if len(recent_errors) > 5:
            lines.append(escape_md(f"… {len(recent_errors) - 5} more"))
    else:
        lines.append(escape_md("  (none)"))

    return _trim_to_limit("\n".join(lines))


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
def format_help() -> str:
    lines = [
        "*🤖 liq\\-telegram\\-bot commands*",
        "",
        escape_md("/status — all strategies at a glance"),
        escape_md("/pnl [4h|2h|1h] — 7-day equity + win rate + Sharpe"),
        escape_md("/trades [4h|2h|1h] [N] — last N trades (default 10, max 50)"),
        escape_md("/positions — open positions with unrealized P&L"),
        escape_md("/config — locked thresholds + per-strategy params"),
        escape_md("/health — systemd + API pings + host stats + recent errors"),
        escape_md("/help — this message"),
        "",
        escape_md("All prices USD. All times UTC. Replies are read-only "
                  "(no execution commands)."),
    ]
    return "\n".join(lines)


# Unknown command
def format_unknown(cmd: str) -> str:
    return (
        escape_md(f"Unknown command: {cmd}") + "\n"
        + escape_md("Try /help")
    )


def format_usage_trades() -> str:
    return escape_md("Usage: /trades [4h|2h|1h] [N]  (N ∈ 1..50)")


def format_rate_limited(retry_after_s: float) -> str:
    return escape_md(f"⏱ Wait {retry_after_s:.1f}s before the next command.")


def format_error(msg: str) -> str:
    return "❌ " + escape_md(f"Error: {msg[:300]}")


def format_loading() -> str:
    return escape_md("⏳ Loading…")


def now_iso_utc() -> str:
    """Shared helper for formatters that want a UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
