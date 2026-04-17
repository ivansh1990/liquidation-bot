# PARKING LOT — Deferred Ideas

## Idea #7: L6b Predictive Liquidation Magnet — Scheduled Retest 2026-04-24

**Origin:** L6 (first pass, INSUFFICIENT DATA) and L6b (second pass, INSUFFICIENT DATA) were both aborted due to Hyperliquid liquidation heatmap collector having fewer than 7 days of backfilled data. The signal class itself is untested, not rejected.

**Status:** DEFERRED pending data availability. Retest scheduled 2026-04-24.

### Why retest is worth doing

- **Only remaining untested hypothesis class.** After L10/L13/L14/L15 cumulative rejections (all positioning-adjacent continuous signals), L6b represents a principally different mechanism: price-targeting magnet effect on liquidation clusters.
- **Orthogonal to all rejected approaches.** Not a positioning extreme, not a velocity, not a funding snapshot. Targets where liquidations *will be triggered* based on cluster density, not where positioning is extreme now.
- **If rejected, exhausts the positioning+magnet signal space entirely** and hands clean decision data to Variant D evaluation.
- **Cheap to test.** Data already collecting since 2026-04-13 (no new backfill investment); research framework (split_folds, smart_filter_adequacy, dual-track PASS criteria) already in place.

### Data requirements at retest time

- **Minimum 7 calendar days** of HL liquidation heatmap data for all 10 coins (BTC, ETH, SOL, DOGE, LINK, AVAX, SUI, ARB, WIF, PEPE)
- Snapshot cadence: every 15 minutes (per current collector design)
- **Pre-retest validation checklist:**
  - [ ] No gaps > 1 hour in heatmap data for any coin since 2026-04-13
  - [ ] Schema consistent (no mid-sample changes from collector updates)
  - [ ] Price data (Binance h4) synchronized with heatmap timestamps
  - [ ] Storage size sane (expect ~50-100 MB total across all coins for 11 days)

If any checklist item fails → defer retest by 7 days and investigate data pipeline health.

### Methodology (adapted for shorter data window)

**Original L6b design** used 30-day windowing for cluster detection. With only 11 days of data at retest time, adapt:

- **Shorter cluster detection window.** Use 3-day rolling window (288 snapshots) instead of original 30-day. Accept reduced signal quality as tradeoff for feasibility.
- **Cross-coin pooling for statistical power.** Pool magnet events across 10 coins to reach N ≥ 100 minimum. Per-coin analysis likely infeasible until 30+ days accumulated.
- **Entry rule draft:**
  - For each coin, at each h4 bar:
    - Identify largest liquidation cluster within ±3% of current price from most recent HL snapshot
    - If current price is moving toward cluster (direction of price change aligns with cluster direction) AND cluster density > threshold
    - Entry: LONG toward cluster above price, or SHORT toward cluster below price
    - Hold: time-based 8h OR cluster touch, whichever first
- **Thresholds to test:** cluster density normalized per coin (z-score over 3-day window), grid {1.5, 2.0, 2.5} matching L15 convention for comparability.

Details to be refined in L6b retest plan (separate document to create 2026-04-23 when data availability confirmed).

### PASS criteria (dual-track, unchanged from L15 standard)

**Primary (L8 parity):**
1. Pooled OOS Sharpe > 2.0
2. Win% > 55%
3. N trades ≥ 100
4. ≥ 2/3 OOS folds positive Sharpe
5. Pooled OOS Sharpe > 1.0

**Strict (Smart Filter adequacy):**
6. Min 30d rolling trading days ≥ 14 — **likely infeasible at 11-day data window; relax to "median trading days ≥ 14 extrapolated"**
7. Median 30d trading days ≥ 14
8. Median 30d win days ratio ≥ 65%
9. Max 30d MDD ≤ 20%

**Additional criterion for L6b specifically:**
10. Correlation with `market_flush` h4 < 0.5 (required — L6b must be complement, not duplicate)

### Verdict adaptation for short-window retest

Given 11-day data window is shorter than standard 180-day backtests, adjust verdict interpretation:

