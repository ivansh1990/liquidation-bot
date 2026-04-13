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

`is_liq_estimated` in `hl_position_snapshots`: `FALSE` = liquidation price from API, `TRUE` = estimated via `entry_px * (1 ± 1/leverage)`. Filter with `WHERE NOT is_liq_estimated` for analysis requiring precise data.

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

## Constraints

- No imports from `crypto-regime-bot` (separate project)
- No API keys needed (all endpoints are public)
- No Docker
- No trading/strategy logic — data collection only
