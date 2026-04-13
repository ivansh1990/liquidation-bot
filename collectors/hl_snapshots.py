"""
Hyperliquid position snapshots collector.

Fetches clearinghouseState for tracked addresses, records positions,
and builds an aggregated liquidation map.

Run every 15 minutes via systemd timer.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import sys
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp
import certifi

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import (
    COINS,
    Config,
    canonical_coin,
    get_config,
    hl_coin,
    price_step,
)
from collectors.db import (
    get_conn,
    get_top_addresses,
    init_pool,
    insert_liquidation_map_batch,
    insert_positions_batch,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Set of HL coin names we care about (for filtering)
HL_COINS_SET = {hl_coin(c) for c in COINS}


# ---------------------------------------------------------------------------
# Rate limiter (token-bucket style)
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple async rate limiter: at most `max_per_min` requests per minute."""

    def __init__(self, max_per_min: int = 1000):
        self._interval = 60.0 / max_per_min
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_event_loop().time()


# ---------------------------------------------------------------------------
# Hyperliquid API helpers
# ---------------------------------------------------------------------------

async def fetch_all_mids(
    session: aiohttp.ClientSession, cfg: Config
) -> dict[str, float]:
    """Fetch current mid prices for all coins."""
    async with session.post(
        f"{cfg.hl_api_url}/info",
        json={"type": "allMids"},
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return {k: float(v) for k, v in data.items()}


async def fetch_clearinghouse_state(
    session: aiohttp.ClientSession,
    cfg: Config,
    address: str,
    sem: asyncio.Semaphore,
    limiter: RateLimiter,
) -> dict | None:
    """Fetch clearinghouseState for a single address."""
    await limiter.acquire()
    async with sem:
        try:
            async with session.post(
                f"{cfg.hl_api_url}/info",
                json={"type": "clearinghouseState", "user": address},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 429:
                    log.warning("Rate limited, pausing 5s")
                    await asyncio.sleep(5)
                    return None
                resp.raise_for_status()
                return await resp.json()
        except Exception as e:
            log.warning("Failed to fetch %s: %s", address[:12], e)
            return None


# ---------------------------------------------------------------------------
# Position parsing
# ---------------------------------------------------------------------------

def parse_positions(
    state: dict,
    address: str,
    snapshot_time: datetime,
    min_usd: float,
) -> list[dict]:
    """Extract positions from clearinghouseState response."""
    rows = []
    for ap in state.get("assetPositions", []):
        pos = ap.get("position", {})
        hl_name = pos.get("coin", "")

        # Only track our 10 coins
        if hl_name not in HL_COINS_SET:
            continue

        coin = canonical_coin(hl_name)

        try:
            szi = float(pos.get("szi", 0))
            entry_px = float(pos.get("entryPx", 0))
            position_value = abs(float(pos.get("positionValue", 0)))
        except (ValueError, TypeError):
            continue

        if position_value < min_usd:
            continue

        side = "long" if szi > 0 else "short"

        # Liquidation price
        liq_px_raw = pos.get("liquidationPx")
        is_estimated = False
        liq_px = None

        if liq_px_raw is not None:
            try:
                liq_px = float(liq_px_raw)
                if liq_px <= 0:
                    liq_px = None
            except (ValueError, TypeError):
                liq_px = None

        # Estimate if missing
        if liq_px is None:
            lev_data = pos.get("leverage", {})
            lev_val = lev_data.get("value") if isinstance(lev_data, dict) else lev_data
            if lev_val is not None and entry_px > 0:
                try:
                    lev = float(lev_val)
                    if lev > 0:
                        if side == "long":
                            liq_px = entry_px * (1 - 1 / lev)
                        else:
                            liq_px = entry_px * (1 + 1 / lev)
                        is_estimated = True
                except (ValueError, TypeError):
                    pass

        # Leverage
        leverage = None
        lev_data = pos.get("leverage", {})
        lev_val = lev_data.get("value") if isinstance(lev_data, dict) else lev_data
        if lev_val is not None:
            try:
                leverage = float(lev_val)
            except (ValueError, TypeError):
                pass

        # Other fields
        try:
            unrealized_pnl = float(pos.get("unrealizedPnl", 0))
        except (ValueError, TypeError):
            unrealized_pnl = 0
        try:
            margin_used = float(pos.get("marginUsed", 0))
        except (ValueError, TypeError):
            margin_used = 0

        rows.append({
            "snapshot_time": snapshot_time,
            "address": address,
            "coin": coin,
            "side": side,
            "size_usd": position_value,
            "entry_px": entry_px,
            "liquidation_px": liq_px,
            "is_liq_estimated": is_estimated,
            "leverage": leverage,
            "unrealized_pnl": unrealized_pnl,
            "margin_used": margin_used,
        })

    return rows


# ---------------------------------------------------------------------------
# Liquidation map aggregation
# ---------------------------------------------------------------------------

def build_liquidation_map(
    positions: list[dict],
    mids: dict[str, float],
    snapshot_time: datetime,
) -> list[dict]:
    """
    Aggregate positions into price-level buckets for the liquidation map.

    For each position with a liquidation price:
    - Round liq_px to the nearest price_step bucket
    - Accumulate position value into the bucket
    """
    # Group by coin
    by_coin: dict[str, list[dict]] = defaultdict(list)
    for p in positions:
        if p["liquidation_px"] is not None:
            by_coin[p["coin"]].append(p)

    map_rows = []

    for coin, coin_positions in by_coin.items():
        hl_name = hl_coin(coin)
        current_price = mids.get(hl_name)
        if current_price is None or current_price <= 0:
            continue

        step = price_step(coin, current_price)
        if step <= 0:
            continue

        # Accumulate into buckets: bucket_price -> {long_usd, short_usd, long_count, short_count}
        buckets: dict[float, dict] = defaultdict(
            lambda: {"long_usd": 0.0, "short_usd": 0.0, "long_count": 0, "short_count": 0}
        )

        for p in coin_positions:
            liq_px = p["liquidation_px"]
            # Round to nearest bucket
            bucket = round(liq_px / step) * step

            if p["side"] == "long":
                buckets[bucket]["long_usd"] += p["size_usd"]
                buckets[bucket]["long_count"] += 1
            else:
                buckets[bucket]["short_usd"] += p["size_usd"]
                buckets[bucket]["short_count"] += 1

        for level, data in buckets.items():
            map_rows.append({
                "snapshot_time": snapshot_time,
                "coin": coin,
                "price_level": level,
                "long_liq_usd": data["long_usd"],
                "short_liq_usd": data["short_usd"],
                "num_long_positions": data["long_count"],
                "num_short_positions": data["short_count"],
                "current_price": current_price,
            })

    return map_rows


# ---------------------------------------------------------------------------
# Main collection cycle
# ---------------------------------------------------------------------------

async def run_snapshot(cfg: Config) -> None:
    snapshot_time = datetime.now(timezone.utc)
    log.info("Starting snapshot at %s", snapshot_time.isoformat())

    # 1. Load addresses
    with get_conn() as conn:
        addresses = get_top_addresses(conn, limit=cfg.hl_max_addresses_per_snapshot)
    log.info("Loaded %d addresses to query", len(addresses))

    if not addresses:
        log.warning("No addresses to query. Run seed_addresses.py first.")
        return

    sem = asyncio.Semaphore(20)
    limiter = RateLimiter(max_per_min=1000)

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as session:
        # 2. Fetch current mid prices
        mids = await fetch_all_mids(session, cfg)
        log.info("Fetched %d mid prices", len(mids))

        # 3. Fetch all clearinghouse states concurrently
        tasks = [
            fetch_clearinghouse_state(session, cfg, addr, sem, limiter)
            for addr in addresses
        ]
        results = await asyncio.gather(*tasks)

    # 4. Parse positions
    all_positions = []
    queried = 0
    for addr, state in zip(addresses, results):
        if state is None:
            continue
        queried += 1
        positions = parse_positions(state, addr, snapshot_time, cfg.hl_min_position_usd)
        all_positions.extend(positions)

    log.info(
        "Queried %d/%d addresses, found %d positions",
        queried, len(addresses), len(all_positions),
    )

    # 5. Insert positions
    with get_conn() as conn:
        n_pos = insert_positions_batch(conn, all_positions)

    # 6. Build and insert liquidation map
    map_rows = build_liquidation_map(all_positions, mids, snapshot_time)
    with get_conn() as conn:
        n_map = insert_liquidation_map_batch(conn, map_rows)

    # Count levels per coin
    coins_levels = defaultdict(int)
    for r in map_rows:
        coins_levels[r["coin"]] += 1

    levels_str = ", ".join(f"{c}: {n}" for c, n in sorted(coins_levels.items()))
    log.info(
        "Snapshot done: %d positions saved, %d map levels (%s)",
        n_pos, n_map, levels_str,
    )


async def main() -> None:
    cfg = get_config()
    init_pool(cfg)

    t0 = time.time()
    await run_snapshot(cfg)
    elapsed = time.time() - t0
    log.info("Total time: %.1fs", elapsed)


if __name__ == "__main__":
    asyncio.run(main())
