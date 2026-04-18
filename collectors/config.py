from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Canonical coin list (user-facing names)
COINS: list[str] = [
    "BTC", "ETH", "SOL", "DOGE", "LINK",
    "AVAX", "SUI", "ARB", "WIF", "PEPE",
]

# Hyperliquid uses "kPEPE" (1000x scaled) instead of "PEPE"
HL_COIN_MAP: dict[str, str] = {"PEPE": "kPEPE"}
HL_REVERSE_MAP: dict[str, str] = {v: k for k, v in HL_COIN_MAP.items()}

# Binance futures uses "1000PEPEUSDT" (PEPEUSDT does not exist)
BINANCE_RAW_MAP: dict[str, str] = {"PEPE": "1000PEPEUSDT"}
BINANCE_CCXT_MAP: dict[str, str] = {"PEPE": "1000PEPE/USDT:USDT"}

# Price step for liquidation map aggregation
PRICE_STEPS: dict[str, float] = {
    "BTC": 200.0,
    "ETH": 20.0,
    "SOL": 2.0,
}
# Others use ~1% of current price (computed dynamically)


def hl_coin(name: str) -> str:
    """Canonical coin name → Hyperliquid API coin name."""
    return HL_COIN_MAP.get(name, name)


def canonical_coin(hl_name: str) -> str:
    """Hyperliquid API coin name → canonical coin name."""
    return HL_REVERSE_MAP.get(hl_name, hl_name)


def binance_raw_symbol(name: str) -> str:
    """Canonical coin name → Binance raw symbol (e.g. BTCUSDT)."""
    return BINANCE_RAW_MAP.get(name, f"{name}USDT")


def binance_ccxt_symbol(name: str) -> str:
    """Canonical coin name → ccxt symbol (e.g. BTC/USDT:USDT)."""
    return BINANCE_CCXT_MAP.get(name, f"{name}/USDT:USDT")


def price_step(coin: str, current_price: float) -> float:
    """Price bucket step for liquidation map aggregation."""
    if coin in PRICE_STEPS:
        return PRICE_STEPS[coin]
    # ~1% of current price, rounded to a nice number
    raw = current_price * 0.01
    if raw <= 0:
        return 0.0
    if raw >= 1:
        return round(raw)
    elif raw >= 0.1:
        return round(raw, 1)
    elif raw >= 0.01:
        return round(raw, 2)
    elif raw >= 0.001:
        return round(raw, 3)
    elif raw >= 0.0001:
        return round(raw, 4)
    elif raw >= 0.00001:
        return round(raw, 5)
    elif raw >= 0.000001:
        return round(raw, 6)
    elif raw >= 0.0000001:
        return round(raw, 7)
    else:
        return round(raw, 8) or raw


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LIQ_",
        extra="ignore",
    )

    # Database
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "liquidation"
    db_user: str = "postgres"
    db_password: str = ""

    # Hyperliquid
    hl_api_url: str = "https://api.hyperliquid.xyz"
    hl_ws_url: str = "wss://api.hyperliquid.xyz/ws"
    hl_leaderboard_url: str = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
    hl_coins: list[str] = COINS
    hl_min_position_usd: float = 10_000
    hl_max_addresses_per_snapshot: int = 500

    # Binance
    binance_fapi_url: str = "https://fapi.binance.com"
    binance_symbols: list[str] = [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
        "DOGE/USDT:USDT", "LINK/USDT:USDT", "AVAX/USDT:USDT",
        "SUI/USDT:USDT", "ARB/USDT:USDT", "WIF/USDT:USDT",
        "1000PEPE/USDT:USDT",
    ]

    # Telegram (new bot, separate from momentum)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # CoinGlass
    coinglass_api_key: str = ""


@lru_cache
def get_config() -> Config:
    return Config()