- **PASS** — requires all 10 criteria. Rare at 11-day window; if achieved, extends to h2/h1 (Phase 2b) and 30-day wait for full Smart Filter validation before deployment.
- **PROMISING** (new category specific to L6b retest) — primary 5 met, strict partial, correlation < 0.5. Action: extend data collection for 3 more weeks, re-run at 2026-05-15 with full 30-day window.
- **FAIL** — primary criteria fail OR correlation ≥ 0.5. Action: document as rejected alongside positioning-signal rejections, proceed to Variant D.

### Decision triggers post-retest

- **PASS or PROMISING:** follow branch A/B of LIVE_TRADING_MASTER_PLAN.md decision tree. Deploy to paper or extend research.
- **FAIL:** proceed to Variant D. L6b is the last research-path fallback before business-model re-evaluation.

### Resources / prerequisites

- HL collector running: `collectors/hyperliquid_heatmap.py` (or whatever actual filename) verified healthy since 2026-04-13
- Schema: `hyperliquid_heatmap` table populated (validate `SELECT COUNT(*), MIN(snapshot_time), MAX(snapshot_time) FROM hyperliquid_heatmap GROUP BY symbol;` shows expected cardinality)
- Reuse research framework from L15 (test harness, `check_lookahead_guard`, Smart Filter adequacy, walk-forward split)
- Hand Claude Code a new L6b retest plan file (to be drafted 2026-04-23 after data availability check)

### Expected time commitment

- Pre-retest data validation: 30 min on 2026-04-23
- L6b retest plan drafting (architect): 1-2 hours
- Claude Code execution: 1-2 hours
- Result analysis + documentation: 1 hour
- **Total: ~half-day session on 2026-04-24**

### Do NOT (at retest time)

- Do not extend data window beyond what's available — short-window testing is the point
- Do not add additional coins beyond current 10
- Do not test signal beyond h4 initially (h2/h1 deferred to Phase 2b if h4 PASS)
- Do not deploy to live or showcase account on L6b PASS alone — 14-day paper trading required first
- Do not abandon `market_flush` paper trading during L6b retest — parallel operation preserves current edge validation

---

**End of L6b parking-lot entry.**

## Идея #6: Predictive Liquidation Map — кандидат на замену/дополнение market_flush

**Создано:** 17 апреля 2026
**Статус:** DEFERRED — вернуться через неделю (24 апреля 2026)
**Условие возврата:** накопится 3+ недели данных `hl_liquidation_map` (с 13 апреля → данных достаточно к концу апреля)

### Суть идеи

Текущий `market_flush` бот **реактивный**: ждёт пока ликвидации случились → входит на отскок. Задержка = вход после того как большие деньги уже вошли.

Predictive подход: **предсказываем flush за 1-2 часа до того как он случится** через анализ Hyperliquid liquidation map. Если в карте большой кластер ликвидаций близко к market price — вероятность того что price будет туда двигаться высокая (magnet effect).

### Почему это принципиально другое

1. **Timing:** predictive vs reactive → лучшая entry price
2. **Механизм:** используем данные о позиционировании участников (где сидят liquidation levels), не результат их ликвидации
3. **Leading indicator:** видим цель движения до того как оно началось
4. **Lead trading pitch:** "предсказывает ликвидации" звучит сильнее чем "ждёт ликвидации" для копировщиков

### Что у нас уже есть

- `hl_liquidation_map` table собирается с 13 апреля 2026, каждые 15 минут
- `current_price` в каждом snapshot — можем аггрегировать mid-price
- L6 (`analyze_liq_clusters.py`) и L6b (`analyze_liq_clusters_v2.py`) — framework существует, провалился на малой выборке (2.5 дня)
- Pure functions уже написаны: `build_buckets`, `detect_clusters`, `compute_magnet_score`, `attach_oi_to_snapshots`

### Почему провалилось раньше (L6/L6b)

- **Только 2.5 дня данных** → недостаточно кластеров для статистической значимости
- Проекция: при 3+ неделях данных → 100+ кластеров на monету → walk-forward становится возможным
- В L6 paradigm отсекаем 99% кандидатов на "INSUFFICIENT DATA"; к концу апреля большинство пороговых уровней получит достаточно кластеров

### Критерии возврата к идее

Через неделю (24 апреля):

