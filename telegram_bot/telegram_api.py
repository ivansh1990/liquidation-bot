"""
Raw aiohttp wrappers for the Telegram Bot API + MarkdownV2 helpers.

We deliberately avoid python-telegram-bot / aiogram so there is no new dep
beyond `aiohttp` (already required by the collectors). Keep this module
tiny — just sendMessage, editMessageText, and the MarkdownV2 escape helper.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from telegram_bot.config import TelegramBotConfig

log = logging.getLogger(__name__)

# Telegram's MarkdownV2 requires every one of these characters to be
# backslash-escaped when outside of a code-block / pre context.
# https://core.telegram.org/bots/api#markdownv2-style
_MD2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"


def escape_md(s: str) -> str:
    """
    Escape a raw string so Telegram accepts it as MarkdownV2 body text.

    Use this for ANY user-supplied or dynamically formatted string. Static
    Markdown syntax (``**bold**``, fenced code blocks) should be composed
    AFTER escaping the dynamic content — never around the escaper.
    """
    if not s:
        return ""
    out: list[str] = []
    for ch in s:
        if ch in _MD2_SPECIALS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


async def send_message(
    cfg: TelegramBotConfig,
    chat_id: str | int,
    text: str,
    *,
    parse_mode: str = "MarkdownV2",
    reply_to: int | None = None,
    session: aiohttp.ClientSession | None = None,
) -> int | None:
    """POST /sendMessage. Returns the new message_id on success, None on failure."""
    if not cfg.telegram_bot_token:
        log.warning("send_message: no bot token configured")
        return None
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to

    owns = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status == 200 and body.get("ok"):
                return int(body["result"]["message_id"])
            log.warning(
                "sendMessage %d: %s", resp.status,
                str(body)[:300],
            )
            return None
    except Exception as e:
        log.warning("sendMessage failed: %s", e)
        return None
    finally:
        if owns:
            await session.close()


async def edit_message(
    cfg: TelegramBotConfig,
    chat_id: str | int,
    message_id: int,
    text: str,
    *,
    parse_mode: str = "MarkdownV2",
    session: aiohttp.ClientSession | None = None,
) -> bool:
    """POST /editMessageText. Returns True on success, False on failure."""
    if not cfg.telegram_bot_token:
        return False
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/editMessageText"
    payload: dict[str, Any] = {
        "chat_id": str(chat_id),
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    owns = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status == 200 and body.get("ok"):
                return True
            log.warning(
                "editMessageText %d: %s", resp.status,
                str(body)[:300],
            )
            return False
    except Exception as e:
        log.warning("editMessageText failed: %s", e)
        return False
    finally:
        if owns:
            await session.close()


async def get_updates(
    cfg: TelegramBotConfig,
    offset: int | None,
    *,
    session: aiohttp.ClientSession | None = None,
) -> list[dict[str, Any]]:
    """POST /getUpdates (long-poll). Returns the raw `result` list or []."""
    if not cfg.telegram_bot_token:
        return []
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/getUpdates"
    payload: dict[str, Any] = {
        "timeout": cfg.poll_timeout_s,
        "allowed_updates": ["message"],
    }
    if offset is not None:
        payload["offset"] = offset

    owns = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=cfg.poll_client_timeout_s),
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status == 200 and body.get("ok"):
                return list(body.get("result", []))
            log.warning("getUpdates %d: %s", resp.status, str(body)[:300])
            return []
    finally:
        if owns:
            await session.close()
