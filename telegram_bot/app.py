"""
Entrypoint for liq-telegram-bot.service.

Wires together:
  * TelegramBotConfig  (env-loaded)
  * RateLimiter        (per-chat, 5 s default)
  * poll_updates       (long-poll)
  * dispatch           (command router + error handler)

Run:   python -m telegram_bot.app
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any

from telegram_bot import formatters as F
from telegram_bot.config import TelegramBotConfig, get_telegram_bot_config
from telegram_bot.handlers import (
    COMMANDS,
    NEEDS_LOADING,
    HandlerContext,
    parse_command,
)
from telegram_bot.polling import poll_updates
from telegram_bot.rate_limit import RateLimiter
from telegram_bot.telegram_api import edit_message, escape_md, send_message

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("telegram_bot.app")


def build_dispatcher(
    cfg: TelegramBotConfig,
    limiter: RateLimiter,
):
    async def dispatch(msg: dict[str, Any]) -> None:
        text = msg.get("text", "")
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        message_id = msg.get("message_id")

        parsed = parse_command(text)
        if not parsed:
            return  # not a slash command — ignore
        cmd, args = parsed

        handler = COMMANDS.get(cmd)
        if handler is None:
            await send_message(
                cfg, chat_id, F.format_unknown(cmd),
                reply_to=message_id,
            )
            return

        allowed, retry_after = limiter.check(chat_id)
        if not allowed:
            await send_message(
                cfg, chat_id, F.format_rate_limited(retry_after),
                reply_to=message_id,
            )
            return

        loading_id: int | None = None
        if cmd in NEEDS_LOADING:
            loading_id = await send_message(
                cfg, chat_id, F.format_loading(),
                reply_to=message_id,
            )

        ctx = HandlerContext(cfg=cfg, chat_id=chat_id, args=args,
                             message_id=message_id)
        try:
            result = await asyncio.wait_for(
                handler(ctx), timeout=cfg.command_reply_timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning("handler %s timed out", cmd)
            result = F.format_error("command timed out")
        except Exception as e:
            log.error("handler %s crashed:\n%s", cmd, traceback.format_exc())
            # Truncate stacktrace; only the exception message reaches Telegram.
            result = F.format_error(f"{type(e).__name__}: {e}")

        if loading_id is not None:
            ok = await edit_message(cfg, chat_id, loading_id, result)
            if not ok:
                # fallback — edit failed (rare), send as new message.
                await send_message(cfg, chat_id, result, reply_to=message_id)
        else:
            await send_message(cfg, chat_id, result, reply_to=message_id)

    return dispatch


async def main() -> None:
    cfg = get_telegram_bot_config()
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        log.error(
            "telegram_bot_token / telegram_chat_id not configured — aborting.",
        )
        return

    limiter = RateLimiter(cfg.rate_limit_window_s)
    dispatcher = build_dispatcher(cfg, limiter)

    startup_text = escape_md(
        "🟢 liq-telegram-bot online. Send /help for commands.",
    )
    await send_message(cfg, cfg.telegram_chat_id, startup_text)

    log.info(
        "liq-telegram-bot started. Authorized chat: %s",
        cfg.telegram_chat_id,
    )
    await poll_updates(cfg, dispatcher)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
