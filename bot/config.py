"""
Bot configuration.

Subclasses `collectors.config.Config` to inherit DB + Telegram + CoinGlass env
vars and the `LIQ_` env prefix / `.env` loading. Adds bot-specific fields with
hardcoded defaults that match the L3b-2 `market_flush` signal exactly.

Do NOT parameterize signal thresholds via env — the signal is locked from the
backtest. Risk and paper-trading params are overridable for quick tuning.
"""
from __future__ import annotations

from functools import lru_cache

from collectors.config import Config


class BotConfig(Config):
    # --- Signal (locked from L3b-2; DO NOT CHANGE without a new backtest) ---
    bot_coins: list[str] = [
        "BTC", "ETH", "SOL", "DOGE", "LINK",
        "AVAX", "SUI", "ARB", "WIF", "PEPE",
    ]
    z_threshold_self: float = 1.0      # market_flush.long_vol_zscore > 1.0
    z_threshold_market: float = 1.5    # CROSS_COIN_FLUSH_Z
    min_coins_flushing: int = 4        # market_flush.n_coins_flushing >= 4
    z_lookback: int = 90               # rolling window (matches L2 backtest)
    holding_hours: int = 8             # 2 × 4H bars

    # --- Risk ---
    max_loss_pct: float = 5.0          # unleveraged price drop; SL threshold
    max_positions: int = 5

    # --- Paper account ---
    initial_capital: float = 1000.0
    position_size_pct: float = 10.0    # % of capital used as margin per trade
    leverage: float = 3.0

    # --- Paths ---
    state_file: str = "state/paper_state.json"

    # --- CoinGlass request constants (mirror backfill) ---
    coinglass_exchange_list: str = (
        "Binance,OKX,Bybit,Bitget,dYdX,Huobi,Gate,Bitmex,CoinEx,Kraken"
    )
    coinglass_request_sleep_s: float = 2.5


@lru_cache
def get_bot_config() -> BotConfig:
    return BotConfig()
