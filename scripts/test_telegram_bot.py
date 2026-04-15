#!/usr/bin/env python3
"""
Integration test for the telegram_bot/ package.

Seven offline blocks (no live APIs required):
  1. MarkdownV2 escape — every special char, decimal round-trip, sparkline chars.
  2. Formatters        — format_status / pnl / trades / positions / help / config.
  3. PnL aggregations  — pnl_today, equity_by_day, best_worst_trade, sharpe.
  4. Rate limiter      — window + per-chat independence.
  5. Registry          — load_executor for 4H + stubs, corrupt-file recovery.
  6. Dispatcher / handlers — mocked send_message/edit_message, /status /trades
                             /positions /help plus unauthorized + rate-limited
                             + handler-exception paths.
  7. Edge cases        — /positions with failing price fetch, empty state files.

Run:   .venv/bin/python scripts/test_telegram_bot.py
Exit 0 on all-pass, 1 otherwise.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram_bot import formatters as F
from telegram_bot import handlers as HND
from telegram_bot import health as H
from telegram_bot import pnl as P
from telegram_bot.config import TelegramBotConfig
from telegram_bot.rate_limit import RateLimiter
from telegram_bot.registry import REGISTRY, StrategyEntry, find_entry, load_executor
from telegram_bot.telegram_api import escape_md


logging.basicConfig(
    level=logging.WARNING,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

passed = 0
failed = 0


def report(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")


# ---------------------------------------------------------------------------
# Block 1: MarkdownV2 escape
# ---------------------------------------------------------------------------
def test_escape_md() -> None:
    print("\n--- Block 1: MarkdownV2 escape ---")

    # Every special char documented by Telegram.
    specials = r"_*[]()~`>#+-=|{}.!\\"
    escaped = escape_md(specials)
    # Each special char should be preceded by exactly one backslash.
    # That means output length = 2 * len(input) (the backslash char '\\' itself
    # gets escaped too).
    report("every special gets escaped",
           len(escaped) == 2 * len(specials),
           f"len(in)={len(specials)} len(out)={len(escaped)}")

    # Decimal round-trip: "1.05%" → "1\.05%"
    out = escape_md("1.05%")
    report("decimal escaped", out == "1\\.05%", f"got {out!r}")

    # Plain ASCII letters/digits untouched.
    out = escape_md("BTC 123 abc")
    report("plain untouched", out == "BTC 123 abc", f"got {out!r}")

    # Sparkline block chars are NOT in the escape list, so they pass through.
    blocks = "▁▂▃▄▅▆▇█"
    out = escape_md(blocks)
    report("sparkline chars pass through", out == blocks, f"got {out!r}")

    # Empty string.
    report("empty string safe", escape_md("") == "")


# ---------------------------------------------------------------------------
# Block 2: Formatters
# ---------------------------------------------------------------------------
def test_formatters() -> None:
    print("\n--- Block 2: Formatters ---")

    # format_status: all 3 strategy states
    status = F.format_status([
        {
            "label": "4H market_flush",
            "state": "active",
            "equity": 1005.97, "pnl_today_usd": 2.40, "pnl_today_pct": 0.24,
            "pnl_total_usd": 5.97, "pnl_total_pct": 0.597,
            "open_positions": 2,
            "last_cycle_iso": "2026-04-15 08:05 UTC",
            "systemd_state": "active", "systemd_uptime": "4h 12m",
        },
        {"label": "2H strategy", "state": "not_deployed"},
        {"label": "1H aggressive", "state": "not_deployed"},
    ])
    # Underscores get escaped by MD2 -> "market\_flush"
    report("format_status contains all 3 labels",
           "market" in status and "flush" in status
           and "2H strategy" in status
           and "1H aggressive" in status)
    report("format_status shows active equity (money-escaped)",
           "1,005" in status, "full text check relaxed for commas/dots")
    report("format_status marks stubs as not deployed",
           "not deployed" in status)

    # format_status with error state must not raise
    err_status = F.format_status([
        {"label": "4H market_flush", "state": "error", "error": "bad JSON"},
    ])
    report("format_status: error renders", "error" in err_status
           and "bad JSON" in err_status)

    # format_trades: 0 trades
    out = F.format_trades([], "4H market_flush", 10)
    report("format_trades empty", "No trades yet" in out)

    # format_trades: 51 trades, limit 50 → 50 rows, 1 omitted
    trades = [
        {
            "coin": "SOL",
            "exit_time": f"2026-04-{(i % 28) + 1:02d}T12:00:00+00:00",
            "entry_price": 100.0 + i, "exit_price": 101.0 + i,
            "pnl_pct": 1.0, "pnl_usd": 1.0, "exit_reason": "timeout",
        }
        for i in range(51)
    ]
    out = F.format_trades(trades, "4H market_flush", 50)
    # Count data rows inside the code block (skip header + separator line).
    # Crude: count "SOL" occurrences in the body — 50 rows × 1 occurrence each.
    report("format_trades respects limit",
           out.count("SOL") == 50, f"SOL count={out.count('SOL')}")
    report("format_trades marks omitted rows", "1 more rows omitted" in out)

    # format_positions: empty
    out = F.format_positions([])
    report("format_positions empty", "No open positions" in out)

    # format_pnl: <10 trades → sharpe em-dash
    out = F.format_pnl(
        strategy_label="4H market_flush",
        equity_days=[
            (datetime(2026, 4, 9, tzinfo=timezone.utc).date(), 1000.0),
            (datetime(2026, 4, 15, tzinfo=timezone.utc).date(), 1010.0),
        ],
        win_rate_pct=50.0,
        best_trade={"coin": "SOL", "pnl_pct": 1.5},
        worst_trade={"coin": "DOGE", "pnl_pct": -1.0},
        sharpe=None,
        total_trades=3,
    )
    report("format_pnl no-sharpe prints em-dash",
           "Sharpe" in out and "—" in out)

    # format_pnl sparkline characters present
    # Feed 7 monotonically varying equities; expect 7 block chars in output.
    from datetime import date as _date
    eq_days = [
        (_date(2026, 4, 9), 1000.0),
        (_date(2026, 4, 10), 1005.0),
        (_date(2026, 4, 11), 1010.0),
        (_date(2026, 4, 12), 1008.0),
        (_date(2026, 4, 13), 1012.0),
        (_date(2026, 4, 14), 1015.0),
        (_date(2026, 4, 15), 1020.0),
    ]
    out = F.format_pnl("4H market_flush", eq_days, 55.0, None, None, None, 5)
    # Count block chars (any of ▁▂▃▄▅▆▇█)
    block_count = sum(1 for ch in out if ch in "▁▂▃▄▅▆▇█")
    report("sparkline 7 block chars", block_count == 7, f"got {block_count}")
    # lowest value → ▁; highest → █
    report("sparkline lowest=▁", "▁" in out)
    report("sparkline highest=█", "█" in out)

    # format_help mentions all 7 public commands
    help_text = F.format_help()
    for cmd in ["/status", "/pnl", "/trades", "/positions",
                "/config", "/health", "/help"]:
        report(f"help mentions {cmd}", cmd in help_text)

    # format_config renders 4H thresholds and marks 2H/1H as not deployed
    cfg = TelegramBotConfig(state_file="/tmp/_unused.json")
    out = F.format_config(cfg, REGISTRY)
    # MD2 escapes _ → \_. Check for "market" + "z\_self" instead.
    report("format_config shows 4H label",
           "market" in out and "flush" in out and "z\\_self" in out)
    report("format_config marks stubs",
           out.count("not deployed") >= 2)

    # format_health trivial (all mocked values)
    out = F.format_health(
        systemd=[{"unit": "liq-paper-bot.service", "state": "active",
                  "uptime": "4h 12m"}],
        host={"cpu_pct": 3.5, "mem_used_mb": 412, "mem_total_mb": 2048,
              "disk_pct": 64.0},
        apis=[{"name": "Binance", "ok": True, "ms": 87},
              {"name": "Bitget", "ok": False, "ms": 5000}],
        recent_errors=[],
    )
    report("format_health shows API ok", "Binance" in out and "87" in out)
    report("format_health shows API fail", "Bitget" in out)
    report("format_health no-errors",
           "(none)" in out or "none" in out.lower())


# ---------------------------------------------------------------------------
# Block 3: PnL aggregations
# ---------------------------------------------------------------------------
def test_pnl_aggregations() -> None:
    print("\n--- Block 3: PnL aggregations ---")

    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    trades = [
        # 2 trades today
        {"exit_time": "2026-04-15T04:00:00+00:00",
         "pnl_pct": 1.5, "pnl_usd": 4.5, "coin": "SOL"},
        {"exit_time": "2026-04-15T08:00:00+00:00",
         "pnl_pct": -0.5, "pnl_usd": -1.5, "coin": "DOGE"},
        # 1 yesterday
        {"exit_time": "2026-04-14T20:00:00+00:00",
         "pnl_pct": 2.0, "pnl_usd": 6.0, "coin": "BTC"},
    ]
    usd, pct = P.pnl_today(trades, 1000.0, now=now)
    report("pnl_today sums today's only",
           abs(usd - 3.0) < 1e-9 and abs(pct - 0.3) < 1e-9,
           f"usd={usd}, pct={pct}")

    # pnl_total
    usd, pct = P.pnl_total(1020.0, 1000.0)
    report("pnl_total", abs(usd - 20.0) < 1e-9 and abs(pct - 2.0) < 1e-9)

    # equity_by_day — history starting day 11, ask for last 7 ending day 15.
    # Window = 09..15 (7 days). Days 09, 10 have NO entries → carry prior (day 08 = 1008).
    # Days 11..15 come straight from the history.
    hist = [
        {"time": f"2026-04-{day:02d}T12:00:00+00:00", "equity": 1000.0 + day}
        for day in range(8, 16)  # days 08..15 inclusive
        if day == 8 or day >= 11  # skip days 09, 10
    ]
    days = P.equity_by_day(hist, 1000.0, days=7, now=now)
    report("equity_by_day returns 7 days", len(days) == 7)
    # Last day (2026-04-15) has an entry of 1015.
    report("equity_by_day last=1015", abs(days[-1][1] - 1015.0) < 1e-9,
           f"got {days[-1][1]}")
    # First window day (2026-04-09) has no entry and the prior known value
    # is day 08 = 1008. Should carry 1008 forward.
    report("equity_by_day carries prior day",
           abs(days[0][1] - 1008.0) < 1e-9, f"got {days[0][1]}")

    # best_worst_trade — empty + populated
    best, worst = P.best_worst_trade([])
    report("best_worst empty", best is None and worst is None)
    best, worst = P.best_worst_trade(trades)
    report("best_worst: best is BTC @ +2%",
           best is not None and best.get("coin") == "BTC")
    report("best_worst: worst is DOGE @ -0.5%",
           worst is not None and worst.get("coin") == "DOGE")

    # sharpe_ratio: <10 trades → None
    report("sharpe None for <10", P.sharpe_ratio(trades, 8) is None)

    # Construct 20 trades with known returns, manually compute Sharpe
    import math
    rets = [0.01, 0.02, -0.01, 0.015, 0.005,
            -0.005, 0.012, -0.015, 0.02, 0.008,
            0.01, 0.02, -0.01, 0.015, 0.005,
            -0.005, 0.012, -0.015, 0.02, 0.008]
    sample_trades = [
        {"pnl_pct": r * 100, "pnl_usd": 1.0, "exit_time": "2026-04-15T00:00:00+00:00",
         "coin": "X"}
        for r in rets
    ]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    periods = (365 * 24) / 8
    expected = mean / std * math.sqrt(periods)
    got = P.sharpe_ratio(sample_trades, 8)
    report("sharpe_ratio matches manual",
           got is not None and abs(got - expected) < 1e-6,
           f"got={got}, expected={expected}")

    # win_rate
    wr = P.win_rate(trades)  # 2 wins / 3 = 66.67%
    report("win_rate", abs(wr - (2/3 * 100)) < 1e-6, f"got {wr}")
    report("win_rate empty", P.win_rate([]) == 0.0)


# ---------------------------------------------------------------------------
# Block 4: Rate limiter
# ---------------------------------------------------------------------------
def test_rate_limiter() -> None:
    print("\n--- Block 4: Rate limiter ---")
    fake_time = [100.0]

    def clock() -> float:
        return fake_time[0]

    rl = RateLimiter(window_s=5.0, clock=clock)

    ok, wait = rl.check("A")
    report("first call allowed", ok and wait == 0.0)

    ok, wait = rl.check("A")
    report("second call denied", not ok and wait > 0)

    # Advance past window
    fake_time[0] += 5.01
    ok, wait = rl.check("A")
    report("after window, allowed again", ok and wait == 0.0)

    # Different chat is independent
    fake_time[0] = 100.0
    rl2 = RateLimiter(window_s=5.0, clock=clock)
    _ = rl2.check("A")
    ok, _ = rl2.check("B")
    report("different chat independent", ok)


# ---------------------------------------------------------------------------
# Block 5: Registry
# ---------------------------------------------------------------------------
def test_registry() -> None:
    print("\n--- Block 5: Registry ---")

    entry_4h = find_entry("4h")
    report("find_entry 4h",
           entry_4h is not None and entry_4h.key == "4h")
    report("find_entry 4H (case)", find_entry("4H") is entry_4h)
    report("find_entry xyz -> None", find_entry("xyz") is None)

    entry_2h = find_entry("2h")
    report("2h is stubbed (state_file None)",
           entry_2h is not None and not entry_2h.is_deployed)

    # load_executor for 4h with a tempdir-backed state file
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "paper_state.json")
        cfg = TelegramBotConfig(state_file=state_path)
        # Inject a copy of entry_4h but with the tempdir state_file
        fake_entry = StrategyEntry(
            key="4h", label="4H market_flush",
            state_file=state_path,
            systemd_unit="liq-paper-bot.service", holding_hours=8,
        )
        ex = load_executor(fake_entry, cfg)
        report("load_executor returns executor", ex is not None)
        report("fresh state: capital == initial",
               ex is not None and abs(ex.state["capital"] - cfg.initial_capital) < 1e-9)

        # Now write a corrupt JSON and verify recovery to fresh state
        with open(state_path, "w") as f:
            f.write("{not valid json")
        ex2 = load_executor(fake_entry, cfg)
        report("corrupt file recovers to default",
               ex2 is not None and abs(ex2.state["capital"] - cfg.initial_capital) < 1e-9)

    # load_executor for stubbed entry returns None
    ex_none = load_executor(entry_2h, TelegramBotConfig(state_file="/tmp/_u.json"))
    report("stub entry -> None", ex_none is None)


# ---------------------------------------------------------------------------
# Block 6: Dispatcher + handlers
# ---------------------------------------------------------------------------
def test_dispatcher_and_handlers() -> None:
    print("\n--- Block 6: Dispatcher & handlers ---")

    from telegram_bot.app import build_dispatcher

    # Run the async tests synchronously.
    asyncio.run(_dispatcher_async())


async def _dispatcher_async() -> None:
    from telegram_bot.app import build_dispatcher

    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "paper_state.json")

        # Populate a realistic state file
        initial_state = {
            "capital": 1010.50,
            "positions": [
                {
                    "coin": "SOL",
                    "entry_price": 100.0,
                    "entry_time": (datetime.now(timezone.utc) -
                                   timedelta(hours=2)).isoformat(),
                    "exit_due": (datetime.now(timezone.utc) +
                                 timedelta(hours=6)).isoformat(),
                    "margin_usd": 100.0, "notional_usd": 300.0,
                    "z_score_at_entry": 2.1, "n_coins_at_entry": 5,
                },
            ],
            "closed_trades": [
                {
                    "coin": "BTC", "entry_price": 50000.0, "exit_price": 50500.0,
                    "entry_time": "2026-04-14T04:00:00+00:00",
                    "exit_time": "2026-04-14T12:00:00+00:00",
                    "pnl_pct": 1.0, "pnl_usd": 3.0, "exit_reason": "timeout",
                },
                {
                    "coin": "DOGE", "entry_price": 0.1, "exit_price": 0.098,
                    "entry_time": "2026-04-14T08:00:00+00:00",
                    "exit_time": "2026-04-14T16:00:00+00:00",
                    "pnl_pct": -2.0, "pnl_usd": -6.0, "exit_reason": "sl_hit",
                },
            ],
            "equity_history": [
                {"time": "2026-04-14T16:00:00+00:00", "equity": 1007.5},
                {"time": "2026-04-15T00:00:00+00:00", "equity": 1010.5},
            ],
            "last_summary_date": None,
        }
        with open(state_path, "w") as f:
            json.dump(initial_state, f)

        cfg = TelegramBotConfig(
            state_file=state_path,
            telegram_bot_token="TEST",
            telegram_chat_id="12345",
            rate_limit_window_s=5.0,
            command_reply_timeout_s=5.0,
        )

        # Patch the registry so 4H uses our tempdir state file, 2H/1H stay stubs.
        fake_registry = [
            StrategyEntry(
                key="4h", label="4H market_flush",
                state_file=state_path, systemd_unit="liq-paper-bot.service",
                holding_hours=8,
            ),
            StrategyEntry(
                key="2h", label="2H strategy",
                state_file=None, systemd_unit=None, holding_hours=None,
            ),
            StrategyEntry(
                key="1h", label="1H aggressive",
                state_file=None, systemd_unit=None, holding_hours=None,
            ),
        ]
        # Patch REGISTRY in both modules that use it (handlers imported it by name)
        sends: list[tuple] = []
        edits: list[tuple] = []

        async def fake_send(cfg_, chat_id, text, **kw):
            sends.append((str(chat_id), text, kw))
            return 999  # fake message_id

        async def fake_edit(cfg_, chat_id, message_id, text, **kw):
            edits.append((str(chat_id), message_id, text, kw))
            return True

        # Systemd mock (Darwin would return "unknown"; pin for determinism)
        async def fake_systemd(unit):
            return {"unit": unit, "state": "active", "uptime": "1h 5m",
                    "raw": "active"}

        async def fake_ping_all(timeout=5.0):
            return [
                {"name": "Binance", "ok": True, "ms": 87},
                {"name": "CoinGlass", "ok": True, "ms": 120},
                {"name": "Hyperliquid", "ok": True, "ms": 95},
                {"name": "Bitget", "ok": True, "ms": 110},
            ]

        async def fake_recent_errors(unit, hours=1, limit=10):
            return []

        # Patch get_current_price to avoid live ccxt
        def fake_price(self, coin):
            return {"SOL": 102.0, "BTC": 51000.0, "DOGE": 0.11}.get(coin, 100.0)

        with patch("telegram_bot.handlers.REGISTRY", fake_registry), \
             patch("telegram_bot.registry.REGISTRY", fake_registry), \
             patch("telegram_bot.app.send_message", new=fake_send), \
             patch("telegram_bot.app.edit_message", new=fake_edit), \
             patch("telegram_bot.handlers.H.check_systemd_unit",
                   new=fake_systemd), \
             patch("telegram_bot.handlers.H.ping_all", new=fake_ping_all), \
             patch("telegram_bot.handlers.H.recent_errors",
                   new=fake_recent_errors), \
             patch.object(HND.PaperExecutor, "get_current_price",
                          fake_price, create=False):

            limiter = RateLimiter(cfg.rate_limit_window_s,
                                  clock=lambda: _fake_clock[0])
            _fake_clock = [1000.0]  # mutable time for rate-limit tests
            dispatch = build_dispatcher(cfg, limiter)

            def make_msg(text: str, chat_id: str = "12345") -> dict:
                return {
                    "message_id": 1, "text": text,
                    "chat": {"id": int(chat_id)},
                    "from": {"id": int(chat_id)},
                }

            # /help -> direct send (no loading)
            sends.clear(); edits.clear()
            await dispatch(make_msg("/help"))
            report("/help replied via send_message",
                   len(sends) == 1 and "status" in sends[0][1])
            report("/help did not use edit", len(edits) == 0)

            # /status -> loading + edit
            sends.clear(); edits.clear()
            _fake_clock[0] += 10  # past rate limit window
            await dispatch(make_msg("/status"))
            report("/status sends loading + edits final",
                   len(sends) == 1 and len(edits) == 1)
            final_text = edits[0][2]
            report("/status final contains 4H label",
                   "market" in final_text and "flush" in final_text)
            report("/status final shows 2H stub",
                   "not deployed" in final_text)

            # /trades 4h 5  -> edit with table
            sends.clear(); edits.clear()
            _fake_clock[0] += 10
            await dispatch(make_msg("/trades 4h 5"))
            final = edits[0][2] if edits else ""
            report("/trades: rendered",
                   "market" in final and "flush" in final and "BTC" in final)

            # /trades 2h -> not deployed
            sends.clear(); edits.clear()
            _fake_clock[0] += 10
            await dispatch(make_msg("/trades 2h"))
            final = edits[0][2] if edits else ""
            report("/trades 2h: not deployed", "not deployed" in final)

            # /trades xyz  -> usage reply (not loading since arg parse fails)
            sends.clear(); edits.clear()
            _fake_clock[0] += 10
            await dispatch(make_msg("/trades xyz"))
            # The dispatcher starts a loading message (since /trades is in
            # NEEDS_LOADING) and then edits with a usage error.
            got = (edits[0][2] if edits else sends[0][1])
            report("/trades xyz: usage help",
                   "Usage" in got)

            # /positions -> SOL listed with unrealized PnL
            sends.clear(); edits.clear()
            _fake_clock[0] += 10
            await dispatch(make_msg("/positions"))
            final = edits[0][2] if edits else ""
            report("/positions: SOL listed", "SOL" in final)
            report("/positions: unrealized shown", "Unrealized" in final)

            # /config -> shows 4H thresholds
            sends.clear(); edits.clear()
            _fake_clock[0] += 10
            await dispatch(make_msg("/config"))
            # /config isn't in NEEDS_LOADING, so it goes to send_message
            final = sends[0][1] if sends else ""
            report("/config contains z_self", "z_self" in final
                   or "z\\_self" in final)

            # /health -> systemd + pings + errors
            sends.clear(); edits.clear()
            _fake_clock[0] += 10
            await dispatch(make_msg("/health"))
            final = edits[0][2] if edits else ""
            report("/health contains Binance ping", "Binance" in final)
            report("/health contains Bitget ping", "Bitget" in final)

            # Unauthorized chat: dispatcher is only called by poll_updates
            # AFTER the authorization check, so this test exercises the
            # polling.is_authorized helper separately.
            from telegram_bot.polling import _is_authorized
            authorized_msg = make_msg("/status")
            report("is_authorized: correct chat",
                   _is_authorized(authorized_msg, "12345"))
            wrong_msg = make_msg("/status", chat_id="99999")
            report("is_authorized: wrong chat",
                   not _is_authorized(wrong_msg, "12345"))

            # Rate-limited path
            sends.clear(); edits.clear()
            # Two commands within 5 s (our limiter uses _fake_clock)
            _fake_clock[0] += 10
            await dispatch(make_msg("/help"))
            # Second /help immediately -> should be rate-limited
            sends.clear(); edits.clear()
            _fake_clock[0] += 0.5
            await dispatch(make_msg("/help"))
            report("second /help rate-limited",
                   any("Wait" in s[1] for s in sends))

            # Handler raises -> reply contains "Error", polling loop survives
            sends.clear(); edits.clear()
            async def boom_handler(_ctx):
                raise RuntimeError("synthetic test crash")

            _fake_clock[0] += 10
            original = HND.COMMANDS["/help"]
            try:
                HND.COMMANDS["/help"] = boom_handler
                # But dispatcher captured the table by reference at import
                # time? No — build_dispatcher reads COMMANDS inside the inner
                # dispatch call.
                await dispatch(make_msg("/help"))
                got = edits[0][2] if edits else (sends[0][1] if sends else "")
                report("handler-crash reply mentions Error",
                       "Error" in got and "synthetic" in got)
            finally:
                HND.COMMANDS["/help"] = original


# ---------------------------------------------------------------------------
# Block 7: Edge cases
# ---------------------------------------------------------------------------
def test_edge_cases() -> None:
    print("\n--- Block 7: Edge cases ---")

    asyncio.run(_edge_async())


async def _edge_async() -> None:
    # /positions with a price fetcher that times out should produce "—" rows,
    # not an error.
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "paper_state.json")
        state = {
            "capital": 1000.0,
            "positions": [{
                "coin": "SOL", "entry_price": 100.0,
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "exit_due": (datetime.now(timezone.utc) +
                             timedelta(hours=4)).isoformat(),
                "margin_usd": 100.0, "notional_usd": 300.0,
                "z_score_at_entry": 2.0, "n_coins_at_entry": 4,
            }],
            "closed_trades": [],
            "equity_history": [],
            "last_summary_date": None,
        }
        with open(state_path, "w") as f:
            json.dump(state, f)

        cfg = TelegramBotConfig(
            state_file=state_path,
            telegram_bot_token="TEST",
            telegram_chat_id="12345",
            position_price_timeout_s=0.1,
        )
        fake_reg = [StrategyEntry(
            key="4h", label="4H market_flush",
            state_file=state_path, systemd_unit="liq-paper-bot.service",
            holding_hours=8,
        )]

        def failing_price(self, coin):
            raise RuntimeError("ccxt down")

        with patch("telegram_bot.handlers.REGISTRY", fake_reg), \
             patch.object(HND.PaperExecutor, "get_current_price",
                          failing_price, create=False):
            ctx = HND.HandlerContext(cfg=cfg, chat_id="12345", args=[])
            out = await HND.handle_positions(ctx)
            report("/positions: price-fetch failure doesn't raise",
                   "SOL" in out and "Unrealized: —" in out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    test_escape_md()
    test_formatters()
    test_pnl_aggregations()
    test_rate_limiter()
    test_registry()
    test_dispatcher_and_handlers()
    test_edge_cases()

    print("\n" + "=" * 60)
    print(f"PASS: {passed} | FAIL: {failed}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
