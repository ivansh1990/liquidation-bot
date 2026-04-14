"""
Bot-specific Telegram message formatters.

Thin wrappers over `collectors.alerts.send_alert` — we reuse the underlying
Telegram primitive and only define the message strings here.
"""
from __future__ import annotations

from typing import Any

from bot.config import BotConfig
from collectors.alerts import send_alert


def _fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def _fmt_price(p: float) -> str:
    # PEPE is sub-cent; BTC is tens of thousands. Use adaptive precision.
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:.4f}"
    return f"${p:.8f}"


async def notify_startup(cfg: BotConfig, summary: dict[str, Any]) -> bool:
    msg = (
        "🟢 <b>liq-paper-bot started</b>\n"
        f"Capital: {_fmt_money(summary['equity'])}\n"
        f"Open positions: {summary['open_positions']}\n"
        f"Total trades: {summary['total_trades']}\n"
        f"Signal: market_flush (z&gt;{cfg.z_threshold_self}, "
        f"n≥{cfg.min_coins_flushing})\n"
        f"Holding: {cfg.holding_hours}h  |  SL: −{cfg.max_loss_pct}%"
    )
    return await send_alert(cfg, msg)


async def notify_market_flush(cfg: BotConfig, sig_res: dict[str, Any]) -> bool:
    n = sig_res["n_coins_flushing"]
    candidates = sig_res["entry_coins"] or ["(none passed self-threshold)"]
    top_z_lines = [
        f"  {c}: z={z:+.2f}"
        for c, z in sorted(
            sig_res["all_z_scores"].items(),
            key=lambda kv: kv[1], reverse=True,
        )[:5]
    ]
    msg = (
        f"🔥 <b>MARKET FLUSH</b>: {n}/{len(cfg.bot_coins)} coins flushing\n"
        f"Candidates (z&gt;{cfg.z_threshold_self}): {', '.join(candidates)}\n"
        "Top z:\n" + "\n".join(top_z_lines)
    )
    return await send_alert(cfg, msg)


async def notify_opened(cfg: BotConfig, pos: dict[str, Any]) -> bool:
    msg = (
        f"📈 <b>OPENED LONG {pos['coin']}</b> @ {_fmt_price(pos['entry_price'])}\n"
        f"Signal: z={pos['z_score_at_entry']:+.2f}, "
        f"{pos['n_coins_at_entry']} coins flushing\n"
        f"Size: {_fmt_money(pos['notional_usd'])} "
        f"(margin {_fmt_money(pos['margin_usd'])}, {cfg.leverage:g}× lev)\n"
        f"TP: timeout {cfg.holding_hours}h  |  SL: −{cfg.max_loss_pct}% price"
    )
    return await send_alert(cfg, msg)


async def notify_closed(
    cfg: BotConfig,
    trade: dict[str, Any],
    equity: float,
) -> bool:
    icon = "✅" if trade["pnl_pct"] > 0 else "❌"
    msg = (
        f"{icon} <b>CLOSED {trade['coin']}</b>: "
        f"{trade['pnl_pct']:+.2f}% ({trade['pnl_usd']:+.2f} USD)\n"
        f"Entry {_fmt_price(trade['entry_price'])} → "
        f"Exit {_fmt_price(trade['exit_price'])}\n"
        f"Reason: {trade['exit_reason']}\n"
        f"Equity: {_fmt_money(equity)}"
    )
    return await send_alert(cfg, msg)


async def notify_daily_summary(
    cfg: BotConfig, summary: dict[str, Any]
) -> bool:
    pnl_usd = summary["total_pnl_usd"]
    pnl_pct = pnl_usd / cfg.initial_capital * 100.0 if cfg.initial_capital else 0.0
    msg = (
        "📊 <b>DAILY SUMMARY</b>\n"
        f"Equity: {_fmt_money(summary['equity'])} "
        f"({pnl_pct:+.2f}% all-time)\n"
        f"Open: {summary['open_positions']} position(s)\n"
        f"Today: {summary['daily_trades']} trade(s), "
        f"{summary['daily_wins']} win / {summary['daily_losses']} loss\n"
        f"Total: {summary['total_trades']} trade(s), "
        f"{summary['win_rate']:.1f}% win rate"
    )
    return await send_alert(cfg, msg)


async def notify_error(cfg: BotConfig, exc: BaseException) -> bool:
    return await send_alert(cfg, f"⚠️ <b>liq-paper-bot cycle error</b>: {exc}")
