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
