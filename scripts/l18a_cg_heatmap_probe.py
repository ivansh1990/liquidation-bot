#!/usr/bin/env python3
"""
L18a Track 2 Step 1 — CoinGlass liquidation heatmap endpoint probe.

Validates that /api/futures/liquidation/heatmap is callable on our current
CoinGlass tier and documents the response schema so we can design a
collector. Tries both /heatmap and /heatmap/model2 and a small grid of
known CG params (range, interval, exchange_list).

Does NOT write to DB. Read-only HTTP probe.

Usage:
    .venv/bin/python scripts/l18a_cg_heatmap_probe.py [BTC]
    .venv/bin/python scripts/l18a_cg_heatmap_probe.py BTC 2>&1 | tee analysis/cg_heatmap_probe.txt

Requires LIQ_COINGLASS_API_KEY in .env.
Exit 0 on any successful probe, 1 if every combo fails.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
from typing import Any

import aiohttp
import certifi

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import get_config

try:
    from scripts.backfill_coinglass_oi import CG_BASE, CG_EXCHANGES
except ImportError:
    # Fallback if the backfill module's import shape drifts.
    CG_BASE = "https://open-api-v4.coinglass.com"
    CG_EXCHANGES = "Binance,OKX,Bybit,Bitget,Gate,BinanceFutures"


# CoinGlass docs describe two heatmap endpoints. Try both.
HEATMAP_PATHS = (
    "/api/futures/liquidation/heatmap",
    "/api/futures/liquidation/heatmap/model2",
)

# Plausible param combos. CG heatmap endpoints historically accept `range`
# (lookback window) rather than `interval`. We try both to be safe.
PARAM_COMBOS: tuple[dict[str, str], ...] = (
    {"range": "12h"},
    {"range": "24h"},
    {"range": "3d"},
    {"range": "7d"},
    {"interval": "h1"},
    {"interval": "h4"},
    {},  # no extra param — some endpoints default sensibly
)


def _preview(obj: Any, depth: int = 0, max_items: int = 3) -> str:
    """Compact, depth-limited JSON preview for first-look schema inspection."""
    if depth > 4:
        return "…"
    if isinstance(obj, dict):
        keys = list(obj.keys())
        shown = keys[:10]
        inner = {k: _preview(obj[k], depth + 1, max_items) for k in shown}
        extra = f" (+{len(keys) - len(shown)} more keys)" if len(keys) > len(shown) else ""
        return "{" + ", ".join(f'"{k}": {v}' for k, v in inner.items()) + "}" + extra
    if isinstance(obj, list):
        if not obj:
            return "[]"
        sample = [_preview(x, depth + 1, max_items) for x in obj[:max_items]]
        extra = f" (+{len(obj) - len(sample)} more items)" if len(obj) > len(sample) else ""
        return "[" + ", ".join(sample) + "]" + extra
    if isinstance(obj, str):
        return f'"{obj[:80]}"' + ("…" if len(obj) > 80 else "")
    return str(obj)


async def probe_one(
    session: aiohttp.ClientSession,
    api_key: str,
    path: str,
    symbol: str,
    extra_params: dict[str, str],
) -> tuple[bool, int | None, dict, dict | list | None]:
    """Single probe attempt. Returns (success, http_status, resp_headers, body)."""
    url = f"{CG_BASE}{path}"
    params: dict[str, str] = {
        "symbol": symbol,
        "exchange_list": CG_EXCHANGES,
        **extra_params,
    }
    headers = {"CG-API-KEY": api_key}

    try:
        async with session.get(
            url, headers=headers, params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            status = resp.status
            resp_headers = dict(resp.headers)
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = {"_raw_text": (await resp.text())[:500]}
    except asyncio.TimeoutError:
        return False, None, {}, {"_error": "timeout"}
    except Exception as exc:
        return False, None, {}, {"_error": f"{type(exc).__name__}: {exc}"}

    # CoinGlass convention: {"code": "0", "msg": "success", "data": [...]}
    ok = status == 200 and isinstance(body, dict) and str(body.get("code", "")) == "0"
    return ok, status, resp_headers, body


def _interesting_headers(h: dict) -> dict:
    """Pull rate-limit / identity headers worth keeping in the log."""
    keys = [
        "CG-APIKey-Tier",
        "CG-Rate-Limit",
        "CG-Rate-Limit-Used",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
        "RateLimit-Limit",
        "RateLimit-Remaining",
        "Retry-After",
        "Content-Type",
    ]
    return {k: h[k] for k in keys if k in h}


async def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    cfg = get_config()
    api_key = cfg.coinglass_api_key

    print("=" * 72)
    print(f"CoinGlass liquidation heatmap probe — symbol={symbol}")
    print("=" * 72)

    if not api_key:
        print("ERROR: LIQ_COINGLASS_API_KEY is not set. Populate .env and retry.")
        return 1

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    any_success = False
    first_success_payload: dict | None = None

    async with aiohttp.ClientSession(connector=connector) as session:
        for path in HEATMAP_PATHS:
            for extra in PARAM_COMBOS:
                label_params = {"symbol": symbol, "exchange_list": CG_EXCHANGES, **extra}
                print("\n" + "-" * 72)
                print(f"GET {path}")
                print(f"    params: {label_params}")

                ok, status, headers, body = await probe_one(
                    session, api_key, path, symbol, extra,
                )

                print(f"    http_status: {status}")
                hot = _interesting_headers(headers)
                if hot:
                    print(f"    headers: {hot}")

                if isinstance(body, dict):
                    code = body.get("code")
                    msg = body.get("msg")
                    if code is not None or msg is not None:
                        print(f"    api_code: {code!r}  msg: {msg!r}")

                    data = body.get("data")
                    if isinstance(data, (list, dict)):
                        if isinstance(data, list):
                            print(f"    data: list of {len(data)} items")
                        else:
                            print(f"    data: dict with {len(data)} keys")
                        print(f"    data_preview: {_preview(data)}")
                    elif body.get("_error"):
                        print(f"    error: {body['_error']}")
                    elif body.get("_raw_text"):
                        print(f"    raw_text: {body['_raw_text'][:200]}")
                else:
                    print(f"    body_type: {type(body).__name__}")

                if ok:
                    any_success = True
                    if first_success_payload is None:
                        first_success_payload = {
                            "path": path,
                            "params": label_params,
                            "status": status,
                            "headers": _interesting_headers(headers),
                            "body": body,
                        }
                    print("    ==> SUCCESS")
                await asyncio.sleep(2.5)  # respect rate limit between attempts

    print("\n" + "=" * 72)
    if any_success:
        print("PROBE RESULT: at least one combo returned a successful payload.")
        print("=" * 72)
        assert first_success_payload is not None
        print("\nFULL PAYLOAD OF FIRST SUCCESS (for schema design):")
        print(f"  path:    {first_success_payload['path']}")
        print(f"  params:  {first_success_payload['params']}")
        print(f"  status:  {first_success_payload['status']}")
        print(f"  headers: {first_success_payload['headers']}")
        print("\n  full body:")
        print(json.dumps(first_success_payload["body"], indent=2, default=str))
        return 0
    else:
        print("PROBE RESULT: every combo failed. Paste output back to architect.")
        print("Likely causes: endpoint not on current tier, wrong path, wrong params,")
        print("or expired API key. Check a 403/401 code above for tier-error signal.")
        print("=" * 72)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
