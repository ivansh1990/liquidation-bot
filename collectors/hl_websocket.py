"""
Hyperliquid WebSocket collector.

Long-running process that subscribes to real-time trades for all 10 coins.
Tracks:
  - Latest prices (for use by other collectors)
  - Large trades (>$50K) logged to stdout
  - Per-coin trade volume

Run as a persistent systemd service.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone

import websockets
import websockets.exceptions

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import COINS, get_config, hl_coin, canonical_coin

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

LARGE_TRADE_USD = 50_000


class HLWebSocket:
    def __init__(self):
        self.cfg = get_config()
        self.stop_event = asyncio.Event()
        self.latest_prices: dict[str, float] = {}
        self.volume_1m: dict[str, float] = {}  # rolling 1-minute volume
        self._reconnect_count = 0

    def _setup_signals(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_signal, sig)

    def _handle_signal(self, sig: signal.Signals) -> None:
        log.info("Received %s, shutting down...", sig.name)
        self.stop_event.set()

    async def run(self) -> None:
        self._setup_signals()
        log.info("Starting HL WebSocket collector for %d coins", len(COINS))

        backoff = 5
        while not self.stop_event.is_set():
            try:
                async with websockets.connect(
                    self.cfg.hl_ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    log.info("Connected to %s", self.cfg.hl_ws_url)
                    await self._subscribe_all(ws)
                    backoff = 5
                    self._reconnect_count = 0
                    await self._read_loop(ws)
            except websockets.exceptions.ConnectionClosed as e:
                self._reconnect_count += 1
                log.warning(
                    "WS connection closed (code=%s), reconnect #%d in %ds",
                    e.code, self._reconnect_count, backoff,
                )
            except Exception as e:
                self._reconnect_count += 1
                log.warning(
                    "WS error: %s, reconnect #%d in %ds",
                    e, self._reconnect_count, backoff,
                )

            if self._reconnect_count >= 3:
                log.error(
                    "3+ consecutive reconnects! Check network / HL API status."
                )
                # Alert would go here if alerts module is available
                try:
                    from collectors.alerts import send_alert_sync
                    send_alert_sync(
                        self.cfg,
                        f"⚠️ HL WebSocket: {self._reconnect_count} reconnects",
                    )
                except Exception:
                    pass

            if self.stop_event.is_set():
                break

            jitter = random.uniform(0, 2)
            await asyncio.sleep(backoff + jitter)
            backoff = min(backoff * 2, 60)

        log.info("WebSocket collector stopped.")

    async def _subscribe_all(self, ws) -> None:
        for coin in COINS:
            msg = {
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": hl_coin(coin)},
            }
            await ws.send(json.dumps(msg))
        log.info("Subscribed to trades for %d coins", len(COINS))

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            if self.stop_event.is_set():
                break
            try:
                data = json.loads(raw)
                self._process_message(data)
            except json.JSONDecodeError:
                continue

    def _process_message(self, data: dict) -> None:
        channel = data.get("channel")
        if channel != "trades":
            return

        trades = data.get("data", [])
        for trade in trades:
            hl_name = trade.get("coin", "")
            coin = canonical_coin(hl_name)

            try:
                px = float(trade.get("px", 0))
                sz = float(trade.get("sz", 0))
            except (ValueError, TypeError):
                continue

            notional = px * sz

            # Update latest price
            self.latest_prices[coin] = px

            # Accumulate volume
            self.volume_1m[coin] = self.volume_1m.get(coin, 0) + notional

            # Log large trades
            if notional >= LARGE_TRADE_USD:
                side = trade.get("side", "?")
                side_str = "BUY" if side == "B" else "SELL" if side == "A" else side
                log.info(
                    "LARGE TRADE: %s %s $%s @ %s (%.4f)",
                    coin, side_str, f"{notional:,.0f}", px, sz,
                )


async def main() -> None:
    ws = HLWebSocket()
    await ws.run()


if __name__ == "__main__":
    asyncio.run(main())
