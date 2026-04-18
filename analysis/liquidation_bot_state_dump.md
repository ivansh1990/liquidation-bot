# Liquidation Bot — State Dump for L18 Planning

**Generated:** 2026-04-18 (dev box, Darwin 24.6.0; live VPS data integrated from `scripts/vps_state_dump.sh` run at 11:33 UTC)
**Repo:** `/Users/ivanshytikov/liquidation-bot`
**Branch:** dev box at `master` / `264c4c8` (l-jensen). **VPS is one commit ahead at `d5672fc l18 invistigate licvidation bot strategy`** — architect should `git pull` before drafting L18 plan.
**Source authority:** [CLAUDE.md](../CLAUDE.md) (1500+ lines, comprehensive through L16) + on-disk artifacts + VPS systemd / Postgres / journal data. See §7 for VPS dump details.

---

## §0 Glossary (L1–L16 terminology)

Quick reference for terms used through this document.

| Term | Definition |
|---|---|
| **L<N>** | Numbered research session; documented as a top-level section in [CLAUDE.md](../CLAUDE.md) |
| **h4 / h2 / h1 / 30m** | Bar duration in hours / minutes. Locked baseline trades on h4 |
| **`bar_hours` / `bar_minutes`** | Internal scaling constant per timeframe. L16 introduced `bar_minutes` (canonical) because 30m forces `bar_hours = 0.5`, breaking integer math |
| **`market_flush`** | The single validated signal: per-coin `long_vol_zscore > 1.0` AND `n_coins_flushing >= 4` (cross-coin breadth at z>1.5). 8h holding. LONG only |
| **`z_self` / `z_market`** | The two locked z-thresholds inside `market_flush` (1.0 entry, 1.5 breadth count) |
| **`n_coins_flushing` / K** | Count of coins simultaneously above the breadth z-threshold. `K` = the configurable cutoff tested in L14 (locked at 4) |
| **NetPos** | Aggregated CoinGlass net-position metric (per-bar long/short delta). Tested L10 — REJECT |
| **CVD** | Aggregated Cumulative Volume Delta — taker buy/sell aggressor flow. Tested L13 — REJECT (filter and standalone) |
| **Smart Filter** | Binance Copy Trading lead-trader adequacy gates: ≥14 trading days/30, win-day ratio ≥65%, MDD ≤20%. The structural blocker for current strategy |
| **Walk-forward / OOS folds** | 4-fold expanding-window split (`scripts/walkforward_h1_flush.py:split_folds`); fold 0 = train baseline, folds 1–3 = out-of-sample |
| **PASS / MARGINAL / FAIL** | Verdict ladder. PASS requires all primary + strict gates. MARGINAL = primary met but strict fails OR pooled OOS Sharpe > 8.0 (look-ahead smell, manual review) |
| **`SUSPICIOUS_SHARPE`** | The 8.0 cutoff that auto-demotes a PASS to MARGINAL pending manual review |
| **L6b** | "Predictive Liquidation Magnet" — the only liquidation-based hypothesis class that has NOT been rejected. Scheduled retest 2026-04-24 once HL heatmap data has ≥7 days |
| **showcase account** | Live $500 / 15× isolated Binance Futures account spec defined in `exchange/`. NOT yet deployed (per L7 plan) |
| **paper bot** | `bot.scheduler` — simulates LONG entries on `market_flush` for live validation. State persisted at `state/paper_state.json` (see §4 for status) |
| **L-jensen** | Undocumented (in CLAUDE.md) post-L16 work: CAPM/Newey-West regression of `market_flush` daily P&L on BTC daily returns to test for genuine alpha vs leveraged beta. See [analysis/jensen_alpha.py](jensen_alpha.py) and commits `fab8b3f`, `264c4c8` |

---

## §1 Infrastructure inventory

| Module | Source | Storage | Frequency | Running on VPS? | Last record |
|---|---|---|---|---|---|
| [collectors/hl_websocket.py](../collectors/hl_websocket.py) | Hyperliquid `wss://api.hyperliquid.xyz/ws` (live trades) | (in-memory; no DB writes from this process — `LARGE TRADE` log lines only) | Event-driven (continuous WS) | ✅ active (`liq-hl-websocket.service`) | n/a (not a table); last log line 2026-04-18 11:33:21 UTC |
| [collectors/hl_snapshots.py](../collectors/hl_snapshots.py) | Hyperliquid REST `POST /info clearinghouseState` per address + `allMids` | `hl_position_snapshots`, `hl_liquidation_map`, `hl_addresses` | Timer `*:0/15` (every 15 min) | ✅ enabled timer (last fired 11:30:17, next 11:45:00) | `hl_liquidation_map.max(snapshot_time) = 2026-04-18 11:30 UTC` |
| [collectors/binance_collector.py](../collectors/binance_collector.py) | Binance Futures public REST: OI, funding, L/S ratio, taker | `binance_oi`, `binance_funding`, `binance_ls_ratio`, `binance_taker` | Timer `hourly` | ✅ enabled (last fired 11:00:17, 14s, 0 errors) | `binance_oi`, `binance_ls_ratio` → 2026-04-18 11:00 UTC |
| [collectors/coinglass_oi_collector.py](../collectors/coinglass_oi_collector.py) | CoinGlass `/api/futures/open-interest/aggregated-history?interval=h4` + `/funding-rate/oi-weight-history?interval=h8` (Binance only) | `coinglass_oi`, `coinglass_funding` | Timer `*-*-* 00,04,08,12,16,20:05:00 UTC` (4H + 5 min buffer) | ✅ enabled (last fired 08:05:00 → +10 OI rows / +10 funding rows; next 12:05:00) | `coinglass_oi.max = 2026-04-18 08:00 UTC` |
| [bot/signal.py:SignalComputer.fetch_recent_liquidations](../bot/signal.py) | CoinGlass `/api/futures/liquidation/aggregated-history?interval=h4` (called by paper bot every cycle) | `coinglass_liquidations` (side-effect insert; not its own collector) | 4H-aligned + 5 min buffer (whenever paper bot runs) | ✅ active via paper bot | `coinglass_liquidations.max = 2026-04-18 08:00 UTC` |
| [bot/scheduler.py](../bot/scheduler.py) (paper bot) | Aggregates signal + executes paper orders | `state/paper_state.json` | 4H-aligned + 5 min buffer | ✅ active (`liq-paper-bot.service`); last cycle 08:05 UTC, equity $1014.13, 5 trades closed, 100% win | n/a |
| [exchange/scheduler.py](../exchange/scheduler.py) (showcase bot) | Real Binance Futures execution | exchange-side orders + state (per L7 design) | 4H-aligned + 5 min buffer | **❌ inactive; unit `enabled=not-found` on VPS** (file in repo but not installed at `/etc/systemd/system/`). Stale `state/showcase_state.json.lock` remains | n/a |
| [telegram_bot/app.py](../telegram_bot/app.py) | Telegram `getUpdates` long-poll | None (read-only) | Continuous long-poll | ✅ active since 2026-04-15 16:50 UTC, authorized chat `229287803` | n/a |

