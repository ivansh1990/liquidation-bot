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
- Funding rate OHLC: `GET /api/futures/funding-rate/oi-weight-ohlc-history?symbol=BTC&interval=h8` (fallbacks: `/funding-rate/aggregated-history`, `interval=h4`)
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

- **Endpoints**: OI → `/api/futures/open-interest/aggregated-history?interval=h4`; Funding → `/api/futures/funding-rate/oi-weight-ohlc-history` tried first, falling back to `/funding-rate/aggregated-history`; interval `h8` preferred (matches Binance's 8h funding cadence), falling back to `h4`. First non-empty `(path, interval)` combo wins per coin; the chosen combo is logged and printed in the final summary.
- **Tables**: `coinglass_oi (timestamp, symbol, open_interest, oi_high, oi_low)` and `coinglass_funding (timestamp, symbol, funding_rate)`, both with `UNIQUE (timestamp, symbol)` for idempotency. Created inline via `ensure_tables()` — same policy as `coinglass_liquidations`, not added to `collectors/db.py:SCHEMA_SQL`.
- **Hobbyist pattern**: single request per `(coin, endpoint)` with `startTime`/`endTime` passed defensively but filtered client-side — API ignores them and returns ≤1000 buckets. 2.5s sleep between requests; full run is ~60 requests ≈ 3 min including funding combo probes.
- **Field-name safety**: the first record of each endpoint is always dumped as pretty JSON (via `_probe_dump`) so real field names are visible without `--verbose`. Parsers (`build_oi_rows`, `build_funding_rows`) use a `_pick_float` helper with multi-key fallbacks covering common variants (`close`/`c`/`openInterest`/`aggregated_open_interest_usd` for OI close; `close`/`c`/`fundingRate`/`rate` for funding). If all fallbacks miss, inserts write `0` — easy to spot in the summary and patch.
- **Flags**: `--days` (1–365, default 180), `--coin <BTC>` (single-coin probe), `--verbose`, `--skip-oi`, `--skip-funding`. PEPE falls back to `1000PEPE` automatically, same as the liquidations backfill.

Run on VPS with `.venv/bin/python scripts/backfill_coinglass_oi.py --days 180` and inspect the raw-JSON probe + summary on first run. If the OI or funding-rate column is stuck at 0 in the summary, the fallback key list in `build_oi_rows` / `build_funding_rows` needs the real field name appended; rerun is idempotent. Actual row counts + the `(path, interval)` combo that won for funding should be noted here after the first successful run.
