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
│   └── alerts.py           — Telegram notifications
├── scripts/
│   ├── init_db.py          — Create database and tables
│   ├── seed_addresses.py   — Seed whale addresses from leaderboard
│   ├── test_collectors.py  — Integration test for all endpoints
│   ├── backfill_binance.py — Backfill last 30 days of Binance history (one-shot)
│   ├── backfill_coinglass.py — Backfill 180 days of CoinGlass aggregated liquidations (one-shot)
│   ├── backfill_coinglass_oi.py — Backfill 180 days of CoinGlass aggregated OI + funding (one-shot)
│   ├── backtest_liquidation_flush.py — H1/H2/H3 backtest: liquidation asymmetry → reversal (L2 baseline, locked)
│   ├── walkforward_h1_flush.py — L3: 6-fold expanding-window walk-forward validation of H1
│   ├── backtest_h1_with_stops.py — L3: ATR-based TP/SL grid (64 configs/coin) using H1 entries
│   ├── analyze_heatmap_signal.py — L3: HL heatmap overlay framework (top-decile clusters, preceding-snapshot match)
│   ├── backtest_combo.py   — L3b-2: combo signal backtest (9 combos × 10 coins × 4 holding periods, portfolio + walk-forward)
│   └── quick_analysis.py   — Data analysis (run after 2+ days)
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
- Hobbyist-tier quirk: aggregated endpoints ignore `startTime`/`endTime` and return the latest ≤1000 buckets — so backfills use a single request per coin and filter the window client-side.
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

`is_liq_estimated` in `hl_position_snapshots`: `FALSE` = liquidation price from API, `TRUE` = estimated via `entry_px * (1 ± 1/leverage)`. Filter with `WHERE NOT is_liq_estimated` for analysis requiring precise data.

The four `binance_*` tables gain a `UNIQUE(timestamp, symbol)` constraint the first time `scripts/backfill_binance.py` runs (added lazily via `ALTER TABLE ... ADD CONSTRAINT`). This makes backfill + hourly collector coexist safely through `ON CONFLICT DO NOTHING`.

`coinglass_liquidations` is created by `scripts/backfill_coinglass.py` (inline `CREATE TABLE IF NOT EXISTS` with `CONSTRAINT uq_cg_liq UNIQUE (timestamp, symbol)`). There is no hourly CoinGlass collector yet — we only backfill and backtest until edge is confirmed.

`coinglass_oi` and `coinglass_funding` are created by `scripts/backfill_coinglass_oi.py` (inline `CREATE TABLE IF NOT EXISTS` with `CONSTRAINT uq_cg_oi` / `uq_cg_fr` on `(timestamp, symbol)`). Same policy: backfill-only, no hourly collector until edge is confirmed via a combo-signal backtest that joins `coinglass_liquidations ⋈ coinglass_oi ⋈ coinglass_funding` on `(timestamp, symbol)`.

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
- `LIQ_COINGLASS_API_KEY` — CoinGlass Hobbyist-tier API key (required for `backfill_coinglass.py` and `backfill_coinglass_oi.py`)

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