**Systemd units present** (in repo at [systemd/](../systemd/)):

| Unit | Type | Schedule / Restart |
|---|---|---|
| `liq-hl-websocket.service` | simple | `Restart=always`, `RestartSec=10` |
| `liq-hl-snapshots.service` + `.timer` | oneshot | `OnCalendar=*:0/15` |
| `liq-binance.service` + `.timer` | oneshot | `OnCalendar=hourly` |
| `liq-coinglass-oi.service` + `.timer` | oneshot | `OnCalendar=*-*-* 00,04,08,12,16,20:05:00 UTC` |
| `liq-paper-bot.service` | simple | `Restart=always`, `RestartSec=30` |
| `liq-showcase-bot.service` | simple | `Restart=always`, `RestartSec=30` |
| `liq-telegram-bot.service` | simple | `Restart=always`, `RestartSec=30` |

No `docker-compose.yml` in this repo, no `crontab` for liquidation-bot. **However** the VPS hosts an out-of-repo Docker stack (`grafana` + `prometheus`, both up 10 days) plus several non-liquidation-bot strategy services (`aggressive`, `momentum`, `strategy_2h`, `leaderboard`, `portfolio`, plus a separate `telegram-bot.service`). See **§7 Notable additions discovered** for the full list — these are not part of the liquidation-bot codebase and may be a parallel project on the same host.

**Last-record timestamps for all tables:** see live data in §7 — full count + min/max for all 22 tables. All liquidation-bot collectors are healthy and current.

---

## §2 Data streams status

### Per-source feasibility table

