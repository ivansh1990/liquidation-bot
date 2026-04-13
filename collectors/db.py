from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

from collectors.config import Config, get_config

log = logging.getLogger(__name__)

_pool: psycopg2.pool.SimpleConnectionPool | None = None

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- Адреса трейдеров Hyperliquid (обнаруженные через leaderboard + seed)
CREATE TABLE IF NOT EXISTS hl_addresses (
    address TEXT PRIMARY KEY,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_volume_usd DOUBLE PRECISION DEFAULT 0,
    trade_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_hl_addr_vol ON hl_addresses(total_volume_usd DESC);

-- Снимки позиций (каждые 15 минут, top-N адресов)
CREATE TABLE IF NOT EXISTS hl_position_snapshots (
    id BIGSERIAL PRIMARY KEY,
    snapshot_time TIMESTAMPTZ NOT NULL,
    address TEXT NOT NULL,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,
    size_usd DOUBLE PRECISION,
    entry_px DOUBLE PRECISION,
    liquidation_px DOUBLE PRECISION,
    is_liq_estimated BOOLEAN NOT NULL DEFAULT FALSE,
    leverage DOUBLE PRECISION,
    unrealized_pnl DOUBLE PRECISION,
    margin_used DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_snap_time ON hl_position_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_snap_coin ON hl_position_snapshots(coin, snapshot_time);

-- Агрегированная liquidation map (каждые 15 мин)
CREATE TABLE IF NOT EXISTS hl_liquidation_map (
    id BIGSERIAL PRIMARY KEY,
    snapshot_time TIMESTAMPTZ NOT NULL,
    coin TEXT NOT NULL,
    price_level DOUBLE PRECISION NOT NULL,
    long_liq_usd DOUBLE PRECISION DEFAULT 0,
    short_liq_usd DOUBLE PRECISION DEFAULT 0,
    num_long_positions INTEGER DEFAULT 0,
    num_short_positions INTEGER DEFAULT 0,
    current_price DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_liqmap_coin_time ON hl_liquidation_map(coin, snapshot_time);

-- Binance Open Interest
CREATE TABLE IF NOT EXISTS binance_oi (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    open_interest DOUBLE PRECISION,
    open_interest_usd DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_boi_sym_ts ON binance_oi(symbol, timestamp);

-- Binance Funding Rate
CREATE TABLE IF NOT EXISTS binance_funding (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    funding_rate DOUBLE PRECISION,
    mark_price DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_bfr_sym_ts ON binance_funding(symbol, timestamp);

-- Binance Long/Short Ratio (top traders)
CREATE TABLE IF NOT EXISTS binance_ls_ratio (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    long_account_pct DOUBLE PRECISION,
    short_account_pct DOUBLE PRECISION,
    long_short_ratio DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_bls_sym_ts ON binance_ls_ratio(symbol, timestamp);

-- Binance Taker Buy/Sell Volume
CREATE TABLE IF NOT EXISTS binance_taker (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    buy_vol DOUBLE PRECISION,
    sell_vol DOUBLE PRECISION,
    buy_sell_ratio DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_btk_sym_ts ON binance_taker(symbol, timestamp);
"""

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------


def _dsn(cfg: Config, dbname: str | None = None) -> str:
    return (
        f"host={cfg.db_host} port={cfg.db_port} "
        f"dbname={dbname or cfg.db_name} "
        f"user={cfg.db_user} password={cfg.db_password}"
    )


def init_pool(cfg: Config | None = None) -> None:
    global _pool
    if _pool is not None:
        return
    cfg = cfg or get_config()
    _pool = psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=5, dsn=_dsn(cfg))
    log.info("DB pool initialized (db=%s)", cfg.db_name)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextmanager
def get_conn():
    """Yield a connection from the pool. Auto-commit on success, rollback on error."""
    if _pool is None:
        init_pool()
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


# ---------------------------------------------------------------------------
# Database / schema creation
# ---------------------------------------------------------------------------


def create_database(cfg: Config | None = None) -> None:
    """Create the 'liquidation' database if it doesn't exist."""
    cfg = cfg or get_config()
    conn = psycopg2.connect(_dsn(cfg, dbname="postgres"))
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (cfg.db_name,)
    )
    if cur.fetchone() is None:
        cur.execute(f'CREATE DATABASE "{cfg.db_name}"')
        log.info("Created database %s", cfg.db_name)
    else:
        log.info("Database %s already exists", cfg.db_name)
    cur.close()
    conn.close()


def create_tables(cfg: Config | None = None) -> None:
    """Create all tables and indexes."""
    cfg = cfg or get_config()
    conn = psycopg2.connect(_dsn(cfg))
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(SCHEMA_SQL)
    cur.close()
    conn.close()
    log.info("All tables created/verified")


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------


def upsert_address(
    conn,
    address: str,
    total_volume_usd: float = 0,
    trade_count: int = 0,
) -> None:
    conn.cursor().execute(
        """
        INSERT INTO hl_addresses (address, total_volume_usd, trade_count)
        VALUES (%s, %s, %s)
        ON CONFLICT (address) DO UPDATE SET
            last_seen = NOW(),
            total_volume_usd = GREATEST(hl_addresses.total_volume_usd, EXCLUDED.total_volume_usd),
            trade_count = GREATEST(hl_addresses.trade_count, EXCLUDED.trade_count)
        """,
        (address, total_volume_usd, trade_count),
    )


def insert_positions_batch(
    conn, rows: list[dict[str, Any]]
) -> int:
    """Batch insert position snapshots. Returns number of rows inserted."""
    if not rows:
        return 0
    cur = conn.cursor()
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO hl_position_snapshots
            (snapshot_time, address, coin, side, size_usd, entry_px,
             liquidation_px, is_liq_estimated, leverage, unrealized_pnl, margin_used)
        VALUES %s
        """,
        [
            (
                r["snapshot_time"], r["address"], r["coin"], r["side"],
                r["size_usd"], r["entry_px"], r["liquidation_px"],
                r["is_liq_estimated"], r["leverage"],
                r["unrealized_pnl"], r["margin_used"],
            )
            for r in rows
        ],
        page_size=500,
    )
    return len(rows)


def insert_liquidation_map_batch(
    conn, rows: list[dict[str, Any]]
) -> int:
    if not rows:
        return 0
    cur = conn.cursor()
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO hl_liquidation_map
            (snapshot_time, coin, price_level, long_liq_usd, short_liq_usd,
             num_long_positions, num_short_positions, current_price)
        VALUES %s
        """,
        [
            (
                r["snapshot_time"], r["coin"], r["price_level"],
                r["long_liq_usd"], r["short_liq_usd"],
                r["num_long_positions"], r["num_short_positions"],
                r["current_price"],
            )
            for r in rows
        ],
        page_size=500,
    )
    return len(rows)


def insert_binance_oi(conn, symbol: str, ts, oi: float, oi_usd: float) -> None:
    conn.cursor().execute(
        """
        INSERT INTO binance_oi (timestamp, symbol, open_interest, open_interest_usd)
        VALUES (%s, %s, %s, %s)
        """,
        (ts, symbol, oi, oi_usd),
    )


def insert_binance_funding(
    conn, symbol: str, ts, funding_rate: float, mark_price: float
) -> None:
    conn.cursor().execute(
        """
        INSERT INTO binance_funding (timestamp, symbol, funding_rate, mark_price)
        VALUES (%s, %s, %s, %s)
        """,
        (ts, symbol, funding_rate, mark_price),
    )


def insert_binance_ls_ratio(
    conn,
    symbol: str,
    ts,
    long_pct: float,
    short_pct: float,
    ratio: float,
) -> None:
    conn.cursor().execute(
        """
        INSERT INTO binance_ls_ratio
            (timestamp, symbol, long_account_pct, short_account_pct, long_short_ratio)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (ts, symbol, long_pct, short_pct, ratio),
    )


def insert_binance_taker(
    conn,
    symbol: str,
    ts,
    buy_vol: float,
    sell_vol: float,
    ratio: float,
) -> None:
    conn.cursor().execute(
        """
        INSERT INTO binance_taker
            (timestamp, symbol, buy_vol, sell_vol, buy_sell_ratio)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (ts, symbol, buy_vol, sell_vol, ratio),
    )


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_top_addresses(conn, limit: int = 500) -> list[str]:
    """Return top addresses by volume."""
    cur = conn.cursor()
    cur.execute(
        "SELECT address FROM hl_addresses ORDER BY total_volume_usd DESC LIMIT %s",
        (limit,),
    )
    return [row[0] for row in cur.fetchall()]


def get_address_count(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM hl_addresses")
    return cur.fetchone()[0]
