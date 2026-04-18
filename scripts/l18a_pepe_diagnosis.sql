-- L18a Track 1 Step 1 — PEPE collection diagnosis
-- Run on VPS: psql liquidation -f scripts/l18a_pepe_diagnosis.sql
-- Paste full output back to architect for scenario interpretation.
--
-- Goal: distinguish between
--   Scenario B   — canonicalization regression (kPEPE rows leaked into tables)
--   Scenario AGG — canonicalization OK but liquidation-map aggregation drops PEPE
--   Scenario E   — no code bug; whales simply don't hold PEPE positions large
--                  enough to pass the min_usd filter (default $10k per hl_snapshots.py)
--   Scenario ??  — unexpected pattern → escalate

\pset pager off
\timing off

\echo '=============================================================='
\echo 'BLOCK 1: Canonicalization sanity — any kPEPE rows leaked?'
\echo '=============================================================='
\echo '  Expect: zero rows. Any hit means canonicalization broke.'

SELECT 'hl_liquidation_map' AS table_name, coin, COUNT(*) AS rows,
       MIN(snapshot_time) AS first_ts, MAX(snapshot_time) AS last_ts
  FROM hl_liquidation_map
 WHERE coin ILIKE '%pepe%' OR coin LIKE 'k%'
 GROUP BY coin
 ORDER BY coin;

SELECT 'hl_position_snapshots' AS table_name, coin, COUNT(*) AS rows,
       MIN(snapshot_time) AS first_ts, MAX(snapshot_time) AS last_ts
  FROM hl_position_snapshots
 WHERE coin ILIKE '%pepe%' OR coin LIKE 'k%'
 GROUP BY coin
 ORDER BY coin;

\echo ''
\echo '=============================================================='
\echo 'BLOCK 2: Per-coin coverage — hl_liquidation_map last 24h & 7d'
\echo '=============================================================='
\echo '  Expect: PEPE either present (good) or absent (confirms L6b blocker).'

SELECT coin,
       COUNT(*) AS snapshots_24h,
       COUNT(DISTINCT snapshot_time) AS distinct_snapshot_times_24h,
       MIN(snapshot_time) AS first_ts,
       MAX(snapshot_time) AS last_ts
  FROM hl_liquidation_map
 WHERE snapshot_time >= now() - interval '24 hours'
 GROUP BY coin
 ORDER BY snapshots_24h DESC;

SELECT coin,
       COUNT(*) AS snapshots_7d,
       COUNT(DISTINCT snapshot_time) AS distinct_snapshot_times_7d,
       COUNT(DISTINCT DATE(snapshot_time)) AS distinct_days
  FROM hl_liquidation_map
 WHERE snapshot_time >= now() - interval '7 days'
 GROUP BY coin
 ORDER BY snapshots_7d DESC;

\echo ''
\echo '=============================================================='
\echo 'BLOCK 3: Per-coin coverage — hl_position_snapshots last 24h'
\echo '=============================================================='
\echo '  If PEPE has rows here but NOT in hl_liquidation_map → aggregation bug.'
\echo '  If PEPE has zero rows here → Scenario E (whales do not hold PEPE).'

SELECT coin,
       COUNT(*) AS positions_24h,
       COUNT(DISTINCT address) AS distinct_traders_24h,
       ROUND(AVG(size_usd)::numeric, 2) AS avg_size_usd,
       ROUND(MIN(size_usd)::numeric, 2) AS min_size_usd,
       ROUND(MAX(size_usd)::numeric, 2) AS max_size_usd,
       COUNT(*) FILTER (WHERE NOT is_liq_estimated) AS real_liq_px_rows,
       COUNT(*) FILTER (WHERE is_liq_estimated)      AS estimated_liq_px_rows
  FROM hl_position_snapshots
 WHERE snapshot_time >= now() - interval '24 hours'
 GROUP BY coin
 ORDER BY positions_24h DESC;

\echo ''
\echo '=============================================================='
\echo 'BLOCK 4: PEPE-specific deep-dive — last 7d, ALL sizes'
\echo '=============================================================='
\echo '  Shows if PEPE positions exist at all (including sub-min_usd).'
\echo '  Collector filters with min_usd=10000 by default (hl_snapshots.py:147).'
\echo '  If this block is empty → whales track zero PEPE exposure → Scenario E confirmed.'

SELECT DATE(snapshot_time) AS day,
       COUNT(*) AS rows,
       COUNT(DISTINCT address) AS distinct_traders,
       COUNT(DISTINCT snapshot_time) AS distinct_snapshots,
       ROUND(MIN(size_usd)::numeric, 2) AS min_size,
       ROUND(AVG(size_usd)::numeric, 2) AS avg_size,
       ROUND(MAX(size_usd)::numeric, 2) AS max_size,
       SUM(CASE WHEN side='long'  THEN 1 ELSE 0 END) AS longs,
       SUM(CASE WHEN side='short' THEN 1 ELSE 0 END) AS shorts
  FROM hl_position_snapshots
 WHERE coin = 'PEPE'
   AND snapshot_time >= now() - interval '7 days'
 GROUP BY DATE(snapshot_time)
 ORDER BY day DESC;

\echo ''
\echo '=============================================================='
\echo 'BLOCK 5: Tracked addresses — seed coverage'
\echo '=============================================================='
\echo '  How many tracked whale addresses exist, and what are they currently'
\echo '  trading? Helps distinguish seed-list staleness from PEPE-specific absence.'

SELECT COUNT(*) AS tracked_addresses,
       COUNT(*) FILTER (WHERE total_volume_usd IS NOT NULL) AS have_volume_data,
       ROUND(AVG(total_volume_usd)::numeric, 0) AS avg_volume_usd
  FROM hl_addresses;

-- Top 20 coins currently held by tracked whales (any size)
SELECT coin,
       COUNT(DISTINCT address) AS traders_holding,
       COUNT(*) AS total_positions_last_cycle
  FROM hl_position_snapshots
 WHERE snapshot_time >= (
     SELECT MAX(snapshot_time) - interval '30 minutes'
       FROM hl_position_snapshots
 )
 GROUP BY coin
 ORDER BY traders_holding DESC
 LIMIT 20;

\echo ''
\echo '=============================================================='
\echo 'BLOCK 6: Freshness — last collection cycle per coin'
\echo '=============================================================='
\echo '  Confirms snapshots are running. PEPE absence here combined with'
\echo '  other coins being fresh → PEPE-specific issue (not a collector outage).'

SELECT coin,
       MAX(snapshot_time) AS last_snapshot,
       now() - MAX(snapshot_time) AS age
  FROM hl_liquidation_map
 GROUP BY coin
 ORDER BY last_snapshot DESC;

\echo ''
\echo 'DIAGNOSIS COMPLETE. Paste full output (all 6 blocks) back to architect.'
