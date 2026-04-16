"""
Main scheduler loop for the showcase live trading bot.

Mirrors bot/scheduler.py structure with these differences:
  - Uses LiveExecutor + BinanceClient (real orders, exchange-side TP/SL)
  - Applies STRICTER conviction filter than paper:
      showcase_z_threshold=2.0 (paper=1.0)
      showcase_min_coins_flushing=5 (paper=4)
  - SafetyGuard circuit breakers before every entry
  - File lock on state file prevents dual instances
  - Fixed $35 margin per position (not % of capital)

Run: `python -m exchange.scheduler`. Systemd keeps it alive.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import sys
from datetime import datetime, timedelta, timezone

import aiohttp

from bot.scheduler import next_wake_ts
from bot.signal import SignalComputer
from collectors.alerts import send_alert
from collectors.db import init_pool
from exchange.binance_client import BinanceClient
from exchange.config import ExchangeConfig, get_exchange_config
from exchange.live_executor import LiveExecutor
from exchange.safety import SafetyGuard

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Alerts (thin wrappers over collectors.alerts.send_alert)
# ------------------------------------------------------------------

def _fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def _fmt_price(p: float) -> str:
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:.4f}"
    return f"${p:.8f}"


async def _notify_startup(
    cfg: ExchangeConfig, summary: dict, balance: dict, dry_run: bool,
) -> None:
    mode = "🔴 DRY RUN" if dry_run else "🟢 LIVE"
    msg = (
        f"{mode} <b>liq-showcase-bot started</b>\n"
        f"Capital (state): {_fmt_money(summary['equity'])}\n"
        f"Balance (exchange): free={_fmt_money(balance['free'])} "
        f"total={_fmt_money(balance['total'])}\n"
        f"Open positions: {summary['open_positions']}\n"
        f"Signal: market_flush (z&gt;{cfg.showcase_z_threshold}, "
        f"n≥{cfg.showcase_min_coins_flushing})\n"
        f"Risk: {cfg.showcase_leverage}x isolated, "
        f"margin=${cfg.showcase_margin_usd}, "
        f"TP={cfg.showcase_tp_pct}%, SL={cfg.showcase_sl_pct}%\n"
        f"Holding: {cfg.showcase_holding_hours}h | "
        f"Max positions: {cfg.showcase_max_positions}"
    )
    await send_alert(cfg, msg)


async def _notify_opened(cfg: ExchangeConfig, pos: dict) -> None:
    msg = (
        f"📈 <b>OPENED LONG {pos['coin']}</b> @ {_fmt_price(pos['entry_price'])}\n"
        f"Signal: z={pos['z_score_at_entry']:+.2f}, "
        f"{pos['n_coins_at_entry']} coins flushing\n"
        f"Size: {_fmt_money(pos['notional_usd'])} "
        f"(margin {_fmt_money(pos['margin_usd'])}, {cfg.showcase_leverage}x lev)\n"
        f"TP: {_fmt_price(pos['tp_price'])} (+{cfg.showcase_tp_pct}%) | "
        f"SL: {_fmt_price(pos['sl_price'])} (-{cfg.showcase_sl_pct}%)\n"
        f"Timeout: {cfg.showcase_holding_hours}h"
    )
    await send_alert(cfg, msg)


async def _notify_closed(
    cfg: ExchangeConfig, trade: dict, equity: float,
) -> None:
    icon = "✅" if trade["pnl_pct"] > 0 else "❌"
    msg = (
        f"{icon} <b>CLOSED {trade['coin']}</b>: "
        f"{trade['pnl_pct']:+.2f}% ({trade['pnl_usd']:+.2f} USD)\n"
        f"Entry {_fmt_price(trade['entry_price'])} → "
        f"Exit {_fmt_price(trade['exit_price'])}\n"
        f"Reason: {trade['exit_reason']}\n"
        f"Equity: {_fmt_money(equity)}"
    )
    await send_alert(cfg, msg)


async def _notify_daily_summary(cfg: ExchangeConfig, summary: dict) -> None:
    pnl_usd = summary["total_pnl_usd"]
    pnl_pct = (
        pnl_usd / cfg.showcase_capital * 100.0 if cfg.showcase_capital else 0.0
    )
    msg = (
        "📊 <b>SHOWCASE DAILY SUMMARY</b>\n"
        f"Equity: {_fmt_money(summary['equity'])} "
        f"({pnl_pct:+.2f}% all-time)\n"
        f"Open: {summary['open_positions']} position(s)\n"
        f"Today: {summary['daily_trades']} trade(s), "
        f"{summary['daily_wins']} win / {summary['daily_losses']} loss\n"
        f"Total: {summary['total_trades']} trade(s), "
        f"{summary['win_rate']:.1f}% win rate"
    )
    await send_alert(cfg, msg)


async def _notify_circuit_breaker(cfg: ExchangeConfig, reason: str) -> None:
    await send_alert(
        cfg,
        f"🛑 <b>CIRCUIT BREAKER</b>: {reason}\nNew entries paused.",
    )


async def _notify_error(cfg: ExchangeConfig, exc: BaseException) -> None:
    await send_alert(
        cfg,
        f"⚠️ <b>liq-showcase-bot cycle error</b>: {exc}",
    )


# ------------------------------------------------------------------
# Main cycle
# ------------------------------------------------------------------

async def run_cycle(
    sig: SignalComputer,
    ex: LiveExecutor,
    cfg: ExchangeConfig,
    guard: SafetyGuard,
) -> None:
    now = datetime.now(timezone.utc)
    log.info("=== SHOWCASE CYCLE START %s ===", now.isoformat(timespec="seconds"))

    # 1. Check positions FIRST (TP/SL may have fired)
    closed = await asyncio.to_thread(ex.check_positions)
    for trade in closed:
        await _notify_closed(cfg, trade, ex.state["capital"])

    # 2. Signal evaluation (same SignalComputer as paper)
    async with aiohttp.ClientSession() as session:
        sig_res = await sig.check_market_flush(session)

    log.info(
        "signal: is_flush=%s n_flushing=%d fetch_failed=%s",
        sig_res["is_market_flush"], sig_res["n_coins_flushing"],
        sig_res["fetch_failed"],
    )

    # 3. Re-evaluate with SHOWCASE thresholds.
    # z_threshold_market=1.5 is the CROSS-COIN flush threshold (unchanged
    # from paper). It defines "how many coins are flushing" — a market-wide
    # breadth measure. Only the per-coin ENTRY threshold is stricter in
    # showcase: 1.0 → 2.0 (showcase_z_threshold).
    if sig_res["fetch_failed"]:
        log.warning("skipping entries: partial/stale CoinGlass fetch")
    else:
        n_flushing = sum(
            1 for z in sig_res["all_z_scores"].values()
            if z > cfg.z_threshold_market
        )
        is_flush = n_flushing >= cfg.showcase_min_coins_flushing

        if is_flush:
            log.info(
                "showcase flush: n_flushing=%d >= %d",
                n_flushing, cfg.showcase_min_coins_flushing,
            )
            held = {p["coin"] for p in ex.state["positions"]}
            slots = cfg.showcase_max_positions - len(ex.state["positions"])

            candidates = sorted(
                [
                    c for c, z in sig_res["all_z_scores"].items()
                    if z > cfg.showcase_z_threshold
                ],
                key=lambda c: sig_res["all_z_scores"][c],
                reverse=True,
            )

            for coin in candidates:
                if slots <= 0:
                    log.info("max positions reached; skipping remaining")
                    break
                if coin in held:
                    log.info("already holding %s; skipping", coin)
                    continue

                # Safety guard
                allowed, reason = guard.can_open_position()
                if not allowed:
                    log.warning("guard blocked entry for %s: %s", coin, reason)
                    await _notify_circuit_breaker(cfg, reason)
                    break  # If guard blocks, don't try more coins this cycle

                try:
                    pos = await asyncio.to_thread(
                        ex.open_position,
                        coin,
                        sig_res["all_z_scores"][coin],
                        n_flushing,
                    )
                except Exception as e:
                    log.warning("open_position failed for %s: %s", coin, e)
                    continue

                held.add(coin)
                slots -= 1
                await _notify_opened(cfg, pos)

    # 4. Daily summary — first cycle of UTC day
    today = now.date().isoformat()
    if now.hour < 4 and ex.state.get("last_summary_date") != today:
        await _notify_daily_summary(cfg, ex.get_summary())
        ex.state["last_summary_date"] = today

    # 5. Persist
    ex._save_state()

    s = ex.get_summary()
    log.info(
        "cycle done: equity=$%.2f open=%d trades=%d win%%=%.1f",
        s["equity"], s["open_positions"], s["total_trades"], s["win_rate"],
    )


# ------------------------------------------------------------------
# File lock
# ------------------------------------------------------------------

def _acquire_lock(path: str) -> int:
    """
    Acquire exclusive file lock. Returns fd (keep open for process lifetime).
    Raises SystemExit if another instance holds the lock.
    """
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lock_path = f"{path}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.critical("another instance is running (lock on %s), exiting", lock_path)
        os.close(fd)
        sys.exit(1)
    return fd


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------

async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = get_exchange_config()

    # Validate API keys
    if not cfg.dry_run and (not cfg.binance_api_key or not cfg.binance_api_secret):
        log.critical(
            "LIQ_BINANCE_API_KEY and LIQ_BINANCE_API_SECRET required "
            "when LIQ_DRY_RUN=false"
        )
        sys.exit(1)

    # File lock — prevents dual instances
    lock_fd = _acquire_lock(cfg.showcase_state_file)
    _ = lock_fd  # keep fd open for process lifetime

    init_pool(cfg)

    client = BinanceClient(cfg)
    guard = SafetyGuard(cfg)
    ex = LiveExecutor(cfg, client, guard)

    # Reconstruct guard counters from state
    guard.load_from_state(ex.state["closed_trades"])

    # Startup sync
    await asyncio.to_thread(ex.sync_with_exchange)

    sig = SignalComputer(cfg)

    # Startup alert
    try:
        balance = await asyncio.to_thread(client.fetch_balance)
    except Exception:
        balance = {"free": 0, "total": 0}
    await _notify_startup(cfg, ex.get_summary(), balance, cfg.dry_run)

    log.info(
        "liq-showcase-bot online | capital=$%.2f lev=%dx dry_run=%s "
        "z>%.1f n>=%d margin=$%.0f max_pos=%d",
        cfg.showcase_capital, cfg.showcase_leverage, cfg.dry_run,
        cfg.showcase_z_threshold, cfg.showcase_min_coins_flushing,
        cfg.showcase_margin_usd, cfg.showcase_max_positions,
    )

    while True:
        try:
            await run_cycle(sig, ex, cfg, guard)
        except Exception as e:
            log.exception("unhandled cycle error")
            try:
                await _notify_error(cfg, e)
            except Exception:
                pass

        wake = next_wake_ts()
        sleep_s = max(1.0, (wake - datetime.now(timezone.utc)).total_seconds())
        log.info(
            "sleeping %.0fs until %s",
            sleep_s, wake.isoformat(timespec="seconds"),
        )
        await asyncio.sleep(sleep_s)


if __name__ == "__main__":
    asyncio.run(main())
