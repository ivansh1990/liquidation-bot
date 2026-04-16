"""
Exchange configuration for live Binance Futures trading.

Subclasses `bot.config.BotConfig` to inherit signal thresholds, DB,
Telegram, and CoinGlass settings. Adds Binance API credentials,
showcase account parameters, circuit breakers, and dry-run control.
"""
from __future__ import annotations

from functools import lru_cache

from bot.config import BotConfig


class ExchangeConfig(BotConfig):
    # --- Binance Futures API ---
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = False

    # --- Showcase account parameters ---
    showcase_capital: float = 500.0
    showcase_leverage: int = 15
    showcase_margin_usd: float = 35.0       # fixed USD margin per position
    showcase_max_positions: int = 2
    showcase_tp_pct: float = 5.0            # unleveraged TP % (= 75% margin at 15x)
    showcase_sl_pct: float = 3.0            # unleveraged SL % (= 45% margin at 15x)
    showcase_holding_hours: int = 8

    # --- Conviction filter (stricter than paper) ---
    showcase_z_threshold: float = 2.0       # per-coin entry z; paper uses 1.0
    showcase_min_coins_flushing: int = 5    # cross-coin count; paper uses 4

    # --- Circuit breakers ---
    max_daily_loss_usd: float = 100.0       # 20% of $500 capital
    max_consecutive_losses: int = 5
    max_daily_trades: int = 6               # 3 cycles x 2 positions max

    # --- Dry run (default safe) ---
    dry_run: bool = True

    # --- State file (separate from paper) ---
    showcase_state_file: str = "state/showcase_state.json"


@lru_cache
def get_exchange_config() -> ExchangeConfig:
    return ExchangeConfig()
