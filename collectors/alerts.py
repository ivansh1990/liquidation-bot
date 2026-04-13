"""
Telegram alert module.

Sends notifications for errors, startup, and critical events.
Skips silently if bot token or chat_id is not configured.
"""

from __future__ import annotations

import logging

import aiohttp
import requests

from collectors.config import Config, get_config

log = logging.getLogger(__name__)


async def send_alert(cfg: Config | None, message: str) -> bool:
    """Send a Telegram alert (async). Returns True on success."""
    cfg = cfg or get_config()
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        return False

    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": cfg.telegram_chat_id,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return True
                body = await resp.text()
                log.warning("Telegram API returned %d: %s", resp.status, body[:200])
                return False
    except Exception as e:
        log.warning("Failed to send Telegram alert: %s", e)
        return False


def send_alert_sync(cfg: Config | None, message: str) -> bool:
    """Send a Telegram alert (sync). Returns True on success."""
    cfg = cfg or get_config()
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        return False

    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": cfg.telegram_chat_id,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        log.warning("Telegram API returned %d: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        log.warning("Failed to send Telegram alert: %s", e)
        return False
