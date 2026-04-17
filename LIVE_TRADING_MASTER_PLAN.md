# Liquidation Bot — Live Trading Master Plan

## Current Status (as of 2026-04-17)

### What works
- **`market_flush` at h4** remains the only validated strategy. Backtest Sharpe 5.87, walk-forward 3/3 OOS positive on 180-day 2025-10 → 2026-04 sample. Deployed in paper trading since 2026-04-15.
- Paper bot infrastructure stable: data collection (Hyperliquid, CoinGlass Startup, Binance) reliable, Telegram bot operational (71 tests green, 7 commands), ~240 total test assertions across repo with 0 failures.

### What doesn't work (as of this sample and regime)
- **Smart Filter gate cannot be passed by `market_flush` alone.** Clustering (41 active calendar days / 148) structurally caps trading-days-per-30-day-window at ~8 median. Smart Filter requires ≥14. Confirmed by L13 Phase 3c diagnostic.
- **Breadth relaxation does not fix dispersion while preserving edge.** L14 Phase 1 tested K ∈ {0,1,2,3,4} × h1/h2/h4; only K=0_h4 gives dispersion ≥14 but Sharpe collapses to 0.71. Edge and dispersion are mutually exclusive within the flush architecture.
- **Continuous-signal complements have failed across six approaches.** NetPos (L10), CVD filter (L13 Phase 2), CVD standalone (L13 Phase 3), breadth (L14), funding standalone (L15 Phase 1), OI velocity (L15 Phase 2). All REJECT on 2025-10 → 2026-04 sample. See CLAUDE.md "Tested-and-Rejected Approaches" section for detail.

### Regime characterization (2025-10 → 2026-04)

Six-approach rejection pattern tells us something specific about the sample regime:

- **Trending, not mean-reverting.** Negative-funding extremes predicted continuation (L15 Phase 1 H2 anti-edge); OI velocity extremes produced no contrarian edge (L15 Phase 2). Classical "crowd is wrong at extremes" assumption does not hold.
- **Shorts systematically correct more often than the crowd.** Multiple SHORT hypotheses near-missed or outperformed their LONG mirrors (L15 Phase 1 H1 vs H2, L15 Phase 2 observation). Retail positioning on the long side tends to be punished.
- **Liquidation-flush events remain profitable when they fire.** `market_flush` h4 Sharpe 5.87 is not a fluke — it's a real edge. The problem is frequency, not quality.

Implication: strategies that depend on **crowd-wrong-at-extremes mean reversion** are broken in this regime. Strategies that depend on **event-driven forced selling** still work, but fire too infrequently for Smart Filter.

### What remains untested

**L6b Predictive Liquidation Magnet** — the only active hypothesis not yet rejected. Principally different signal class: price-targeting based on Hyperliquid liquidation cluster density, not positioning extremes. HL heatmap collector started 2026-04-13. Retest scheduled **April 24, 2026** (7-day minimum data requirement).

---

## Next Steps (Decision Tree)

### Primary path: L6b retest April 24

**Pre-conditions:**
- HL heatmap collector running without gaps since 2026-04-13 (verify via `collectors/` health check)
- ≥ 7 calendar days of liquidation heatmap data available for all 10 coins
- Daily snapshots stored with consistent schema (no mid-sample schema changes)

**Methodology:**
- Adapt L6b original design (see L6/L6b session notes in CLAUDE.md) to shorter data window.
- Primary hypothesis: price targets dense liquidation clusters with higher probability than uniform-prior baseline.
- If edge exists: entry when price is within N% of largest nearby cluster, direction toward cluster, exit on cluster touch or time-based.

**Success criteria:**
- Backtest primary 5 (Sharpe > 2.0, Win% > 55%, N ≥ 100, OOS ≥ 2/3 positive, Sharpe > 1.0)
- Smart Filter adequacy check (30d rolling windows, same 4 gates as L15)
- Correlation with `market_flush` h4 < 0.5 (genuine diversification)

### Branch A — L6b PASSes primary + SF adequacy

Deploy as complement to `market_flush` on showcase account. Paper trade for minimum 14 days. If combined portfolio passes Smart Filter gates in real-time paper trading, proceed to Binance lead-trader enrollment with:
- $500 showcase account
- 15x isolated leverage, fixed TP 5% / SL 3%
- Conviction filter z≥2.0 + n≥5 on market_flush side
- L6b adaptive thresholds per its own research findings

