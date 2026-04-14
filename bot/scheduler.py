"""
Main scheduler loop for the paper trading bot.

Cycle, aligned to 4H UTC grid + 5 min buffer (00:05, 04:05, 08:05, ...):
  1. Close any expired or SL-hit positions (runs regardless of signal status)
  2. Fetch signal (skipped only in the sense of *entries* if fetch_failed)
  3. If is_market_flush and not fetch_failed: open new positions (up to max)
  4. Emit daily summary at the first cycle of each UTC day
  5. Atomic-save state
  6. Sleep to next wall-clock 4H + 5min (recomputed each iteration — no drift)

Run: `python -m bot.scheduler`. Systemd keeps it alive; unhandled errors
cause an alert + sleep-to-next-cycle rather than exit.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from bot.alerts import (
    notify_closed,
    notify_daily_summary,
    notify_error,
    notify_market_flush,
    notify_opened,
    notify_startup,
)
from bot.config import BotConfig, get_bot_config
from bot.paper_executor import PaperExecutor
from bot.signal import SignalComputer
from collectors.db import init_pool

log = logging.getLogger(__name__)


async def run_cycle(
    sig: SignalComputer,
    ex: PaperExecutor,
    cfg: BotConfig,
) -> None:
    now = datetime.now(timezone.utc)
    log.info("=== CYCLE START %s ===", now.isoformat(timespec="seconds"))

    # 1. Position exits.
    closed = ex.check_positions()
    for trade in closed:
        await notify_closed(cfg, trade, ex.state["capital"])

    # 2. Signal.
    async with aiohttp.ClientSession() as session:
        sig_res = await sig.check_market_flush(session)

    log.info(
        "signal: is_flush=%s n_flushing=%d fetch_failed=%s entry_coins=%s",
        sig_res["is_market_flush"], sig_res["n_coins_flushing"],
        sig_res["fetch_failed"], sig_res["entry_coins"],
    )

    # 3. Entries (only if signal is clean).
    if sig_res["fetch_failed"]:
        log.warning(
            "skipping entries this cycle: partial/stale CoinGlass fetch"
        )
    elif sig_res["is_market_flush"]:
        await notify_market_flush(cfg, sig_res)
        held = {p["coin"] for p in ex.state["positions"]}
        slots = cfg.max_positions - len(ex.state["positions"])
        # Prioritize highest-z coins first.
        candidates = sorted(
            sig_res["entry_coins"],
            key=lambda c: sig_res["all_z_scores"][c],
            reverse=True,
        )
        for coin in candidates:
            if slots <= 0:
                log.info("max_positions reached; skipping remaining candidates")
                break
            if coin in held:
                log.info("already holding %s; skipping re-entry", coin)
                continue
            try:
                pos = ex.open_position(
                    coin,
                    sig_res["all_z_scores"][coin],
                    sig_res["n_coins_flushing"],
                )
            except Exception as e:
                log.warning("open_position failed for %s: %s", coin, e)
                continue
            held.add(coin)
            slots -= 1
            await notify_opened(cfg, pos)

    # 4. Daily summary — first cycle of the UTC day (hour in [0, 4)).
    today = now.date().isoformat()
    if now.hour < 4 and ex.state.get("last_summary_date") != today:
        await notify_daily_summary(cfg, ex.get_summary())
        ex.state["last_summary_date"] = today

    # 5. Persist.
    ex._save_state()

    s = ex.get_summary()
    log.info(
        "cycle done: equity=$%.2f open=%d trades=%d win%%=%.1f",
        s["equity"], s["open_positions"], s["total_trades"], s["win_rate"],
    )


def next_wake_ts(now: datetime | None = None) -> datetime:
    """
    Next wall-clock 4H mark + 5 min buffer. Always computed from current
    wall time so cycle-length variance doesn't accumulate as drift.
    """
    now = now or datetime.now(timezone.utc)
    next_hour = ((now.hour // 4) + 1) * 4
    if next_hour >= 24:
        target = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
    else:
        target = now.replace(
            hour=next_hour, minute=0, second=0, microsecond=0,
        )
    return target + timedelta(minutes=5)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = get_bot_config()
    init_pool(cfg)

    sig = SignalComputer(cfg)
    ex = PaperExecutor(cfg)

    log.info(
        "liq-paper-bot online | capital=$%.2f coins=%d holding=%dh "
        "z_self>%.1f n>=%d",
        cfg.initial_capital, len(cfg.bot_coins), cfg.holding_hours,
        cfg.z_threshold_self, cfg.min_coins_flushing,
    )
    await notify_startup(cfg, ex.get_summary())

    while True:
        try:
            await run_cycle(sig, ex, cfg)
        except Exception as e:
            log.exception("unhandled cycle error")
            try:
                await notify_error(cfg, e)
            except Exception:
                pass  # never let an alert failure kill the loop

        wake = next_wake_ts()
        sleep_s = max(1.0, (wake - datetime.now(timezone.utc)).total_seconds())
        log.info(
            "sleeping %.0fs until %s",
            sleep_s, wake.isoformat(timespec="seconds"),
        )
        await asyncio.sleep(sleep_s)


if __name__ == "__main__":
    asyncio.run(main())
