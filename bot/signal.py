"""
SignalComputer — live market_flush signal (L3b-2, locked).

Each 4H cycle:
  1. For each of 10 coins, fetch the last ~100 4H bars of aggregated
     liquidations from CoinGlass. Persist to `coinglass_liquidations`.
  2. Compute `long_vol_zscore` with a 90-bar rolling mean/std — mirrors
     `scripts/backtest_liquidation_flush.py:116-119` byte-for-byte.
  3. Freshness gate: the most-recent bar must equal `floor_4h(now) - 4h`
     (the most-recent fully-closed 4H bar). Any earlier = stale = skip.
  4. market_flush fires on coin C when:
       long_vol_zscore[C] > z_threshold_self (1.0)  AND
       n_coins_flushing   >= min_coins_flushing (4)
     where n_coins_flushing counts coins with z > z_threshold_market (1.5),
     inclusive of self.
  5. Partial-fetch fail-stop: if ANY coin fetch fails or is stale, the
     whole cycle returns fetch_failed=True and scheduler skips entries.
     (Position exits in scheduler still run.)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
import numpy as np
import pandas as pd
from psycopg2.extras import execute_values

from bot.config import BotConfig
from collectors.db import get_conn

log = logging.getLogger(__name__)

CG_BASE = "https://open-api-v4.coinglass.com"
CG_AGG_LIQ_PATH = "/api/futures/liquidation/aggregated-history"

# For PEPE, CoinGlass may serve data under "PEPE" or "1000PEPE" depending
# on their ingestion; mirror backfill_coinglass.py's try-primary-then-fallback.
PEPE_ALIASES: list[str] = ["PEPE", "1000PEPE"]


def _floor_4h(dt: datetime) -> datetime:
    """Round DOWN to the most-recent 4H boundary (00,04,08,12,16,20 UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0, hour=(dt.hour // 4) * 4
    )


def _cg_symbols_for(coin: str) -> list[str]:
    """CoinGlass symbol candidates to try in order."""
    if coin == "PEPE":
        return list(PEPE_ALIASES)
    return [coin]


class SignalComputer:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg

    # ------------------------------------------------------------------
    # CoinGlass fetch
    # ------------------------------------------------------------------
    async def fetch_recent_liquidations(
        self,
        session: aiohttp.ClientSession,
        coin: str,
        n_bars: int = 100,
    ) -> pd.DataFrame | None:
        """
        Fetch last `n_bars` 4H liquidation buckets for `coin`. Returns a
        DataFrame indexed by UTC timestamp with columns `long_vol_usd`,
        `short_vol_usd`, or None on failure.

        Side effect: inserts rows into `coinglass_liquidations`
        (ON CONFLICT DO NOTHING).
        """
        if not self.cfg.coinglass_api_key:
            log.error("coinglass_api_key is empty; cannot fetch")
            return None

        url = f"{CG_BASE}{CG_AGG_LIQ_PATH}"
        headers = {"CG-API-KEY": self.cfg.coinglass_api_key}

        symbol_used: str | None = None
        records: list[dict] = []
        for sym in _cg_symbols_for(coin):
            params = {
                "symbol": sym,
                "interval": "h4",
                "exchange_list": self.cfg.coinglass_exchange_list,
            }
            try:
                async with session.get(
                    url, headers=headers, params=params,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json()
            except asyncio.TimeoutError:
                log.warning("CoinGlass timeout for %s (%s)", coin, sym)
                continue
            except Exception as e:
                log.warning("CoinGlass error for %s (%s): %s", coin, sym, e)
                continue

            if data.get("code") != "0" or not data.get("data"):
                log.debug("CoinGlass empty for %s (%s): %s",
                          coin, sym, data.get("msg"))
                continue

            records = data["data"][-n_bars:]
            symbol_used = sym
            break

        if not records or symbol_used is None:
            return None

        rows = []
        for r in records:
            t_raw = int(r["time"])
            t_sec = t_raw / 1000 if t_raw > 10**12 else t_raw
            ts = datetime.fromtimestamp(t_sec, tz=timezone.utc)
            long_vol = float(
                r.get("aggregated_long_liquidation_usd")
                or r.get("longVolUsd")
                or 0
            )
            short_vol = float(
                r.get("aggregated_short_liquidation_usd")
                or r.get("shortVolUsd")
                or 0
            )
            rows.append({
                "timestamp": ts,
                "long_vol_usd": long_vol,
                "short_vol_usd": short_vol,
            })

        # Persist for future analysis; ON CONFLICT DO NOTHING makes reruns safe.
        self._save_to_db(symbol_used, rows)

        df = pd.DataFrame(rows).set_index("timestamp").sort_index()
        return df

    def _save_to_db(self, symbol: str, rows: list[dict]) -> None:
        if not rows:
            return
        db_rows = [
            (r["timestamp"], symbol, r["long_vol_usd"], r["short_vol_usd"], 0, 0)
            for r in rows
        ]
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    execute_values(
                        cur,
                        """
                        INSERT INTO coinglass_liquidations
                            (timestamp, symbol, long_vol_usd, short_vol_usd,
                             long_count, short_count)
                        VALUES %s
                        ON CONFLICT (timestamp, symbol) DO NOTHING
                        """,
                        db_rows,
                        page_size=500,
                    )
        except Exception as e:
            # Never let a DB hiccup block signal evaluation.
            log.warning("coinglass_liquidations save failed (%s): %s", symbol, e)

    # ------------------------------------------------------------------
    # Z-score (mirrors scripts/backtest_liquidation_flush.py:116-119)
    # ------------------------------------------------------------------
    def compute_z_scores(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        rm = df["long_vol_usd"].rolling(self.cfg.z_lookback).mean()
        rs = df["long_vol_usd"].rolling(self.cfg.z_lookback).std()
        df["long_vol_zscore"] = (df["long_vol_usd"] - rm) / rs
        return df

    # ------------------------------------------------------------------
    # Main signal
    # ------------------------------------------------------------------
    async def check_market_flush(
        self, session: aiohttp.ClientSession
    ) -> dict:
        """
        Evaluate market_flush for the most-recent closed 4H bar.

        Returns:
          {
            "is_market_flush": bool,
            "fetch_failed": bool,     # True if any coin missing/stale
            "n_coins_flushing": int,
            "entry_coins": list[str], # coins passing self-threshold when flush
            "all_z_scores": dict[str, float],
            "timestamp": datetime,    # the bar evaluated (UTC)
          }
        """
        now = datetime.now(timezone.utc)
        expected_bar = _floor_4h(now) - timedelta(hours=4)

        all_z: dict[str, float] = {c: 0.0 for c in self.cfg.bot_coins}
        fetch_failed = False

        for coin in self.cfg.bot_coins:
            df = await self.fetch_recent_liquidations(session, coin)
            if df is None or df.empty:
                log.warning("fetch failed for %s", coin)
                fetch_failed = True
            else:
                latest_ts = df.index[-1]
                # Staleness gate: only reject if the newest bar is OLDER than
                # the most-recent fully-closed 4H bucket. CoinGlass may also
                # return the current in-progress bar (latest > expected) — that
                # is NOT stale; accept it and use its z-score as-is.
                if latest_ts.to_pydatetime() < expected_bar:
                    log.warning(
                        "stale data for %s: latest=%s expected=%s",
                        coin, latest_ts.isoformat(), expected_bar.isoformat(),
                    )
                    fetch_failed = True
                elif len(df) < self.cfg.z_lookback + 1:
                    log.warning(
                        "insufficient history for %s: %d bars (<%d required)",
                        coin, len(df), self.cfg.z_lookback + 1,
                    )
                    # Treat as NaN → 0 at iloc[-1], same effect as z not firing.
                    all_z[coin] = 0.0
                else:
                    z_series = self.compute_z_scores(df)["long_vol_zscore"]
                    z_last = z_series.iloc[-1]
                    all_z[coin] = (
                        0.0 if (z_last is None or np.isnan(z_last))
                        else float(z_last)
                    )
            await asyncio.sleep(self.cfg.coinglass_request_sleep_s)

        n_flushing = sum(
            1 for z in all_z.values() if z > self.cfg.z_threshold_market
        )
        is_flush = (
            not fetch_failed
            and n_flushing >= self.cfg.min_coins_flushing
        )

        entry_coins: list[str] = []
        if is_flush:
            entry_coins = [
                c for c, z in all_z.items()
                if z > self.cfg.z_threshold_self
            ]

        return {
            "is_market_flush": is_flush,
            "fetch_failed": fetch_failed,
            "n_coins_flushing": n_flushing,
            "entry_coins": entry_coins,
            "all_z_scores": all_z,
            "timestamp": expected_bar,
        }
