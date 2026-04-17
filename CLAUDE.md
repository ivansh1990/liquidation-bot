# Liquidation Bot

Data collection system for the "Liquidation Magnet" strategy. Collects trader positions and liquidation levels from Hyperliquid, plus open interest, funding rates, long/short ratios, and taker buy/sell from Binance futures. All data stored in PostgreSQL for future analysis.

## Project Structure

```
liquidation-bot/
├── collectors/
│   ├── config.py           — Pydantic config, coin lists, symbol mappings
│   ├── db.py               — PostgreSQL pool, schema, insert/query helpers
│   ├── hl_websocket.py     — Hyperliquid WebSocket: live trades, prices
│   ├── hl_snapshots.py     — Hyperliquid: position snapshots → liquidation map
│   ├── binance_collector.py — Binance: OI, funding, L/S ratio, taker
│   ├── coinglass_oi_collector.py — CoinGlass: OI + funding (4H timer)
│   └── alerts.py           — Telegram notifications
├── bot/
│   ├── config.py           — BotConfig (subclasses collectors.config.Config)
│   ├── signal.py           — SignalComputer: live market_flush signal (L3b-2, locked)
│   ├── paper_executor.py   — PaperExecutor: simulates LONG positions, state JSON
│   ├── alerts.py           — Telegram message formatters (wraps collectors/alerts)
│   └── scheduler.py        — Main 4H-aligned loop (python -m bot.scheduler)
├── telegram_bot/           — L5: interactive Telegram command interface
│   ├── app.py              — Entrypoint: `python -m telegram_bot.app`
│   ├── config.py           — TelegramBotConfig (subclasses BotConfig)
│   ├── registry.py         — StrategyEntry + REGISTRY (4H paper, showcase live, 2H/1H stubs)
│   ├── polling.py          — getUpdates long-poll loop + chat_id auth
│   ├── telegram_api.py     — Raw aiohttp wrappers + escape_md (MarkdownV2)
│   ├── rate_limit.py       — Per-chat 5s window
│   ├── pnl.py              — equity_by_day, pnl_today, sharpe_ratio, best_worst
│   ├── formatters.py       — MarkdownV2 message builders + unicode sparkline
│   ├── health.py           — systemd + journalctl + HTTP pings + host stats
│   └── handlers.py         — Per-command business logic (7 commands)
├── exchange/               — L7: live Binance Futures execution
│   ├── __init__.py
│   ├── config.py           — ExchangeConfig (subclasses BotConfig)
│   ├── binance_client.py   — Authenticated ccxt wrapper (dry-run + testnet)
│   ├── safety.py           — SafetyGuard: circuit breakers
│   ├── live_executor.py    — LiveExecutor: real orders, exchange-side TP/SL
│   └── scheduler.py        — Main 4H loop (python -m exchange.scheduler)
├── scripts/
│   ├── init_db.py          — Create database and tables
│   ├── seed_addresses.py   — Seed whale addresses from leaderboard
│   ├── test_collectors.py  — Integration test for all endpoints
│   ├── test_paper_bot.py   — L4: offline tests for bot/ (z-score parity, state, signal)
│   ├── test_telegram_bot.py — L5: offline tests for telegram_bot/ (escape, formatters, dispatch)
│   ├── test_exchange.py    — L7: offline tests for exchange/ (72 assertions)
│   ├── backfill_binance.py — Backfill last 30 days of Binance history (one-shot)
│   ├── backfill_coinglass.py — Backfill 180 days of CoinGlass aggregated liquidations (one-shot)
│   ├── backfill_coinglass_oi.py — Backfill 180 days of CoinGlass aggregated OI + funding (one-shot)
│   ├── backtest_liquidation_flush.py — H1/H2/H3 backtest: liquidation asymmetry → reversal (L2 baseline, locked)
│   ├── walkforward_h1_flush.py — L3: 6-fold expanding-window walk-forward validation of H1
│   ├── backtest_h1_with_stops.py — L3: ATR-based TP/SL grid (64 configs/coin) using H1 entries
│   ├── analyze_heatmap_signal.py — L3: HL heatmap overlay framework (top-decile clusters, preceding-snapshot match)
│   ├── backtest_combo.py   — L3b-2: combo signal backtest (9 combos × 10 coins × 4 holding periods, portfolio + walk-forward)
│   ├── analyze_liq_clusters.py — L6: liquidation cluster magnet-effect analysis (hit rates + random baseline)
│   ├── test_liq_analyzer.py — L6: offline tests for analyze_liq_clusters.py (41 assertions)
│   ├── analyze_liq_clusters_v2.py — L6b: OI-normalized cluster strength analysis (distance × strength matrix)
│   ├── test_liq_analyzer_v2.py — L6b: offline tests for analyze_liq_clusters_v2.py (34 assertions)
│   ├── test_coinglass_collector.py — L6c: offline tests for coinglass_oi_collector (29 assertions)
│   ├── backfill_coinglass_hourly.py — L8: Backfill 180 days of CoinGlass h1/h2 liquidations + OI (one-shot)
│   ├── backtest_market_flush_multitf.py — L8: market_flush backtest on h1/h2/h4 with walk-forward
│   ├── test_backtest_multitf.py — L8: offline tests for multi-TF backtest (34 assertions)
│   └── quick_analysis.py   — Data analysis (run after 2+ days)
├── state/                  — Bot state (paper + showcase, gitignored)
├── systemd/                — Service and timer files for VPS
└── analysis/               — Generated reports (gitignored)
```

## Tracked Coins

BTC, ETH, SOL, DOGE, LINK, AVAX, SUI, ARB, WIF, PEPE

### Symbol Mappings

| Coin | Hyperliquid | Binance Raw | Binance ccxt |
|------|-------------|-------------|--------------|
| PEPE | **kPEPE** | **1000PEPEUSDT** | **1000PEPE/USDT:USDT** |
| Others | Same as coin name | {COIN}USDT | {COIN}/USDT:USDT |

## API Endpoints

### Hyperliquid
- Base: `https://api.hyperliquid.xyz`
- `POST /info {"type": "allMids"}` — mid prices
- `POST /info {"type": "clearinghouseState", "user": "0x..."}` — positions
- `wss://api.hyperliquid.xyz/ws` — trades WebSocket
- Leaderboard: `GET https://stats-data.hyperliquid.xyz/Mainnet/leaderboard`
- Rate limit: 1200 req/min (collectors use ~1000/min with buffer)

### Binance (all public, no API key)
- OI: `GET /fapi/v1/openInterest?symbol=BTCUSDT`
- Funding: `GET /fapi/v1/fundingRate?symbol=BTCUSDT&limit=1`
- L/S Ratio: `GET /futures/data/topLongShortAccountRatio?symbol=BTCUSDT&period=1h&limit=1`
- Taker: `GET /futures/data/takerlongshortRatio?symbol=BTCUSDT&period=1h&limit=1`

**Important**: L/S ratio and taker endpoints use `/futures/data/` path, NOT `/fapi/v1/`.

### CoinGlass (requires API key)
- Base: `https://open-api-v4.coinglass.com`
- Aggregated liquidations: `GET /api/futures/liquidation/aggregated-history?symbol=BTC&interval=h4`
- Aggregated OI OHLC: `GET /api/futures/open-interest/aggregated-history?symbol=BTC&interval=h4`
- Funding rate OHLC: `GET /api/futures/funding-rate/oi-weight-history?symbol=BTC&interval=h8` (fallback: `/funding-rate/vol-weight-history`, `interval=h4`). Note: path is `oi-weight-history` (no `-ohlc-`), and `aggregated-history` does NOT exist for funding rate — only for liquidations and OI.
- Header: `CG-API-KEY: <key>`
- Rate limit: 30 req/min on Hobbyist tier → collectors pause 2.5s between requests
- Historical range on Hobbyist: 180 days at h4 interval (~1080 records/coin); funding at h8 ≈ 540/coin
- Hobbyist-tier quirk: aggregated endpoints ignore `startTime`/`endTime` and return the latest ≤1000 buckets — so Hobbyist-tier backfills (`backfill_coinglass.py`, `backfill_coinglass_oi.py`) use a single request per coin and filter the window client-side. Startup tier honors `endTime`, which `backfill_coinglass_hourly.py` (L8) uses to paginate for >1000-bar windows (walks `endTime` backward page-by-page, ≤10 pages per coin/endpoint).
- Symbol format: base name (`BTC`, `ETH`, ...); `PEPE` may require `1000PEPE` fallback — both `backfill_coinglass.py` and `backfill_coinglass_oi.py` try the primary name first and fall back automatically.

## Database Schema (PostgreSQL `liquidation`)

| Table | Purpose | Key Fields |
|-------|---------|------------|
| `hl_addresses` | Tracked trader wallets | address (PK), total_volume_usd |
| `hl_position_snapshots` | Position data every 15 min | coin, side, size_usd, liquidation_px, is_liq_estimated |
| `hl_liquidation_map` | Aggregated liq levels | coin, price_level, long_liq_usd, short_liq_usd |
| `binance_oi` | Open Interest | symbol, open_interest, open_interest_usd |
| `binance_funding` | Funding Rate | symbol, funding_rate, mark_price |
| `binance_ls_ratio` | Long/Short Ratio | symbol, long_account_pct, short_account_pct |
| `binance_taker` | Taker Buy/Sell | symbol, buy_vol, sell_vol, buy_sell_ratio |
| `coinglass_liquidations` | Aggregated liquidations (4H) | symbol, long_vol_usd, short_vol_usd, long_count, short_count |
| `coinglass_oi` | Aggregated OI OHLC (4H) | symbol, open_interest (close), oi_high, oi_low |
| `coinglass_funding` | Aggregated funding rate (8H or 4H) | symbol, funding_rate |
| `coinglass_liquidations_h1` | Aggregated liquidations (1H, L8) | symbol, long_vol_usd, short_vol_usd, long_count, short_count |
| `coinglass_liquidations_h2` | Aggregated liquidations (2H, L8) | symbol, long_vol_usd, short_vol_usd, long_count, short_count |
| `coinglass_oi_h1` | Aggregated OI OHLC (1H, L8) | symbol, open_interest, oi_high, oi_low |
| `coinglass_oi_h2` | Aggregated OI OHLC (2H, L8) | symbol, open_interest, oi_high, oi_low |

`is_liq_estimated` in `hl_position_snapshots`: `FALSE` = liquidation price from API, `TRUE` = estimated via `entry_px * (1 ± 1/leverage)`. Filter with `WHERE NOT is_liq_estimated` for analysis requiring precise data.

The four `binance_*` tables gain a `UNIQUE(timestamp, symbol)` constraint the first time `scripts/backfill_binance.py` runs (added lazily via `ALTER TABLE ... ADD CONSTRAINT`). This makes backfill + hourly collector coexist safely through `ON CONFLICT DO NOTHING`.

`coinglass_liquidations` is created by `scripts/backfill_coinglass.py` (inline `CREATE TABLE IF NOT EXISTS` with `CONSTRAINT uq_cg_liq UNIQUE (timestamp, symbol)`). There is no hourly CoinGlass collector yet — we only backfill and backtest until edge is confirmed.

`coinglass_oi` and `coinglass_funding` are created by `scripts/backfill_coinglass_oi.py` (inline `CREATE TABLE IF NOT EXISTS` with `CONSTRAINT uq_cg_oi` / `uq_cg_fr` on `(timestamp, symbol)`). Live 4H collector `collectors/coinglass_oi_collector.py` (L6c) keeps both tables current via `liq-coinglass-oi.timer` (every 4H, 5 min after bar close). Backfill script is still used for initial historical fill; live collector and backfill coexist safely via `ON CONFLICT DO NOTHING`.

## Running Locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Edit .env with DB credentials

.venv/bin/python scripts/init_db.py
.venv/bin/python scripts/seed_addresses.py
.venv/bin/python scripts/test_collectors.py

# Manual collection run
.venv/bin/python -m collectors.hl_snapshots
.venv/bin/python -m collectors.binance_collector

# One-shot historical backfill (last 30 days of Binance futures data)
# Idempotent: safe to rerun and to run alongside the hourly collector.
.venv/bin/python scripts/backfill_binance.py --days 30

# One-shot CoinGlass liquidation backfill (180 days, 4H interval).
# Requires LIQ_COINGLASS_API_KEY in .env. Idempotent.
.venv/bin/python scripts/backfill_coinglass.py --days 180

# One-shot CoinGlass OI + funding backfill (180 days; OI at h4, funding at h8→h4).
# Creates coinglass_oi + coinglass_funding if missing. Idempotent.
# First BTC record of each endpoint is dumped as raw JSON so field names are
# inspectable without --verbose. Flags: --coin BTC, --skip-oi, --skip-funding.
.venv/bin/python scripts/backfill_coinglass_oi.py --days 180

# Backtest H1/H2/H3: liquidation flush → reversal.
# Reads coinglass_liquidations + fetches Binance 4H klines via ccxt on-the-fly.
.venv/bin/python scripts/backtest_liquidation_flush.py

# L3: walk-forward validation of H1 long-flush signal.
# 6 folds, expanding window, altcoins only (SOL/DOGE/LINK/AVAX/SUI/ARB).
# Prints per-coin fold table + portfolio PASS/FAIL summary.
.venv/bin/python scripts/walkforward_h1_flush.py

# L3: ATR-based TP/SL backtest using H1 entries.
# Grid = 4 TP×ATR × 4 SL×ATR × 4 max_hold = 64 configs/coin.
# Entry z-thresholds hardcoded in DEFAULT_THRESHOLDS (update after walk-forward).
.venv/bin/python scripts/backtest_h1_with_stops.py

# L3: HL heatmap overlay analysis (framework).
# Matches H1 flush events to the immediately-preceding hl_liquidation_map snapshot.
# With ~1 day of HL data, usually prints "insufficient, projected ready date: ...".
.venv/bin/python scripts/analyze_heatmap_signal.py

# L3b-2: combo signal backtest.
# Merges coinglass_liquidations + coinglass_oi + coinglass_funding + Binance
# 4H klines into per-coin feature frames, then tests 9 pre-defined filter
# combos (flush, capitulation, normalized_flush, market_flush, double_flush,
# flush_extreme_funding, full_capitulation, normalized_market,
# flush_volume_spike) across all 10 coins × 4 holding periods. Emits a per-
# coin table, a combo ranking pooled at h=8, a portfolio summary, and a
# 4-fold walk-forward on the best combo (fixed thresholds, PASS = ≥2/3 OOS
# positive AND pooled OOS Sharpe>1.0; skipped if N<30).
.venv/bin/python scripts/backtest_combo.py | tee analysis/combo_L3b.txt
```

## VPS Deployment

```bash
cd ~
git clone <repo-url> liquidation-bot
cd liquidation-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env  # fill DB password + Telegram token

.venv/bin/python scripts/init_db.py
.venv/bin/python scripts/seed_addresses.py
.venv/bin/python scripts/test_collectors.py

# Install systemd services
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now liq-hl-websocket.service
sudo systemctl enable --now liq-hl-snapshots.timer
sudo systemctl enable --now liq-binance.timer
sudo systemctl enable --now liq-coinglass-oi.timer

