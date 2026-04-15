"""
Telegram command-bot configuration.

Subclasses `BotConfig` so DB, signal thresholds, state-file path, and
`telegram_bot_token` / `telegram_chat_id` are inherited from the same
`.env` file the paper bot uses. Only adds a handful of long-poll +
rate-limit knobs that are specific to this service.
"""
from __future__ import annotations

from functools import lru_cache

from bot.config import BotConfig


class TelegramBotConfig(BotConfig):
    # --- Long-poll ---
    poll_timeout_s: int = 30                 # getUpdates?timeout=30 (Telegram max)
    poll_client_timeout_s: int = 40          # aiohttp ClientTimeout must > poll_timeout_s
    poll_error_backoff_s: float = 5.0        # sleep before retry on network error

    # --- Command handling ---
    command_reply_timeout_s: float = 15.0    # cap per-handler execution (bumped for /health)
    position_price_timeout_s: float = 2.0    # per-coin ccxt fetch bound in /positions
    rate_limit_window_s: float = 5.0         # min seconds between commands from the same chat

    # --- Authorization ---
    # We intentionally reuse cfg.telegram_chat_id from the parent (single chat_id only).
    # No allow-list — per user choice during planning phase.


@lru_cache
def get_telegram_bot_config() -> TelegramBotConfig:
    return TelegramBotConfig()