1. Проверить `COUNT(*) FROM hl_liquidation_map` — должно быть ≥ 8000 rows (11 дней × 96 snapshots × 10 coins)
2. Проверить покрытие для strong/mega clusters в L6b framework → должно быть ≥ 20 cells с N ≥ 20
3. Если оба условия — запустить L6b v3 (`analyze_liq_clusters_v2.py`) на свежих данных
4. Если результаты покажут magnet_score > 1.5 с hit_rate > 55% — это уже достаточный edge чтобы строить L12 Predictive Executor

### Что L12 Predictive Executor будет делать (эскиз, не план)

1. Каждые 15 мин читать `hl_liquidation_map`
2. Для каждой monetы находить ближайший strong/mega cluster (> OI threshold, < 5% от mid-price)
3. Если cluster найден + есть подтверждающий сигнал (например funding rate или OI build-up) → **открыть позицию в направлении magnet**
4. TP = цена кластера (−0.5% safety margin)
5. SL = symmetric distance от entry
6. Max holding time = 4-6 часов (magnets обычно срабатывают быстро)

### Отличие от market_flush

| Аспект | market_flush (текущий) | predictive_liquidation (идея #6) |
|--------|------------------------|-----------------------------------|
| Trigger | Ликвидации УЖЕ случились | Кластер ликвидаций формируется |
| Direction | Mean-reversion (против движения) | Trend-following (в направлении кластера) |
| Timing | Реактивный (после flush) | Предиктивный (до flush) |
| TP logic | Fixed % (5%) | Dynamic (цена кластера) |
| Holding | 8h time-based | Target-based + time fallback |
| Data source | coinglass_liquidations (aggregated) | hl_liquidation_map (per-level) |

### Риск профиль

**Положительный:**
- Независимая стратегия, низкая корреляция с market_flush (разные triggers, разные directions)
- Возможно **обе могут работать одновременно** → реальная diversification для Smart Filter
- Narrative для копировщиков сильнее

**Отрицательный:**
- HL liquidation map это **только Hyperliquid** — может не отражать Binance реальность полностью
- 11-15 дней данных всё ещё мало для walk-forward (будет 2-3 fold'а, не 4)
- Predictive backtest сложнее чем reactive — нужна careful entry timing simulation

### Связь с другими треками

- **L10 Phase 2b** (H1_z1.5_h2 validation) — может оказаться что diversification уже найдена через h2+NetPos, тогда идея #6 не срочна
- **L11 SHORT research** — если SHORT PASS, идея #6 становится третьей стратегией (3 независимых trade generators)
- **Live deployment** (через 3-4 недели) — может пойти без идеи #6, или мы отложим launch чтобы добавить

### Что делать сейчас

**Ничего.** Данные копятся автоматически через `hl-snapshots.timer`. Никаких действий до 24 апреля.

Если захочется посмотреть количество собранных данных раньше:

```bash
psql -d liquidation -c "SELECT coin, COUNT(*) AS snapshots, MIN(snapshot_time)::date AS first, MAX(snapshot_time)::date AS last FROM hl_liquidation_map GROUP BY coin ORDER BY coin;"
```

---

## Другие parked идеи (для контекста — не деферим, просто записываем)

### Идея #1: Trade на ИЗМЕНЕНИИ паттерна
Детектировать когда исторический сигнал перестаёт срабатывать — это само по себе информация. "Регрессия паттерна" → breakout expected. Требует 3+ месяца history. Возвращаться позже.

### Идея #2: Trading против своих копировщиков (reflexivity as feature)
Требует уже активной copy trading позиции. Невозможно до live. Рассмотреть через 2-3 месяца после первых копировщиков.

### Идея #3: News / sentiment
Требует новый data pipeline. Большая отдельная работа. Откладываем на квартал 2+.

### Идея #4: Adaptive weights
Online learning feedback loop. Risk overfitting на малых данных. Вернуться когда ≥ 100 live trades наберётся.

### Идея #5: SHORT как gater для LONG
Простая модификация, но не фундаментальная. Может быть добавлена во время L11 SHORT research как вариант теста (если SHORT сигнал обнаружен — "вычитать" его из LONG signal pool).
