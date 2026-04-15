"""
getUpdates long-poll loop.

Responsibilities:
  * Maintain `offset` = last_update_id + 1 in memory so we never double-process
    an update within a single process lifetime.
  * Filter to text messages starting with `/` sent from cfg.telegram_chat_id.
  * Dispatch one update at a time (serial) to `handler`. Rate limiting lives
    downstream in the dispatcher.

On a network or Telegram-side error we log and sleep `poll_error_backoff_s`
then retry. The loop is only ever terminated by cancelling the task.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from telegram_bot.config import TelegramBotConfig
from telegram_bot.telegram_api import get_updates

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


def _extract_message(update: dict[str, Any]) -> dict[str, Any] | None:
    """
    Return the top-level message payload we care about, or None to drop.
    We ignore edited_message, channel_post, callback_query, etc.
    """
    msg = update.get("message")
    if not msg:
        return None
    if "text" not in msg:
        return None
    return msg


def _is_authorized(msg: dict[str, Any], authorized_chat_id: str) -> bool:
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    if cid is None:
        return False
    return str(cid) == str(authorized_chat_id)


async def poll_updates(
    cfg: TelegramBotConfig,
    handler: Handler,
) -> None:
    """Run forever — caller cancels the task to stop."""
    offset: int | None = None
    while True:
        try:
            updates = await get_updates(cfg, offset)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("getUpdates error: %s", e)
            await asyncio.sleep(cfg.poll_error_backoff_s)
            continue

        for upd in updates:
            upd_id = upd.get("update_id")
            if isinstance(upd_id, int):
                offset = max(offset or 0, upd_id + 1)

            msg = _extract_message(upd)
            if msg is None:
                continue

            if not _is_authorized(msg, cfg.telegram_chat_id):
                chat_id = (msg.get("chat") or {}).get("id")
                log.info("ignored update from chat %s (not authorized)", chat_id)
                continue

            try:
                await handler(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Defensive: dispatch should never leak, but if it does we
                # log and keep polling so the bot stays alive.
                log.exception("dispatch crashed: %s", e)