| Source | Endpoint(s) used | Tables / storage | Coverage window (per CLAUDE.md) | Notes |
|---|---|---|---|---|
| **Hyperliquid public positions** | `POST /info {"type":"clearinghouseState","user":"0x..."}` per leaderboard address | `hl_position_snapshots`, `hl_addresses` | "every 15 min" since seed; exact start unknown without DB query | Yes — collected. `is_liq_estimated` flag distinguishes API-provided vs `entry × (1 ± 1/lev)` estimates |
| **Hyperliquid liquidation heatmap (per-level)** | Same `clearinghouseState` aggregated per `price_level` bucket | `hl_liquidation_map` | Started ~**2026-04-13** per CLAUDE.md (L6 / parking-lot idea #6) | Yes — this is the L6b retest substrate. ≥7 days data needed by 2026-04-24 |
| **Hyperliquid live trades** | `wss://api.hyperliquid.xyz/ws` | (in-memory in `hl_websocket.py`; CLAUDE.md describes WS but no Postgres trades table is in `SCHEMA_SQL`) | n/a | Stream is alive but does not appear to persist trades to Postgres. **Worth confirming via VPS dump.** |
| **CoinGlass aggregated liquidations** | `/api/futures/liquidation/aggregated-history` at `interval` ∈ `{30m, h1, h2, h4}` | `coinglass_liquidations` (h4), `_h1`, `_h2`, `_30m` | h4: 180 days (Hobbyist single-request); h1/h2: 180 days (Startup `limit=4320/2160`); 30m: 90 days (Startup, `2026-01-17 → 2026-04-17`) | Yes — Startup tier ($79/mo) confirmed by CLAUDE.md L8 + L16. `startTime`/`endTime` ignored server-side; `limit` honored |
| **CoinGlass aggregated OI (OHLC)** | `/api/futures/open-interest/aggregated-history` at same intervals | `coinglass_oi` (h4), `_h1`, `_h2`, `_30m` | Same as liquidations | Yes |
| **CoinGlass aggregated funding rate** | `/api/futures/funding-rate/oi-weight-history?interval=h8` (fallback `vol-weight-history`, `interval=h4`) | `coinglass_funding` | h8: 540 records/coin per 180 days (~3 buckets/day) | Yes. Probe order documented in L3b-1 |
| **CoinGlass net-position** | `/api/futures/v2/net-position/history?exchange=Binance` (single-exchange) | `coinglass_netposition_h1/h2/h4` | 180 days each interval | Yes (L10 Phase 1 backfill) — single-exchange (Binance), no live collector |
| **CoinGlass aggregated CVD** | `/api/futures/aggregated-cvd/history` at h1/h2/h4 | `coinglass_cvd_h1/h2/h4` | 180 days each | Yes (L13 Phase 1 backfill) — no live collector |
| **CoinGlass health probe** | `/api/futures/supported-coins` | None (Telegram bot health check only) | n/a | One read-only call per `/health` Telegram command |
| **Binance Futures OI** | `GET /fapi/v1/openInterest?symbol=...` | `binance_oi` | Hourly cron (per L4 note: "binance_oi / binance_funding hold only ~21 days") | Yes — hourly collector |
| **Binance Futures funding rate** | `/fapi/v1/fundingRate?limit=1` | `binance_funding` | Same — ~21 days | Yes |
| **Binance Futures top trader L/S ratio** | `/futures/data/topLongShortAccountRatio` | `binance_ls_ratio` | Same | Yes — note `/futures/data/` path, not `/fapi/v1/` |
| **Binance Futures taker buy/sell** | `/futures/data/takerlongshortRatio` | `binance_taker` | Same | Yes |
| **Binance order book / tick / trades** | None | None | n/a | **NOT collected.** No code references; would require `wss://fstream.binance.com` + new schema |
| **Multi-exchange (OKX, Bybit, Bitget, dYdX)** | None directly. CoinGlass aggregates Binance + OKX + Bybit + Bitget + Hyperliquid + dYdX server-side via `exchange_list` param (L8 baseline) | n/a (only the aggregate is stored) | n/a | We rely on CoinGlass aggregation, never query other exchanges directly. Bitget pinged in `telegram_bot/health.py:152` as a liveness probe only |

### Concrete row counts (live from VPS, 2026-04-18 11:33 UTC)

Full per-table breakdown is in **§7 Postgres tables — live row counts**. Headlines:

- `hl_liquidation_map`: **114,901** rows (vs L6 estimate of ~2,800 after 3 days — actual rate is ~10× higher; 4d 15h coverage is well above the 8,000-row L6b pre-flight floor).
- `hl_position_snapshots`: **141,078** rows over 4d 16h.
- `coinglass_liquidations` (h4): **10,230** rows over 171 days; live updates via paper-bot side-effect insert.
- `coinglass_liquidations_h1` / `_h2`: 43,200 / 21,600 — full 180 days, but **stale by 2 days** (no live collector; last manual backfill 2026-04-16).
- `coinglass_liquidations_30m`: **43,200** rows over 90 days; stale by 19 hours.
- `coinglass_oi_30m`: **🚨 0 rows** — was apparently never backfilled (table created by L16 code, but the OI half of the L16 backfill did not run or used `--skip-oi`).
- `coinglass_netposition_h1/h2/h4`, `coinglass_cvd_h1/h2/h4`: all populated to 180-day cap, all stale by 1–2 days (no live collectors).
- Binance live tables (`binance_oi/funding/ls_ratio/taker`): all current (max ts within the last hour).

---

## §3 Tested approaches L1–L16 + L-jensen

Per [CLAUDE.md](../CLAUDE.md) "Tested-and-Rejected Approaches" table + per-session sections. Verdicts quoted from CLAUDE.md unless flagged otherwise. Notes column links artifacts in [analysis/](.) where present.

| L | Name | Hypothesis (1 line) | Verdict | Reason | Files / artifacts |
|---|---|---|---|---|---|
| L1 | (Initial collectors) | Stand up Hyperliquid + Binance public data feeds | DONE | n/a — infrastructure only | [collectors/](../collectors/) |
| L2 | `liquidation_flush` H1/H2/H3 baseline | Liquidation asymmetry → mean-reversion bounce | LOCKED BASELINE | Forms the substrate for all later research; not "passed" but used as reference | [scripts/backtest_liquidation_flush.py](../scripts/backtest_liquidation_flush.py) |
| L3 | Walk-forward + ATR stops + heatmap overlay | Validate L2 on 6-fold WF; size with ATR-based TP/SL | PARTIAL — only SOL passed walk-forward standalone | "5 altcoins failed" (CLAUDE.md L3b-2 motivation line) | [scripts/walkforward_h1_flush.py](../scripts/walkforward_h1_flush.py), [scripts/backtest_h1_with_stops.py](../scripts/backtest_h1_with_stops.py), [scripts/analyze_heatmap_signal.py](../scripts/analyze_heatmap_signal.py) |
| L3a | Bitget leaderboard analytics prototype | (mentioned as Variant D Option 3 fallback in master plan) | **DEPLOYED on VPS** as `leaderboard.service` ("Bitget Leaderboard Copy Trading Tracker"), active at dump time | No code in this repo — runs from a separate codebase on the same host. Confirms Variant-D-Option-3 is partially live already | (out-of-repo; service unit only) |
| L3b-1 | CoinGlass OI + funding backfill | Extend OI / funding history beyond Binance's 21-day window | DONE | Data layer; enabled L3b-2 | [scripts/backfill_coinglass_oi.py](../scripts/backfill_coinglass_oi.py) |
| L3b-2 | Combo signal backtest (9 combos × 10 coins) | Combine flush + OI + funding + breadth filters | **PASS — `market_flush` discovered.** Locked: `z_self>1.0` + `n_coins>=4`. Pooled Sharpe 5.60, Win 60.7%, N=422 | Single passing combo; the rest documented as also-rans | [scripts/backtest_combo.py](../scripts/backtest_combo.py); analysis dump per CLAUDE.md: `analysis/combo_L3b.txt` (not present in [analysis/](.) on this machine — likely VPS-only or untracked) |
| L4 | Paper trading bot | Deploy `market_flush` h4 to live paper trading | DONE | Bot stands; state file empty on dev box (see §4) | [bot/](../bot/), [scripts/test_paper_bot.py](../scripts/test_paper_bot.py) (19 assertions PASS) |
| L5 | Telegram command bot | On-demand status / pnl / health view | DONE | 71-assertion test suite PASS; 7 commands | [telegram_bot/](../telegram_bot/), [scripts/test_telegram_bot.py](../scripts/test_telegram_bot.py) |
| L6 | LiqMapAnalyzer (cluster magnet) | Price tends toward large liq clusters | INSUFFICIENT DATA (first pass) | Only ~2.5 days of `hl_liquidation_map` at run time | [scripts/analyze_liq_clusters.py](../scripts/analyze_liq_clusters.py), [scripts/test_liq_analyzer.py](../scripts/test_liq_analyzer.py) (41 assertions) |
| L6b | OI-normalized cluster strength v2 | Same as L6 but normalize cluster vol to OI | INSUFFICIENT DATA (second pass); **scheduled retest 2026-04-24** | Same data shortage; framework code is ready to rerun | [scripts/analyze_liq_clusters_v2.py](../scripts/analyze_liq_clusters_v2.py), [scripts/test_liq_analyzer_v2.py](../scripts/test_liq_analyzer_v2.py) (34 assertions) |
| L6c | Live CoinGlass OI collector | Keep `coinglass_oi` and `_funding` current between manual backfills | DONE | Service `liq-coinglass-oi.timer` runs every 4H + 5min buffer | [collectors/coinglass_oi_collector.py](../collectors/coinglass_oi_collector.py), [scripts/test_coinglass_collector.py](../scripts/test_coinglass_collector.py) (29 assertions) |
| L7 | BinanceExecutor / showcase live infra | Build real Binance Futures execution layer | DONE — infrastructure only | "actual live launch is L9 after paper results confirm edge"; not yet enabled per CLAUDE.md | [exchange/](../exchange/), [scripts/test_exchange.py](../scripts/test_exchange.py) (72 assertions) |
| L8 | Multi-timeframe `market_flush` h1/h2/h4 | Increase frequency on finer TFs | DONE — h4 baseline locked: Sharpe 5.87, Win 61.0%, N=428, 3/3 OOS positive | h1/h2 results not in CLAUDE.md tables here ("TBD — to be filled after VPS runs"); see L8 section | [scripts/backtest_market_flush_multitf.py](../scripts/backtest_market_flush_multitf.py), [scripts/test_backtest_multitf.py](../scripts/test_backtest_multitf.py) (34 assertions) |
| L10 P1 | Net Position data layer | Backfill aggregated NetPos from CoinGlass | DONE | Data layer only | [scripts/backfill_coinglass_netposition.py](../scripts/backfill_coinglass_netposition.py) |
| L10 P2 | NetPos research | "H1 Contrarian + H2 Confirmation" filters over `market_flush` | 18 variants → 1 stable MARGINAL (`H1_z1.5_h2`) | Pooled OOS Sharpe 5.15 / Win 61.6% / N=296 / 3/3 OOS positive | [scripts/research_netposition.py](../scripts/research_netposition.py), `scripts/test_research_netposition.py` |
| L10 P2b | H1_z1.5_h2 validation | 3 tests: correlation / rolling Sharpe / combined portfolio | **REJECT (ALARM)** | "Correlation 0.76 with h4 baseline → no diversification" | [scripts/validate_h1_z15_h2.py](../scripts/validate_h1_z15_h2.py); CLAUDE.md "tested-and-rejected" row 1; existing artifact in [analysis/validation_h1_z15_h2_2026-04-17.txt](validation_h1_z15_h2_2026-04-17.txt) |
| L11 | SHORT research | Symmetric SHORT-side study | DEFERRED | Not started; mentioned as fallback in multiple later sessions | (no script) |
| L13 P1 | CVD data layer | Backfill aggregated CVD | DONE | Data layer | [scripts/backfill_coinglass_cvd.py](../scripts/backfill_coinglass_cvd.py) |
| L13 P2 | CVD as filter over `market_flush` | 18 variants × 3 TFs | **REJECT — 18/18 FAIL** | "CVD extreme events do not coincide with flush events" — 96–100% of baseline trades filtered out | [scripts/research_cvd.py](../scripts/research_cvd.py); existing artifact in [analysis/cvd_research_2026-04-17.txt](cvd_research_2026-04-17.txt) |
| L13 P3 / 3b / 3c | CVD standalone (not as filter) | H5 exhaustion + H7 divergence as direct entry | **REJECT** — 1 MARGINAL (H5_z2.5_h2) failed Phase 3c Smart Filter adequacy | Phase 3b ALARM was h4 baseline rolling Sharpe FAIL; Phase 3c re-tested with actual Smart Filter rules → still inadequate | [scripts/research_cvd_standalone.py](../scripts/research_cvd_standalone.py), [scripts/validate_h5_z25_h2.py](../scripts/validate_h5_z25_h2.py), [scripts/smart_filter_adequacy.py](../scripts/smart_filter_adequacy.py) |
| L14 P1 | Per-coin breadth relaxation (K∈{0,1,2,3,4}) | Drop / lower the breadth gate to disperse trading days | **REJECT** — "edge and dispersion mutually exclusive within market_flush" | K=0_h4 reaches Min30dTD=15 but Sharpe collapses to 0.71; the breadth gate IS the edge | [scripts/research_breadth.py](../scripts/research_breadth.py) |
| L15 P1 | Funding rate z-score standalone | Snapshot-contrarian LONG/SHORT on funding extremes | **REJECT — 6/6 FAIL.** H2 systematic anti-edge (Sharpe −2.57) | Negative-funding extremes predicted continuation, not reversion → regime is trending, not mean-reverting | [scripts/research_funding_standalone.py](../scripts/research_funding_standalone.py) |
| L15 P2 | OI velocity z-score standalone | Velocity-contrarian on OI growth spikes + price direction | **REJECT — 6/6 FAIL.** Best Sharpe outlier-driven (auto-MARGINAL via `SUSPICIOUS_SHARPE`) | Universal Smart Filter adequacy failure | [scripts/research_oi_standalone.py](../scripts/research_oi_standalone.py); look-ahead guard added in commit `4026bb0` |
| L16 | `market_flush` retest at h30m (30-minute bars) | Same locked logic on finer timeframe → disperse trading days | SCAFFOLD COMPLETE; results "TBD" (CLAUDE.md L16 Results section explicitly empty) | `bar_minutes` refactor landed; 90-day backfill pulled. Awaiting VPS run | [scripts/backtest_market_flush_multitf.py](../scripts/backtest_market_flush_multitf.py) (refactored), [scripts/backfill_coinglass_hourly.py](../scripts/backfill_coinglass_hourly.py) (`30m` interval added) |
| **L-jensen** (post-L16, **NOT in CLAUDE.md**) | Jensen's alpha regression | CAPM regression of `market_flush` daily P&L on BTC daily returns; classify GENUINE_ALPHA / LEVERAGED_BETA / etc. | OPEN — script exists, not yet documented in CLAUDE.md "Sessions" sections | Verdict label set: `GENUINE_ALPHA` / `MIXED_ALPHA_BETA` / `LEVERAGED_BETA` / `NEGATIVE_ALPHA` / `WEAK_SIGNAL` / `INCONCLUSIVE`. Output goes to gitignored `analysis/jensen_report_<UTC>.md`. Two recent commits: `fab8b3f l-jensen: alpha-vs-beta gating regression for market_flush h4` and `264c4c8 l-jensen: local h4 loader to bypass L16 bar_hours float bug` | [analysis/jensen_alpha.py](jensen_alpha.py); test file `scripts/test_jensen_alpha.py` referenced in module docstring |

**Cumulative pattern (CLAUDE.md "Tested-and-Rejected" summary):** six positioning-adjacent continuous signals rejected on the same 2025-10 → 2026-04 sample. CLAUDE.md conclusion (verbatim): *"no positioning-adjacent continuous signal (funding, OI, netposition, CVD) produces a tradable edge compatible with Smart Filter requirements on this sample in this regime."*

---

## §4 L8 `market_flush` current state

### Locked configuration (do NOT modify per CLAUDE.md)

| Field | Value | Source |
|---|---|---|
| `z_threshold_self` (entry) | `1.0` (`long_vol_zscore > 1.0`) | L3b-2; `bot/config.py:BotConfig` defaults |
| `z_threshold_market` (breadth count) | `1.5` | L3b-2; constant `CROSS_COIN_FLUSH_Z` in `backtest_market_flush_multitf.py` |
| `min_coins_flushing` (K) | `4` | L3b-2 |
| `z_lookback` (rolling window) | `90` h4 bars = 15 calendar days | matches L2 `compute_signals` byte-for-byte |
| `holding_hours` | `8` | L3b-2 / `RANK_HOLDING_HOURS` |
| Side | LONG only | L3b-2 |
| Coins | `[BTC, ETH, SOL, DOGE, LINK, AVAX, SUI, ARB, WIF, PEPE]` | `collectors/config.COINS` |

### Backtest reference numbers (sample window 2025-10 → 2026-04, 180 days)

| Metric | Value | Source |
|---|---|---|
| Pooled Sharpe | **5.87** | L8 reference (used as parity check in L10/L13/L14/L15/L16) |
| Win % | **61.0%** | same |
| N trades | **428** | same |
| OOS folds positive | **3/3** | walk-forward 4-fold split |
| **Profit Factor** | unknown — not tracked in research scripts | grep of all `research_*.py` shows no PF computation |
| **Max DD** | unknown at strategy level. **Reported per 30-day window** in `smart_filter_adequacy.py`. CLAUDE.md notes some MARGINAL backtests showed >100% MDD (unit-return cumulative drawdown — not a bug, but caveat-worthy) |

### Paper trading status

| Field | Value |
|---|---|
| Exchange | Paper-only, ccxt Binance USDM Futures `BTC/USDT:USDT` for ticker prices (live ticker for entry, no real orders) |
| Started (per `LIVE_TRADING_MASTER_PLAN.md`) | **2026-04-15** |
| State file path | `state/paper_state.json` |
| **State on dev box (this machine)** | **MISSING** — only `.gitkeep` present in `state/`. `paper_state.json` does not exist locally; expected, since systemd unit targets the Linux VPS path |
| **State on VPS** (live, 2026-04-18 11:33 UTC) | `capital = $1014.13` (started $1000.00 → **+1.41 %**); `open_positions = 0`; `closed_trades = 5`; `equity_history` length 5; `last_summary_date = 2026-04-18` |
| Trades closed in paper | **5**; per `liq-paper-bot` log win rate **100.0 %** (very small N — not statistically meaningful yet) |
| Last cycle | 2026-04-18 08:05:31 UTC — `is_flush=False n_flushing=0`, no entries; sleeping until 12:05:00. Next cycle in ~30 min from VPS dump |
| Active monitoring | Telegram bot (commands `/status`, `/pnl`, `/trades`, `/positions`, `/health`, `/config`, `/help`) per L5; daily summary at 00:05 UTC; 4-API health pings (Binance, CoinGlass, Hyperliquid, Bitget). Authorized chat `229287803` |

### Live execution path

`exchange/` infrastructure exists per L7 (72 test assertions PASS); systemd unit `liq-showcase-bot.service` exists in repo. Per CLAUDE.md L7 deploy notes: NOT enabled by default; "actual live launch is L9 after paper results confirm edge". Per `LIVE_TRADING_MASTER_PLAN.md`: showcase deployment depends on L6b retest outcome (April 24) + ≥14 days of paper trading mixed-strategy validation.

---

## §5 Existing research framework reusability

If a new L18 strategy needs **X**, file **Y** provides it:

| Need | File | Capability |
|---|---|---|
| **Walk-forward fold split** | [scripts/walkforward_h1_flush.py](../scripts/walkforward_h1_flush.py) | `split_folds(index, n_folds)` — expanding-window 4-fold; reused by every research_*.py |
| **Per-bar metric computation** | [scripts/backtest_market_flush_multitf.py](../scripts/backtest_market_flush_multitf.py) | `_metrics_for_trades(returns, hours)` — N, Win%, Sharpe (annualized via `bar_hours`) |
| **Combo filter engine** | [scripts/backtest_combo.py](../scripts/backtest_combo.py) | `apply_combo(df, filters)` — boolean-mask combo runner over (col, op, threshold) tuples |
| **Variant runner + WF wrapper** | [scripts/research_netposition.py](../scripts/research_netposition.py) | `run_variant`, `run_walkforward`, `format_variant_block`, `format_final_ranking`, `SUSPICIOUS_SHARPE`, `_fmt_num` — the standard research-block harness reused by L13/L14/L15 |
| **Smart Filter adequacy gates** | [scripts/smart_filter_adequacy.py](../scripts/smart_filter_adequacy.py) | `compute_daily_metrics`, `simulate_smart_filter_windows`, `summarize_smart_filter_results`, `SMART_FILTER_CONFIGS` (30d/60d/90d) |
| **Per-trade record extraction** | [scripts/validate_h1_z15_h2.py](../scripts/validate_h1_z15_h2.py) | `extract_trade_records(dfs, filters, holding_hours)` — recovers per-trade list from a research-driver mask (parity-risky, see notes in CLAUDE.md L13 Phase 3b) |
| **Validation triple (correlation / rolling / combined)** | [scripts/validate_h1_z15_h2.py](../scripts/validate_h1_z15_h2.py) | `compute_correlation_test`, `compute_rolling_sharpe_test`, `compute_combined_portfolio_test` — the L10 Phase 2b template, reused in L13 Phase 3b |
| **Multi-TF feature builder** | [scripts/backtest_market_flush_multitf.py](../scripts/backtest_market_flush_multitf.py) | `build_features_tf`, `compute_signals_tf`, `_zscore_tf`, `_z_window`, `_lookback_24h`, `_forward_periods`, `_interval_to_bar_hours`, `_interval_to_bar_minutes` (post-L16), `_interval_to_ccxt_timeframe` |
| **Cross-coin breadth feature** | [scripts/backtest_combo.py](../scripts/backtest_combo.py) | `compute_cross_coin_features(per_coin, flush_z)` |
| **Per-coin loader (with PEPE fallback)** | [scripts/backtest_combo.py](../scripts/backtest_combo.py) | `_try_load_with_pepe_fallback(coin, loader_fn)` |
| **Substrate loaders** | [scripts/backtest_market_flush_multitf.py](../scripts/backtest_market_flush_multitf.py) | `load_liquidations_tf`, `load_oi_tf`, `load_funding`, `fetch_klines_ohlcv` |
| **Look-ahead guard** | `scripts/research_funding_standalone.py`, `scripts/research_oi_standalone.py` | `check_lookahead_guard(folds, min_n)`, `SUSPICIOUS_WIN_RATE_MIN_N=30` (added in commit `4026bb0`) |
| **Jensen alpha / CAPM regression** | [analysis/jensen_alpha.py](jensen_alpha.py) | `assert_unit_consistency`, `_run_ols` (HAC robust), `resolve_verdict`, `compute_clustering_metrics`, `run_subsample_stability`, `format_report`. Reusable as a generic alpha-vs-beta gate for any new daily P&L series |
| **Multi-strategy orchestration** | none yet | No registry-driven multi-strategy runner. `telegram_bot/registry.py` has a `REGISTRY` of strategy slots (4h live, 2H/1H stubs) but it is read-only display only |

---

## §6 Open parking lot items

From [LIVE_TRADING_MASTER_PLAN.md](../LIVE_TRADING_MASTER_PLAN.md) and [parking_lot_ideas.md](../parking_lot_ideas.md):

### Active / scheduled

| Item | Status | Trigger date | Source |
|---|---|---|---|
| **L6b Predictive Liquidation Magnet** retest | DEFERRED | **2026-04-24** | `parking_lot_ideas.md` Idea #7. Pre-flight checklist: ≥7 days HL data, no gaps, schema stable, ≥8000 rows in `hl_liquidation_map` |
| **L6b → L12 Predictive Executor** | Sketch only | Conditional on L6b PASS | `parking_lot_ideas.md` Idea #6 (Russian section) — entry into magnet direction, TP at cluster price, 4–6h max hold |

### Variant D — business model re-evaluation (triggered if L6b fails)

Three concrete alternatives in `LIVE_TRADING_MASTER_PLAN.md`:
1. **Alternative platform** — Bybit, OKX, Bitget copy-trading (different filter rules)
2. **Different asset class** — equity index futures, FX majors using same liquidation-cascade mechanic
3. **Different product** — analytics/data product (Bitget leaderboard, liquidation alerts, research-as-a-service)

### Other parked ideas (from `parking_lot_ideas.md` Russian section)

| # | Idea | Trigger condition |
|---|---|---|
| #1 | Trade on PATTERN CHANGE | Need 3+ months of history on existing signals before evaluating |
| #2 | Trade against own copy traders (reflexivity) | Requires ≥1 active copy trader; impossible until live deployment |
| #3 | News / sentiment | New data pipeline; quarter 2+ effort |
| #4 | Adaptive weights / online learning | Need ≥100 live trades to avoid overfit |
| #5 | SHORT as gater for LONG | Could be added during L11 SHORT research as a variant |

### L11 SHORT research

Mentioned as fallback in CLAUDE.md L13 Phase 2b / L14 / L15 but never started. No `research_short_*.py` in `scripts/`. Implicitly deferred behind L6b.

### No cross-exchange arbitrage scoping

No artifact mentions cross-exchange arbitrage (basis trades, funding arbitrage, etc.) as a scoped or parked item.

---

## §7 Current VPS state

Live data from `scripts/vps_state_dump.sh` run on the production VPS at **2026-04-18 11:33 UTC**.

### Host

| Field | Value |
|---|---|
| Host | `ubuntu-4gb-nbg1-1` (Hetzner Cloud Nuremberg, 4GB) |
| OS | Ubuntu 24.04.4 LTS (Noble Numbat), kernel 6.8.0-106 |
| Uptime | 16 days 17 hours |
| Load avg | 0.00 / 0.00 / 0.00 (idle) |
| User | `ivan` |
| Disk | 19G / 75G used (27%) — comfortable headroom |
| Project size | `~/liquidation-bot` = 493 MB; `analysis/` = 460 KB; `state/` = 12 KB |
| Postgres data dir | unknown (sudo password required for `du`) |

### **🚨 Notable additions discovered (NOT documented in CLAUDE.md)**

The VPS hosts a parallel set of strategies, tooling, and observability that are not part of the liquidation-bot repo or its CLAUDE.md sessions. Architect should know these exist before scoping L18:

1. **Docker observability stack — running 10 days**:
   - `grafana` (`grafana/grafana:latest`) — dashboards
   - `prometheus` (`prom/prometheus:latest`) — metrics scrape
   - **No `docker-compose.yml` in this repo.** These containers are deployed outside the liquidation-bot codebase.
2. **Other strategy services running side-by-side** (per `systemctl list-units`):
   - `aggressive.service` — "Aggressive 1H Momentum Strategy (Paper)" — **active**
   - `momentum.service` — "Momentum Cross-Sectional Strategy (Paper)" — **active**
   - `momentum-healthcheck.service` + `.timer` — **active**, fires every 30 min
   - `strategy_2h.service` — "Strategy-2H Cross-Sectional Momentum (Paper)" — **active**
   - `leaderboard.service` — "Bitget Leaderboard Copy Trading Tracker" — **active** (this is the L3a / Variant-D-Option-3 prototype mentioned in `LIVE_TRADING_MASTER_PLAN.md` — already deployed)
   - `portfolio.service` + `portfolio.timer` — "Daily Portfolio Summary Report", fires `00:05 UTC` daily
   - `telegram-bot.service` — "Telegram Status Bot" — **separate from `liq-telegram-bot.service`**
3. **Tmux session `showcase`** open since 2026-04-16 — interactive workspace, not in any service file.
4. **Untracked git artifacts** — repo on VPS is at `master` (origin synced) but has many untracked files: `analysis/*.txt` (16 result files from L2/L3/L8/L10/L13/L14/L15/L16 runs that were never committed) plus several **likely-accidental shell-redirect outputs at repo root**: files literally named `1`, `1.0`, `2.0`, `55%`, `REJECT_BOTH`. **Worth cleaning up** but no functional impact.
5. **`state/showcase_state.json.lock` exists** — `liq-showcase-bot.service` was started at some point even though it's currently inactive; the lock file is stale.
6. **L18 commit `d5672fc l18 invistigate licvidation bot strategy` is on VPS HEAD** — dev-box gitStatus showed `264c4c8` (L-jensen) as top commit, so dev box is **one commit behind VPS** for the L18 work. Architect should `git pull` before drafting L18 plan.

### Liquidation-bot systemd unit state

| Unit | Active | Enabled | Last run / signal |
|---|---|---|---|
| `liq-hl-websocket.service` | ✅ active (running) | enabled | Streaming continuously, last `LARGE TRADE` log @ 11:33:21 UTC |
| `liq-hl-snapshots.service` (oneshot) | inactive (idle) | enabled via timer | Last fired 11:30:17, next 11:45:00 (`*:0/15`). Each run takes ~31s, finds ~314 positions, 256 map levels |
| `liq-binance.service` (oneshot) | inactive (idle) | enabled via timer | Last fired 11:00:17, next 12:00:00 (`hourly`). Each run ~14s, 0 errors |
| `liq-coinglass-oi.service` (oneshot) | inactive (idle) | enabled via timer | Last fired 08:05:00, next 12:05:00. Last run +10 OI rows, +10 funding rows, 0 errors |
| `liq-paper-bot.service` | ✅ active (running) | enabled | Last cycle 08:05:00 — `is_flush=False`, no entries; sleeping until 12:05:00. **5 trades closed lifetime, 100% win, equity $1014.13** |
| `liq-showcase-bot.service` | ❌ inactive (dead) | **`enabled=not-found`** (unit file not installed at `/etc/systemd/system/`) | No journal entries (never started). **Showcase trading is not deployed.** |
| `liq-telegram-bot.service` | ✅ active (running) | enabled | Started 2026-04-15 16:50; authorized chat 229287803 |

**Errors in last hour (all liquidation units):** none.

### Postgres tables — live row counts

Connection via `localhost:5432` as `postgres` to db `liquidation` succeeded.

| Table | Rows | Min ts | Max ts | Notes |
|---|---:|---|---|---|
| `hl_addresses` | 500 | 2026-04-13 19:35 | 2026-04-13 19:35 | Seeded once, no updates |
| `hl_position_snapshots` | **141,078** | 2026-04-13 19:35 | 2026-04-18 11:30 | 4d 16h coverage |
| `hl_liquidation_map` | **114,901** | 2026-04-13 19:35 | 2026-04-18 11:30 | Same span; **L6b retest substrate** |
| `binance_oi` | 6,130 | 2026-03-24 16:00 | 2026-04-18 11:00 | ~25 days, hourly |
| `binance_funding` | 1,152 | 2026-03-15 12:00 | 2026-04-18 16:00 | ~34 days |
| `binance_ls_ratio` | 5,847 | 2026-03-24 16:00 | 2026-04-18 11:00 | ~25 days |
| `binance_taker` | 5,950 | 2026-03-24 15:00 | 2026-04-18 09:00 | ~25 days |
| `coinglass_liquidations` (h4) | 10,230 | 2025-10-30 | 2026-04-18 08:00 | 171 days, 10 coins × ~1023 |
| `coinglass_liquidations_h1` | 43,200 | 2025-10-18 20:00 | 2026-04-16 19:00 | 180 days, 10 × 4320. **Stale by 2 days** — last manual backfill, no live collector |
| `coinglass_liquidations_h2` | 21,600 | 2025-10-18 20:00 | 2026-04-16 18:00 | Same staleness |
| `coinglass_liquidations_30m` | 43,200 | 2026-01-17 16:30 | 2026-04-17 16:00 | 90 days, L16 backfill. **Stale by 19h** |
| `coinglass_oi` (h4) | 10,170 | 2025-10-30 04:00 | 2026-04-18 08:00 | Live via `liq-coinglass-oi.timer`, current |
| `coinglass_oi_h1` | 43,200 | 2025-10-18 20:00 | 2026-04-16 19:00 | Stale by 2 days |
| `coinglass_oi_h2` | 21,600 | 2025-10-18 20:00 | 2026-04-16 18:00 | Stale by 2 days |
| `coinglass_oi_30m` | **0** | — | — | **🚨 EMPTY** despite L16 plan calling for 30m OI backfill. CLAUDE.md L16 documents the table existing but the data layer was apparently not backfilled (script supports `--skip-oi` flag — likely run with it accidentally) |
| `coinglass_funding` | 5,510 | 2025-10-17 | 2026-04-18 08:00 | Live, current |
| `coinglass_netposition_h1` | 43,200 | 2025-10-18 21:00 | 2026-04-16 20:00 | Stale by 2 days |
| `coinglass_netposition_h2` | 21,600 | 2025-10-18 22:00 | 2026-04-16 20:00 | Stale by 2 days |
| `coinglass_netposition_h4` | 10,800 | 2025-10-19 00:00 | 2026-04-16 20:00 | Stale by 2 days |
| `coinglass_cvd_h1` | 43,200 | 2025-10-19 07:00 | 2026-04-17 06:00 | Stale by 1.2 days |
| `coinglass_cvd_h2` | 21,600 | 2025-10-19 08:00 | 2026-04-17 06:00 | Same |
| `coinglass_cvd_h4` | 10,800 | 2025-10-19 08:00 | 2026-04-17 04:00 | Same |

**Top 5 tables by size:** `hl_position_snapshots` 27 MB · `hl_liquidation_map` 14 MB · `coinglass_netposition_h1` / `_liquidations_h1` / `_oi_h1` ~7.5 MB each. Total schema fits comfortably; Postgres is not a constraint.

### `hl_liquidation_map` per-coin coverage (L6b pre-flight)

Per the `parking_lot_ideas.md` Idea #7 checklist (≥7 calendar days needed by 2026-04-24):

| Coin | Snapshots | First | Last | Span |
|---|---:|---|---|---|
| ARB | 3,157 | 2026-04-13 | 2026-04-18 | 4d 15h |
| AVAX | 9,767 | 2026-04-13 | 2026-04-18 | 4d 15h |
| BTC | 34,201 | 2026-04-13 | 2026-04-18 | 4d 15h |
| DOGE | 10,612 | 2026-04-13 | 2026-04-18 | 4d 15h |
| ETH | 22,616 | 2026-04-13 | 2026-04-18 | 4d 15h |
| LINK | 7,270 | 2026-04-13 | 2026-04-18 | 4d 15h |
| SOL | 13,778 | 2026-04-13 | 2026-04-18 | 4d 15h |
| SUI | 7,778 | 2026-04-13 | 2026-04-18 | 4d 15h |
| WIF | 5,722 | 2026-04-13 | 2026-04-18 | 4d 15h |
| **PEPE** | **— (missing from per-coin output)** | — | — | **🚨 Verify whether PEPE is being collected at all** — could be a `canonical_coin()` mapping issue (HL stores `kPEPE` but `hl_snapshots.py` is supposed to canonicalize). |

**L6b retest readiness assessment:** at 4d 15h on 2026-04-18, the 7-day prerequisite hits on **2026-04-20 ~10:35 UTC** — comfortably ahead of the scheduled 2026-04-24 retest. Total `hl_liquidation_map` row count = 114,901 (well above the 8,000-row pre-flight floor). **Open issue: PEPE coverage** — must be resolved before retest, otherwise altcoin pool drops from 10 to 9.

### Paper trading state (live from `state/paper_state.json`)

| Field | Value |
|---|---|
| `capital` | **$1014.13** (started $1000.00 → +1.41%) |
| `open_positions` | 0 |
| `closed_trades` | **5** |
| `equity_history` length | 5 |
| `last_summary_date` | 2026-04-18 |
| Win rate (per `liq-paper-bot` log) | **100.0%** (small N) |
| Last cycle | 2026-04-18 08:05 UTC, signal `is_flush=False n_flushing=0` (no entries) |

5 wins on 5 trades = positive sample but extremely small N — not yet meaningful for Smart Filter adequacy. All recent cycles show `is_flush=False`, consistent with CLAUDE.md's "clustering: 41 active days / 148" finding (most cycles produce no signal).

### Git state on VPS

- Branch `master`, synced with `origin/master`.
- HEAD = `d5672fc l18 invistigate licvidation bot strategy` (one commit ahead of dev box, which sits at `264c4c8`).
- Untracked: 16 `analysis/*.txt` result files + 5 likely-accidental root-level files (`1`, `1.0`, `2.0`, `55%`, `REJECT_BOTH`) + `state/showcase_state.json.lock`.

---

## §8 Data that can be added

### Hyperliquid

| Endpoint / stream | Status | Feasibility for L18 |
|---|---|---|
| `clearinghouseState` per address | **already collected** every 15 min into `hl_position_snapshots` | n/a |
| Aggregated liquidation map | **already collected** every 15 min into `hl_liquidation_map` | This IS the L6b substrate |
| Live trades WebSocket | **stream alive** but no Postgres trades table in schema (`hl_websocket.py` runs `liq-hl-websocket.service`, but `SCHEMA_SQL` has no `hl_trades` table — VPS dump should confirm where these go) | Medium — add a `hl_trades` table + insert path; rate-limit considerations small (single WS connection) |
| Public order book (`l2Book`) | **NOT collected** | High effort — high volume, would require sampling strategy + new schema |
| Funding history | **NOT collected directly from HL** (we use CoinGlass aggregated) | Low marginal value — already have CoinGlass aggregated |

### CoinGlass (current tier: Startup, $79/mo per CLAUDE.md L8)

**Currently used endpoints** (grepped in repo):

| Endpoint | Used in |
|---|---|
| `/api/futures/liquidation/aggregated-history` | `bot/signal.py`, `scripts/backfill_coinglass.py`, `scripts/backfill_coinglass_hourly.py` |
| `/api/futures/open-interest/aggregated-history` | `scripts/backfill_coinglass_oi.py`, `scripts/backfill_coinglass_hourly.py`, `collectors/coinglass_oi_collector.py` |
| `/api/futures/funding-rate/oi-weight-history` (fallback `vol-weight-history`) | `scripts/backfill_coinglass_oi.py`, `collectors/coinglass_oi_collector.py` |
| `/api/futures/v2/net-position/history` | `scripts/backfill_coinglass_netposition.py` |
| `/api/futures/aggregated-cvd/history` | `scripts/backfill_coinglass_cvd.py` |
| `/api/futures/supported-coins` | `telegram_bot/health.py:152` (read-only health check only) |

**Endpoints documented in CG public API conventions but NOT used in this repo** (flagged for L18 evaluation — verify against current Starter-tier docs before relying):

| Likely-available endpoint | L18 implication if available |
|---|---|
| `/api/futures/liquidation/heatmap` (CG-side liquidation heatmap, server-side bucketing across multiple exchanges) | **Direct alternative substrate for L6b on the Binance side.** Currently the L6b prerequisite is Hyperliquid-only (`hl_liquidation_map`). A CG aggregated heatmap would: (a) extend the magnet hypothesis to Binance, where we actually trade, and (b) backfill historical heatmaps without waiting for HL collection. **High potential L18 value** |
| `/api/futures/liquidation/orders` (top single-event liquidation orders) | Could replace bucket-aggregated `_h1`/`_30m` data with event-stream granularity — useful if L18 wants to study individual liquidation cascades vs aggregate metrics |
| `/api/futures/longshort-ratio` (positions / global / top traders) | Currently we use Binance-direct `binance_ls_ratio`. CG version would aggregate across exchanges. Probably duplicative; only worth it if L18 specifically needs cross-exchange ratio |
| `/api/futures/orderbook/history` (L2 historical depth aggregated) | New axis entirely — not collected anywhere. Would enable order-flow-based hypotheses outside the rejected positioning-signal class |
| `/api/futures/large-limit-order/history` ("whale" limit orders detection) | Could enable a "smart-money limit-order" hypothesis — orthogonal to all rejected approaches (those measured aggregate flow, this measures specific large orders) |
| `/api/futures/whale-position/history` | Per-account positioning of large traders, aggregated. Adjacent to NetPos (already rejected) but at a different granularity |
| Per-exchange breakdowns (most aggregated endpoints accept `exchange=Binance` param vs the aggregated `exchange_list`) | We currently only consume aggregated multi-exchange views (and Binance-only NetPos as a hand-picked case). Per-exchange divergence between Binance and other venues might surface signals that aggregate views average away |
| Spot-side endpoints (`/api/spot/...`) | NOT collected. Could enable spot-vs-perp basis hypotheses |

**Caveat:** the above "likely-available" list is inferred from CG documentation conventions and the URL family pattern observed in our existing endpoints. The exact set available on the Startup tier should be verified against the current CG dashboard before scoping any L18 work that depends on a specific endpoint.

### Cross-exchange funding comparison

| Exchange | Code present? | Feasibility |
|---|---|---|
| Binance | yes (`binance_collector.py`, ccxt `BINANCEUSDM` in `exchange/binance_client.py`) | n/a — already in |
| Hyperliquid | yes (positions/liq); funding data NOT collected per-coin | Medium — HL exposes funding rates via `meta` and `clearinghouseState`; could add a small periodic collector |
| Bybit | only health-pinged via `telegram_bot/health.py`'s endpoint list (CLAUDE.md mentions Bitget, Bybit not pinged); NO data collection | Medium — public REST funding endpoint, would need new schema |
| OKX, Bitget | not collected | Same — public endpoints, schema work + a hourly cron |

### Binance VIP / API-tier access

**No evidence of VIP-tier auth in code.** `exchange/binance_client.py` accepts `LIQ_BINANCE_API_KEY` / `LIQ_BINANCE_API_SECRET` for trading auth (per `.env.example`). All data collection in `collectors/binance_collector.py` uses **public** endpoints (no API key required) — confirmed by CLAUDE.md "all public, no API key needed" line. There is no VIP-tier or Spot-API-tier classification in the codebase. The `ExchangeConfig` carries no VIP flags.

---

**End of state dump.** For follow-up:
- Run [scripts/vps_state_dump.sh](../scripts/vps_state_dump.sh) on the VPS for §1, §2, §4 live numbers
- Decide whether L-jensen work needs a CLAUDE.md "Session L-jensen" section before L18 starts (not currently documented there)
- L6b retest still gates 2026-04-24 — pre-flight per `parking_lot_ideas.md` Idea #7
