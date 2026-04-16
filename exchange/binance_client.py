"""
Authenticated ccxt wrapper for Binance USDM Futures.

Every public method:
  1. Logs entry with parameters.
  2. In dry_run mode: returns a synthetic dict, never calls authenticated endpoints.
  3. Uses amount_to_precision / price_to_precision before every order.
  4. Logs result on success, logs + re-raises on error.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import ccxt

from collectors.config import binance_ccxt_symbol
from exchange.config import ExchangeConfig

log = logging.getLogger(__name__)


class BinanceClient:
    def __init__(self, cfg: ExchangeConfig):
        self.cfg = cfg
        self.dry_run = cfg.dry_run
        self._exchange: ccxt.binance | None = None
        self._configured_symbols: set[str] = set()

    def _get_exchange(self) -> ccxt.binance:
        if self._exchange is None:
            self._exchange = ccxt.binance({
                "apiKey": self.cfg.binance_api_key,
                "secret": self.cfg.binance_api_secret,
                "options": {"defaultType": "swap"},
                "enableRateLimit": True,
            })
            if self.cfg.binance_testnet:
                self._exchange.set_sandbox_mode(True)
                log.info("Binance sandbox (testnet) mode enabled")
        return self._exchange

    def _symbol(self, coin: str) -> str:
        return binance_ccxt_symbol(coin)

    def _dry_id(self) -> str:
        return f"DRY-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def set_leverage(self, coin: str) -> None:
        """Set isolated margin mode and leverage. Idempotent; cached per run."""
        symbol = self._symbol(coin)
        if symbol in self._configured_symbols:
            return
        log.info("set_leverage %s lev=%d mode=isolated", coin, self.cfg.showcase_leverage)
        if self.dry_run:
            self._configured_symbols.add(symbol)
            return
        ex = self._get_exchange()
        try:
            ex.set_margin_mode("isolated", symbol)
        except ccxt.BaseError as e:
            # "No need to change margin type" or similar — already isolated
            if "margin type" not in str(e).lower() and "no need" not in str(e).lower():
                raise
            log.debug("margin mode already isolated for %s: %s", coin, e)
        try:
            ex.set_leverage(self.cfg.showcase_leverage, symbol)
        except ccxt.BaseError as e:
            if "no need" not in str(e).lower():
                raise
            log.debug("leverage already set for %s: %s", coin, e)
        self._configured_symbols.add(symbol)

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------
    def get_ticker_price(self, coin: str) -> float:
        """Fetch last price via public ticker (works in dry_run)."""
        symbol = self._symbol(coin)
        ex = self._get_exchange()
        ticker = ex.fetch_ticker(symbol)
        last = ticker.get("last") or ticker.get("close")
        if last is None:
            raise RuntimeError(f"ticker missing last/close for {symbol}")
        price = float(last)
        log.debug("ticker %s = %.8f", coin, price)
        return price

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def open_market_long(self, coin: str, margin_usd: float) -> dict:
        """
        Market buy LONG. Returns dict with keys:
        id, average, filled, timestamp, status.
        """
        symbol = self._symbol(coin)
        price = self.get_ticker_price(coin)
        notional = margin_usd * self.cfg.showcase_leverage
        raw_amount = notional / price

        log.info(
            "open_market_long %s margin=$%.2f notional=$%.2f raw_qty=%.8f",
            coin, margin_usd, notional, raw_amount,
        )

        if self.dry_run:
            ex = self._get_exchange()
            try:
                amount = float(ex.amount_to_precision(symbol, raw_amount))
            except Exception:
                amount = raw_amount
            result = {
                "id": self._dry_id(),
                "average": price,
                "filled": amount,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "closed",
            }
            log.info("[DRY_RUN] open_market_long %s → %s", coin, result)
            return result

        ex = self._get_exchange()
        amount = float(ex.amount_to_precision(symbol, raw_amount))
        if amount <= 0:
            raise ValueError(f"amount rounded to 0 for {symbol}, raw={raw_amount}")

        order = ex.create_order(symbol, "market", "buy", amount)
        result = {
            "id": str(order["id"]),
            "average": float(order.get("average") or order.get("price") or price),
            "filled": float(order.get("filled") or amount),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": order.get("status", "closed"),
        }
        log.info("open_market_long %s filled → %s", coin, result)
        return result

    def place_tp_order(self, coin: str, amount: float, tp_price: float) -> dict:
        """TAKE_PROFIT_MARKET sell order, reduceOnly."""
        symbol = self._symbol(coin)
        ex = self._get_exchange()
        tp_price_precise = float(ex.price_to_precision(symbol, tp_price))
        amount_precise = float(ex.amount_to_precision(symbol, amount))

        log.info(
            "place_tp_order %s qty=%.8f tp=%.8f",
            coin, amount_precise, tp_price_precise,
        )

        if self.dry_run:
            result = {
                "id": self._dry_id(),
                "status": "open",
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": tp_price_precise,
            }
            log.info("[DRY_RUN] place_tp_order %s → %s", coin, result)
            return result

        order = ex.create_order(
            symbol, "TAKE_PROFIT_MARKET", "sell", amount_precise,
            params={
                "stopPrice": tp_price_precise,
                "reduceOnly": True,
                "workingType": "MARK_PRICE",
            },
        )
        result = {
            "id": str(order["id"]),
            "status": order.get("status", "open"),
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": tp_price_precise,
        }
        log.info("place_tp_order %s → %s", coin, result)
        return result

    def place_sl_order(self, coin: str, amount: float, sl_price: float) -> dict:
        """STOP_MARKET sell order, reduceOnly."""
        symbol = self._symbol(coin)
        ex = self._get_exchange()
        sl_price_precise = float(ex.price_to_precision(symbol, sl_price))
        amount_precise = float(ex.amount_to_precision(symbol, amount))

        log.info(
            "place_sl_order %s qty=%.8f sl=%.8f",
            coin, amount_precise, sl_price_precise,
        )

        if self.dry_run:
            result = {
                "id": self._dry_id(),
                "status": "open",
                "type": "STOP_MARKET",
                "stopPrice": sl_price_precise,
            }
            log.info("[DRY_RUN] place_sl_order %s → %s", coin, result)
            return result

        order = ex.create_order(
            symbol, "STOP_MARKET", "sell", amount_precise,
            params={
                "stopPrice": sl_price_precise,
                "reduceOnly": True,
                "workingType": "MARK_PRICE",
            },
        )
        result = {
            "id": str(order["id"]),
            "status": order.get("status", "open"),
            "type": "STOP_MARKET",
            "stopPrice": sl_price_precise,
        }
        log.info("place_sl_order %s → %s", coin, result)
        return result

    def close_market(self, coin: str, amount: float) -> dict:
        """Emergency market sell, reduceOnly."""
        symbol = self._symbol(coin)
        ex = self._get_exchange()
        amount_precise = float(ex.amount_to_precision(symbol, amount))

        log.info("close_market %s qty=%.8f", coin, amount_precise)

        if self.dry_run:
            price = self.get_ticker_price(coin)
            result = {
                "id": self._dry_id(),
                "average": price,
                "filled": amount_precise,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "closed",
            }
            log.info("[DRY_RUN] close_market %s → %s", coin, result)
            return result

        order = ex.create_order(
            symbol, "market", "sell", amount_precise,
            params={"reduceOnly": True},
        )
        result = {
            "id": str(order["id"]),
            "average": float(order.get("average") or order.get("price") or 0),
            "filled": float(order.get("filled") or amount_precise),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": order.get("status", "closed"),
        }
        log.info("close_market %s → %s", coin, result)
        return result

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------
    def cancel_order(self, order_id: str, coin: str) -> dict | None:
        """Cancel by ID. Returns None if already filled/cancelled."""
        symbol = self._symbol(coin)
        log.info("cancel_order %s order=%s", coin, order_id)

        if self.dry_run:
            log.info("[DRY_RUN] cancel_order %s → ok", coin)
            return {"id": order_id, "status": "canceled"}

        ex = self._get_exchange()
        try:
            result = ex.cancel_order(order_id, symbol)
            log.info("cancel_order %s → %s", coin, result.get("status"))
            return result
        except ccxt.OrderNotFound:
            log.debug("cancel_order %s: order %s not found (already filled/cancelled)", coin, order_id)
            return None
        except ccxt.BaseError as e:
            if "unknown order" in str(e).lower() or "order not found" in str(e).lower():
                log.debug("cancel_order %s: %s", coin, e)
                return None
            raise

    def fetch_order(self, order_id: str, coin: str) -> dict | None:
        """Fetch order details. Returns ccxt order dict or None on error."""
        symbol = self._symbol(coin)
        log.debug("fetch_order %s order=%s", coin, order_id)

        if self.dry_run:
            return {
                "id": order_id,
                "status": "open",
                "average": None,
                "filled": 0,
                "timestamp": None,
            }

        ex = self._get_exchange()
        try:
            order = ex.fetch_order(order_id, symbol)
            return order
        except ccxt.OrderNotFound:
            log.warning("fetch_order %s: order %s not found", coin, order_id)
            return None
        except ccxt.BaseError as e:
            log.warning("fetch_order %s failed: %s", coin, e)
            return None

    # ------------------------------------------------------------------
    # Account state
    # ------------------------------------------------------------------
    def fetch_positions(self) -> list[dict]:
        """Fetch all open positions, filter to non-zero."""
        log.debug("fetch_positions")

        if self.dry_run:
            log.debug("[DRY_RUN] fetch_positions → []")
            return []

        ex = self._get_exchange()
        positions = ex.fetch_positions()
        result = []
        for p in positions:
            contracts = float(p.get("contracts") or 0)
            if contracts > 0:
                result.append({
                    "symbol": p.get("symbol", ""),
                    "side": p.get("side", "long"),
                    "contracts": contracts,
                    "entryPrice": float(p.get("entryPrice") or 0),
                    "unrealizedPnl": float(p.get("unrealizedPnl") or 0),
                    "markPrice": float(p.get("markPrice") or 0),
                    "liquidationPrice": float(p.get("liquidationPrice") or 0),
                    "collateral": float(p.get("collateral") or 0),
                })
        log.debug("fetch_positions → %d open", len(result))
        return result

    def fetch_balance(self) -> dict:
        """Fetch USDT balance."""
        log.debug("fetch_balance")

        if self.dry_run:
            result = {"free": self.cfg.showcase_capital, "total": self.cfg.showcase_capital}
            log.debug("[DRY_RUN] fetch_balance → %s", result)
            return result

        ex = self._get_exchange()
        balance = ex.fetch_balance()
        usdt = balance.get("USDT", {})
        result = {
            "free": float(usdt.get("free") or 0),
            "total": float(usdt.get("total") or 0),
        }
        log.debug("fetch_balance → %s", result)
        return result
