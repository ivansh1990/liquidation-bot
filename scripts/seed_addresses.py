#!/usr/bin/env python3
"""
Seed hl_addresses with top traders from the Hyperliquid leaderboard.

Primary source: https://stats-data.hyperliquid.xyz/Mainnet/leaderboard
Fallback: hardcoded whale addresses.
"""

import logging
import sys
import os

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import get_config
from collectors.db import init_pool, get_conn, upsert_address, get_address_count

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Known whale addresses as fallback (from Hyperliquid leaderboard / explorer)
HARDCODED_WHALES: list[dict] = [
    {"address": "0x162cc7c861ebd0c06b3d72319201150482518185", "volume": 692_000_000_000},
    {"address": "0xdaf2e3f42c571893f0de6a26c7685b0e04d85d9d", "volume": 100_000_000_000},
    {"address": "0x4e5b2e1dc63f6b91cb6cd759936495434c7e972f", "volume": 80_000_000_000},
]

MIN_ACCOUNT_VALUE = 100_000  # $100K minimum to qualify as "whale"


def fetch_leaderboard(url: str) -> list[dict]:
    """Fetch the full leaderboard from stats-data.hyperliquid.xyz."""
    log.info("Fetching leaderboard from %s ...", url)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    rows = data.get("leaderboardRows", data if isinstance(data, list) else [])
    log.info("Leaderboard returned %d entries", len(rows))
    return rows


def extract_all_time_volume(row: dict) -> float:
    """Extract allTime trading volume from leaderboard entry."""
    for window_name, perf in row.get("windowPerformances", []):
        if window_name == "allTime":
            return abs(float(perf.get("vlm", 0)))
    return 0.0


def seed_from_leaderboard(cfg, top_n: int = 500) -> int:
    """Seed addresses from leaderboard. Returns count inserted."""
    rows = fetch_leaderboard(cfg.hl_leaderboard_url)

    # Filter by minimum account value and sort
    qualified = []
    for row in rows:
        try:
            account_value = float(row.get("accountValue", 0))
        except (ValueError, TypeError):
            continue
        if account_value < MIN_ACCOUNT_VALUE:
            continue
        qualified.append({
            "address": row["ethAddress"],
            "account_value": account_value,
            "volume": extract_all_time_volume(row),
        })

    qualified.sort(key=lambda x: x["account_value"], reverse=True)
    qualified = qualified[:top_n]

    log.info(
        "Filtered to %d addresses (account_value >= $%s, top %d)",
        len(qualified), f"{MIN_ACCOUNT_VALUE:,.0f}", top_n,
    )

    if qualified:
        log.info(
            "Top account: %s ($%s)",
            qualified[0]["address"][:16] + "...",
            f"{qualified[0]['account_value']:,.0f}",
        )

    inserted = 0
    with get_conn() as conn:
        for entry in qualified:
            upsert_address(
                conn,
                address=entry["address"],
                total_volume_usd=entry["volume"],
            )
            inserted += 1

    return inserted


def seed_hardcoded() -> int:
    """Insert hardcoded whale addresses as fallback."""
    log.info("Using hardcoded whale addresses as fallback")
    inserted = 0
    with get_conn() as conn:
        for whale in HARDCODED_WHALES:
            upsert_address(
                conn,
                address=whale["address"],
                total_volume_usd=whale["volume"],
            )
            inserted += 1
    return inserted


def main() -> None:
    cfg = get_config()
    init_pool(cfg)

    # Try leaderboard first
    inserted = 0
    try:
        inserted = seed_from_leaderboard(cfg, top_n=cfg.hl_max_addresses_per_snapshot)
    except Exception as e:
        log.error("Leaderboard fetch failed: %s", e)

    # Fallback if not enough addresses
    if inserted < 50:
        log.warning("Only %d addresses from leaderboard, adding hardcoded whales", inserted)
        inserted += seed_hardcoded()

    with get_conn() as conn:
        total = get_address_count(conn)

    log.info("Seeding complete: %d inserted this run, %d total in DB", inserted, total)


if __name__ == "__main__":
    main()