# Verify
sudo systemctl status liq-hl-websocket
sudo systemctl list-timers | grep liq
journalctl -u liq-hl-snap -f
```

## Updating on VPS

```bash
cd ~/liquidation-bot
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart liq-hl-websocket
# Timers auto-pick up changes on next run
```

## Environment Variables

All prefixed with `LIQ_`:
- `LIQ_DB_HOST`, `LIQ_DB_PORT`, `LIQ_DB_NAME`, `LIQ_DB_USER`, `LIQ_DB_PASSWORD`
- `LIQ_TELEGRAM_BOT_TOKEN`, `LIQ_TELEGRAM_CHAT_ID`
- `LIQ_COINGLASS_API_KEY` — CoinGlass Hobbyist-tier API key (required for `backfill_coinglass.py`, `backfill_coinglass_oi.py`, and the live `coinglass_oi_collector`)
- `LIQ_BINANCE_API_KEY` — Binance Futures API key (required for `exchange.scheduler` when `LIQ_DRY_RUN=false`)
- `LIQ_BINANCE_API_SECRET` — Binance Futures API secret
- `LIQ_BINANCE_TESTNET` — `true` to use Binance testnet (sandbox mode)
- `LIQ_DRY_RUN` — `true` (default) to log orders without sending; `false` for real trading

## Constraints

- No imports from `crypto-regime-bot` (separate project)
- All Hyperliquid and Binance endpoints are public (no API key needed); CoinGlass requires a free Hobbyist-tier key
- No Docker
- No trading/strategy logic — data collection + offline backtesting only

## Tested-and-Rejected Approaches (as of 2026-04-17)

On the 180-day sample 2025-10 → 2026-04 covering 10 coins (BTC, ETH, SOL, DOGE, LINK, AVAX, SUI, ARB, WIF, PEPE) and the Binance Smart Filter goal (≥14 trading days / 30, MDD ≤ 20%, win days ≥ 65%), the following signal approaches have been systematically tested and **REJECTED**. Do not re-explore these without explicit architectural decision documented in LIVE_TRADING_MASTER_PLAN.md.

### Summary table

| # | Approach | Session | Commit | N variants | Verdict | Root cause |
|---|----------|---------|--------|------------|---------|------------|
| 1 | NetPos Contrarian/Confirmation (L10) | L10 Phase 1/2/2b | `df5d7ce`, `2987d2c`, `13cc78b` | 18 | REJECT | Correlation 0.76 with h4 `market_flush` — duplicate, not diversification |
| 2 | CVD as filter over market_flush | L13 Phase 2 | `966db3e` | 18 | REJECT | 18/18 FAIL — CVD extreme events do not coincide with flush events |
| 3 | CVD standalone H5/H7 | L13 Phase 3/3b/3c | `34387ee`, `644ca18`, `fe2cf8c` | 14 | REJECT | 2 MARGINAL → failed Phase 3c Smart Filter adequacy |
| 4 | Per-coin breadth relaxation (K<4) | L14 Phase 1 | `e80a5f6` | 15 | REJECT | Inverse tradeoff: edge and temporal dispersion mutually exclusive within market_flush |
| 5 | Funding rate z-score standalone | L15 Phase 1 | `9accd00` | 6 | REJECT | 6/6 FAIL — H2 showed systematic anti-edge (Sharpe −2.57) in trending regime |
| 6 | OI velocity z-score standalone | L15 Phase 2 | `cc9e111`, `4026bb0` (guard fix) | 6 | REJECT | 6/6 FAIL — universal Smart Filter adequacy failure; best Sharpe achieved via outlier-driven noise (auto-flagged) |

### Cumulative evidence

**Six rejected continuous-signal approaches** on identical 180-day 2025-10 → 2026-04 sample. Plus the L14 breadth-is-edge finding: breadth was never a filter — it was the core edge, and removing it kills Sharpe while fixing dispersion.

**Strong cumulative conclusion:** no positioning-adjacent continuous signal (funding, OI, netposition, CVD) produces a tradable edge compatible with Smart Filter requirements on this sample in this regime. Both snapshot-contrarian (funding) and velocity-contrarian (OI) hypotheses fail — the 2025-10 → 2026-04 regime does not mean-revert on positioning extremes.

### What this means for future research

- **Do NOT re-test any of rows 1–6 with threshold tweaks, window changes, or coin selection.** Parameter-space has been explored adequately. Further tweaks constitute overfitting to the sample.
- **Do NOT combine rejected signals into compound filters** without first establishing that each component has independent edge. We tried this with `flush_extreme_funding` (L3b-2) and CVD-filter-over-flush (L13 Phase 2) — both REJECT.
- **DO consider principally different signal classes:** the untested hypothesis class is **price-targeting** signals (not positioning-based). L6b Predictive Liquidation Magnet is the only active candidate in this class, scheduled for retest April 24, 2026 when HL heatmap data will have ≥ 7 days of history.
- **DO consider regime-dependence:** all rejections are sample-specific. If market regime shifts to mean-reverting / ranging behavior in 2026-Q3+, some of these approaches may deserve retest with updated data. Flag in future work as "rejected in 2025-10 → 2026-04 trending regime" — not "universally broken."

### Active strategy

Current only validated strategy: **`market_flush` at h4** (z>1.0 + n_coins≥4 breadth, 8h holding). Backtest Sharpe 5.87, 3/3 OOS positive. Documented in L8.

Known limitations of `market_flush`:
- Clustering (41 active calendar days out of 148) — structurally incompatible with Smart Filter temporal dispersion requirement
- Breadth filter is the core edge; relaxing it to improve dispersion destroys Sharpe (L14 Phase 1 proof)

Therefore `market_flush` alone is **insufficient** to meet Smart Filter gates for Binance lead-trader. Requires complementary strategy from different class. L6b retest is the current path; if L6b fails, business-model re-evaluation (Variant D) is the fallback.

### Research discipline reminders (learnings integrated)

1. **Dual-track PASS criteria mandatory.** Primary (L8 parity: Sharpe, Win%, N, OOS folds) + Strict (Smart Filter 30d rolling adequacy: min TD, median TD, win days, MDD). Single-track PASS hides catastrophic clustering (L13 Phase 3c lesson).
2. **Correlation check before calling "diversified."** L10 Phase 2b caught NetPos as 0.76-correlated to h4 — without correlation check, we would have deployed a duplicate.
3. **Rolling 30d metrics, not aggregates.** Aggregate Sharpe hides temporal distribution. Smart Filter operates on rolling windows, so research must too.
4. **Win days on trading-days basis, not calendar days.** L13 Phase 3c finding.
5. **Look-ahead guard must be N-aware.** Fixed April 17, 2026 (`4026bb0`). 100% win rate on small-N OOS folds is sample artifact, not evidence of bug.
6. **Unit-return MDD can exceed 100%.** Not a bug — cumulative drawdown relative to starting peak. But misleading for deployment risk; always report caveat when MDD > 100%.
7. **Per-coin z-score normalization essential.** Raw funding/OI/CVD values span 5+ orders of magnitude across coins (PEPE vs BTC); cross-coin comparison impossible without normalization.
8. **Suspicious Sharpe auto-flag (>8.0 → MARGINAL).** Outlier-driven Sharpe inflation is common in small-N backtests; explicit flag prevents false PASS.

### File-level reference

Tested research drivers (locked, import-only reuse):
- `scripts/research_netposition.py` — L10
- `scripts/research_cvd.py`, `scripts/research_cvd_standalone.py` — L13
- `scripts/research_breadth.py` — L14
- `scripts/research_funding_standalone.py` — L15 Phase 1
- `scripts/research_oi_standalone.py` — L15 Phase 2

Shared Smart Filter utilities:
- `scripts/smart_filter_adequacy.py` — `compute_daily_metrics`, `simulate_smart_filter_windows`, `SMART_FILTER_CONFIGS`

Walk-forward split:
- `scripts/walkforward_h1_flush.py` — `split_folds`

Look-ahead guard helper (added `4026bb0`):
- `check_lookahead_guard(folds, min_n)` — present in `research_funding_standalone.py` and `research_oi_standalone.py`; `SUSPICIOUS_WIN_RATE_MIN_N = 30`

## Session L3 — Walk-forward + ATR stops + heatmap overlay

Three new scripts added, reusing `load_liquidations` / `fetch_klines_4h` / `compute_signals` / `backtest_signal` from `scripts/backtest_liquidation_flush.py` (L2 baseline — do not modify).

- **`scripts/walkforward_h1_flush.py`** — 6 folds (fold 0 = train-only, folds 1–5 = OOS), expanding-window. Grid = z ∈ {1.0,1.5,2.0,2.5,3.0} × h ∈ {4,8,12} with min train N=5; falls back to `(z=2.0, h=8)` (L2 consensus) when no combo qualifies. Pooled OOS Sharpe is computed on concatenated trade returns across folds. PASS per coin = ≥4/5 positive folds AND pooled Sharpe>0.5 AND pooled win%>55. Coins: SOL, DOGE, LINK, AVAX, SUI, ARB (BTC/ETH skipped — no L2 edge).
- **`scripts/backtest_h1_with_stops.py`** — ATR(14, shifted +1 bar) TP/SL simulator. Grid = TP×ATR ∈ {1.0,1.5,2.0,2.5} × SL×ATR ∈ {0.5,0.75,1.0,1.5} × max_hold ∈ {2,3,4,6} bars (= 8h/12h/16h/24h) → 64 configs/coin. Entry thresholds in `DEFAULT_THRESHOLDS` dict at top of file — update by hand after walk-forward confirms winners. Same-bar TP+SL = pessimistic (SL first). Gap-through-SL: if `bar.open <= sl`, fill at `bar.open` (worse than sl); tracked as `SL_gap` separately from clean `SL` in the exit-reason breakdown. Gap-through-TP handled symmetrically. Adds a new OHLC fetcher `fetch_klines_4h_ohlc` local to this script (L2's `fetch_klines_4h` returns close only).
- **`scripts/analyze_heatmap_signal.py`** — framework for HL heatmap overlay. Cluster rule = top-decile per snapshot (rank rows by `short_liq_usd` / `long_liq_usd`, keep top 10%). HL match = `snapshot_time <= flush_ts ORDER BY DESC LIMIT 1` with max staleness 30 min (no look-ahead). Coin scope = same 6 altcoins. If `n_matched < 30`, prints projected ready date based on match rate; re-run after that date.

HL heatmap data collection started ~2026-04-13, so the overlay script will usually emit "insufficient data" for the first few weeks. Walk-forward and ATR backtest require only `coinglass_liquidations` + on-the-fly Binance klines.

## Session L3b-1 — CoinGlass OI + Funding Backfill

Motivation: Binance hourly `binance_oi` / `binance_funding` hold only ~21 days, too short for a combo-signal backtest. CoinGlass aggregated OI/funding extends the OI + funding series to the same ~167-day horizon we already have for `coinglass_liquidations`, joinable on `(timestamp, symbol)`.

New script `scripts/backfill_coinglass_oi.py` (modeled on `backfill_coinglass.py`):

- **Endpoints**: OI → `/api/futures/open-interest/aggregated-history?interval=h4`; Funding → `/api/futures/funding-rate/oi-weight-history` tried first, falling back to `/funding-rate/vol-weight-history`; interval `h8` preferred (matches Binance's 8h funding cadence), falling back to `h4`. First non-empty `(path, interval)` combo wins per coin; the chosen combo is logged and printed in the final summary. Note: `aggregated-history` does not exist for funding — only for liquidations and OI.
- **Tables**: `coinglass_oi (timestamp, symbol, open_interest, oi_high, oi_low)` and `coinglass_funding (timestamp, symbol, funding_rate)`, both with `UNIQUE (timestamp, symbol)` for idempotency. Created inline via `ensure_tables()` — same policy as `coinglass_liquidations`, not added to `collectors/db.py:SCHEMA_SQL`.
- **Hobbyist pattern**: single request per `(coin, endpoint)` with `startTime`/`endTime` passed defensively but filtered client-side — API ignores them and returns ≤1000 buckets. 2.5s sleep between requests; full run is ~60 requests ≈ 3 min including funding combo probes.
- **Field-name safety**: the first record of each endpoint is always dumped as pretty JSON (via `_probe_dump`) so real field names are visible without `--verbose`. Parsers (`build_oi_rows`, `build_funding_rows`) use a `_pick_float` helper with multi-key fallbacks covering common variants (`close`/`c`/`openInterest`/`aggregated_open_interest_usd` for OI close; `close`/`c`/`fundingRate`/`rate` for funding). If all fallbacks miss, inserts write `0` — easy to spot in the summary and patch.
- **Flags**: `--days` (1–365, default 180), `--coin <BTC>` (single-coin probe), `--verbose`, `--skip-oi`, `--skip-funding`. PEPE falls back to `1000PEPE` automatically, same as the liquidations backfill.

**First run (2026-04-14) outcome:**
- `coinglass_oi`: 10 × 1000 = 10,000 rows, range **2025-10-30 → 2026-04-14** (167 days, hit the 1000-bucket Hobbyist cap as predicted).
- `coinglass_funding`: 10 × 540 rows = 5,400 rows, range **2025-10-17 → 2026-04-14** (full 180 days, 3 buckets/day at h8).
- Funding combo that won on first try for all 10 coins: **`oi-weight-history@h8`**. Fallbacks (vol-weight-history, h4) never had to trigger.
- OI response fields: `open/high/low/close` as **strings** (e.g. `"74879897315"`). Funding response fields: `open/high/low/close` as **strings** (e.g. `"0.003537"`). `_pick_float` parses both cleanly via `float(str)`.
- **Funding-rate unit caveat**: values like BTC `close="0.003537"` and `high="0.007162"` in Oct-2025 look like decimal rates per 8h period (≈0.35% per 8h), not percentage points. Binance typically returns ~0.0001 (= 0.01%) in calm markets — these numbers are ~30× that, consistent with the late-2025 bull funding spike. Double-check units before using in a signal (compare one day's `coinglass_funding.funding_rate` × 3 × 100 against the known Binance daily rate for the same day). Column is stored as-returned.

Troubleshooting notes: the original `FUNDING_PATHS` guess (`oi-weight-ohlc-history`, `aggregated-history`) 404'd — CoinGlass funding uses `oi-weight-history` / `vol-weight-history` (no `-ohlc-`), and `aggregated-history` is liquidations-and-OI-only. Fixed before the successful run.

## Session L3b-2 — Combo Signal Backtest

Motivation: standalone signals have been weak — L/S ratio ≈ 50/50 (L2), single long-flush z>2.0 only passed walk-forward for SOL (L3, 5 altcoins failed). One signal catches both real capitulations and noise; combining complementary filters (OI drop, price drawdown, cross-coin breadth, normalized-to-OI scale, funding, volume) should isolate real capitulations. Unblocked by L3b-1 (coinglass_oi + coinglass_funding).

New script `scripts/backtest_combo.py` — reuses L2/L3 helpers, writes no new DB tables, runs fully offline against existing coinglass_* data plus on-the-fly Binance klines.

- **Reused (imports, do not reimplement)**:
  - `backtest_liquidation_flush.load_liquidations` + `compute_signals` — gives long/short z-scores, total_vol, price, forward returns (90-bar z-score window, matches L2).
  - `walkforward_h1_flush.split_folds(index, n_folds)` — fold boundary helper for phase 4.
  - `collectors.config.COINS`, `binance_ccxt_symbol`, `collectors.db.init_pool` / `get_conn`.
- **Written fresh in this script**:
  - `fetch_klines_4h_ohlcv(ccxt_symbol, since_ms)` — L3's `fetch_klines_4h_ohlc` drops volume, but `volume_zscore` is needed for `flush_volume_spike`. Mirrors the OHLC paginated loop and keeps the `volume` column.
  - `load_oi(symbol)` / `load_funding(symbol)` — simple SELECT wrappers returning UTC-indexed DFs.
  - `_try_load_with_pepe_fallback` — PEPE symbol in coinglass_* tables is "PEPE" if the primary backfill request succeeded, "1000PEPE" otherwise (mirror of the backfill pattern). Loader tries both and uses the first non-empty result.
  - `build_features` — merges liquidations + OI + funding + OHLCV into one 4H-indexed DF per coin. Adds `oi_change_1`, `oi_change_6` (pct_change of open_interest), `liq_oi_ratio` = total_vol / oi, `liq_oi_zscore` (90-bar, matches L2), ATR(14, shifted +1), `volume_zscore`, `drawdown_24h` = `price.pct_change(6) * 100` (cumulative 24h pct change, past-looking), `funding_rate` + `funding_extreme` (abs > 5e-4) with h8→4H ffill, `long_vol_zscore_prev` for `double_flush`. All reindex operations use ffill (no look-ahead) since CoinGlass and Binance klines share the 00/04/08/12/16/20 UTC grid.
  - `compute_cross_coin_features` — single pass across all 10 coins: `n_coins_flushing[t] = (z_wide > 1.5).sum(axis=1)` inclusive of self, `market_liq_total[t] = sum total_vol across coins`. Per-coin DFs get both columns merged back in on index.
  - `apply_combo` / `test_combo` — boolean-mask combo engine supporting `>, <, >=, <=, ==`. NaN → False (missing features silently don't fire). Per-combo metrics at h ∈ {4,8,12,24}: N, win%, avg%, annualized Sharpe; skip a holding period when N<5 (same rule as L2's `backtest_signal`).
- **9 combos (fixed thresholds, pre-declared — no in-sample tuning)**: `baseline_flush` (z>2.0, L2 sanity), `capitulation` (z>1.5 + oi_change_6<-3 + drawdown_24h<-3), `normalized_flush` (liq_oi_zscore>2.0), `market_flush` (z>1.0 + n_coins_flushing>=4), `double_flush` (z>1.5 + prev z>1.0), `flush_extreme_funding` (z>1.5 + funding_rate<-3e-4 — note CoinGlass funding units are per-period decimals, see L3b-1 caveat), `full_capitulation` (z>1.5 + oi_change_6<-2 + drawdown_24h<-2 + n_coins_flushing>=3), `normalized_market` (liq_oi_zscore>1.5 + n_coins_flushing>=3), `flush_volume_spike` (z>1.5 + volume_zscore>1.5).
- **Execution flow**: load all 10 coins → inject cross-coin features → print per-coin combo table (Signals / →4h / →8h / →12h / →24h) → rank combos globally by pooled Sharpe @ h=8 (requires N≥5 to rank) → portfolio summary for winner (per-coin breakdown, TOTAL row, frequency, monthly estimate) → 4-fold walk-forward (fixed thresholds; fold 0 = train baseline, folds 1–3 = OOS; skip if pooled N<30; PASS = ≥2/3 OOS folds with positive Sharpe AND pooled OOS Sharpe>1.0) → sanity check (SOL baseline_flush h=8 should match L2 numbers).
- **ALL 10 coins tested** (not just the L3 altcoin subset): BTC/ETH failed standalone flush but the `capitulation` / `full_capitulation` combos gate on additional filters that may surface edge on large-caps.
- **Ranking holding period**: h=8 (L2 consensus winner for flush-only). The per-coin table still shows all four periods so other h values are inspectable.
- **Interpretation rubric (per spec)**: EDGE if any combo has pooled Sharpe>2.0 AND Win%>60 AND N>30 AND walk-forward ≥2/3 OOS positive. NO EDGE if all combos hover at 50% win rate, or N<10 per coin, or edge only on 1 coin.

Run: `.venv/bin/python scripts/backtest_combo.py | tee analysis/combo_L3b.txt`. Requires `coinglass_oi` and `coinglass_funding` populated (L3b-1). No new DB writes — pure offline analysis.

## Session L4 — Paper Trading Bot (market_flush)

First live signal deployment. L3b-2's `market_flush` combo (422 trades / 60.7% win / Sharpe 5.60 / 3/3 OOS folds positive) moved off the backtest into a real-time loop that fires every 4H, simulates LONG entries, and tracks equity in a JSON state file. No real money — paper only, minimum 2 weeks before considering live.

New `bot/` package (5 modules, all reuse collectors infrastructure):

- **`bot/config.py`** — `BotConfig(Config)` subclasses `collectors.config.Config`, inheriting DB/Telegram/CoinGlass env vars and the `LIQ_` prefix for free. Adds bot-specific fields with hardcoded defaults (signal thresholds locked from L3b-2: `z_threshold_self=1.0`, `z_threshold_market=1.5`, `min_coins_flushing=4`, `z_lookback=90`, `holding_hours=8`; risk: `max_loss_pct=5.0` (unleveraged price), `max_positions=5`; paper: `initial_capital=1000.0`, `position_size_pct=10.0`, `leverage=3.0`). `get_bot_config()` is `@lru_cache`'d and returns `BotConfig`.
- **`bot/signal.py`** — `SignalComputer`:
  - `fetch_recent_liquidations(session, coin, n_bars=100)` → hits `https://open-api-v4.coinglass.com/api/futures/liquidation/aggregated-history?interval=h4` with the same `CG-API-KEY` header, `exchange_list` param, and PEPE→1000PEPE fallback as `scripts/backfill_coinglass.py`. Parses `aggregated_long_liquidation_usd` / `aggregated_short_liquidation_usd` (with `longVolUsd`/`shortVolUsd` fallbacks for older response shapes). Side-effects rows into `coinglass_liquidations` via `execute_values INSERT ... ON CONFLICT DO NOTHING` so the table stays fresh between backfills.
  - `compute_z_scores(df)` — mirrors `scripts/backtest_liquidation_flush.py:116-119` byte-for-byte (default `.rolling(90).std()` ddof, no `min_periods` override). Any drift here will invalidate the L3b-2 backtest parity.
  - `check_market_flush(session)` — fetches all 10 coins serially with a 2.5 s sleep between requests (respects CoinGlass 30-req/min Hobbyist limit). Applies a **freshness gate**: the last bar's timestamp must be `>=` `floor_4h(now) − 4h` (the most-recent fully-closed 4H bucket). Only strictly *older* bars flip `fetch_failed=True` — newer bars (e.g. the current in-progress 4H bucket when CoinGlass updates mid-window) are accepted, since their only risk is a slightly lower z-score from partial accumulation (biased toward not firing, which is safe). NaN z-scores are coerced to 0.0 only at `iloc[-1]` (never mutates the full series). Returns `{is_market_flush, fetch_failed, n_coins_flushing, entry_coins, all_z_scores, timestamp}`.
- **`bot/paper_executor.py`** — `PaperExecutor`:
  - **State schema** (UTC ISO-8601 throughout): `{capital, positions[], closed_trades[], equity_history[], last_summary_date}`. Position rows carry `margin_usd` and `notional_usd` **explicitly** (not inferred) so the formula stays auditable. `last_summary_date` guards against double-sending the daily summary across restarts.
  - **Atomic save**: writes to `state/paper_state.json.tmp` then `os.replace()` — survives SIGKILL mid-write. `.gitignore` excludes both `paper_state.json` and its `.tmp` sibling.
  - **Entry price**: live ccxt Binance futures ticker (`fetch_ticker(binance_ccxt_symbol(coin))["last"]`) at decision time (per user choice — NOT the 4H bar close). Accept ~5 min drift vs the backtest in exchange for realistic paper→live transition behavior.
  - **Exit**: **time-based**, not bar-countdown. `exit_due = entry_time + timedelta(hours=8)` stored at open; `check_positions()` closes when `now >= exit_due` (reason `"timeout"`). Time-based survives systemd restarts / skipped cycles; countdown does not.
  - **Catastrophe SL**: `pnl_pct_price <= -max_loss_pct` (unleveraged price drop) → reason `"sl_hit"`. A −5% price move at 3× lev = −15% margin; matches the backtest `max_loss` column semantically.
  - **P&L formula (do not change without coordinating with backtest)**:  `pnl_pct = (exit − entry) / entry * 100` (unleveraged price move, directly comparable to backtest `return_8h`);  `pnl_usd = pnl_pct / 100 * notional_usd` (leverage applied to dollars only). Capital updates only on close — margin is not debited on open in the paper model.
  - `get_summary()` returns equity, total/daily trade counts, win rate, open-position count.