### Branch B — L6b PASSes primary, FAILs SF adequacy

MARGINAL case. Options:
- Combine L6b + market_flush + tune showcase parameters for mixed-strategy dispersion
- Extend L6b to h2/h1 (Phase 2b) if h4 shows near-miss pattern
- Defer lead-trader enrollment, paper trade mixed strategy for 30 days, re-evaluate

### Branch C — L6b FAILs

All tested hypothesis classes exhausted on this sample. Options:

**Variant D — Business model re-evaluation.** Three concrete alternatives to evaluate if reached:

1. **Alternative platform.** Investigate Bybit, OKX, Bitget copy-trading programs. Different filter criteria may accommodate market_flush's clustering (specifically, platforms that reward high Sharpe over temporal dispersion).

2. **Different asset class.** Same infrastructure (liquidation detection + breadth filter) on futures contracts with different market microstructure. Candidates: equity index futures, FX majors. Significant rework, but liquidation cascade mechanic may transfer.

3. **Different product.** Build data/analytics product leveraging existing infrastructure (HL + CoinGlass integrations, backtest framework) instead of trading. Examples: Bitget leaderboard analytics platform (L3a prototype already done), liquidation alert service, research-as-a-service.

**Variant D timeline:** ≥ 30 days evaluation per alternative before committing. Do not commit infrastructure investment before all three alternatives scored on effort/revenue/risk matrix.

### Branch D — L6b data gap (not enough data by April 24)

Defer retest by 7-day increments until data sufficient. Use wait time for Variant D scoping work (non-committal research, no infrastructure changes).

---

## Milestones and timeline

| Date | Milestone | Status |
|------|-----------|--------|
| 2026-04-13 | HL heatmap collector deployed | ✓ DONE |
| 2026-04-15 | Paper bot live on market_flush h4 | ✓ DONE |
| 2026-04-17 | L15 (funding + OI velocity) complete, REJECTED | ✓ DONE |
| 2026-04-17 | Documentation sweep complete | IN PROGRESS |
| **2026-04-24** | **L6b retest (primary + SF adequacy check)** | **SCHEDULED** |
| 2026-04-24 → 2026-05-08 | L6b paper trading (if PASS) OR Variant D scoping (if FAIL) | CONDITIONAL |
| 2026-05-08 | Go/no-go decision on Binance lead-trader enrollment | CONDITIONAL |
| 2026-05-15 | (Conditional) Lead-trader application submission | CONDITIONAL |

### Infrastructure commitments until decision point

- Keep paper bot running on market_flush h4 through L6b evaluation period (continuous real-world validation)
- Keep all data collectors running (HL, CoinGlass, Binance) — $79/mo CoinGlass + Hetzner VPS + Telegram bot ~$90/mo total
- Do NOT deploy showcase account capital until L6b decision + at least 14 days mixed-strategy paper trading
- Do NOT make live account changes (withdrawals, sub-accounts, leverage changes) until decision point

---

## Realistic expectations (calibrated by cumulative research)

The original target was **$800-1500/mo from ~50 followers × 15% monthly × 20% profit share** as a Binance lead-trader. Calibrating against evidence:

- `market_flush` alone cannot meet Smart Filter gates on this sample.
- 6 continuous-signal complements failed.
- Only L6b (untested) remains in-scope for Binance-specific path.
- P(L6b PASS + SF adequacy) — honestly unknown but not high given pattern. Rough estimate 20-30% based on "principally different signal class worth testing" reasoning, but no quantitative basis.
- If L6b fails, Variant D alternatives require 30+ days evaluation each. Revenue timeline shifts to 2026-Q3+ earliest.

**Honest framing:** the original $800-1500/mo × 50-follower goal assumed a working strategy existed. Current evidence suggests it may require Variant D (different platform / asset class / product). Timeline and revenue expectations should be updated after L6b outcome, not before.

**What this is not:** this is NOT a reason to abandon the project. The infrastructure (data collection, backtest framework, live bot architecture, Telegram integration) is valuable independent of the Binance lead-trader path. Variant D Option 3 (data/analytics product) is a concrete fallback that reuses 100% of current investment.

---

**End of update sections.**
