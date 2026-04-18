# Liquidation Bot — State Dump for L18 Planning

**Generated:** 2026-04-18 (dev box, Darwin 24.6.0; not VPS)
**Repo:** `/Users/ivanshytikov/liquidation-bot`
**Branch:** `master`, clean, top commit `264c4c8 l-jensen: local h4 loader to bypass L16 bar_hours float bug`
**Source authority:** [CLAUDE.md](../CLAUDE.md) (1500+ lines, comprehensive through L16) + on-disk artifacts. Live DB is on the VPS, not reachable from dev box — see Section 7 for the companion shell script that must be run there.

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
| [collectors/hl_websocket.py](../collectors/hl_websocket.py) | Hyperliquid `wss://api.hyperliquid.xyz/ws` (live trades) | (in-memory; no DB writes from this process per CLAUDE.md) | Event-driven (continuous WS) | unknown — run `scripts/vps_state_dump.sh` | n/a (not a table) |
| [collectors/hl_snapshots.py](../collectors/hl_snapshots.py) | Hyperliquid REST `POST /info clearinghouseState` per address + `allMids` | `hl_position_snapshots`, `hl_liquidation_map`, `hl_addresses` | Timer `*:0/15` (every 15 min) | unknown | unknown — VPS only |
| [collectors/binance_collector.py](../collectors/binance_collector.py) | Binance Futures public REST: OI, funding, L/S ratio, taker | `binance_oi`, `binance_funding`, `binance_ls_ratio`, `binance_taker` | Timer `hourly` | unknown | unknown |
| [collectors/coinglass_oi_collector.py](../collectors/coinglass_oi_collector.py) | CoinGlass `/api/futures/open-interest/aggregated-history?interval=h4` + `/funding-rate/oi-weight-history?interval=h8` (Binance only) | `coinglass_oi`, `coinglass_funding` | Timer `*-*-* 00,04,08,12,16,20:05:00 UTC` (4H + 5 min buffer) | unknown | unknown |
| [bot/signal.py:SignalComputer.fetch_recent_liquidations](../bot/signal.py) | CoinGlass `/api/futures/liquidation/aggregated-history?interval=h4` (called by paper bot every cycle) | `coinglass_liquidations` (side-effect insert; not its own collector) | 4H-aligned + 5 min buffer (whenever paper bot runs) | depends on paper bot service status | unknown |
| [bot/scheduler.py](../bot/scheduler.py) (paper bot) | Aggregates signal + executes paper orders | `state/paper_state.json` | 4H-aligned + 5 min buffer | unknown — see §4 | n/a |
| [exchange/scheduler.py](../exchange/scheduler.py) (showcase bot) | Real Binance Futures execution | exchange-side orders + state (per L7 design) | 4H-aligned + 5 min buffer | per L7 spec: NOT enabled by default | n/a |
| [telegram_bot/app.py](../telegram_bot/app.py) | Telegram `getUpdates` long-poll | None (read-only) | Continuous long-poll | unknown | n/a |

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

No `docker-compose.yml`, no `crontab`. Pure systemd deploy.

**Last-record timestamps for all tables:** unknown — local Postgres unreachable from dev box (`psql: connection to server on socket "/tmp/.s.PGSQL.5432" failed`). Run [scripts/vps_state_dump.sh](../scripts/vps_state_dump.sh) on VPS for live counts.

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

### Concrete row counts

**Unknown — local DB unreachable from dev box.** All counts must be obtained from VPS via [scripts/vps_state_dump.sh](../scripts/vps_state_dump.sh) §5 / §6.

Expected order-of-magnitude (per CLAUDE.md):
- `coinglass_oi` (h4): ~10,000 rows (10 coins × 1000 buckets)
- `coinglass_funding` (h8): ~5,400 rows
- `coinglass_liquidations_30m`: ~43,200 rows (10 × 4320; first run April 17)
- `hl_liquidation_map`: ~2,800 unique snapshot×coin pairs after ~3 days (per L6 estimate)
- `coinglass_netposition_h1`: ~43,200 rows (10 × 4320 over 180 days)
- `coinglass_cvd_h1`: same shape

---

## §3 Tested approaches L1–L16 + L-jensen

Per [CLAUDE.md](../CLAUDE.md) "Tested-and-Rejected Approaches" table + per-session sections. Verdicts quoted from CLAUDE.md unless flagged otherwise. Notes column links artifacts in [analysis/](.) where present.