- **`bot/scheduler.py`** — `async main()` runs an infinite loop aligned to the 4H UTC grid + 5 min buffer (00:05, 04:05, 08:05, ...). Each cycle:
  1. `check_positions()` — runs FIRST so exits happen even when CoinGlass is down.
  2. `check_market_flush()` — signal eval.
  3. If `fetch_failed` → log + skip entries. If `is_market_flush` → open up to `max_positions − open_count` positions, highest-z first, deduping against already-held coins (checks both `state.positions` and this cycle's newly-opened list).
  4. Daily summary at the first cycle of each UTC day (`now.hour < 4` and `last_summary_date != today`).
  5. Atomic save, then `next_wake_ts()` recomputes wake target from wall clock each iteration (no accumulated sleep drift).
  - Unhandled exceptions in `run_cycle` are logged + Telegram-alerted, then the loop continues to the next wake. Telegram failures inside the error handler are swallowed so alerts can never kill the loop.
- **`bot/alerts.py`** — thin wrappers over `collectors.alerts.send_alert(cfg, msg)` (reused, not re-implemented). Five HTML-formatted message builders: `notify_startup`, `notify_market_flush`, `notify_opened`, `notify_closed`, `notify_daily_summary`, plus `notify_error` for the main-loop exception path.

**`scripts/test_paper_bot.py`** — offline integration test, standalone (no pytest dependency, matches `scripts/test_collectors.py` pattern):
1. **Z-score parity** — synthesizes a 120-row `long_vol_usd` DF, runs `SignalComputer.compute_z_scores`, and compares element-wise via `pd.testing.assert_series_equal` against the inline L2 formula. Guards against accidental drift in the rolling-window parameters.
2. **State round-trip** — `PaperExecutor` pointed at a `tempfile.TemporaryDirectory`, patches `get_current_price` via `unittest.mock.patch.object(..., autospec=True)` to avoid live ccxt, opens 2 positions → saves → reloads → closes one via forced `exit_due` in the past (verifies `"timeout"` reason and exact P&L) → triggers the catastrophe SL on the other via a −6% mock price move (verifies `"sl_hit"` reason).
3. **Signal end-to-end** — monkeypatches `SignalComputer.fetch_recent_liquidations` to return canned DFs whose last bar is aligned to `floor_4h(now) − 4h` and whose z-scores are crafted per coin (4 coins at z≈2.0, 1 at z≈1.2, 5 at z≈0.3). Asserts `n_coins_flushing=4`, `is_market_flush=True`, `entry_coins = {BTC, DOGE, ETH, LINK, SOL}`. A second leg uses a stale index (last bar 4h too old) to verify `fetch_failed=True` gating.
4. **Optional**: live CoinGlass smoke test (fetches BTC), skipped if `LIQ_COINGLASS_API_KEY` is empty.

All 19 assertions pass locally on 2026-04-14.

Run: `.venv/bin/python scripts/test_paper_bot.py` (exit 0 on all-pass).

**`systemd/liq-paper-bot.service`** — `Type=simple` + `Restart=always` + `RestartSec=30`, `ExecStart=.venv/bin/python -m bot.scheduler`, mirrors `liq-hl-websocket.service`. Not enabled by default — add manually after first VPS deploy:

```bash
cd ~/liquidation-bot && git pull
mkdir -p state
.venv/bin/python scripts/test_paper_bot.py
sudo cp systemd/liq-paper-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now liq-paper-bot.service
sudo journalctl -u liq-paper-bot -f
```

Monitoring cheatsheet:

```bash
# Equity + trade count
python3 -c "import json; s=json.load(open('state/paper_state.json')); \
    print(f'Equity: \${s[\"capital\"]:.2f}, closed: {len(s[\"closed_trades\"])}, open: {len(s[\"positions\"])}')"

# Last 4H CoinGlass row per coin
psql -d liquidation -c "SELECT symbol, MAX(timestamp) FROM coinglass_liquidations GROUP BY symbol ORDER BY 2 DESC;"
```

**Things to watch in the first 2 weeks**:

- Paper win-rate at N≥20 trades should sit within ~10 pp of the backtest 60.7%. A sustained <50% over 30+ trades = investigate before extending.
- The `fetch_failed=True` rate. If CoinGlass's 4H update is routinely >5 min late, the freshness gate will suppress signals — the 5-min buffer in `next_wake_ts()` may need to grow.
- The `coinglass_liquidations` table now gains fresh rows every 4H as a side effect of the bot running — previously this table only grew during explicit backfills.

**Do NOT**:

- change the signal definition, thresholds, holding period, or coin list (locked to L3b-2).
- drop the freshness gate or the fail-stop on partial fetches.
- "improve" P&L to use a leveraged `pnl_pct` (breaks backtest parity).
- add ATR TP/SL, trailing stops, or live execution — those are separate future sessions.

## Session L5 — Telegram Command Bot

Motivation: L4 only emits a daily summary at 00:05 UTC. Between those there is no way to ask "how are we doing?" without SSHing into the VPS. Goal: a second long-running service that polls Telegram for `/status`, `/pnl`, `/trades`, `/positions`, `/config`, `/health`, `/help` and replies with an on-demand view of paper-bot state. Completely independent from `liq-paper-bot` — a crash here cannot kill the trading loop.

New `telegram_bot/` package (11 modules):

- **`telegram_bot/app.py`** — entrypoint (`python -m telegram_bot.app`). `build_dispatcher(cfg, limiter)` returns the `async dispatch(msg)` handler wired to `poll_updates`. For commands in `NEEDS_LOADING` (`/status`, `/pnl`, `/trades`, `/positions`, `/health`) it sends `⏳ Loading…` first, then `editMessageText` with the real reply — trims perceived latency for commands with I/O. Wraps each handler in `asyncio.wait_for(..., timeout=cfg.command_reply_timeout_s=15.0)` and catches all exceptions so a handler bug cannot crash the polling loop. Exception messages are truncated to 300 chars and escaped — no stack traces leak to Telegram.
- **`telegram_bot/config.py`** — `TelegramBotConfig(BotConfig)` inherits `.env` loading, `telegram_bot_token`, `telegram_chat_id`, and all L3b-2 signal thresholds. Adds `poll_timeout_s=30` (Telegram max long-poll), `poll_client_timeout_s=40` (must > poll_timeout), `command_reply_timeout_s=15.0` (bumped from the original 10s to accommodate `/health`'s 4 parallel pings + systemd subprocess), `position_price_timeout_s=2.0` (per-coin ccxt bound in `/positions`), `rate_limit_window_s=5.0`.
- **`telegram_bot/registry.py`** — frozen `StrategyEntry` dataclass + module-level `REGISTRY` with 3 entries: `4h` (live, `state_file=state/paper_state.json`, `systemd_unit=liq-paper-bot.service`, `holding_hours=8`), `2h` (stub, all fields `None`), `1h` (stub). `load_executor(entry, cfg)` returns `None` for non-deployed entries or constructs a `PaperExecutor` pointed at `entry.state_file` via `cfg.model_copy(update={...})` so the shared BotConfig stays immutable. `find_entry("4h"|"4H"|"4")` normalizes case + trailing "h". When 2H/1H ship, flip on `state_file` + `systemd_unit` + `holding_hours` — no other code change needed in the telegram bot.
- **`telegram_bot/telegram_api.py`** — raw `aiohttp` wrappers for `sendMessage` / `editMessageText` / `getUpdates`. No dependency on python-telegram-bot or aiogram. `escape_md(s)` is the single source of truth for MarkdownV2 escaping — covers all 18 specials (``_*[]()~`>#+-=|{}.!`` plus `\`). Static MarkdownV2 syntax (``**bold**``, ` ```code``` `) is composed AROUND the escaped body, never inside.
- **`telegram_bot/polling.py`** — `async poll_updates(cfg, handler)` maintains `offset = last_update_id + 1` in memory. Filters to `message.text.startswith("/")` AND `str(message.chat.id) == cfg.telegram_chat_id`. Any other chat is silently dropped with a single INFO log line (`ignored update from chat <id> (not authorized)`). Network errors → log + `sleep(poll_error_backoff_s=5)` + retry. The loop only terminates on task cancellation. `_is_authorized(msg, chat_id)` is factored out so `scripts/test_telegram_bot.py` can exercise it directly.
- **`telegram_bot/rate_limit.py`** — per-chat `dict[chat_id, last_monotonic]`. `check(chat_id) -> (allowed, retry_after_s)`. `allowed=True` records the tick; `allowed=False` does NOT update the tick (so spam doesn't extend the window). Accepts an injectable `clock` callable → deterministic tests.
- **`telegram_bot/pnl.py`** — pure read-only aggregations over `PaperExecutor.state`:
  - `pnl_today(closed_trades, initial_capital, now=None)` — sum of `pnl_usd` for trades whose `exit_time.date() == now.date()` UTC. Percent is vs `initial_capital` (stable denominator matching `notify_daily_summary`).
  - `pnl_total(equity, initial_capital)` — `(equity - initial, pct_of_initial)`.
  - `equity_by_day(equity_history, initial_capital, days=7, now=None)` — one `(date, end_of_day equity)` per UTC day for the last `days` days. Days with no equity change carry the previous known value forward. Days before the first recorded entry fall back to `initial_capital`.
  - `sharpe_ratio(closed_trades, holding_hours, min_trades=10)` — annualized Sharpe using sample std (ddof=1) matching pandas default, so the printed number is directly comparable to `scripts/backtest_liquidation_flush.py` and L3b-2 tables. Returns `None` below `min_trades`.
  - `best_worst_trade` / `win_rate` — trivial.
- **`telegram_bot/formatters.py`** — 8 pure message builders (`format_status`, `format_pnl`, `format_pnl_not_deployed`, `format_trades`, `format_positions`, `format_config`, `format_health`, `format_help`) plus small utilities (`format_unknown`, `format_usage_trades`, `format_rate_limited`, `format_loading`, `format_error`). Output is MarkdownV2 clamped to 4000 chars (under Telegram's 4096 with trim-marker headroom). Long tables use fenced ``` text ``` blocks so ASCII dividers (`|`, `-`, `.`) inside don't need escaping. `_sparkline(values)` maps a list of floats to `▁▂▃▄▅▆▇█` — unicode block chars are NOT in the MD2 escape list, so they render inline safely (asserted by a test).
- **`telegram_bot/health.py`** — lazy-tolerant health primitives:
  - `check_systemd_unit(unit)` — `systemctl is-active` + `systemctl show -p ActiveEnterTimestamp`. Returns `{state: "unknown", uptime: None}` when `systemctl` isn't on PATH (Darwin dev box). Parses both `'Tue 2026-04-14 12:00:00 UTC'` and naked-timestamp formats; elapsed → `"4h 12m"`.
  - `recent_errors(unit, hours=1)` — best-effort `journalctl -p err --no-pager`. Drops the `-- Logs begin at ...` banner. `[]` on systems without journalctl.
  - `ping_endpoint(session, spec, timeout=5.0)` — `(name, ok, ms)`. `ping_all()` runs the 4 API endpoints in parallel via `asyncio.gather`.
  - **Endpoints pinged** (all public, no auth needed): Binance `fapi/v1/ping`, CoinGlass `futures/supported-coins`, Hyperliquid `POST /info {"type":"meta"}`, Bitget `api/v2/public/time`. Bitget is pinged even though the repo has no Bitget trading integration — it's a liveness probe on the data source that CoinGlass aggregates from.
  - `host_stats()` — `os.getloadavg()` → CPU %, `shutil.disk_usage("/")` → disk %, `/proc/meminfo` → MemTotal / MemAvailable (Linux only; returns None on Darwin so the formatter prints `—`).
- **`telegram_bot/handlers.py`** — 7 async command handlers, each `async def handle_X(ctx: HandlerContext) -> str`. Handlers return MarkdownV2 strings; they do NOT send Telegram messages directly. `HandlerContext(cfg, chat_id, args, message_id)`.
  - `handle_status` — iterates `REGISTRY`, loads each executor, gathers `pnl_today` + `pnl_total` + `summary["open_positions"]` + systemd state + last-cycle timestamp derived from `os.path.getmtime(entry.state_file)` (scheduler calls `_save_state` every cycle regardless of trades, so mtime is a reliable heartbeat). Each entry wrapped in its own try/except; one broken state file renders `❌ error: …` without killing the rest.
  - `handle_pnl` — per-strategy; returns `format_pnl_not_deployed` for stubs.
  - `handle_trades [4h|2h|1h] [N]` — default `strategy=4h, N=10`, clamps N ∈ [1, 50]. Unknown arg → `format_usage_trades`.
  - `handle_positions` — aggregates across all deployed strategies. Fetches current prices in parallel via `asyncio.gather([asyncio.wait_for(asyncio.to_thread(ex.get_current_price, coin), timeout=2.0), ...])`. Per-coin failure → row renders `Current: —, Unrealized: —` rather than crashing the whole command. Computes estimated LONG liquidation as `entry * (1 - 1/leverage)` (matches `hl_snapshots.py` estimation style).
  - `handle_config` / `handle_health` / `handle_help` — trivial.
  - `parse_command("/trades@botname 4h 5")` → `("/trades", ["4h", "5"])`. Strips the optional `@botname` that Telegram appends in group chats.
- **`scripts/test_telegram_bot.py`** — standalone integration test (no pytest), matches `scripts/test_paper_bot.py` style. 71 assertions across 7 blocks:
  1. MarkdownV2 escape — every special, decimal round-trip, sparkline passthrough, empty-string.
  2. Formatters — all 8 builders, including `format_trades` with N>limit (omitted-rows marker), `format_status` with all 4 states (active/stopped/not_deployed/error), sparkline count and endpoints.
  3. PnL aggregations — `pnl_today` today-vs-yesterday split, `equity_by_day` carries prior-day value, `sharpe_ratio` matches a hand-computed value to 1e-6.
  4. Rate limiter — window enforcement + per-chat independence (via injectable clock).
  5. Registry — `find_entry` case + trailing-h normalization, `load_executor` with tempdir state, corrupt-file recovery.
  6. Dispatcher + handlers — `AsyncMock` stand-ins for `send_message` / `edit_message` / `systemctl` / `ping_all`; patches `PaperExecutor.get_current_price` to avoid live ccxt. Exercises `/help` (direct send, no loading), `/status` (loading + edit), `/trades 4h 5` (arg parse + rendering), `/trades 2h` (stub path), `/trades xyz` (usage error), `/positions` (mock prices), `/config`, `/health`, unauthorized chat via `_is_authorized` directly, rate-limited second call, handler-crash isolation.
  7. Edge cases — `/positions` with a `get_current_price` that raises → rows show `Unrealized: —` and the command still completes.
  Mocking note: tests must patch both `telegram_bot.handlers.REGISTRY` AND `telegram_bot.registry.REGISTRY` because `find_entry` in registry.py consults the latter and bypasses the former.
- **`systemd/liq-telegram-bot.service`** — clone of `liq-paper-bot.service`. `Type=simple` + `Restart=always` + `RestartSec=30`, `ExecStart=.venv/bin/python -m telegram_bot.app`, `SyslogIdentifier=liq-telegram-bot`. Not enabled by default.

**Deploy (manual, after `git pull`)**:

```bash
cd ~/liquidation-bot && git pull
.venv/bin/python scripts/test_telegram_bot.py       # expect PASS: 71 | FAIL: 0
sudo cp systemd/liq-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now liq-telegram-bot.service
sudo journalctl -u liq-telegram-bot -f
```

Then in the Telegram chat that matches `LIQ_TELEGRAM_CHAT_ID`:

- `/help` — list of commands.
- `/status` — all three strategy slots; 2H / 1H render `⚪ not deployed`.
- `/health` — `liq-paper-bot`, `liq-telegram-bot`, 4 API pings, host stats, last-hour errors.

**Do NOT**:

- add trade-mutating commands (`/close`, `/halt`, `/kill`, `/deposit`). Paper bot is read-only over Telegram. Any live-execution work belongs in a separate session with its own threat model.
- drop the `chat_id` authorization gate in `polling.py:_is_authorized`. The bot has no other access control.
- re-use `collectors.alerts.send_alert` — it hard-codes `parse_mode: HTML` and does not support `editMessage`, which the loading→final reply pattern needs. `bot/alerts.py` stays on HTML for outbound notifications; `telegram_bot/` is MarkdownV2 only.
- change the `command_reply_timeout_s` default below 15s without first profiling `/health` on the VPS — the 4 parallel pings + systemd subprocess + aiohttp DNS can approach 5-8s under load.
- add a second Telegram-library dependency. The raw aiohttp wrappers are deliberately minimal; adding python-telegram-bot or aiogram would roughly double the install size of the venv and introduce transitive deps for a feature we already have.

**Known limitations**:

- `offset` is in-memory only. After a restart, Telegram may replay up to 24h of buffered commands. For `/status`-style reads this is harmless. If we ever add mutating commands, persist `offset` to disk.
- On Darwin dev boxes without `systemctl`, `/status` shows systemd state as `unknown` and still renders `active` in the strategy chip (we treat `unknown == active` for display purposes so local dev is readable). On the VPS, `systemctl` is always present.
- Long-poll means the bot holds one HTTP connection open at all times. If the VPS has a strict NAT timeout < 40s, lower `poll_timeout_s` accordingly.

## Session L6 — LiqMapAnalyzer (Liquidation Cluster Magnet Effect)

Motivation: the `hl_liquidation_map` table (15-min snapshots, ~13 April 2026+) records per-coin liquidation volumes at each price level. Hypothesis: price has a tendency to move TOWARD large liquidation clusters — large SHORT-liq clusters above price attract price upward, large LONG-liq clusters below attract price downward. This session tests the hypothesis offline and emits PASS/FAIL.

### New scripts

- **`scripts/analyze_liq_clusters.py`** — standalone analysis (no new DB tables, no new deps). Steps:
  0. **Schema exploration**: prints `hl_liquidation_map` columns and per-coin row/snapshot counts.
  1. **Data loading**: all `hl_liquidation_map` rows + Binance 1H klines (ccxt, public, paginated) per coin. Uses `current_price` from the snapshot as mid-price (no Binance needed for detection-time price). Klines cached per coin.
  2. **Cluster detection**: for each sampled (snapshot_time, coin) pair, groups price_levels into buckets of width 0.5% of mid_price. Levels above mid → uses `short_liq_usd`, side `"short_liq_above"`. Below → `long_liq_usd`, side `"long_liq_below"`. Buckets whose total USD exceeds a threshold → cluster. Four thresholds tested: $500K, $1M, $2M, $5M.
  3. **Hit-rate check**: for each cluster, checks whether Binance kline high (for above) or low (for below) reached the cluster's bucket_center within 1h / 4h / 8h / 24h.
  4. **Random baseline**: for each real cluster, generates a "phantom" at the same distance from mid_price but on the **opposite** side. Compares hit rates → `magnet_score = cluster_hr / random_hr`.
  5. **Output**: per-threshold tables (cluster count, hit rates, magnet scores), per-coin breakdown at 8h, per-distance breakdown (0-2%, 2-4%, 4-6%, 6%+), and a PASS/FAIL verdict.
  6. **PASS criteria** (ALL must hold for ≥1 threshold): `magnet_score_8h > 1.3` AND `cluster_hit_rate_8h > 50%` AND `total_clusters >= 100`. If clusters < 100 across all thresholds → `INSUFFICIENT DATA` with projected ready date.
  7. **Additional analysis** (if PASS and clusters ≥ 200): cluster-size vs hit-rate correlation, average first-hit horizon, recommended runtime parameters.
  - Sampling: uses every 4th snapshot (configurable via `SNAPSHOT_SAMPLE_INTERVAL`) to manage processing time with dense 15-min data.

- **`scripts/test_liq_analyzer.py`** — 41 offline assertions, 8 blocks. Tests pure functions imported from `analyze_liq_clusters`: `build_buckets`, `detect_clusters`, `check_hit`, `compute_hit_rate`, `compute_magnet_score`, `distance_bucket_label`. No DB/network.

### Pure functions (importable from `analyze_liq_clusters`)

| Function | Purpose |
|----------|---------|
| `build_buckets(rows, mid_price, bucket_pct)` | Group price_level rows into side-classified, pct-distance buckets |
| `detect_clusters(rows, mid_price, threshold, bucket_pct)` | Filter buckets exceeding USD threshold |
| `check_hit(side, cluster_price, future_highs, future_lows)` | Check if price reached cluster level per horizon |
| `compute_hit_rate(results, key)` | Aggregate hit percentage |
| `compute_magnet_score(cluster_hr, random_hr)` | Ratio with zero guard |
| `distance_bucket_label(pct)` | Map % distance to "0-2%"/"2-4%"/"4-6%"/"6%+" |

### `hl_liquidation_map` schema (confirmed from `collectors/db.py`)

| Column | Type | Notes |
|--------|------|-------|
| id | BIGSERIAL | PK |
| snapshot_time | TIMESTAMPTZ | every 15 min |
| coin | TEXT | canonical name (BTC, ETH, PEPE — not kPEPE) |
| price_level | DOUBLE PRECISION | rounded to price_step bucket |
| long_liq_usd | DOUBLE PRECISION | aggregated long liquidation volume at this level |
| short_liq_usd | DOUBLE PRECISION | aggregated short liquidation volume at this level |
| num_long_positions | INTEGER | count of long positions at this level |
| num_short_positions | INTEGER | count of short positions at this level |
| current_price | DOUBLE PRECISION | mid price at snapshot time (from Hyperliquid allMids) |

Note: `coin` stores **canonical** names (via `canonical_coin()`), not HL names. `analyze_heatmap_signal.py` (L3) queries with `hl_coin()` (kPEPE) but only processes altcoins where the mapping is identity, so this has not been a practical issue.

### Run

```bash
# Tests (TDD — written before implementation)
.venv/bin/python scripts/test_liq_analyzer.py    # expect PASS: 41 | FAIL: 0

# Analysis (requires DB with hl_liquidation_map data + internet for Binance klines)
.venv/bin/python scripts/analyze_liq_clusters.py
```

### What to expect on first run

With ~3 days of 15-min snapshots (13–16 April 2026), expect:
- ~280 unique snapshots × 10 coins = ~2800 (snapshot, coin) pairs → ~700 sampled (every 4th).
- Cluster count depends on the USD threshold and market conditions. At $500K threshold, expect hundreds of clusters. At $5M, possibly tens or fewer.
- If `INSUFFICIENT DATA`: script prints projected ready date and collection rate.
- Binance 1H kline fetch takes ~10s per coin (3 days ≈ 72 bars each).

### Do NOT

- Add runtime signal modules (bot/liq_targets.py, etc.) — that is L6b, only if PASS.
- Modify bot/, collectors/, telegram_bot/.
- Change existing DB tables or add new ones.
- Add new dependencies to requirements.txt.

## Session L6b — OI-Normalized Cluster Strength Analysis

Motivation: L6 used absolute USD thresholds ($500K–$5M) identically for BTC (OI ~$30B) and WIF (OI ~$100M). A $1M cluster is 1% of WIF's OI but 0.003% of BTC's — the absolute threshold distorts cross-coin comparisons. L6b normalizes cluster volume to Open Interest per coin, creating a `strength_pct = (cluster_usd / oi_usd) * 100` metric, and builds a (distance × strength) hit-rate matrix.

### What changed from v1 to v2

| Aspect | v1 (`analyze_liq_clusters.py`) | v2 (`analyze_liq_clusters_v2.py`) |
|--------|------|------|
| Threshold | 4 absolute ($500K–$5M) | 1 floor ($500K) + OI normalization |
| Strength metric | None (USD only) | `strength_pct = cluster_usd / oi_usd * 100` → weak/medium/strong/mega |
| Distance buckets | 2% width (0-2%, 2-4%, 4-6%, 6%+) | 1% width (0-1%, 1-2%, ..., 4-5%) |
| Max distance | Unlimited | 5% (further clusters discarded) |
| Matrix | Per-threshold flat table | (distance × strength) matrix with random baseline per cell |
| OI source | None | `coinglass_oi` (preferred, 4H, 167d) → `binance_oi` fallback |
| PASS criteria | magnet>1.3 + hit>50% + N≥100 | Zone: hit>50% + magnet>1.5 + N≥20 per cell |

### New scripts

- **`scripts/analyze_liq_clusters_v2.py`** — standalone analysis (no new DB tables, no new deps). Imports pure functions from v1 (`build_buckets`, `detect_clusters`, `check_hit`, `compute_hit_rate`, `compute_magnet_score`, `compute_future_extremes`, `load_all_liq_map`, `fetch_klines_1h_ohlc`). New pure functions: `compute_cluster_strength`, `classify_strength`, `fine_distance_bucket_label`, `attach_oi_to_snapshots`, `build_strength_matrix`, `find_algorithmic_zones`.

- **`scripts/test_liq_analyzer_v2.py`** — 34 offline assertions, 8 blocks. Tests: OI normalization, strength classification, fine distance buckets, matrix aggregation, insufficient cell filtering, zone detection, empty OI handling, OI staleness via merge_asof.

### Pure functions (importable from `analyze_liq_clusters_v2`)

| Function | Purpose |
|----------|---------|
| `compute_cluster_strength(cluster_usd, oi_usd)` | `(cluster_usd / oi_usd) * 100`, guards zero/NaN OI |
| `classify_strength(pct)` | Map to "weak" (<0.5%) / "medium" (0.5-2%) / "strong" (2-5%) / "mega" (>5%) |
| `fine_distance_bucket_label(pct)` | 1%-width buckets: "0-1%"…"4-5%", "" for ≥5% |
| `attach_oi_to_snapshots(snap_df, oi_df, max_staleness_hours)` | `merge_asof` with 4h tolerance |
| `build_strength_matrix(results, random_results)` | Group into (distance × strength) cells, compute hit rates + magnet scores |
| `find_algorithmic_zones(matrix, min_n, min_hit_8h, min_magnet_8h)` | Filter cells meeting all criteria |

### OI data source

`coinglass_oi` (4H interval, created by `backfill_coinglass_oi.py`): `open_interest` field is aggregated USD across exchanges. Attached to each `hl_liquidation_map` snapshot via `pd.merge_asof` with backward direction and 4h tolerance. Snapshots with no OI within tolerance are skipped. Fallback: `binance_oi.open_interest_usd` (hourly, shorter history).

### Run

```bash
# Tests (TDD — written before implementation)
.venv/bin/python scripts/test_liq_analyzer_v2.py    # expect PASS: 34 | FAIL: 0

# Analysis (requires DB with hl_liquidation_map + coinglass_oi data + internet for Binance klines)
.venv/bin/python scripts/analyze_liq_clusters_v2.py | tee analysis/liq_clusters_v2.txt
```

### Expected outcome with early data

With ~3 days of `hl_liquidation_map` (Apr 13-16) and `coinglass_oi` covering Oct 2025 – Apr 2026, the overlap is only ~3 days. Most matrix cells will have N < 20 → INSUFFICIENT DATA or FAIL. The key value is seeing the pattern in populated cells to determine when enough data will be available. Re-run after 1-2 weeks of collection.

### Do NOT

- Create runtime module `bot/liq_targets.py` — only after PASS verdict.
- Modify bot/, collectors/, telegram_bot/.
- Delete `analyze_liq_clusters.py` (v1) — kept for reference.
- Change existing DB tables or add new ones.
- Add new dependencies to requirements.txt.

## Session L6c — Live CoinGlass OI Collector

Motivation: L6b showed only 39% OI coverage because `coinglass_oi` data stopped at the last manual backfill (2026-04-14 16:00 UTC), while `hl_liquidation_map` continues via live 15-min snapshots. The `merge_asof` with 4h tolerance drops all recent snapshots without matching OI. A live 4H collector keeps `coinglass_oi` and `coinglass_funding` current so L6b can be re-run in 2-3 weeks with ~100% OI coverage.

### New files

- **`collectors/coinglass_oi_collector.py`** — live collector, runs every 4H via systemd timer. Fetches latest OI (h4) and funding rate (h8/h4) from CoinGlass for all 10 coins. Reuses `build_oi_rows`, `build_funding_rows`, `_pick_float`, `ensure_tables`, `insert_oi`, `insert_funding`, and all CoinGlass constants from `scripts/backfill_coinglass_oi.py` (imported, not copied). Takes last 5 bars from each API response (20h of OI, enough to cover a missed cycle). PEPE → 1000PEPE fallback. Logging via `logging` module (matches `binance_collector.py`). Idempotent via `ON CONFLICT DO NOTHING`. Total runtime: ~50-60s (10 coins × 2 endpoints × 2.5s rate limit).

- **`systemd/liq-coinglass-oi.service`** — `Type=oneshot`, mirrors `liq-binance.service`. `ExecStart=.venv/bin/python -m collectors.coinglass_oi_collector`.

- **`systemd/liq-coinglass-oi.timer`** — `OnCalendar=*-*-* 00,04,08,12,16,20:05:00 UTC`. Runs 5 minutes after each 4H bar close (gives CoinGlass time to finalize). `Persistent=true` catches up after downtime.

- **`scripts/test_coinglass_collector.py`** — 29 offline assertions, 7 blocks: `_pick_float` multi-key fallback, `build_oi_rows` parsing, `build_funding_rows` parsing, `fetch_latest_oi` with mocked HTTP + PEPE fallback + tail slicing, `fetch_latest_funding` combo fallback, `_cg_symbols` helper, optional live smoke test (skipped without API key).

### Run

```bash
# Tests
.venv/bin/python scripts/test_coinglass_collector.py    # expect PASS: 29 | FAIL: 0

# Manual one-shot (requires .env with LIQ_COINGLASS_API_KEY + DB)
.venv/bin/python -m collectors.coinglass_oi_collector

# Verify
psql -d liquidation -c "SELECT MAX(timestamp), COUNT(*) FROM coinglass_oi;"
```

### Deploy

```bash
cd ~/liquidation-bot && git pull
.venv/bin/python scripts/test_coinglass_collector.py
sudo cp systemd/liq-coinglass-oi.service systemd/liq-coinglass-oi.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now liq-coinglass-oi.timer
sudo systemctl list-timers | grep liq
```

### Do NOT

- Modify `scripts/backfill_coinglass_oi.py` — it's for manual backfills, still needed for initial historical fill.
- Add CoinGlass liquidations collection here — already handled as side-effect in `bot/signal.py:SignalComputer.fetch_recent_liquidations`.
- Add new dependencies to requirements.txt.

## Session L7 — BinanceExecutor (Live Trading Infrastructure)

Motivation: L4 paper trading validates the market_flush signal. L7 builds real Binance Futures execution for a showcase lead-trader account. This session only builds and tests infrastructure — actual live launch is L9 after paper results confirm edge.

### Config Inheritance Chain

`collectors.config.Config` → `bot.config.BotConfig` → `exchange.config.ExchangeConfig`

ExchangeConfig adds Binance API credentials, showcase account parameters (15x isolated, $35 margin, TP=5%, SL=3%), stricter conviction filter (z>=2.0, n_coins>=5), circuit breakers, and dry-run control.

### New `exchange/` package (6 modules)

- **`exchange/config.py`** — `ExchangeConfig(BotConfig)`. `@lru_cache` singleton via `get_exchange_config()`. Key fields: `binance_api_key`, `binance_api_secret`, `binance_testnet`, `showcase_capital=500`, `showcase_leverage=15`, `showcase_margin_usd=35.0`, `showcase_max_positions=2`, `showcase_tp_pct=5.0`, `showcase_sl_pct=3.0`, `showcase_z_threshold=2.0`, `showcase_min_coins_flushing=5`, `max_daily_loss_usd=100.0`, `max_consecutive_losses=5`, `max_daily_trades=6`, `dry_run=True`.

- **`exchange/binance_client.py`** — `BinanceClient`: authenticated ccxt wrapper over Binance USDM Futures (perpetual swaps, `defaultType="swap"`). Supports dry-run (synthetic fills from public ticker) and testnet (`set_sandbox_mode`). Key methods: `set_leverage` (idempotent, cached per run via `_configured_symbols`), `get_ticker_price` (public), `open_market_long`, `place_tp_order` (`TAKE_PROFIT_MARKET`, `reduceOnly`, `workingType=MARK_PRICE`), `place_sl_order` (`STOP_MARKET`, same params), `close_market`, `cancel_order` (safe on "not found"), `fetch_order`, `fetch_positions`, `fetch_balance`. All amounts use `amount_to_precision`, all prices use `price_to_precision`.

- **`exchange/safety.py`** — `SafetyGuard`: circuit breakers checked before every entry. Three limits: `max_daily_loss_usd`, `max_consecutive_losses`, `max_daily_trades`. Daily counters reset on UTC rollover; `consecutive_losses` does NOT reset (spans days). `load_from_state(closed_trades)` reconstructs all counters on startup.

- **`exchange/live_executor.py`** — `LiveExecutor`: real order execution with exchange-side TP/SL. State schema extends PaperExecutor with `amount`, `exchange_order_id`, `tp_price`, `sl_price`, `tp_order_id`, `sl_order_id`. Key behaviors:
  - Entry price = `order["average"]` from market fill (not pre-ticker).
  - TP/SL amount = `order["filled"]` from fill (not pre-computed).
  - State persisted IMMEDIATELY after market fill, BEFORE TP/SL placement (crash recovery).
  - `_close_from_exchange` order: compute P&L → update state → `_save_state()` → `guard.record_trade_result()`.
  - `check_positions`: batch-fetch exchange positions; for gone positions, explicit TP/SL status disambiguation (both-fired → earlier timestamp wins + alert; neither-fired → reason="manual" + alert; API failure → leave in state, retry next cycle).
  - `sync_with_exchange`: reconcile state ↔ exchange on startup. Missing from exchange → close as "sync_missing". Unknown exchange position → alert only, never auto-adopt. Re-place TP/SL for unprotected positions found in state.
  - Same P&L formula as PaperExecutor (`pnl_pct = (exit-entry)/entry*100`, `pnl_usd = pnl_pct/100*notional`).

- **`exchange/scheduler.py`** — Main 4H-aligned loop, mirrors `bot/scheduler.py`. Uses `bot.scheduler.next_wake_ts` (reused, not reimplemented). Conviction filter: `z_threshold_market=1.5` for cross-coin count (unchanged), `showcase_z_threshold=2.0` for per-coin entry (stricter), `showcase_min_coins_flushing=5` (stricter). File lock via `fcntl.flock(LOCK_EX|LOCK_NB)` on state file prevents dual instances. Alerts via `collectors.alerts.send_alert` with custom HTML messages. Run: `python -m exchange.scheduler`.

### Showcase Account Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| Capital | $500 USDT | Fixed |
| Leverage | 15x isolated | Per-position |
| Margin/position | $35 USD | Fixed, not % of capital |
| Max positions | 2 | Simultaneous |
| TP | 5% unleveraged | = 75% of margin at 15x |
| SL | 3% unleveraged | = 45% of margin at 15x |
| Holding timeout | 8h | Same as backtest |
| Entry z threshold | >= 2.0 | Paper uses >= 1.0 |
| Min coins flushing | >= 5 | Paper uses >= 4 |

### Circuit Breakers

| Limit | Value | Reset |
|-------|-------|-------|
| Max daily loss | $100 | UTC midnight |
| Max daily trades | 6 | UTC midnight |
| Max consecutive losses | 5 | On first win (NOT on day rollover) |

### Tests

`scripts/test_exchange.py` — 72 offline assertions, 6 blocks: Config, BinanceClient, LiveExecutor, SafetyGuard, Scheduler integration, LiveExecutor edge cases (both-fired ambiguous, manual close). Run: `.venv/bin/python scripts/test_exchange.py`.

### Deploy (L9, after paper results)

```bash
# 1. Add to .env on VPS:
LIQ_BINANCE_API_KEY=...
LIQ_BINANCE_API_SECRET=...
LIQ_BINANCE_TESTNET=true    # testnet first
LIQ_DRY_RUN=true            # dry run first

# 2. Tests
.venv/bin/python scripts/test_exchange.py

# 3. Dry run
.venv/bin/python -m exchange.scheduler
# Check logs: [DRY_RUN] prefixed operations

# 4. Testnet (real orders on testnet)
# Edit .env: LIQ_DRY_RUN=false, LIQ_BINANCE_TESTNET=true
sudo cp systemd/liq-showcase-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now liq-showcase-bot.service
sudo journalctl -u liq-showcase-bot -f

# 5. Live (after 24h testnet observation)
# Edit .env: LIQ_BINANCE_TESTNET=false
sudo systemctl restart liq-showcase-bot
```

### Do NOT

- Change signal definition, thresholds, or coin list in `bot/signal.py` (locked to L3b-2).
- Close unknown exchange positions automatically — alert only, manual intervention.
- Use `defaultType: "future"` — must be `"swap"` for USDM perpetuals (all 10 codebase usages confirm).
- Use pre-ticker price for entry — always use `order["average"]` from actual fill.
- Use pre-computed amount for TP/SL — always use `order["filled"]` from actual fill.
- Skip state persist between market fill and TP/SL placement — crash recovery requires it.
- Guess exit reason on Binance API failure — leave position in state, retry next cycle.
- Add new dependencies to requirements.txt (ccxt already present).

## Session L8 — Multi-Timeframe Market Flush Backtest

Motivation: the 4H `market_flush` signal (L3b-2) produces only 0–2 trades/day, insufficient for Binance lead trader Smart Filter (≥65% win days, ≥14 trading days/30). Testing the same signal on 1H and 2H intervals to increase trade frequency. CoinGlass Startup tier ($79/mo) was purchased to unlock h1/h2 historical data (180 days, unavailable on Hobbyist).

### New scripts

- **`scripts/backfill_coinglass_hourly.py`** — backfill h1/h2 liquidation + OI data from CoinGlass into new tables. CLI: `--interval h1|h2` (required), `--days 180`, `--coin`, `--verbose`, `--skip-oi`. Includes API probe that fails fast if CoinGlass does not support the requested interval. Creates tables inline: `coinglass_liquidations_{h1,h2}` and `coinglass_oi_{h1,h2}` (same schemas as 4H counterparts). Reuses `CG_SYMBOLS`, `CG_FALLBACKS`, `CG_EXCHANGES`, `REQUEST_SLEEP_S` from `backfill_coinglass.py` and `_pick_float`, `build_oi_rows`, `OI_PATH` from `backfill_coinglass_oi.py`. PEPE → 1000PEPE fallback. Rate limit 2.5s. Idempotent via `ON CONFLICT DO NOTHING`.

- **`scripts/backtest_market_flush_multitf.py`** — backtest `market_flush` combo on h1/h2/h4 with walk-forward. CLI: `--interval h1|h2|h4` (default h4). Tests ONLY `market_flush` (not all 9 combos from `backtest_combo.py`). Key multi-TF adaptations:
  - **`compute_signals_tf(liq_df, price_df, bar_hours)`** — mirrors locked `compute_signals` (L2) with 3 parameterized substitutions: z-score window `int(90 * 4 / bar_hours)` (h1=360, h2=180, h4=90), lookback `int(24 / bar_hours)` (h1=24, h2=12, h4=6), forward returns `hours // bar_hours`. All three maintain the same calendar time as the 4H baseline (15-day z-window, 24h rolling sum).
  - **Holding periods per interval**: h1=[4,8,16,48]h, h2=[8,16,32,48]h, h4=[4,8,12,24]h (L3b-2 baseline). All include 8h for cross-interval ranking.
  - **`build_features_tf`** — mirrors `build_features` with scaled `drawdown_24h` (`pct_change(24/bar_hours)`), `oi_change_24h`, and scaled `_zscore_tf`.
  - **`fetch_klines_ohlcv`** — parameterized timeframe ("1h"/"2h"/"4h") version of `fetch_klines_4h_ohlcv`.
  - **`load_liquidations_tf` / `load_oi_tf`** — load from interval-specific tables (h1/h2) or base tables (h4).
  - Funding loaded from existing `coinglass_funding` (h8, forward-filled to any bar grid).
  - Reuses `apply_combo`, `_metrics_for_trades`, `compute_cross_coin_features` from `backtest_combo.py`, `split_folds` from `walkforward_h1_flush.py`.
  - Walk-forward: 4 folds, PASS criteria = pooled Sharpe > 2.0 AND Win% > 55% AND N ≥ 100 AND ≥2/3 OOS folds positive AND pooled OOS Sharpe > 1.0.
  - At `--interval h4`, prints sanity check vs L3b-2 reference (Sharpe ~5.60, win ~60.7%, N ~422).

- **`scripts/test_backtest_multitf.py`** — 34 offline assertions, 8 blocks:
  1. `compute_signals_tf` parity at h4 — element-wise equality with locked `compute_signals` on synthetic data (`long_vol_zscore`, `short_vol_zscore`, `total_vol`, `return_8h`, `long_vol_24h`, `ratio_24h`).
  2. Z-score scaling at h1 — window=360, first 359 NaN, lookback=24.
  3. Z-score scaling at h2 — window=180, lookback=12.
  4. Forward returns at h1 — `return_4h` shifts 4 bars, `return_48h` shifts 48 bars, `return_8h` shifts 8 bars.
  5. Holding hours map — 8h present in all intervals, h4 matches L3b-2.
  6. Table name derivation — h4 uses base tables, h1/h2 use suffixed tables.
  7. `build_features_tf` drawdown scaling — h1 uses `pct_change(24)`, h4 uses `pct_change(6)`.
  8. Z-score window constants — all intervals give 15 calendar days.

### New tables

| Table | Interval | Schema matches |
|-------|----------|---------------|
| `coinglass_liquidations_h1` | 1H | `coinglass_liquidations` |
| `coinglass_liquidations_h2` | 2H | `coinglass_liquidations` |
| `coinglass_oi_h1` | 1H | `coinglass_oi` |
| `coinglass_oi_h2` | 2H | `coinglass_oi` |

All with `UNIQUE (timestamp, symbol)`, created inline by `backfill_coinglass_hourly.py`.

### Run

```bash
# Tests (offline, no DB/API needed)
.venv/bin/python scripts/test_backtest_multitf.py    # expect PASS: 34 | FAIL: 0

# Backfill h1/h2 data (requires .env with LIQ_COINGLASS_API_KEY + DB)
.venv/bin/python scripts/backfill_coinglass_hourly.py --interval h1 --days 180
.venv/bin/python scripts/backfill_coinglass_hourly.py --interval h2 --days 180

# Verify backfill
psql -d liquidation -c "SELECT symbol, COUNT(*), MIN(timestamp), MAX(timestamp) FROM coinglass_liquidations_h1 GROUP BY symbol;"

# Run backtests
.venv/bin/python scripts/backtest_market_flush_multitf.py --interval h4 | tee analysis/market_flush_h4_reference.txt
.venv/bin/python scripts/backtest_market_flush_multitf.py --interval h2 | tee analysis/market_flush_h2.txt
.venv/bin/python scripts/backtest_market_flush_multitf.py --interval h1 | tee analysis/market_flush_h1.txt
```

### Signal parameters (locked, do NOT change)

- `z_threshold_self` = 1.0 (`long_vol_zscore > 1.0`)
- `z_threshold_market` = 1.5 (for counting `n_coins_flushing`)
- `min_coins_flushing` = 4
- Ranking at h=8 (cross-interval comparison)

### CoinGlass h1/h2 support status (Startup tier)

Startup tier ($79/mo) unlocks h1/h2 intervals on `/api/futures/liquidation/aggregated-history` and `/api/futures/open-interest/aggregated-history`. Empirical findings (16 Apr 2026 probe):

- `startTime` and `endTime` parameters are **silently ignored** — server always returns the latest `limit` bars regardless of window parameters. This holds for both aggregated-history and per-exchange `liquidation/history` endpoints. endTime pagination is therefore impossible.
- `limit` parameter DOES work and the server clamps it to available tier history (~180 days). Tested values on h1: 1000→41d, 3000→125d, 4320→180d, 4500→180d (clamped). This is enough to cover the full Startup-tier window in a single request.
- Strategy: pass `limit = days × bars_per_day` (h1: 4320, h2: 2160, h4: 1080 for 180 days) and receive full history in one call per coin/endpoint. No pagination needed.
- Total requests per full backfill: 10 coins × 2 endpoints = 20 requests ≈ 60s including rate-limit sleeps.

### Walk-forward results

**TBD** — to be filled after VPS runs.

### Expected data volumes

| Interval | Bars/day | 180 days | Single-request limit | Expected rows |
|----------|----------|----------|----------------------|---------------|
| h1 | 24 | 4320 | 4320 | ~4320 |
| h2 | 12 | 2160 | 2160 | ~2160 |
| h4 | 6 | 1080 | 1080 | ~1080 (sibling 4H backfill already single-request on Hobbyist) |

### Do NOT

- Change L3b-2 thresholds (z_self=1.0, z_market=1.5, n_coins>=4) — locked.
- Create live executors for h1/h2 — that is L10 if backtests PASS.
- Modify `bot/signal.py`, `bot/paper_executor.py` — they are for 4H.
- Delete existing `coinglass_liquidations`, `coinglass_oi` tables (they are for 4H).
- Run backfill with `--days > 180` (Startup tier limit).
- Modify `scripts/backtest_liquidation_flush.py` — locked L2 baseline.
- Add new dependencies to requirements.txt.

## Session L10 Phase 1 — Net Position v2 Data Layer

Motivation: `net_long_change` / `net_short_change` per bar give market positioning flow — how much long/short exposure was added or removed at each bar. Hypothesis to be tested in Phase 2: filtering `market_flush` entries by net position extremes improves win rate without crushing trade frequency (Smart Filter requires ≥14 trading days/30, so reducing entries too aggressively defeats the purpose).

### Endpoint findings (16 Apr 2026, Startup tier)

- URL: `https://open-api-v4.coinglass.com/api/futures/v2/net-position/history`
- Required params: `exchange` (not `exchange_list`), `symbol` (pair format `BTCUSDT`, not coin `BTC`), `interval`, `limit`.
- `startTime` / `endTime` silently ignored (same as aggregated-history). `limit` honored up to tier ceiling.
- `limit=4320` on h1 returns 4320 rows covering 180 days in one request — same pattern as aggregated-history after L8 refactor.
- PEPE fallback: `PEPEUSDT` → `400 Not Supported`, `1000PEPEUSDT` → works.
- Response per bar: `time` (ms), `net_long_change`, `net_short_change`, `net_long_change_cum`, `net_short_change_cum`, `net_position_change_cum` — all floats. Units: coin contracts (not USD), but Phase 2 features will normalize to z-scores so units cancel.

### New tables

`coinglass_netposition_h1`, `coinglass_netposition_h2`, `coinglass_netposition_h4` — same schema (timestamp, symbol canonical, exchange, 5 float metrics), `UNIQUE (timestamp, symbol, exchange)` constraint. Created inline by `backfill_coinglass_netposition.py` (no change to `collectors/db.py:SCHEMA_SQL`, matches L8 / L3b-1 pattern).

### Backfill script

`scripts/backfill_coinglass_netposition.py`:
- Single-request strategy (`limit = days × INTERVAL_BARS_PER_DAY[interval]`), reuses `INTERVAL_BARS_PER_DAY` from `backfill_coinglass_hourly.py`.
- Hardcoded `exchange="Binance"` in Phase 1; column kept in schema for future multi-exchange expansion without schema migration.
- Pair mapping: `NETPOS_PAIRS = {coin: f"{coin}USDT"}`, PEPE→1000PEPEUSDT fallback.
- 10 requests per run, ~25s.

### Run

```bash
# Tests (offline + optional live smoke)
.venv/bin/python scripts/test_backfill_netposition.py    # expect PASS: 9 | FAIL: 0

# Backfill all three intervals (10 requests each)
.venv/bin/python scripts/backfill_coinglass_netposition.py --interval h1 --days 180
.venv/bin/python scripts/backfill_coinglass_netposition.py --interval h2 --days 180
.venv/bin/python scripts/backfill_coinglass_netposition.py --interval h4 --days 180

# Verify
psql -d liquidation -c "SELECT symbol, COUNT(*), MIN(timestamp)::date FROM coinglass_netposition_h1 GROUP BY symbol ORDER BY symbol;"
```

### Phase 2 / Phase 3 roadmap (NOT in Phase 1 scope)

- **Phase 2:** `scripts/research_netposition.py` — standalone research script that tests two hypotheses across h1/h2/h4:
  - **H1 Contrarian:** baseline `market_flush` + high `net_short_change` required (logic: shorts pushed price down, then got liquidated, now exhausted).
  - **H2 Confirmation:** baseline `market_flush` + positive `net_long_change` required (logic: someone already bought the dip, confirms real bottom).
  - 2 hypotheses × 3 intervals = 6 backtests + baseline sanity.
  - Output: single report, PASS/FAIL per configuration against L8 criteria (pooled Sharpe > 2.0, Win% > 55%, N ≥ 100, ≥2/3 OOS positive) + extended criteria (trades/day median doesn't drop below 70% of baseline — critical for Smart Filter's 14 trading days/30 requirement).
- **Phase 3 (conditional on Phase 2 PASS):** If any (hypothesis, interval) PASSes, integrate the winning filter into `bot/signal.py` as an opt-in via config flag. Only PASSing configurations ship to live. Rejected hypotheses documented in this CLAUDE.md section as tested-and-rejected so future sessions don't re-explore.

### Do NOT

- Change locked L3b-2 thresholds (z_self=1.0, z_market=1.5, n_coins≥4) in Phase 1, 2, or 3. Net Position is an **additional filter** on top of baseline, not a replacement.
- Modify `bot/signal.py`, `bot/paper_executor.py`, or anything in `exchange/` during Phase 1. Data layer only.
- Add Net Position collection to live 4H/hourly collectors (`coinglass_oi_collector.py`) in Phase 1 — backfill-only until research PASSes.
- Enable multi-exchange aggregation in Phase 1. Single exchange (Binance) is sufficient for hypothesis testing and keeps volume comparable across metrics already collected from Binance (OI, funding, taker).
- Extend `--days` above 180 (Startup tier cap).

## Session L10 Phase 2 — Net Position Research

Research script testing whether net position flow filters improve `market_flush` signal. Matrix: 2 hypotheses × 3 z-thresholds × 3 TF = 18 variants + 1 baseline sanity per interval = 19 backtests (21 rows in the final ranking — 3 baseline reference rows give cross-interval context).

### Hypotheses

- **H1 Contrarian:** `market_flush` AND `net_short_change_zscore > z_netpos` — shorts capitulated, reversion expected.
- **H2 Confirmation:** `market_flush` AND `net_long_change_zscore > z_netpos` — longs confirming bottom, follow-through expected.

### Key design decisions

- **Z-score normalization per-coin** — raw `net_*_change` values span 5 orders of magnitude across coins (PEPE vs BTC), so absolute thresholds are meaningless. Z-score uses `_zscore_tf` (imported from L8), same 15-calendar-day window as baseline (`_z_window`: h4→90, h2→180, h1→360 bars).
- **Cumulative fields unused** — `net_long_change_cum` / `net_short_change_cum` / `net_position_change_cum` are redundant with deltas for a flow filter. Reserved for Phase 3 regime detection if edge requires it.
- **Net Position is ADDITIVE** — filters are `MARKET_FLUSH_FILTERS + [(col, ">", z_netpos)]` so the locked L3b-2 thresholds (z_self>1.0, n_coins>=4) are always preserved verbatim.
- **Walk-forward mandatory** — same 4-fold split as L8 (`split_folds` reused). 3 z-thresholds × 2 hypotheses = 6 filters per interval, so overfit risk is real.
- **Look-ahead guardrail** — variants with pooled OOS Sharpe > 8.0 are flagged MARGINAL (not PASS) and require manual review, mirroring the convention the architect called out during planning.

### New files

- `scripts/research_netposition.py` — standalone research driver. Reuses `build_features_tf`, `compute_signals_tf`, `_zscore_tf`, `_z_window`, `load_{liquidations,oi}_tf`, `load_funding`, `fetch_klines_ohlcv`, `compute_cross_coin_features`, `apply_combo`, `_metrics_for_trades`, `_try_load_with_pepe_fallback`, `split_folds`. Adds `load_netposition_tf`, `build_netposition_features`, `attach_netposition`, `build_hypothesis_filters`, `run_variant`, `run_walkforward`, `evaluate_verdict`, `format_variant_block`, `format_final_ranking`.
- `scripts/test_research_netposition.py` — 12 offline PASS / 15 with optional DB smoke. Four blocks: feature engineering (5), filter application (4), metrics + walk-forward (3), DB smoke (3).

### PASS criteria (all 6 must hold)

Primary (inherited from L8):
1. Pooled OOS Sharpe > 2.0
2. Win% > 55%
3. N trades >= 100
4. >= 2/3 OOS folds positive
5. Pooled OOS Sharpe > 1.0 (formally redundant with #1, documented per spec)

Extended (Smart Filter awareness):
6. trades/day median >= 70% of baseline for same interval — critical for the >= 14 trading days / 30 requirement.

Verdicts:
- **PASS:** all 6.
- **MARGINAL:** primary 5 met, extended #6 failed OR Sharpe > 8.0 (look-ahead suspicion).
- **FAIL:** any primary criterion not met, or walk-forward skipped (N < WF_MIN_TRADES).

### Expected outcomes

- **h4 baseline already PASS** → NetPos filter may strictly dominate (higher Sharpe, trade rate >= 70%) = true PASS, or drop trade rate too far = MARGINAL.
- **h2 / h1 baseline FAIL** → filter may rescue into PASS (feeds into L11 2H/1H executor decision) or confirm FAIL (NetPos insufficient).
- At `--interval h4` the script prints a parity banner comparing observed vs L8-reference Sharpe/Win/N and warns (does not raise) on Sharpe drift > 5%.

### Run

```bash
# Offline tests
.venv/bin/python scripts/test_research_netposition.py                  # 12 PASS

# Full matrix (architect triggers on VPS, ~15-30 min)
.venv/bin/python scripts/research_netposition.py | tee analysis/netposition_research_$(date +%F).txt

# Debug slice
.venv/bin/python scripts/research_netposition.py --intervals h4 --hypotheses H1 --thresholds 1.0
```

### Phase 3 (conditional)

If any (hypothesis, interval, threshold) PASSes with trades/day >= 70 % baseline:
- Add `NET_POSITION_FILTER` opt-in config flag to `bot/signal.py`.
- Integrate the winning `(hypothesis, z_netpos)` into live signal computation.
- Deploy only winning variants to paper trading for a 7-day A/B test.
- Rejected variants documented here with FAIL verdict so future sessions don't re-explore the same combos.

### Do NOT

- Change locked L3b-2 thresholds (z_self=1.0, z_market=1.5, n_coins>=4). Net Position is an **additional filter on baseline**, not a replacement.
- Modify `bot/signal.py`, `bot/paper_executor.py`, or anything in `exchange/` in Phase 2 — research-only.
- Add new DB tables (data layer complete in Phase 1).
- Extend the threshold grid beyond 0.5 / 1.0 / 1.5 without separate plan approval (overfit risk grows quadratically with grid size).
- Recompute baseline numbers from scratch — L8 reference (h4: N=428, Win%=61.0, Sharpe=5.87) is the source of truth for parity checks.
- Mark Sharpe > 8.0 as PASS without manual inspection (look-ahead smell).

## Session L10 Phase 2b — H1_z1.5_h2 Validation

Motivation: Phase 2 identified `H1_z1.5_h2` (N=296, Win% 61.6, pooled OOS Sharpe 5.15, 3/3 OOS positive) as the single stable Net Position filter candidate. But **pooled Sharpe over 3 OOS folds is three observations** — insufficient signal to decide deploy-vs-reject. Three unresolved questions block Phase 3: (1) does H1_z1.5_h2 diversify with the h4 baseline or merely duplicate it? (2) is it stable on a **rolling 30-day** window (Smart Filter operates monthly, not on aggregates)? (3) does a combined 50/50 portfolio show synergy? Phase 2b is a pure read-only analysis layer on top of Phase 2 — no changes to `bot/`, `exchange/`, or any locked script.

### Three validation tests

**Test 1 — Daily-return correlation (diversification):** Pearson correlation between per-day pnl series, plus 4-way overlap breakdown on common active days (both_win / both_lose / h4_win_h2_lose / h4_lose_h2_win). PASS = `corr < 0.5` AND `mixed_pct >= 15%` (diversification benefit, not duplication).

**Test 2 — Rolling 30-day Sharpe stability:** Slide a 30-day window across each strategy's daily pnl series (180d → 151 windows). Per window: skip if `<5` active days (`MIN_ROLLING_WINDOW_ACTIVE_DAYS`) or if `std <= STD_EPS (1e-12)` — the std-guard absorbs floating-point degeneracy on synthetic inputs only (real trade data has continuously-varying `pnl_pct` and therefore never produces exact-constant 30-day slices). Annualized Sharpe = `mean/std(ddof=1) * sqrt(365)`. The report prints `used=N/total  dropped_low_activity_pct` — a warning fires if drop-rate >30% (suggests `min_trading_days` is miscalibrated for the strategy's trade frequency). PASS (each strategy): `min > 0` AND `median > 2.0` AND `>= 60%` windows with Sharpe > 2 AND `>= 50%` windows with win-days >= 65% (Smart Filter monthly condition simulator).

**Test 3 — Combined 50/50 portfolio (synergy):** `combined_usd = 0.5*capital*h4_pct/100 + 0.5*capital*h2_pct/100`. Equity curve -> running-max -> drawdown -> MDD (negative number; "less severe" = "greater"). PASS = `combined_sharpe > max(h4_solo, h2_solo)` AND `combined_mdd > h4_solo_mdd` AND `combined_win_days >= h4_solo_win_days`.

### Recommendation logic

| Condition | Verdict | Next step |
|-----------|---------|-----------|
| Test 2 (h4) FAIL | `ALARM` | Pause Phase 3; investigate baseline |
| Test 1 FAIL or Test 3 FAIL | `REJECT` | Skip to L11 SHORT research |
| Test 1 + Test 3 PASS, Test 2 (h2) PASS | `STRONG_GO` | Phase 3 integration + paper trading |
| Test 1 + Test 3 PASS, Test 2 (h2) FAIL | `WEAK_GO` | Paper trading MANDATORY before live |

### Files

- `scripts/validate_h1_z15_h2.py` — standalone validator. Reuses `_load_coins_for_interval` and `build_hypothesis_filters` from `research_netposition.py`, `MARKET_FLUSH_FILTERS` and `RANK_HOLDING_HOURS` from `backtest_market_flush_multitf.py`, `apply_combo` from `backtest_combo.py`. Pure functions: `extract_trade_records`, `aggregate_daily_pnl`, `compute_correlation_test`, `compute_rolling_sharpe_test`, `compute_combined_portfolio_test`, `recommend`, `format_report`. Entry `main()` loads h4 + h2 dataframes -> extracts per-trade records -> aggregates daily pnl -> runs three tests -> emits recommendation.
- `scripts/test_validate_h1_z15_h2.py` — 14 offline assertions in 5 blocks: daily aggregation (3), correlation & overlap (3), rolling Sharpe (2), combined portfolio (2), edge-case handlers (4 — zero-std skip, all-zero low-activity drop, 4-active-days-threshold boundary, dense-activity zero-drop).

### Trade extraction (key design point)

`run_variant` in `research_netposition.py` returns aggregate-only dicts — no per-trade records. Rather than modify the locked Phase 2 signature, `extract_trade_records` **replicates the internal mask logic** (`apply_combo(df, filters)` -> `df.loc[mask, return_{h}h].dropna()`) while preserving per-trade metadata: `{coin, entry_ts, exit_ts = entry_ts + holding_hours, pnl_pct}`. Parity risk: if this helper ever drifts from `run_variant`'s filter, pooled N/Sharpe would diverge. Mitigation: the report prints observed pooled `(N, Win%, Sharpe)` side by side so drift is visible.

### Run

```bash
# Offline tests (no DB)
.venv/bin/python scripts/test_validate_h1_z15_h2.py       # 14 PASS

# Validation run (requires VPS-populated DB, ~5-10 min)
.venv/bin/python scripts/validate_h1_z15_h2.py | tee analysis/validation_h1_z15_h2_2026-04-17.txt
```

### Do NOT

- Modify `scripts/research_netposition.py` or `scripts/backtest_market_flush_multitf.py` (Phase 2 / L8 locked).
- Change `bot/signal.py`, `bot/paper_executor.py`, or anything under `exchange/` — Phase 2b is research-only.
- Add new DB tables or new deps (`requirements.txt` unchanged).
- Ship to live on `WEAK_GO` without paper trading. Paper is mandatory.
- Interpret Test 3 MDD with "less negative" intuition backwards: `combined_mdd > h4_solo_mdd` means combined drew down LESS than h4 solo (both are <= 0).

## Session L13 Phase 1 — CVD Data Layer

Motivation: After ALARM verdict in L10 Phase 2b (H1_z1.5_h2 correlation 0.76 with h4 baseline → no diversification) a principled new signal class is needed — one that is not another filter on the same `market_flush` substrate. CVD (aggregated Cumulative Volume Delta) shows **aggressive market orders** — who initiated each bar's move, buyers or sellers as aggressors. This is distinct from liquidations (forced exits) and net position (limit-order accumulation), and is the data substrate for future Phase 2 hypotheses on exhaustion and price/CVD divergence.

Phase 1 scope: data layer only — backfill script, schema, tests. NO hypothesis testing (Phase 2).

### Endpoint findings (17 Apr 2026, Startup tier)

- URL: `https://open-api-v4.coinglass.com/api/futures/aggregated-cvd/history`
- Params: `exchange_list=<CG_EXCHANGES>` (multi-exchange aggregation, same set as L8 aggregated-liquidation), `symbol=<COIN_NAME>` (coin-level, not pair format), `interval`, `limit`
- Response per bar: `time` (ms), `agg_taker_buy_vol` (USD), `agg_taker_sell_vol` (USD), `cum_vol_delta` (USD per-bar delta, = buy − sell, despite the "cumulative" name)
- `startTime`/`endTime` silently ignored, same as other aggregated-* endpoints
- `limit = 4320` on h1 → 4320 rows (180 days in one request) — same single-request pattern as L8 aggregated-liquidation and L10 net-position
- PEPE fallback: primary `PEPE` used first; `1000PEPE` reserved as coin-level fallback (mirrors sibling `CG_FALLBACKS`)

### New tables

`coinglass_cvd_h1`, `coinglass_cvd_h2`, `coinglass_cvd_h4` — identical schema. Columns: `timestamp`, `symbol` (canonical coin), `agg_taker_buy_vol`, `agg_taker_sell_vol`, `cum_vol_delta`. `UNIQUE (timestamp, symbol)` + index on `(symbol, timestamp)`. No `exchange` column — CVD is pre-aggregated server-side across the `CG_EXCHANGES` set.

Created inline by `backfill_coinglass_cvd.py` (no `SCHEMA_SQL` change, matches L8/L10 pattern).

### Backfill script

`scripts/backfill_coinglass_cvd.py` — single-request per coin (`limit = days × INTERVAL_BARS_PER_DAY[interval]`), reuses `CG_SYMBOLS` / `CG_EXCHANGES` / `REQUEST_SLEEP_S` / `INTERVAL_BARS_PER_DAY` / `_get_json` / `_pick_float` / `_t` from sibling scripts. 10 requests per run (~25s). PEPE `1000PEPE` fallback on empty primary. Idempotent via `ON CONFLICT (timestamp, symbol) DO NOTHING`.

### Run

```bash
# Tests (offline + optional live smoke)
.venv/bin/python scripts/test_backfill_cvd.py   # expect PASS: 9 | FAIL: 0

# Backfill all three intervals (10 requests each, ~25s)
.venv/bin/python scripts/backfill_coinglass_cvd.py --interval h1 --days 180
.venv/bin/python scripts/backfill_coinglass_cvd.py --interval h2 --days 180
.venv/bin/python scripts/backfill_coinglass_cvd.py --interval h4 --days 180

# Verify
psql -d liquidation -c "SELECT symbol, COUNT(*), MIN(timestamp)::date FROM coinglass_cvd_h1 GROUP BY symbol ORDER BY symbol;"
```

### Phase 2 roadmap (NOT in Phase 1)

Phase 2 will test two hypotheses with **stricter PASS criteria** (lessons from Phase 2b ALARM):

- **H3 CVD Divergence:** `market_flush` AND `cum_vol_delta_zscore > 1.5` — aggressive buyers stepping in despite the flush.
- **H4 CVD Exhaustion:** standalone signal — extreme CVD z-score in one direction for 3+ bars → entry in the opposite direction.

**New PASS criteria (all 5 must hold):**

1. Primary L8 criteria — pooled OOS Sharpe > 2.0, Win% > 55%, N ≥ 100, ≥2/3 OOS folds positive.
2. Rolling 30-day Sharpe — min > 0, median > 2.0, ≥60% windows with Sharpe > 2.
3. Correlation with h4 baseline < 0.5 (mandatory for any new strategy).
4. Combined portfolio Sharpe > max(h4 solo, new solo) — synergy required.
5. Trades/day ≥ 1.5 (absolute floor, not relative to baseline).

No variant deploys without passing all 5. This codifies the L10 Phase 2b lesson: pooled aggregate Sharpe alone is insufficient — diversification, monthly stability, and absolute trade frequency are co-equal gates.

### Do NOT

- Build a live CVD collector in Phase 1 — backfill-only.
- Modify `bot/`, `exchange/`, `telegram_bot/`, or sibling backfill scripts (import-only reuse).
- Add dependencies to `requirements.txt`.
- Run live fetch locally in the planning session — only offline tests (Block 3 skips without key). Full backfill runs on VPS after commit.
- Extend scope (new columns, new indexes, new fallbacks, new hypotheses inside this section) without a separate ExitPlanMode approval.

## Session L13 Phase 2 — CVD Research

Motivation: L10 Phase 2b ALARM (H1_z1.5_h2 correlation 0.76 with h4 baseline → no diversification) justified a principled new signal class, not another filter on the same `market_flush` substrate. CVD (aggregated Cumulative Volume Delta) shows **aggressive market orders** — who initiated each bar's move. This is orthogonal to liquidations (forced exits) and NetPos (passive limit accumulation): CVD captures active positioning at the moment of the flush.

Phase 2 scope: research script + tests only — no changes to `bot/`, `exchange/`, `telegram_bot/`, `collectors/`, or any locked script. Integration (Phase 3) conditional on a passing L13 Phase 2b validation pass (separate session).

### Empirical probe findings (17 Apr 2026, Phase 1 data)

- `coinglass_cvd.cum_vol_delta` is **cumulative since start-of-history**, not per-bar (BTC range −62B to +0.8B, median −39B). Phase 2 ignores it.
- Per-bar delta = `agg_taker_buy_vol − agg_taker_sell_vol` is recomputed on the fly.
- Cross-coin scale spans 5 orders of magnitude (BTC ~40B avg abs vs PEPE ~249M) → **per-coin z-score mandatory** (same pattern as NetPos).
- PEPE fallback (`1000PEPE`) was reserved in `backfill_coinglass_cvd.py` but never triggered — canonical `PEPE` is the stored symbol across all 10 coins. Research script loads CVD directly without `_try_load_with_pepe_fallback`.

### Hypotheses

- **H3 Taker Buy Dominance** (ratio-based): `market_flush AND buy_ratio > threshold`, `buy_ratio = agg_taker_buy_vol / (agg_taker_buy_vol + agg_taker_sell_vol)`. Thresholds: `0.52, 0.55, 0.58`.
- **H4 CVD Delta Divergence** (z-score based): `market_flush AND per_bar_delta_zscore > threshold`, z-window = `_z_window(bar_hours)` (15 calendar days: h4=90, h2=180, h1=360). Thresholds: `0.5, 1.0, 1.5`.

Difference vs NetPos H1/H2: NetPos = accumulated limit orders (passive), CVD = aggressive market orders at the flush bar (active). Possibly one edge exists where the other doesn't.

### PASS criteria (strengthened after L10 Phase 2b ALARM)

**PASS** requires all of:
1. Primary L8 criteria: pooled OOS Sharpe > 2.0, Win% > 55%, N ≥ 100, ≥2/3 OOS folds positive, pooled OOS > 1.0.
2. **Absolute floor**: `trades_per_day ≥ 1.5` (not relative to baseline — Smart Filter needs ≥14 trading days / 30, so we want margin above the 0.5/day minimum).

**MARGINAL**: primary 5 met, but trades/day below 1.5 OR pooled Sharpe > 8.0 (look-ahead smell — manual review required). **FAIL**: any primary criterion missed, or walk-forward skipped.

Correlation vs h4 baseline < 0.5, rolling 30-day Sharpe stability, and combined-portfolio synergy are **deferred to L13 Phase 2b** (mirror of L10 Phase 2b). No PASS/MARGINAL variant ships live without Phase 2b validation.

### New files

- **`scripts/research_cvd.py`** — 18 variants + 3 baselines + walk-forward per interval, emits per-variant blocks, final 21-row ranking, and recommendation block. New helpers: `load_cvd_tf`, `build_cvd_features`, `attach_cvd`, `build_hypothesis_filters`. Reuses L8/NetPos infrastructure: `build_features_tf`, `_zscore_tf`, `fetch_klines_ohlcv`, `load_{liquidations,oi,funding}_tf`, `MARKET_FLUSH_FILTERS`, `run_variant`, `run_walkforward`, `evaluate_verdict`, `format_variant_block`, `format_final_ranking`, `compute_cross_coin_features`, `_try_load_with_pepe_fallback`, `split_folds`. Local wrapper `_format_cvd_variant_block` swaps NetPos's Contrarian/Confirmation description line for a CVD-aware one (buy_ratio vs per_bar_delta_zscore) — the underlying `format_variant_block` only renders `name` (no parsing), so reuse is safe.
- **`scripts/test_research_cvd.py`** — 12 offline PASS (Blocks 1–3) + 3 optional DB smoke (Block 4). Mirrors `test_research_netposition.py` structure.

### CLI

```
--intervals h1,h2,h4        (default all)
--hypotheses H3,H4          (default both)
--thresholds-h3 0.52,0.55,0.58  (buy_ratio, default)
--thresholds-h4 0.5,1.0,1.5     (per_bar_delta_zscore, default)
```

### Variant labels

- `H3_r0.55_h4` — ratio-based (`buy_ratio > 0.55`, interval h4)
- `H4_z1.0_h2` — z-score based (`per_bar_delta_zscore > 1.0`, interval h2)

### Guardrails (from L10 Phase 2 / 2b lessons)

1. **h4 baseline parity check** vs L8 (Sharpe 5.87 / Win 61.0 / N 428) — warn (not fatal) on >5% drift.
2. **Suspicious Sharpe** > 8.0 → auto-demote to MARGINAL; never auto-PASS.
3. **trades/day ≥ 1.5** absolute floor (hard gate — a PASS from `evaluate_verdict` is demoted to MARGINAL when trades/day below this).
4. **Subset monotonicity** covered by offline Block 2.
5. **NaN on zero-denominator** for `buy_ratio` (defensive — real data is positive, but tested).

### Run

```bash
# Offline tests (no DB needed)
.venv/bin/python scripts/test_research_cvd.py                # expect 12 PASS

# Full matrix run (architect trigger on VPS, ~15–30 min)
.venv/bin/python scripts/research_cvd.py | tee analysis/cvd_research_2026-04-17.txt

# Debug slice
.venv/bin/python scripts/research_cvd.py --intervals h4 --hypotheses H3 --thresholds-h3 0.55
```

### Expected outcomes

- Any PASS/MARGINAL → **mandatory** L13 Phase 2b validation (correlation with h4 baseline < 0.5, rolling 30-day Sharpe stability, combined-portfolio synergy) before Phase 3 integration.
- All FAIL → reject CVD filter approach; next candidate: L11 SHORT research.
- Walk-forward results TBD — to be filled in this section after VPS runs.

### Do NOT

- Change locked L3b-2 thresholds (z_self=1.0, z_market=1.5, n_coins≥4). CVD is an **additional filter** on top of baseline, not a replacement.
- Modify `bot/signal.py`, `bot/paper_executor.py`, or anything in `exchange/` in Phase 2 — research only.
- Add new DB tables (Phase 1 data layer complete).
- Extend the threshold grid beyond the 3 defaults per hypothesis without a separate ExitPlanMode approval (overfit risk grows quadratically).
- Mark Sharpe > 8.0 as PASS without manual inspection.
- Ship any PASS/MARGINAL variant to live without completing L13 Phase 2b validation first.
- Re-add the `1000PEPE` fallback for CVD — Phase 1 confirmed canonical `PEPE` stored; fallback is no-op + log clutter.

## Session L13 Phase 3 — CVD Standalone Research

Motivation: L13 Phase 2 (commit `966db3e`) showed CVD **as a filter** over `market_flush` = all variants FAIL — 96-100 % of baseline trades filtered out because flush moments and CVD extremes rarely overlap. The silver lining: CVD carries **orthogonal information** from the liquidation-flush substrate. Phase 3 reframes CVD as a **standalone LONG signal** rather than a filter. Because standalone entries fire at completely different moments than the baseline, low correlation with h4 baseline is implied by construction — directly addressing the L10 Phase 2b ALARM (H1_z1.5_h2 corr=0.76 with h4 baseline).

Scope: LONG only. SHORT stays for L11. Phase 3 is research-only — no changes to `bot/`, `exchange/`, `telegram_bot/`, `collectors/`, or `requirements.txt`. Integration (Phase 4) conditional on a passing L13 Phase 3b validation (separate session).

### Hypotheses

- **H5 — Aggressive Selling Exhaustion (LONG):** `per_bar_delta_zscore < -threshold AND consecutive_negative_delta_bars >= 3`. Thresholds: `1.5, 2.0, 2.5`. Semantics: 3+ consecutive bars of net aggressive selling ⇒ seller exhaustion ⇒ reversion up.
- **H7 — Price-CVD Divergence (LONG):** `price_change_6bars < 0 AND cum_delta_change_zscore > threshold`. Thresholds: `0.5, 1.0, 1.5` (normalized per-coin z-score on 15-calendar-day rolling stddev of `cum_vol_delta.diff(6)`). Semantics: price fell over 6 bars while aggressive-buy flow rose over same window ⇒ smart-money absorption ⇒ reversion up.

Matrix: 2 hypotheses × 3 thresholds × 3 timeframes = 18 variants + 3 baseline context rows (one per interval, labelled `REF`, no PASS/FAIL).

### PASS criteria (all 8 must hold — strengthened after Phase 2b ALARM and Phase 2 CVD-filter reject)

Primary (L8-inherited):
1. Pooled OOS Sharpe > 2.0
2. Win% > 55
3. N ≥ 100
4. ≥ 2/3 OOS folds positive
5. Pooled OOS Sharpe > 1.0 (formally redundant, kept per convention)

Strict (new):
6. **OOS3 (last fold) Sharpe > 0** — freshest period must earn, not just pooled average.
7. **|max_OOS_Sharpe / min_OOS_Sharpe| < 5** when `min < 0` — outlier-fold guard. When all OOS folds > 0, ratio set to `1.0` (sentinel "no concern").
8. **trades/day ≥ 1.5** absolute floor (not relative to baseline — Smart Filter wants 14 trading days / 30, and we want margin above the 0.5/day floor).

Verdicts:
- **PASS** — all 8 met AND pooled OOS Sharpe ≤ `SUSPICIOUS_SHARPE` (8.0).
- **MARGINAL** — primary 5 met but any strict (6–8) fails OR pooled OOS Sharpe > 8.0 (look-ahead smell — manual review required).
- **FAIL** — any primary criterion missed, or walk-forward skipped (N < `WF_MIN_TRADES`).

Correlation vs h4 baseline < 0.5, rolling 30-day Sharpe stability, and combined-portfolio synergy are **deferred to L13 Phase 3b validation** (mirrors L10 Phase 2b). No MARGINAL/PASS variant deploys without Phase 3b.

### Files

- **`scripts/research_cvd_standalone.py`** — standalone-signal research driver. Reuses L8 framework (`build_features_tf`, `fetch_klines_ohlcv`, `load_{liquidations,oi,funding}_tf`, `_z_window`, `MARKET_FLUSH_FILTERS`, `RANK_HOLDING_HOURS`, `WF_FOLDS`, `WF_MIN_TRADES`), NetPos infrastructure (`run_variant`, `run_walkforward`, `format_final_ranking`, `SUSPICIOUS_SHARPE`, `_fmt_num`), CVD base features (`load_cvd_tf(..., include_cum_delta=True)`, `attach_cvd`), combo helpers (`apply_combo`, `_try_load_with_pepe_fallback`, `compute_cross_coin_features`), and `split_folds`. New pure functions: `_consecutive_count`, `build_exhaustion_features` (per-coin; consecutive counter operates on a single coin's frame — no cross-coin bleed because the function is called inside the per-coin loop before `compute_cross_coin_features`), `build_divergence_features` (reads `features_df["price"]`, which `build_features_tf` produces by renaming the ccxt `close` column on line 283 — do NOT expect a `close` column), `build_hypothesis_filters`, `custom_evaluate_verdict` (the 8-rule ladder), `_format_standalone_block` (local formatter — no baseline comparison since standalone signals have no reference).
- **`scripts/test_research_cvd_standalone.py`** — 12 offline PASS + 3 optional DB-smoke (Block 4 skipped without DB). Block 1 (5) = feature engineering; Block 2 (4) = filter application; Block 3 (3) = verdict logic; Block 4 (3) = `load_cvd_tf(include_cum_delta=True)` exposes `cum_vol_delta`, ≥500 rows, UTC index.

### Minimal modification

- **`scripts/research_cvd.py`** — `load_cvd_tf(symbol, interval, include_cum_delta: bool = False)` gained a backward-compatible optional flag. Default `False` preserves Phase 2 behaviour byte-for-byte (two-column SELECT, empty-frame returns `["agg_taker_buy_vol", "agg_taker_sell_vol"]`). Passing `True` adds `cum_vol_delta` to the SELECT and the empty-frame column list. This is the only non-append change in Phase 3 — all other additions are new files.

### Run

```bash
# Tests (offline + optional DB smoke)
.venv/bin/python scripts/test_research_cvd_standalone.py    # expect PASS: 12 | FAIL: 0

# Debug slice
.venv/bin/python scripts/research_cvd_standalone.py --intervals h4 --hypotheses H5 --thresholds-h5 2.0

# Full matrix (VPS, ~15-30 min)
.venv/bin/python scripts/research_cvd_standalone.py | tee analysis/cvd_standalone_research_2026-04-17.txt
```

### Implementation notes verified in this session

- `build_exhaustion_features` operates on one per-coin frame at a time (called inside the per-coin loop in `_load_coins_for_interval`, before `compute_cross_coin_features` merges cross-coin columns). The `_consecutive_count` vectorized routine uses `(mask != mask.shift()).fillna(True).cumsum()` as the group key so each sign flip starts a fresh `cumcount`. No state leaks across coins.
- `price_change_6bars` reads `features_df["price"]`, not `"close"`. `build_features_tf` at line 283 of `backtest_market_flush_multitf.py` renames `close → price` when joining ccxt klines into the feature frame. If that rename ever changes, `build_divergence_features` will set `price_change_6bars = NaN` (guarded by `"price" in features_df.columns` check) rather than raise, so the signal will simply stop firing — a visible symptom, not a silent miscompute.

### Expected outcomes

- Any PASS/MARGINAL → **mandatory** L13 Phase 3b validation (correlation with h4 baseline < 0.5, rolling 30-day Sharpe stability, combined-portfolio synergy) before Phase 4 integration.
- All FAIL → reject CVD standalone approach; next candidate: L11 SHORT research.
- Walk-forward results TBD — to be filled in this section after VPS runs.

### Do NOT

- Change locked L3b-2 thresholds (z_self=1.0, z_market=1.5, n_coins≥4). Phase 3 does NOT use `market_flush` at all — standalone hypotheses have no dependency on it. The baseline row in the ranking is purely for cross-variant context (labelled `REF`, no PASS/FAIL).
- Modify `bot/signal.py`, `bot/paper_executor.py`, or anything in `exchange/` in Phase 3 — research only.
- Add new DB tables (Phase 1 data layer complete).
- Test SHORT variants in this script — SHORT research belongs to L11 with its own threat model and hypothesis set.
- Add a live CVD collector — backfill-only until a standalone edge is confirmed by Phase 3b.
- Extend the threshold grid beyond the 3 defaults per hypothesis without a separate ExitPlanMode approval (overfit risk grows quadratically).
- Mark pooled OOS Sharpe > 8.0 as PASS without manual inspection — auto-demoted to MARGINAL.
- Ship any PASS/MARGINAL variant to live without completing L13 Phase 3b validation first.

## Session L13 Phase 3b — H5_z2.5_h2 Validation

Motivation: L13 Phase 3 produced exactly one **qualified MARGINAL** — **H5_z2.5_h2** (Aggressive Selling Exhaustion on h2): N=137, Win% 56.2, pooled OOS Sharpe 6.20, **3/3 OOS folds positive** (unique property across all L13 research), OOS3 Sharpe +3.82 (works in the freshest window), fold pattern monotone (7.18 → 6.30 → 3.82, no OOS2 outlier). Failed only on the strict `trades/day ≥ 1.5` absolute floor (got 0.77 ≈ 23 trades/month — still enough for Smart Filter's 14 trading days / 30 when evenly distributed). Pooled 3-fold aggregates alone are insufficient to decide deploy-vs-reject; three questions remain unresolved and are resolved here the same way L10 Phase 2b resolved them for H1_z1.5_h2.

### Three validation tests

**Test 1 — Daily-return correlation (diversification):** Pearson correlation between per-day pnl series plus 4-way overlap breakdown (both_win / both_lose / h4_win_h5_lose / h4_lose_h5_win) on common active days. PASS = `corr < 0.5` AND `mixed_pct ≥ 15%`. Low correlation is implied by construction (H5 fires on CVD extremes, baseline fires on flush — Phase 2 showed these rarely overlap) but the test quantifies it.

**Test 2 — Rolling 30-day Sharpe stability (per strategy, h4 + h5 each):** Slide a 30-day window across each strategy's daily pnl series (reuses `compute_rolling_sharpe_test` from `validate_h1_z15_h2.py` — `STD_EPS=1e-12`, `min_trading_days=5`, drop-rate warning when >30%). PASS (each strategy) = `min > 0` AND `median > 2.0` AND `≥60%` windows with Sharpe > 2 AND `≥50%` windows with win-days ≥ 65%.

**Test 3 — Combined 50/50 portfolio synergy:** `combined_usd = 0.5 × capital × h4_pct/100 + 0.5 × capital × h5_pct/100`; equity curve → running-max → drawdown → MDD. PASS = `combined_sharpe > max(h4_solo, h5_solo)` AND `combined_mdd > h4_solo_mdd` (less severe — both are ≤ 0) AND `combined_win_days ≥ h4_solo_win_days`. Strict dominance over h4 solo is required — no slot for a partner that does not strictly improve the baseline portfolio.

### Recommendation tree

| Condition | Verdict | Next action |
|---|---|---|
| Test 2 (h4) FAIL | `ALARM` | Pause Phase 3b; reinvestigate baseline rolling stability |
| Test 1 FAIL or Test 3 FAIL | `REJECT` | Skip to L11 SHORT-side research |
| Tests 1 + 3 PASS, Test 2 (h5) PASS | `STRONG_GO` | Phase 4 integration + paper deploy parallel to h4 |
| Tests 1 + 3 PASS, Test 2 (h5) FAIL | `WEAK_GO` | Paper trading parallel to h4, min 30 days; no live until stable |

### Files

- **`scripts/validate_h5_z25_h2.py`** — standalone validator. Thin-reuse of `validate_h1_z15_h2.py` helpers (all module-level importable, no refactor): `extract_trade_records`, `aggregate_daily_pnl`, `compute_correlation_test`, `compute_rolling_sharpe_test`, `compute_combined_portfolio_test`, `STD_EPS`, `DEFAULT_CAPITAL_USD`, `TRADING_DAYS_PER_YEAR`. Other imports: `_load_coins_for_interval` and `build_hypothesis_filters` from `research_cvd_standalone.py` (the CVD-standalone loader — includes `attach_cvd + build_exhaustion_features + build_divergence_features`; **do NOT import the homonym from `research_netposition.py` — it lacks CVD columns H5 needs**), `run_variant` from `research_netposition.py`, `MARKET_FLUSH_FILTERS` + `RANK_HOLDING_HOURS` from `backtest_market_flush_multitf.py`. New helpers: `run_h4_baseline()`, `run_h5_z25_h2()`, `recommend()`, `format_report()`, `_strategy_stats_from_trades()`.
- **`scripts/test_validate_h5_z25_h2.py`** — 7 required PASS (+1 optional DB smoke). Block 1 (3) = imports + H5 filter shape + `format_report` smoke; Block 2 (4) = `recommend()` verdict tree branches (ALARM / STRONG_GO / WEAK_GO / REJECT); Block 3 (optional) = live DB smoke that skips without a reachable DB. Logic-level coverage of the reused helpers is already in `test_validate_h1_z15_h2.py` (14 PASS) — duplicating it here would be dead weight.

### Parity risk

`extract_trade_records` replicates `run_variant`'s internal `apply_combo` mask to recover per-trade metadata (`run_variant` returns only pooled aggregates). If the replicated mask ever drifts from `run_variant`'s internal path, pooled `(N, Win%, Sharpe)` will diverge between the `run_variant` summary and the extracted-trade-list summary. The report prints both numbers side-by-side so drift is visible — same safeguard used in `validate_h1_z15_h2.py`.

### Run

```bash
# Offline tests (DB smoke auto-skips)
.venv/bin/python scripts/test_validate_h5_z25_h2.py    # expect 7 PASS (8 with DB)

# Validation run (VPS with populated DB, ~5-10 min)
.venv/bin/python scripts/validate_h5_z25_h2.py | tee analysis/validation_h5_z25_h2_2026-04-17.txt
```

### Key reuse note (`h2_solo_*` naming)

`compute_combined_portfolio_test` returns the partner strategy's metrics under keys prefixed `h2_solo_*` — the helper was authored for L10 Phase 2b where the partner was an h2-interval strategy. Phase 3b re-uses the helper verbatim; `format_report` reads `h2_solo_sharpe / h2_solo_mdd / h2_solo_win_days / h2_solo_trades_per_day` and renders them as the h5 strategy's metrics. If you change the helper's return keys, both validators break.

### Do NOT

- Modify `scripts/validate_h1_z15_h2.py`, `scripts/research_cvd_standalone.py`, `scripts/research_netposition.py`, or `scripts/backtest_market_flush_multitf.py` — import-only.
- Change `bot/`, `exchange/`, `telegram_bot/`, or `collectors/` — research-only session.
- Add new DB tables or new deps (`requirements.txt` unchanged).
- Ship to live on `WEAK_GO` without paper trading. Paper is mandatory.
- Interpret Test 3 MDD sign backwards — both MDDs are ≤ 0, and "less severe" = "greater" (closer to zero).
- Import `_load_coins_for_interval` from `research_netposition.py` for H5 — it does not attach CVD features and H5 filters will silently produce 0 trades.

## Session L13 Phase 3c — Smart Filter Adequacy Test

Motivation: L13 Phase 3b verdict = **ALARM**, but close reading showed the verdict was an **auto-trigger from h4 baseline Test 2 FAIL**, not a judgment on H5_z2.5_h2. Underlying numbers were more nuanced — Test 1 correlation 0.44 with 20.7 % mixed days (diversification proven), Test 3 combined Sharpe 1.907 > h4 solo 1.796 with MDD halved to $381 — but Test 2's abstract "rolling 30-day Sharpe > 2 + win_days ≥ 65 %" gate penalised sparse strategies unfairly because `win_days_ratio` was computed on **total calendar days** rather than **active trading days**. That metric does not match any actual exchange rule. Phase 3c reformulates the validation against the **actual Binance Copy Trading Smart Filter** to see whether the Phase 3b ALARM is a false negative.

### Smart Filter actual rules (Binance Copy Trading Lead Trader)

1. Trading days >= 14 in the last 30 days.
2. PnL positive on 30d / 60d / 90d rolling windows.
3. Win days ratio >= 65 % (30d/60d), >= 60 % (90d) — computed on **trading days**, not calendar days.
4. Max drawdown <= 20 %.

Window configs (hardcoded in `SMART_FILTER_CONFIGS`): 30d/14 trading days/65 %/20 %, 60d/28/65 %/20 %, 90d/42/60 %/20 %. `min_trading_days` scaled pro rata (14/30 = 0.467).

### Files

- **`scripts/smart_filter_adequacy.py`** — standalone driver. Imports `run_h4_baseline`, `run_h5_z25_h2` from `validate_h5_z25_h2.py` (both already module-level, no modification needed) and `DEFAULT_CAPITAL_USD` from `validate_h1_z15_h2.py`. Pure functions: `compute_daily_metrics`, `simulate_smart_filter_windows`, `summarize_smart_filter_results`, `recommend`, `format_report`. Main flow: init DB → load h4 + H5 trade lists → build common `date_range` → per-day metrics per strategy → inline 50/50 combined DataFrame (`combined_pnl_usd = 0.5*capital*h4_pct/100 + 0.5*capital*h5_pct/100`) → slide 30d/60d/90d windows across each → 9 summaries → `recommend()` → print.
- **`scripts/test_smart_filter_adequacy.py`** — 10 offline PASS + 1 optional DB smoke. Block 1 (3) = `compute_daily_metrics` (columns, zero-trade days, equity invariants), Block 2 (4) = `simulate_smart_filter_windows` (length invariant, PASS case, low-activity fail, negative-pnl fail), Block 3 (4) = `recommend` verdict tree covering STRONG_GO / H4_DONT_ADD / REJECT_BOTH / H5_ONLY branches, Block 4 (optional) = live `run_h4_baseline` against DB.

### Metric reformulation (vs Phase 3b)

| Metric | Phase 3b | Phase 3c |
|--------|----------|----------|
| Win days ratio | `winning_days / total_calendar_days` | `winning_days / trading_days` (active basis) |
| PnL gate | Rolling Sharpe > 2 (abstract) | `sum(pnl_usd) > 0` (actual rule) |
| Trading-days gate | Implicit (drop windows with < 5 active) | Explicit `>= 14 (pro-rata)` per-window gate |
| MDD gate | Not in criteria | Explicit `abs(mdd_pct) <= 20` per-window |

### Recommendation tree (6 outcomes)

Adequacy = `pass_rate_pct >= 60 %` of 30d windows (the primary Smart Filter gate). 60d/90d printed for diagnostic context but not gating.

| h4 30d | H5 30d | Combined 30d | Verdict | Next action |
|--------|--------|--------------|---------|-------------|
| ≥60% | ≥60% | ≥60% | `STRONG_GO` | Phase 4 integration, deploy combined |
| ≥60% | <60% | ≥60% | `H4_ONLY` | Deploy h4 solo, paper H5 |
| <60% | ≥60% | ≥60% | `H5_ONLY` | Deploy H5 solo (unusual — verify manually) |
| <60% | <60% | ≥60% | `COMBINED_ONLY` | Deploy combined only; synergy required |
| ≥60% | * | <60% | `H4_DONT_ADD` | Deploy h4 solo, skip H5, move to L11 SHORT |
| <60% | * | <60% | `REJECT_BOTH` | Fundamental rethink |

### Phase 3b vs Phase 3c deployment authority

**Phase 3c supersedes Phase 3b for the `h4 + H5` combined-portfolio decision.** The Phase 3b ALARM rested on a metric (total-day win ratio) that does not match actual exchange rules. If Phase 3c's 30d pass rates clear 60 % for any combination, the Phase 3b verdict is considered a false negative for that configuration. Phase 3b's diversification (Test 1) and synergy (Test 3) findings stand independently — they don't conflict with Phase 3c.

### Run

```bash
# Offline tests (DB smoke auto-skips)
.venv/bin/python scripts/test_smart_filter_adequacy.py    # expect 10 PASS (11 with DB)

# Adequacy run (VPS with populated DB, ~5-10 min)
.venv/bin/python scripts/smart_filter_adequacy.py | tee analysis/smart_filter_adequacy_2026-04-17.txt
```

### Do NOT

- Modify `scripts/validate_h5_z25_h2.py`, `validate_h1_z15_h2.py`, `research_cvd_standalone.py`, or any other L8/L10/L13 locked script — import-only reuse.
- Change `bot/`, `exchange/`, `telegram_bot/`, or `collectors/` — research-only session.
- Add new DB tables or new deps (`requirements.txt` unchanged).
- Shoehorn `aggregate_daily_pnl` (Phase 2b) into the combined-portfolio math — it returns a `pd.Series` and lacks `trade_count` / `is_active` / `is_winning` / `equity_usd` columns needed for Smart Filter gates. Build `compute_daily_metrics` from scratch on the raw trade list.
- Use Phase 3b's Test 2 verdict as a Smart Filter adequacy substitute — the calendar-day vs trading-day basis is a material difference that penalises sparse strategies unfairly.
- Raise the adequacy threshold above 60 % without a separate ExitPlanMode approval — the 60 % value is calibrated to leave slack for month-to-month variance while still signalling habitual adequacy.
- Deploy live on an `H5_ONLY` verdict without manual investigation — h4 failing solo while H5 succeeds solo is an unusual outcome that warrants skeptical review (may indicate a regime shift that invalidates the locked baseline).

## Session L14 Phase 1 — Per-Coin Independent Flush Research

Motivation: L13 Phase 3c exposed a **structural** problem with `market_flush`. Over 148 backtest calendar days the bot traded only 41 of them (28%) because the locked breadth filter `n_coins_flushing >= 4` (with `CROSS_COIN_FLUSH_Z=1.5`) fires all ~10 coins at once when it fires at all — clustering entries into a few days and leaving long stretches silent. Binance Copy Trading Smart Filter requires **≥14 trading days / 30** rolling; our baseline tops out near 13, which is structurally short. Phase 1 tests whether relaxing the breadth threshold K gives temporal dispersion at acceptable Sharpe cost.

### Breadth semantics (clarified)

- `compute_cross_coin_features(..., flush_z=1.5)` computes `n_coins_flushing[t] = count of coins with long_vol_zscore > 1.5` (inclusive of self). Locked.
- Entry condition stays `long_vol_zscore > 1.0` (L3b-2 locked).
- The two thresholds differ (1.0 vs 1.5) deliberately — self does not always count toward `n_coins_flushing` when firing.
- K=0 drops the breadth tuple entirely from the filter list → **truly per-coin independent** (a coin with z ∈ (1.0, 1.5] AND no other coin flushing can now fire).
- K=1 still requires at least one coin (self or other) to have z > 1.5.
- K=4 is the locked L3b-2 baseline; K=4 per-interval doubles as the parity check vs L8 reference (h4 Sharpe 5.87 / Win 61.0 / N 428, warn on >5 % drift).

### Matrix

5 breadth values × 3 intervals (h1, h2, h4) = **15 variants**. Per-coin z-score threshold (1.0), cross-coin flush z (1.5), and holding hours (RANK_HOLDING_HOURS=8) stay locked — only K varies.

### Dual-track PASS criteria

**Primary (L8-inherited, Sharpe track):**
1. Pooled OOS Sharpe > 2.0
2. Win% > 55 %
3. N ≥ 100
4. ≥ 2/3 OOS folds positive

**Strict (Smart Filter adequacy track):**
5. **Min 30d TD ≥ 14** — every rolling 30-day window must clear the Smart Filter floor (strict minimum).
6. **Median 30d TD ≥ 14** — the majority of months must clear (stronger, not merely one good month).

### Verdict ladder

| State | Criteria |
|-------|----------|
| **STRONG_PASS** | primary (1–4) + strict-5 + strict-6 AND pooled OOS Sharpe ≤ 8.0 |
| **PASS** | primary + strict-5 (strict-6 fails) AND Sharpe ≤ 8.0 |
| **MARGINAL** | primary met, strict-5 fails OR pooled OOS Sharpe > 8.0 (look-ahead smell, manual review) |
| **FAIL** | any primary criterion missed, or walk-forward skipped (N < `WF_MIN_TRADES`) |

Correlation vs h4 baseline, rolling 30-day Sharpe stability, and combined-portfolio synergy are **deferred to an L14 Phase 1b validation** (mirror of L10 Phase 2b / L13 Phase 3b). No PASS/STRONG_PASS variant deploys to live without Phase 1b clearance.

### Files

- **`scripts/research_breadth.py`** — 15-variant research driver. Reuses: `build_features_tf`, `fetch_klines_ohlcv`, `load_liquidations_tf`, `load_oi_tf`, `load_funding`, `RANK_HOLDING_HOURS`, `WF_FOLDS`, `WF_MIN_TRADES`, `CROSS_COIN_FLUSH_Z`, `MARKET_FLUSH_FILTERS`, `_interval_to_bar_hours` (all from `backtest_market_flush_multitf.py`); `compute_cross_coin_features`, `_try_load_with_pepe_fallback` (from `backtest_combo.py`); `run_variant`, `run_walkforward`, `_fmt_num`, `SUSPICIOUS_SHARPE` (from `research_netposition.py`); `extract_trade_records` (from `validate_h1_z15_h2.py`). New pure functions: `build_breadth_filters(K)` (K=0 drops breadth; validates K ∈ [0,10]), `compute_trading_days_distribution(trades, window_days=30)` (unique trading days + rolling 30d window min/median/max/%≥14), `evaluate_breadth_verdict`, `format_variant_block`, `format_final_ranking_with_days` (adds `Min30dTD` / `Med30dTD` columns), `recommend` (ladder-aware message — specifically calls out when K=0 appears only as MARGINAL, since that confirms breadth relaxation doesn't resolve clustering).
- **`scripts/test_research_breadth.py`** — 13 offline assertions + 1 optional DB smoke. Block 1 (3) filter construction; Block 2 (4) trading-days distribution including empty-list and span<30 edge cases; Block 3 (6) verdict tree covering all paths (FAIL on wf-skipped, STRONG_PASS, PASS-but-not-STRONG, MARGINAL on strict-5 fail, FAIL on primary, MARGINAL on Sharpe > 8 guard).

### Reused data loader

`_load_coins_for_interval` mirrors the pattern in `research_netposition.py` / `research_cvd_standalone.py` minus the hypothesis-specific attach step — breadth research only needs liquidations/OI/funding/klines. Load once per interval; reuse across all 5 K values.

### Run

```bash
# Offline tests (DB smoke auto-skips)
.venv/bin/python scripts/test_research_breadth.py       # expect PASS=13 FAIL=0

# Local plumbing sanity (single variant)
.venv/bin/python scripts/research_breadth.py --intervals h4 --breadth 2

# Full matrix (VPS, architect-triggered, ~15-30 min)
.venv/bin/python scripts/research_breadth.py | tee analysis/research_breadth_2026-04-17.txt
```

### Report layout

Per-variant block: variant header with K semantic label → Signal metrics (N, Win%, Sharpe, trades/day) → Walk-forward folds table + Pooled OOS → Trading Days Distribution block (total + calendar span + rolling-window min/median/max + %≥14) → Primary checklist → Strict checklist → suspicious-Sharpe flag if applicable → VERDICT line.

Final ranking sorted by pooled OOS Sharpe descending, columns: `Rank | Variant | N | Win% | Sharpe | OOS | Tr/d | Min30dTD | Med30dTD | Verdict`.

### Recommendation logic

- Any `STRONG_PASS` → Phase 1b validation, then Phase 2 integration candidate. Best (highest pooled OOS Sharpe) goes to paper deploy parallel to h4.
- `PASS` only (no `STRONG_PASS`) → Paper deploy with caveat that not every month will clear Smart Filter.
- Only `MARGINAL` variants and **K=0 among them** → breadth relaxation alone does NOT resolve temporal clustering. Escalate to L15 (new orthogonal signal class).
- Only `MARGINAL` with no K=0 (K=0 was FAIL) → also L15; unusual but same conclusion.
- All `FAIL` → structural issue confirmed; L15 or L16 pivot.

### Results

**TBD** — to be filled after VPS run.

### Do NOT

- Change locked L3b-2 thresholds (z_self=1.0, CROSS_COIN_FLUSH_Z=1.5). Only K varies in this session.
- Modify `bot/signal.py`, `bot/paper_executor.py`, anything in `exchange/`, `telegram_bot/`, or `collectors/`. Research-only.
- Modify any locked script (`backtest_market_flush_multitf.py`, `backtest_combo.py`, `research_netposition.py`, `research_cvd_standalone.py`, `validate_*.py`, `smart_filter_adequacy.py`) — import-only reuse.
- Add new DB tables or new dependencies to `requirements.txt`.
- Extend K outside [0, 10] (the validator rejects) — grows overfit risk with no additional signal.
- Ship any PASS/STRONG_PASS variant to live without completing an L14 Phase 1b validation first (correlation < 0.5 vs h4, rolling 30-day stability, combined-portfolio synergy).
- Mark pooled OOS Sharpe > 8.0 as STRONG_PASS — auto-demoted to MARGINAL for look-ahead review.

## Session L15 Phase 1 — Funding Rate Z-Score Standalone Research

Motivation: L14 Phase 1 proved `market_flush` architecturally unsuitable for Binance Smart Filter — clustering (41 active calendar days / 148) and breadth dependency are mutually exclusive (K=0 at h4 reached Min30dTD=15 but Sharpe collapsed to 0.71). All prior L-sessions (L10 NetPos, L13 CVD Phase 2/3/3b/3c, L14 breadth) share a liquidation-based clustering substrate. L15 pivots to a **continuous-by-design** signal class: funding rate extremes. Funding updates every 8h on every coin by construction — the temporal dispersion needed for Smart Filter's ≥14 trading days / 30 is built in. Funding also carries orthogonal information (crowded positioning) vs liquidation cascades (forced exits), so low correlation with the h4 baseline is implied by construction.

Phase 1 scope: **research only** on h4 single timeframe. No changes to `bot/`, `exchange/`, `telegram_bot/`, `collectors/`, `requirements.txt`, or any locked script. h2/h1 extension deferred to Phase 2 conditional on any h4 PASS. Integration (Phase 3) conditional on a Phase 1b validation pass (correlation < 0.5, rolling 30-day stability, combined-portfolio synergy — mirrors L10 Phase 2b / L13 Phase 3b).

### Hypotheses

- **H1 Contrarian SHORT:** `funding_zscore > z_fund` → SHORT entry (longs paying shorts heavily → crowded longs → expect flush down).
- **H2 Contrarian LONG:** `funding_zscore < -z_fund` → LONG entry (shorts paying longs heavily → crowded shorts → expect squeeze up).

H1 is the first SHORT variant tested in L-series research. Phase 1 accepts SHORT as a deliberate scope expansion — funding crowding naturally has symmetric directions and testing only LONG would half-truth the signal. Execution-side SHORT work (order sizing, liquidation buffers, borrow-fee accounting) still belongs to a future L11 session.

### Key design decisions

- **Z-score on h8 source, not h4-ffilled.** A 45-bar rolling window applied to the h8 funding series equals exactly 15 calendar days (3 bars/day × 15 days). Applying the same window on the h4-ffilled series would double-count each funding value and give a mathematically different mean/std. Implementation: `compute_funding_zscore(series, window=45)` runs on h8; `load_funding_features_h4(symbol, h4_index)` computes zscore at h8 then ffills both `funding_rate` and `funding_zscore` to h4.
- **Direction flipping via `return_{h}h` column.** SHORT entries need returns inverted for Sharpe/Win% to come out direction-consistent. Rather than writing parallel `_run_funding_variant` / `_run_funding_walkforward` helpers (as originally planned), the implementation re-uses `run_variant` / `run_walkforward` from `research_netposition.py` by pre-flipping the `return_8h` column per variant via `apply_direction(df, direction)`. Less new code, zero behavior change in the reused helpers.
- **Fixed 8h holding at h4.** Matches L3b-2 convention. Shorter holdings tested in prior L-sessions didn't improve — 8h is the cross-interval ranking anchor.
- **Baseline REF row is nominal only.** Funding is a new signal class, not a filter on `market_flush`. The REF row in the final ranking carries only the raw trading-grid stats (total rows across coins) so downstream readers can sanity-check coverage. It does NOT run `MARKET_FLUSH_FILTERS` — cross-signal-class baselines would be misleading here.
- **100%-win OOS fold halts the script.** Look-ahead guard — no pooled metrics should be trusted if any OOS fold reports a clean 100% win rate.

### PASS criteria (dual-track)

Primary (L8 parity, all must hold):
1. Pooled OOS Sharpe > 2.0
2. Win% > 55
3. N ≥ 100
4. ≥ 2/3 OOS folds positive
5. Pooled OOS Sharpe > 1.0 (formally redundant, kept per spec convention)

Strict (Smart Filter adequacy on 30d rolling windows):
6. Min 30d trading days ≥ 14
7. Median 30d trading days ≥ 14
8. Median 30d win days ratio ≥ 65%
9. Max 30d absolute MDD ≤ 20%

**Verdicts:** PASS = all 9 met AND pooled OOS Sharpe ≤ 8.0. MARGINAL = primary 5 met but strict (6–9) partially failed, OR pooled OOS Sharpe > 8.0. FAIL = any primary criterion missed, or walk-forward skipped (N < `WF_MIN_TRADES`).

### Files

- **`scripts/research_funding_standalone.py`** — 6-variant driver. Pure functions: `compute_funding_zscore`, `build_funding_filters`, `apply_direction`, `extract_trade_records`, `evaluate_verdict`. Data loaders: `load_funding_features_h4`, `_load_coins_h4`. Reuses `run_variant` / `run_walkforward` / `format_final_ranking` / `_fmt_num` / `SUSPICIOUS_SHARPE` from `research_netposition.py`; `compute_daily_metrics` / `simulate_smart_filter_windows` / `summarize_smart_filter_results` from `smart_filter_adequacy.py`; `apply_combo` / `_try_load_with_pepe_fallback` from `backtest_combo.py`; `load_funding` / `fetch_klines_ohlcv` / `WF_FOLDS` / `WF_MIN_TRADES` from `backtest_market_flush_multitf.py`.
- **`scripts/test_research_funding.py`** — 13 offline PASS (Blocks 1–4) + 2 optional DB-smoke (Block 5). Block 1 z-score computation (4); Block 2 hypothesis filter + monotonicity (4); Block 3 direction-adjusted trade extraction (3); Block 4 Smart Filter integration + verdict ladder (2).

### Run

```bash
# Offline tests (DB smoke auto-skips via LIQ_SKIP_DB_TESTS)
.venv/bin/python scripts/test_research_funding.py    # expect PASS: 13 | FAIL: 0

# Debug slice (single variant, local DB required)
.venv/bin/python scripts/research_funding_standalone.py --hypotheses H1 --thresholds 2.0

# Full matrix (VPS, architect-triggered, ~5-10 min)
.venv/bin/python scripts/research_funding_standalone.py | tee analysis/funding_standalone_2026-04-17.txt
```

### Phase 2 roadmap (conditional)

- Any h4 PASS → Phase 2: same 6-variant matrix on h2 and h1 to surface additional trading timeframes. Funding is h8, so h2/h1 just resolve the entry-timing lottery — expect similar signal shape, possibly more trades/day.
- Any h4 PASS → mandatory **L15 Phase 1b validation** (correlation < 0.5 vs h4 baseline, rolling 30-day stability, combined-portfolio synergy) before Phase 3 integration into `bot/signal.py`.
- All h4 FAIL → document as tested-and-rejected and proceed to L15 Phase 2 alternative (OI z-score — another continuous signal class).

### Do NOT

- Change `market_flush` signal, locked L3b-2 thresholds, `bot/signal.py`, `bot/paper_executor.py`, or anything in `exchange/` / `telegram_bot/` / `collectors/` — research only.
- Reuse the `flush_extreme_funding` combo logic from L3b-2 — that was filter-over-`market_flush` (already rejected). L15 tests funding as standalone.
- Test on h1/h2 in Phase 1 — funding is h8, finer timeframe just duplicates signal resolution without edge benefit.
- Extend the z-threshold grid beyond 1.5 / 2.0 / 2.5 without a separate ExitPlanMode approval (overfit risk grows quadratically).
- Mark pooled OOS Sharpe > 8.0 as PASS — auto-demoted to MARGINAL for look-ahead review.
- Add a live funding collector — backfill data is sufficient for research; live collection belongs to Phase 3 integration.
- Ship any PASS/MARGINAL variant to live without completing L15 Phase 1b validation first.
- Skip the Smart Filter strict gates (criteria 6–9) under any circumstance — they exist specifically because L13 Phase 3c exposed that primary-only verdicts missed structural clustering.

## Session L15 Phase 2 — OI Velocity Z-Score Standalone Research

Motivation: L15 Phase 1 (commit `9accd00`) tested funding rate z-score as a standalone LONG/SHORT signal across 6 variants at h4. **All 6 FAIL.** The damning finding: `H2_z1.5` / `H2_z2.0` showed systematic **anti-edge** (Sharpe −1.85 / −2.57), meaning the classical "negative funding ⇒ shorts crowded ⇒ squeeze up" contrarian assumption is broken on the 2025-10 → 2026-04 sample. Funding measures a positional *snapshot* (who pays whom) — and the snapshot-contrarian hypothesis doesn't survive the current regime.

Phase 2 pivots to **OI velocity** (per-bar `pct_change` of `open_interest`, z-scored per coin over a 15-calendar-day rolling window) as a mechanistically different continuous-signal class. OI measures *positional growth*, not snapshot. A positive OI-velocity spike on a rising price indicates aggressive new longs piling in (over-extension, H1 SHORT); on a falling price, aggressive new shorts (squeeze risk, H2 LONG). Price-direction filter disambiguates long-crowding from short-crowding — this asymmetry vs Phase 1 (where H1/H2 used the sign of funding) is intentional, because OI velocity is magnitude-only.

Phase 2 scope: research only on h4 single timeframe. No changes to `bot/`, `exchange/`, `telegram_bot/`, `collectors/`, `requirements.txt`, or any locked script. h2/h1 extension deferred to a conditional Phase 2b; integration (Phase 3) conditional on Phase 2b validation clearance (mirrors L10 Phase 2b / L13 Phase 3b).

### Hypotheses

- **H1 Contrarian SHORT:** `oi_velocity_zscore > z_oi AND price_change_1 > 0` — aggressive new longs on rising price ⇒ over-extension ⇒ pullback.
- **H2 Contrarian LONG:** `oi_velocity_zscore > z_oi AND price_change_1 < 0` — aggressive new shorts on falling price ⇒ squeeze.

Both hypotheses require the same upward OI-velocity spike (`> z_oi`); the price-direction filter is what distinguishes which side is crowded. This is asymmetric vs Phase 1.

### Key design decisions

- **Velocity, not level.** Raw `open_interest` spans 5+ orders of magnitude PEPE↔BTC and secular growth (bull market = all OI rising) would dominate a level-based z-score. `pct_change(1)` normalizes per-coin *and* isolates bar-to-bar growth — the mechanism we actually want to measure. `diff()` was considered and rejected for cross-coin scale reasons.
- **OI is h4-native.** Source table `coinglass_oi_h4` lives on the same 00/04/08/12/16/20 UTC grid as Binance 4H klines — no h8→h4 ffill gymnastics (unlike Phase 1 funding). `load_oi_velocity_features_h4` reindexes onto the ccxt h4 index with ffill purely as a minor-misalignment guard.
- **Window = 90 h4 bars = 15 calendar days.** Matches L8 convention and Phase 1's 45 h8 bars semantically.
- **First 90 rows NaN.** `pct_change(1)` introduces a leading NaN; `rolling(window=90, min_periods=90)` requires a full clean window. First valid z-score lands at row 90.
- **No refactor of Phase 1.** `apply_direction`, `extract_trade_records`, `evaluate_verdict`, `format_variant_block` are duplicated locally (~120 lines). Phase 1 is locked per spec; duplication keeps the lock clean.
- **MDD caveat line.** Phase 1 `H2_z1.5` reported 553% MDD — correct for unit-return cumulative drawdown against a small peak, but cryptic without context. `format_variant_block` now prints a one-line caveat when `max_abs_mdd > 100%` noting that dollar-sized MDD depends on position sizing. Not a verdict gate — verdict still uses the 20% strict threshold.

### PASS criteria (identical to Phase 1, 9 gates)

Primary (L8 parity):
1. Pooled OOS Sharpe > 2.0
2. Win% > 55%
3. N ≥ 100
4. ≥ 2/3 OOS folds positive
5. Pooled OOS Sharpe > 1.0 (formally redundant, kept per spec)

Strict Smart Filter 30d:
6. Min 30d trading days ≥ 14
7. Median 30d trading days ≥ 14
8. Median 30d win-days ratio ≥ 65%
9. Max 30d |MDD| ≤ 20%

**Verdicts:** PASS = all 9 met AND pooled Sharpe ≤ 8.0. MARGINAL = primary 5 met, strict partial fail OR pooled Sharpe > 8.0 (look-ahead smell). FAIL = any primary fails, or walk-forward skipped (N < 30). Auto-halt: any OOS fold with 100% win rate → halt and print look-ahead warning before ranking output.

### Files

- **`scripts/research_oi_standalone.py`** — 6-variant research driver. New helpers: `compute_oi_velocity_zscore(series, window=90)`, `build_oi_filters(hypothesis, z_oi)` (returns 2-tuple filter list + direction), `load_oi_velocity_features_h4(symbol, h4_index)`, `_oi_loader_wrapper`, `_load_coins_h4` (adds `price_change_1 = price.pct_change(1)` alongside OI features). Local duplicates of Phase 1 helpers: `apply_direction`, `extract_trade_records`, `evaluate_verdict`, `format_variant_block` (Phase-2 variant with MDD>100% caveat branch and OI-semantic description text). Reuses `run_variant`, `run_walkforward`, `format_final_ranking`, `SUSPICIOUS_SHARPE`, `_fmt_num` from `research_netposition.py`; `load_oi_tf`, `fetch_klines_ohlcv`, `WF_FOLDS`, `WF_MIN_TRADES` from `backtest_market_flush_multitf.py`; `compute_daily_metrics`, `simulate_smart_filter_windows`, `summarize_smart_filter_results` from `smart_filter_adequacy.py`; `apply_combo`, `_try_load_with_pepe_fallback` from `backtest_combo.py`.
- **`scripts/test_research_oi.py`** — 13 offline PASS (Blocks 1–4) + 2 optional DB smoke (Block 5). Block 1 (4): z-score computation (hand-computed match, 90-row cold start, zero-stddev→NaN, default window=90). Block 2 (4): `build_oi_filters` (H1/H2 filter shape + direction, monotonicity z=1.5 ≥ z=2.5, H1/H2 disjoint on non-zero price bars). Block 3 (3): `apply_direction` (long preserves / short inverts / exit−entry = 8h). Block 4 (2): `simulate_smart_filter_windows` column schema + `evaluate_verdict` 4 branches (PASS / MARGINAL / FAIL / suspicious→MARGINAL). Block 5 (optional): `load_oi_velocity_features_h4("BTC", 180d h4 idx)` returns ≥1000 rows with both columns + non-NaN zscore post warm-up; skipped via `LIQ_SKIP_DB_TESTS` or on any connection failure.

### CLI

```
--hypotheses H1,H2           (default both)
--thresholds 1.5,2.0,2.5     (default — same grid as Phase 1)
```

Variant labels: `H1_z2.0_h4`, `H2_z1.5_h4`, etc.

### Run

```bash
# Offline tests
LIQ_SKIP_DB_TESTS=1 .venv/bin/python scripts/test_research_oi.py   # 13 PASS

# Single-variant plumbing probe (architect-triggered, requires DB)
.venv/bin/python scripts/research_oi_standalone.py --hypotheses H1 --thresholds 2.0 | head -80

# Full matrix (VPS, architect-triggered, ~5-10 min)
.venv/bin/python scripts/research_oi_standalone.py | tee analysis/oi_standalone_2026-04-17.txt
```

### Expected outcomes

- Any PASS/MARGINAL → **mandatory** L15 Phase 2b validation (correlation < 0.5 vs h4 baseline, rolling 30-day Sharpe stability, combined-portfolio synergy) before Phase 3 integration.
- All FAIL → document OI velocity as tested-and-rejected alongside Phase 1 funding. Cumulative reject set (NetPos, CVD filter, CVD standalone, funding, OI velocity) is strong evidence that no liquidation/positioning-adjacent continuous signal produces tradable edge on the 2025-10 → 2026-04 sample. Pivot candidates: L11 SHORT research, L6b Predictive Magnet, or re-examine business model.
- Walk-forward results TBD — to be filled in this section after VPS runs.

### Do NOT

- Change locked L3b-2 thresholds or signal definition (z_self=1.0, z_market=1.5, n_coins≥4). OI velocity is standalone, not a filter over `market_flush`.
- Modify `bot/signal.py`, `bot/paper_executor.py`, anything in `exchange/`, `telegram_bot/`, or `collectors/`.
- Modify `scripts/research_funding_standalone.py` (Phase 1 locked), `backtest_market_flush_multitf.py` (L8 locked), or any other sibling research/validate/backfill script — import-only reuse.
- Add new DB tables, new dependencies to `requirements.txt`, or a live OI-change collector (backfill data is sufficient).
- Extend the threshold grid beyond {1.5, 2.0, 2.5} without separate ExitPlanMode approval — overfit risk grows quadratically.
- Test h2/h1 in Phase 2 — deferred to Phase 2b, conditional on any h4 PASS.
- Mark pooled OOS Sharpe > 8.0 as PASS — auto-demoted to MARGINAL for look-ahead review.
- Ship any PASS/MARGINAL variant to live without completing L15 Phase 2b validation first.
- Interpret the MDD>100% caveat as a verdict gate — it's a reader-orientation note only. The verdict uses the 20% strict gate.

## Session L16 — `market_flush` Retest at h30m

Motivation: after L15 Phase 2 rejected OI velocity (`4026bb0`), the tested-and-rejected set covers six positioning-adjacent continuous signals. Only `market_flush` at h4 is validated (Sharpe 5.87, 3/3 OOS) but fails Binance Smart Filter adequacy due to clustering (~41 of 148 active calendar days, median 30d trading days ≈ 8 vs the ≥14 gate). L14 Phase 1 proved breadth IS the edge, so the last structural lever untested on the validated strategy is **timeframe**. Hypothesis: clustering at h4 is an aggregation artifact — finer 30-min granularity may expose intraday breadth events and disperse trading days across the calendar while preserving the underlying liquidation-cascade edge.

Scope: single identical-logic timeframe-scaling test. `market_flush` thresholds (`z_self > 1.0`, `z_market > 1.5`, `n_coins ≥ 4`), holding (8h), and coin list locked from L8. Only the bar duration changes (240 min → 30 min).

### Pre-flight CoinGlass 30m probe (2026-04-17)

`GET /api/futures/liquidation/aggregated-history?symbol=BTC&interval=30m&exchange_list=...&limit=4320` → HTTP 200, **4320 rows returned**, range `2026-01-17T16:00:00 → 2026-04-17T15:30:00` (89.98 days). Response shape identical to h1/h2 aggregated-history. Conclusion: single-request `limit = days × 48` works — no pagination code needed. 10 coins × 2 endpoints ≈ 60s with 2.5s rate-limit sleeps.

### `bar_minutes` refactor (U2)

`backtest_market_flush_multitf.py` previously keyed all TF-dependent behavior on integer `bar_hours`. 30m forces `bar_hours = 0.5`. Refactor introduces `bar_minutes` as the canonical internal representation:

- **`INTERVAL_TO_BAR_MINUTES = {"30m": 30, "h1": 60, "h2": 120, "h4": 240}`** — single source of truth.
- **`_interval_to_bar_minutes(interval)`** — primary helper.
- **`_interval_to_bar_hours(interval) -> float`** — derived (`bar_minutes / 60.0`). Returns float (0.5 at 30m, 4.0 at h4) — downstream `_z_window` / `_lookback_24h` accept float and cast internally.
- **`_interval_to_ccxt_timeframe(interval)`** — maps `30m → "30m"`, `h4 → "4h"`. Replaces the old `f"{bar_hours}h"` construction that would produce `"4.0h"` or `"0.5h"`.
- **`_forward_periods(holding_hours, bar_hours)`** — `max(1, int(round(holding_hours / bar_hours)))`. Replaces `hours // bar_hours` which yielded floats at sub-hour TFs and degraded `pct_change`. Byte-equal at integer bar_hours (existing h4 parity test still passes).
- **`HOLDING_HOURS_BY_MINUTES`** — new primary dict keyed by bar_minutes: `{30: [4,8,16,48], 60: [4,8,16,48], 120: [8,16,32,48], 240: [4,8,12,24]}`. All include 8h ranking anchor.
- **`HOLDING_HOURS_MAP`** — backward-compat derived dict keyed by integer bar_hours. Preserved for existing L8/L15 test imports — still works, just omits the sub-hour 30m entry.

The refactor is invisible at h4/h1/h2 (existing tests all pass unchanged, including the 6-assertion byte-for-byte L2 parity block).

### Smart Filter adequacy block (U3) — new functionality in `main()`

Previous L8 verdict reported the 5 primary criteria only. L16 adds the 4 strict Smart Filter gates by consuming the already-reusable helpers in `scripts/smart_filter_adequacy.py`:

- `compute_daily_metrics(trades, date_range)` — per-day pnl / trade_count / equity.
- `simulate_smart_filter_windows(daily, window_days, ...)` — slides 30/60/90d windows, returns per-window rows with gate outcomes.
- `summarize_smart_filter_results(window_df, label)` — aggregate pass rates + per-criterion breakdown.

Trade list is built at RANK_HOLDING_HOURS=8h: `exit_ts = entry_ts + 8h`, `pnl_pct = return_8h`. Date range spans the earliest to latest exit date. Strict criteria computed on the 30d window:
6. `min(trading_days_in_window) >= 14`
7. `median(trading_days_in_window) >= 14`
8. `median(win_days_ratio) >= 0.65`
9. `max(|mdd_in_window_pct|) <= 20%`

### Dual-track verdict ladder

- **PASS** = all 5 primary + all 4 strict met AND pooled Sharpe ≤ 8.0.
- **MARGINAL** = primary 5 met but strict partially fails, OR pooled Sharpe > 8.0 (auto-demoted for look-ahead review).
- **FAIL** = any primary criterion missed, or walk-forward skipped (N < `WF_MIN_TRADES`).

Auto-halt: N-aware 100%-win OOS fold guard from `4026bb0` still active.

### Files

- `scripts/backtest_market_flush_multitf.py` — `bar_minutes` refactor + 30m CLI choice + Smart Filter adequacy block + dual-track verdict. h4 / h2 / h1 behavior unchanged (byte-for-byte parity preserved at h4).
- `scripts/backfill_coinglass_hourly.py` — `--interval` gained `30m` choice; `INTERVAL_BARS_PER_DAY["30m"] = 48`; `ensure_tables` creates `coinglass_liquidations_30m` + `coinglass_oi_30m` inline (same pattern as sibling h1/h2 tables). PEPE → 1000PEPE fallback unchanged. `ON CONFLICT (timestamp, symbol) DO NOTHING` unchanged.
- `scripts/test_backtest_multitf.py` — +6 assertions across 2 new blocks (`test_30m_scaling`, `test_bar_minutes_consistency`). Total now 40 PASS.
- **No changes** to `bot/`, `exchange/`, `telegram_bot/`, `collectors/` (incl. `collectors/db.py:SCHEMA_SQL`), or any locked research/validate script. No new dependencies.

### Sample caveat

90 days (2026-01-17 → 2026-04-17) is **half** the L8 180-day sample. Walk-forward 4-fold → ~22 days/fold (vs ~45 for L8). Smart Filter 30d rolling yields ~61 overlapping windows (vs ~151 on 180d). Statistical confidence is materially lower; a borderline PASS should be re-run once the live collector accumulates 180 days of 30m history (~mid-May 2026 at earliest). The driver prints an explicit caveat line when `sample_days < 180`.

### Run

```bash
# Offline tests
.venv/bin/python scripts/test_backtest_multitf.py           # expect 40 PASS | 0 FAIL

# h4 parity regression (must match L8 reference within 5%)
.venv/bin/python scripts/backtest_market_flush_multitf.py --interval h4 \
    | tee analysis/market_flush_h4_parity_2026-04-17.txt

# 30m single-coin probe (optional)
.venv/bin/python scripts/backfill_coinglass_hourly.py --interval 30m --days 90 --coin BTC --skip-oi

# 30m full backfill (10 coins, ~60s incl. rate-limit sleeps)
.venv/bin/python scripts/backfill_coinglass_hourly.py --interval 30m --days 90 \
    | tee analysis/l16_backfill_log.txt

# SQL sanity
# psql: SELECT symbol, COUNT(*), MIN(timestamp)::date, MAX(timestamp)::date
#       FROM coinglass_liquidations_30m GROUP BY symbol ORDER BY symbol;
# Expect: 10 rows, ~4320 count/symbol, 2026-01-17 → 2026-04-17

# 30m research run
.venv/bin/python scripts/backtest_market_flush_multitf.py --interval 30m \
    | tee analysis/market_flush_30m_2026-04-17.txt
```

### Results

**TBD** — populate after VPS `--interval 30m` run. Report must include: per-coin metrics, pooled walk-forward verdict, Smart Filter adequacy block (30d/60d/90d + strict gate outcomes), sample-adequacy caveat, and final dual-track verdict + recommendation.

### Conditional follow-ups

- **PASS** (all 9 + Sharpe ≤ 8): promote 30m as primary Binance-Smart-Filter-compatible strategy. Plan L17 (integration into `bot/signal.py` as opt-in strategy slot + 14-day paper trade parallel to h4). Update `LIVE_TRADING_MASTER_PLAN.md`. L6b retest demoted to secondary track.
- **MARGINAL** (primary met, strict partial): document as "promising, needs more data". Extend sample via live 30m collector (new task) + wait 90+ days, OR proceed with L6b as primary path while 30m matures.
- **FAIL** (any primary): document `market_flush` as timeframe-dependent edge, clustering confirmed fundamental. L6b (April 24) becomes the last research hope before Variant D business-model pivot. Append a row to the tested-and-rejected summary table.

### Do NOT

- Change `market_flush` signal definition, thresholds, holding hours, or coin list. Identical-logic test is the whole premise of L16.
- Extend to OI / funding / CVD / NetPos on 30m — six structural rejections already exhausted the positioning-signal search space.
- Deploy live or to showcase on L16 PASS alone. 14-day paper trade is mandatory.
- Skip the h4 parity regression — the `bar_minutes` refactor must be proven neutral before 30m numbers are trusted. L2 byte-for-byte parity test covers the code path; the VPS h4 run covers the data path.
- Drop the sample-adequacy caveat — 90-day window is materially shorter than L8's 180-day.
- Add new dependencies to `requirements.txt` or touch `bot/` / `exchange/` / `telegram_bot/` / `collectors/`.
- Build a live 30m collector in this session — backfill is sufficient for research; live collection belongs to L17 if PASS.
- Extend the grid of intervals beyond `{h1, h2, h4, 30m}` without separate ExitPlanMode approval.