| L | Name | Hypothesis (1 line) | Verdict | Reason | Files / artifacts |
|---|---|---|---|---|---|
| L1 | (Initial collectors) | Stand up Hyperliquid + Binance public data feeds | DONE | n/a — infrastructure only | [collectors/](../collectors/) |
| L2 | `liquidation_flush` H1/H2/H3 baseline | Liquidation asymmetry → mean-reversion bounce | LOCKED BASELINE | Forms the substrate for all later research; not "passed" but used as reference | [scripts/backtest_liquidation_flush.py](../scripts/backtest_liquidation_flush.py) |
| L3 | Walk-forward + ATR stops + heatmap overlay | Validate L2 on 6-fold WF; size with ATR-based TP/SL | PARTIAL — only SOL passed walk-forward standalone | "5 altcoins failed" (CLAUDE.md L3b-2 motivation line) | [scripts/walkforward_h1_flush.py](../scripts/walkforward_h1_flush.py), [scripts/backtest_h1_with_stops.py](../scripts/backtest_h1_with_stops.py), [scripts/analyze_heatmap_signal.py](../scripts/analyze_heatmap_signal.py) |
| L3a | Bitget leaderboard analytics prototype | (mentioned only as Variant D fallback in master plan) | DEFERRED | Not part of trading critical path | (no script in repo with `L3a` prefix; mention only in `LIVE_TRADING_MASTER_PLAN.md`) |
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
| **State on dev box (this machine)** | **MISSING** — only `.gitkeep` present in `state/`. `paper_state.json` does not exist locally |
| **Diagnosis** | Paper bot has not been run on this dev box. Systemd services [systemd/liq-paper-bot.service](../systemd/liq-paper-bot.service) target `/home/ivan/liquidation-bot/.venv/...` (Linux VPS path), not the macOS dev box. Empty state here is expected behavior; the live state lives on the VPS only |
| **State on VPS** | unknown — must run [scripts/vps_state_dump.sh](../scripts/vps_state_dump.sh) §9. Section 9 of the script invokes Python to read `capital`, `positions[]`, `closed_trades[]`, `equity_history[]`, `last_summary_date` from the JSON |
| Trades closed in paper | unknown — same |
| Active monitoring | Telegram bot (commands `/status`, `/pnl`, `/trades`, `/positions`, `/health`, `/config`, `/help`) per L5; daily summary at 00:05 UTC; 4-API health pings (Binance, CoinGlass, Hyperliquid, Bitget) |

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

**This dev box is Darwin, not the production VPS.** The following commands were run locally and reflect dev-box state only:

```
$ uname -a
Darwin 24.6.0  (per environment block)
$ systemctl list-units ...
(unavailable on Darwin)
$ docker ps
(no docker installed; project is pure systemd, no compose file)
$ tmux list-sessions
(unknown — not run, dev box not running tmux for this project)
$ psql -d liquidation -c '\dt'
psql: error: connection to server on socket "/tmp/.s.PGSQL.5432" failed:
  No such file or directory  (no local Postgres on dev box)
```

### What to run on the VPS

`scripts/vps_state_dump.sh` (newly added) is a **read-only** shell script. It:

1. Sources `~/liquidation-bot/.env` for DB credentials.
2. Prints `uname`, `uptime`, OS release, current UTC time.
3. Lists all systemd services + timers; reports `is-active`/`is-enabled` per liquidation unit.
4. Gracefully reports docker and tmux as not-installed (expected).
5. Probes Postgres connectivity; if connected, reports `count + min(timestamp) + max(timestamp)` for **all 22 tables** in the documented schema (incl. inline-created `coinglass_*_h1/h2/30m`, `coinglass_netposition_*`, `coinglass_cvd_*`).
6. Per-coin row count + first/last day for `hl_liquidation_map` (the L6b retest pre-flight check from `parking_lot_ideas.md`).
7. Top 15 tables by `pg_total_relation_size`.
8. `df -h` on `/` and the project root, `du -sh` on `state/`, `analysis/`, `logs/`, and Postgres data dir if accessible.
9. Reads `state/paper_state.json` and prints capital / positions / closed_trades / equity_history length.
10. Last 30 lines of `journalctl` per liquidation unit.
11. Errors in the last hour across all liquidation units.
12. `git rev-parse --abbrev-ref HEAD`, `git log --oneline -10`, `git status -sb`.

**Run on VPS:**

```bash
cd ~/liquidation-bot
bash scripts/vps_state_dump.sh > /tmp/vps_state_$(date -u +%Y%m%dT%H%M%SZ).txt 2>&1
cat /tmp/vps_state_*.txt
```

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
