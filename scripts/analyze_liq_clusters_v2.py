#!/usr/bin/env python3
"""
L6b: Refined liquidation cluster magnet-effect analysis with OI-normalized
cluster strength.

Builds on L6 (analyze_liq_clusters.py) by normalizing cluster volume to
Open Interest per coin, creating a strength_pct metric, and building a
(distance x strength x coin) hit-rate matrix.

Key improvement over v1: absolute USD thresholds ($500K-$5M) are replaced
with OI-relative strength tiers (weak/medium/strong/mega), so a $1M cluster
on WIF (OI ~$100M) is correctly classified as "strong" (1%) while the same
$1M on BTC (OI ~$30B) is "weak" (0.003%).

Usage:
    .venv/bin/python scripts/analyze_liq_clusters_v2.py
"""
from __future__ import annotations

import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

# Path setup so sibling scripts and the collectors package both import.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)

from collectors.config import COINS, binance_ccxt_symbol, binance_raw_symbol, canonical_coin
from collectors.db import get_conn, init_pool

# Reuse v1 pure functions + DB/IO helpers (do not reimplement)
from analyze_liq_clusters import (
    BUCKET_PCT,
    HORIZON_HOURS,
    HORIZONS,
    SNAPSHOT_SAMPLE_INTERVAL,
    build_buckets,
    check_hit,
    compute_future_extremes,
    compute_hit_rate,
    compute_magnet_score,
    detect_clusters,
    fetch_klines_1h_ohlc,
    load_all_liq_map,
)

# ---------------------------------------------------------------------------
# Constants (v2)
# ---------------------------------------------------------------------------

THRESHOLD_V2: float = 500_000       # single floor threshold (noise filter)
MAX_DISTANCE_PCT: float = 5.0       # ignore clusters beyond 5%
OI_MAX_STALENESS_HOURS: int = 4

STRENGTH_TIERS: dict[str, tuple[float, float]] = {
    "weak":   (0.0, 0.5),
    "medium": (0.5, 2.0),
    "strong": (2.0, 5.0),
    "mega":   (5.0, float("inf")),
}
STRENGTH_ORDER: list[str] = ["weak", "medium", "strong", "mega"]
DISTANCE_BUCKETS: list[str] = ["0-1%", "1-2%", "2-3%", "3-4%", "4-5%"]

PEPE_ALIASES: list[str] = ["PEPE", "1000PEPE"]

# PASS criteria (v2)
MIN_ZONE_N: int = 20
MIN_HIT_8H: float = 50.0
MIN_MAGNET_8H: float = 1.5


# ---------------------------------------------------------------------------
# Pure functions (importable, tested by test_liq_analyzer_v2.py)
# ---------------------------------------------------------------------------

def compute_cluster_strength(cluster_usd: float, oi_usd: float) -> float:
    """
    Cluster strength as percentage of open interest.
    Returns (cluster_usd / oi_usd) * 100.
    Returns 0.0 if oi_usd <= 0, is NaN, or cluster_usd <= 0.
    """
    if oi_usd is None or (isinstance(oi_usd, float) and math.isnan(oi_usd)):
        return 0.0
    if oi_usd <= 0 or cluster_usd <= 0:
        return 0.0
    return (cluster_usd / oi_usd) * 100


def classify_strength(strength_pct: float) -> str:
    """
    Map strength percentage to tier label.
    Boundaries: 0.5 -> medium, 2.0 -> strong, 5.0 -> mega (inclusive lower).
    """
    if strength_pct >= 5.0:
        return "mega"
    if strength_pct >= 2.0:
        return "strong"
    if strength_pct >= 0.5:
        return "medium"
    return "weak"


def fine_distance_bucket_label(pct: float) -> str:
    """
    1%-width fine distance buckets for v2 matrix.
    Returns "0-1%", "1-2%", ..., "4-5%".
    Returns "" for pct >= 5.0 (caller should filter these out).
    """
    if pct < 0 or pct >= 5.0:
        return ""
    if pct < 1.0:
        return "0-1%"
    if pct < 2.0:
        return "1-2%"
    if pct < 3.0:
        return "2-3%"
    if pct < 4.0:
        return "3-4%"
    return "4-5%"


def attach_oi_to_snapshots(
    snap_df: pd.DataFrame,
    oi_df: pd.DataFrame,
    max_staleness_hours: int = 4,
) -> pd.DataFrame:
    """
    Attach OI values to snapshot rows using pd.merge_asof.

    snap_df must have columns: snapshot_time (datetime, tz-aware), coin (str)
    oi_df must have columns: timestamp (datetime, tz-aware), symbol (str),
                             open_interest (float)

    For each (snapshot_time, coin), finds the most recent OI reading where
    timestamp <= snapshot_time and age <= max_staleness_hours.

    Returns snap_df with added column 'oi_usd'. Rows where no OI is found
    within the staleness window get oi_usd = NaN.
    """
    if oi_df.empty or snap_df.empty:
        result = snap_df.copy()
        result["oi_usd"] = float("nan")
        return result

    # Align column names for merge
    oi_aligned = oi_df.rename(columns={"symbol": "coin", "timestamp": "snapshot_time"}).copy()
    oi_aligned = oi_aligned[["snapshot_time", "coin", "open_interest"]].sort_values("snapshot_time")

    snap_sorted = snap_df.sort_values("snapshot_time").copy()

    merged = pd.merge_asof(
        snap_sorted,
        oi_aligned,
        on="snapshot_time",
        by="coin",
        direction="backward",
        tolerance=pd.Timedelta(hours=max_staleness_hours),
    )
    merged.rename(columns={"open_interest": "oi_usd"}, inplace=True)
    return merged


def build_strength_matrix(
    results: list[dict],
    random_results: list[dict],
) -> list[dict]:
    """
    Build the (distance_bucket x strength_tier) hit-rate matrix.

    Each result dict must contain:
      - distance_pct: float
      - strength_pct: float (OI-normalized)
      - hit_1h, hit_4h, hit_8h, hit_24h: bool

    random_results must contain:
      - distance_pct: float
      - hit_1h, hit_4h, hit_8h, hit_24h: bool

    Returns list of cell dicts with keys:
      distance, strength, n, hit_rate_{h}, random_hr_{h}, magnet_{h}
    """
    # Group results by (distance_bucket, strength_tier)
    cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in results:
        dist = fine_distance_bucket_label(r["distance_pct"])
        tier = classify_strength(r["strength_pct"])
        if dist:
            cells[(dist, tier)].append(r)

    # Group random results by distance bucket only
    random_by_dist: dict[str, list[dict]] = defaultdict(list)
    for r in random_results:
        dist = fine_distance_bucket_label(r["distance_pct"])
        if dist:
            random_by_dist[dist].append(r)

    matrix: list[dict] = []
    for (dist, tier), cell_results in cells.items():
        n = len(cell_results)
        cell: dict = {"distance": dist, "strength": tier, "n": n}

        rand_pool = random_by_dist.get(dist, [])

        for h in HORIZONS:
            key = f"hit_{h}"
            c_hr = compute_hit_rate(cell_results, key)
            r_hr = compute_hit_rate(rand_pool, key) if rand_pool else 0.0
            ms = compute_magnet_score(c_hr, r_hr)
            cell[f"hit_rate_{h}"] = c_hr
            cell[f"random_hr_{h}"] = r_hr
            cell[f"magnet_{h}"] = ms

        matrix.append(cell)

    return matrix


def find_algorithmic_zones(
    matrix: list[dict],
    min_n: int = 20,
    min_hit_8h: float = 50.0,
    min_magnet_8h: float = 1.5,
) -> list[dict]:
    """
    Filter matrix cells meeting ALL:
      hit_rate_8h > min_hit_8h AND magnet_8h > min_magnet_8h AND n >= min_n
    Returns qualifying cells sorted by magnet_8h descending.
    """
    zones = [
        c for c in matrix
        if c.get("n", 0) >= min_n
        and c.get("hit_rate_8h", 0) > min_hit_8h
        and c.get("magnet_8h", 0) > min_magnet_8h
    ]
    zones.sort(key=lambda c: c.get("magnet_8h", 0), reverse=True)
    return zones


# ---------------------------------------------------------------------------
# DB / IO helpers (new for v2)
# ---------------------------------------------------------------------------

def explore_oi_schema() -> None:
    """Print coinglass_oi and binance_oi schema + data summary."""
    with get_conn() as conn:
        cur = conn.cursor()

        for table in ("coinglass_oi", "binance_oi"):
            print(f"\n  --- {table} ---")
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = %s
                ORDER BY ordinal_position
                """,
                (table,),
            )
            cols = cur.fetchall()
            if not cols:
                print(f"    (table does not exist)")
                continue
            print("  Schema:")
            for name, dtype in cols:
                print(f"    {name:<25} {dtype}")

            cur.execute(
                f"""
                SELECT symbol, COUNT(*) AS rows,
                       MIN(timestamp) AS first_ts,
                       MAX(timestamp) AS last_ts
                FROM {table}
                GROUP BY symbol ORDER BY symbol
                """
            )
            rows = cur.fetchall()
            print(f"\n  Data per symbol:")
            print(f"    {'symbol':<12} {'rows':>8} {'first':>22} {'last':>22}")
            for sym, cnt, first, last in rows:
                print(f"    {sym:<12} {cnt:>8} {str(first):>22} {str(last):>22}")

            # Sample rows
            cur.execute(f"SELECT * FROM {table} ORDER BY timestamp DESC LIMIT 3")
            samples = cur.fetchall()
            col_names = [desc[0] for desc in cur.description]
            print(f"\n  Sample rows (latest 3):")
            for s in samples:
                print(f"    {dict(zip(col_names, s))}")


def load_oi_data(coins: list[str]) -> pd.DataFrame:
    """
    Load OI from coinglass_oi (preferred, 167-day history, already in USD).
    Fallback to binance_oi.open_interest_usd for coins missing from coinglass.

    Returns DataFrame with columns: [timestamp, symbol, open_interest]
    All timestamps UTC-localized.
    """
    dfs: list[pd.DataFrame] = []
    loaded_coins: set[str] = set()

    with get_conn() as conn:
        # Primary: coinglass_oi
        for coin in coins:
            aliases = PEPE_ALIASES if coin == "PEPE" else [coin]
            found = False
            for alias in aliases:
                df = pd.read_sql(
                    "SELECT timestamp, symbol, open_interest FROM coinglass_oi "
                    "WHERE symbol = %s ORDER BY timestamp",
                    conn,
                    params=(alias,),
                    parse_dates=["timestamp"],
                )
                if not df.empty:
                    df["symbol"] = coin  # normalize to canonical
                    dfs.append(df)
                    loaded_coins.add(coin)
                    found = True
                    break
            if not found:
                pass  # will try binance fallback

        # Fallback: binance_oi for missing coins
        missing = set(coins) - loaded_coins
        if missing:
            print(f"  OI fallback to binance_oi for: {sorted(missing)}")
            for coin in missing:
                raw_sym = binance_raw_symbol(coin)
                df = pd.read_sql(
                    "SELECT timestamp, symbol, open_interest_usd AS open_interest "
                    "FROM binance_oi WHERE symbol = %s ORDER BY timestamp",
                    conn,
                    params=(raw_sym,),
                    parse_dates=["timestamp"],
                )
                if not df.empty:
                    df["symbol"] = coin  # normalize
                    dfs.append(df)
                    loaded_coins.add(coin)

    if not dfs:
        return pd.DataFrame(columns=["timestamp", "symbol", "open_interest"])

    result = pd.concat(dfs, ignore_index=True)
    if not result.empty and result["timestamp"].dt.tz is None:
        result["timestamp"] = result["timestamp"].dt.tz_localize("UTC")
    return result


# ---------------------------------------------------------------------------
# Printing helpers
# ---------------------------------------------------------------------------

def _print_matrix_table(matrix: list[dict], horizon: str = "8h") -> None:
    """Print hit-rate and magnet-score matrix tables for a given horizon."""
    hr_key = f"hit_rate_{horizon}"
    mg_key = f"magnet_{horizon}"
    rr_key = f"random_hr_{horizon}"

    # Index by (distance, strength)
    lookup: dict[tuple[str, str], dict] = {}
    for c in matrix:
        lookup[(c["distance"], c["strength"])] = c

    # Hit rates table
    print(f"\n  Hit rates ({horizon}) by distance x strength [ALL COINS]:")
    header = f"    {'':>8}"
    for tier in STRENGTH_ORDER:
        header += f"  {tier:>10}"
    header += "     N per cell"
    print(header)

    for dist in DISTANCE_BUCKETS:
        row = f"    {dist:>8}"
        ns = []
        for tier in STRENGTH_ORDER:
            cell = lookup.get((dist, tier))
            if cell and cell["n"] >= 10:
                row += f"  {cell[hr_key]:>9.1f}%"
            elif cell:
                row += f"  {'—':>10}"
            else:
                row += f"  {'·':>10}"
            ns.append(str(cell["n"]) if cell else "0")
        row += f"   ({','.join(ns)})"
        print(row)

    # Magnet scores table
    print(f"\n  Magnet scores ({horizon}) by distance x strength:")
    header2 = f"    {'':>8}"
    for tier in STRENGTH_ORDER:
        header2 += f"  {tier:>10}"
    print(header2)

    for dist in DISTANCE_BUCKETS:
        row = f"    {dist:>8}"
        for tier in STRENGTH_ORDER:
            cell = lookup.get((dist, tier))
            if cell and cell["n"] >= 10:
                val = cell[mg_key]
                if val == float("inf"):
                    row += f"  {'inf':>10}"
                else:
                    row += f"  {val:>10.2f}"
            elif cell:
                row += f"  {'—':>10}"
            else:
                row += f"  {'·':>10}"
        print(row)

    # Random hit rates (by distance, pooled across strength)
    print(f"\n  Random baseline ({horizon}) by distance:")
    for dist in DISTANCE_BUCKETS:
        # Find any cell with this distance to get the random rate
        for tier in STRENGTH_ORDER:
            cell = lookup.get((dist, tier))
            if cell:
                print(f"    {dist:>8}  {cell[rr_key]:>6.1f}%")
                break
        else:
            print(f"    {dist:>8}  {'—':>6}")


def _print_per_coin_matrix(results: list[dict], horizon: str = "8h") -> None:
    """Print per-coin best (strength, distance) combo."""
    hr_key = f"hit_{horizon}"

    # Group by coin
    by_coin: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_coin[r.get("coin", "?")].append(r)

    print(f"\n  Per-coin breakdown ({horizon}):")
    for coin in sorted(by_coin.keys()):
        coin_results = by_coin[coin]

        # Group into cells
        cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for r in coin_results:
            dist = fine_distance_bucket_label(r["distance_pct"])
            tier = classify_strength(r["strength_pct"])
            if dist:
                cells[(dist, tier)].append(r)

        best_cell = None
        best_hr = -1.0
        for (dist, tier), cell_res in cells.items():
            n = len(cell_res)
            if n < 5:
                continue
            hr = compute_hit_rate(cell_res, hr_key)
            if hr > best_hr:
                best_hr = hr
                best_cell = (dist, tier, n, hr)

        if best_cell:
            dist, tier, n, hr = best_cell
            print(f"    {coin:<6}  best cell = ({tier}, {dist}),  hit={hr:.1f}%,  N={n}")
        else:
            print(f"    {coin:<6}  insufficient data")


# ---------------------------------------------------------------------------
# Main analysis pipeline
# ---------------------------------------------------------------------------

def run_analysis_v2(
    liq_df: pd.DataFrame,
    oi_df: pd.DataFrame,
    klines_cache: dict[str, pd.DataFrame],
) -> None:
    """Run the v2 cluster analysis with OI-normalized strength."""

    # --- Step 1: Prepare snapshot-coin pairs ---
    snap_coins = (
        liq_df[["snapshot_time", "coin", "current_price"]]
        .drop_duplicates(subset=["snapshot_time", "coin"])
        .sort_values("snapshot_time")
    )
    unique_times = sorted(snap_coins["snapshot_time"].unique())
    sampled_times = set(unique_times[::SNAPSHOT_SAMPLE_INTERVAL])
    snap_coins = snap_coins[snap_coins["snapshot_time"].isin(sampled_times)].copy()
    print(f"\n  Snapshots total: {len(unique_times)}, sampled (every {SNAPSHOT_SAMPLE_INTERVAL}th): {len(sampled_times)}")
    print(f"  Unique (snapshot, coin) pairs to process: {len(snap_coins)}")

    # --- Step 1b: Attach OI ---
    snap_coins = attach_oi_to_snapshots(snap_coins, oi_df, OI_MAX_STALENESS_HOURS)
    oi_valid = snap_coins["oi_usd"].notna().sum()
    oi_total = len(snap_coins)
    print(f"  OI coverage: {oi_valid}/{oi_total} pairs ({oi_valid/oi_total*100:.1f}%) have valid OI")

    if oi_valid == 0:
        print("\n  ERROR: No OI data matched any snapshot. Cannot compute strength.")
        print("  Check that coinglass_oi timestamps overlap with hl_liquidation_map timestamps.")
        return

    # Pre-group liq_df rows by (snapshot_time, coin) for fast lookup
    grouped = liq_df.groupby(["snapshot_time", "coin"])

    all_results: list[dict] = []
    random_results: list[dict] = []
    skipped_no_oi = 0
    processed = 0

    for _, row in snap_coins.iterrows():
        snap_time = row["snapshot_time"]
        coin = row["coin"]
        mid_price = row["current_price"]
        oi_usd = row["oi_usd"]

        if mid_price is None or mid_price <= 0 or (isinstance(mid_price, float) and np.isnan(mid_price)):
            continue

        # Skip if no OI
        if oi_usd is None or (isinstance(oi_usd, float) and math.isnan(oi_usd)):
            skipped_no_oi += 1
            continue

        canon = canonical_coin(coin)
        klines = klines_cache.get(canon)
        if klines is None or klines.empty:
            continue

        try:
            snap_group = grouped.get_group((snap_time, coin))
        except KeyError:
            continue

        rows_list = snap_group[["price_level", "long_liq_usd", "short_liq_usd"]].to_dict("records")

        future_highs, future_lows = compute_future_extremes(klines, snap_time)
        if not future_highs:
            continue

        # Detect clusters at single threshold, filter by distance
        clusters = detect_clusters(rows_list, mid_price, THRESHOLD_V2, BUCKET_PCT)
        for cl in clusters:
            if cl["distance_pct"] > MAX_DISTANCE_PCT:
                continue

            strength_pct = compute_cluster_strength(cl["total_usd"], oi_usd)

            hit = check_hit(cl["side"], cl["bucket_center"], future_highs, future_lows)
            result = {
                "coin": canon,
                "snapshot_time": snap_time,
                "side": cl["side"],
                "distance_pct": cl["distance_pct"],
                "total_usd": cl["total_usd"],
                "oi_usd": oi_usd,
                "strength_pct": strength_pct,
                "strength_tier": classify_strength(strength_pct),
                **hit,
            }
            all_results.append(result)

            # Random baseline: mirror cluster to opposite side
            if cl["side"] == "short_liq_above":
                phantom_price = mid_price - (cl["bucket_center"] - mid_price)
                phantom_side = "long_liq_below"
            else:
                phantom_price = mid_price + (mid_price - cl["bucket_center"])
                phantom_side = "short_liq_above"
            phantom_hit = check_hit(phantom_side, phantom_price, future_highs, future_lows)
            random_results.append({
                "coin": canon,
                "side": phantom_side,
                "distance_pct": cl["distance_pct"],
                **phantom_hit,
            })

        processed += 1

    print(f"  Processed {processed} (snapshot, coin) pairs")
    print(f"  Skipped (no OI): {skipped_no_oi}")
    print(f"  Total clusters (within {MAX_DISTANCE_PCT}%, >=${THRESHOLD_V2:,.0f}): {len(all_results)}")
    print(f"  Random baselines: {len(random_results)}")

    if not all_results:
        print("\n  No clusters found. Nothing to analyze.")
        return

    # --- Step 5: Build matrix ---
    matrix = build_strength_matrix(all_results, random_results)

    # --- Step 6: Print matrix ---
    print(f"\n{'='*70}")
    print("  DISTANCE x STRENGTH MATRIX")
    print(f"{'='*70}")

    for h in ["1h", "4h", "8h"]:
        _print_matrix_table(matrix, h)

    # Per-coin breakdown
    _print_per_coin_matrix(all_results, "8h")

    # Strength tier distribution
    print("\n  Strength tier distribution:")
    tier_counts: dict[str, int] = defaultdict(int)
    for r in all_results:
        tier_counts[r["strength_tier"]] += 1
    for tier in STRENGTH_ORDER:
        cnt = tier_counts.get(tier, 0)
        pct = cnt / len(all_results) * 100 if all_results else 0
        print(f"    {tier:<8}  {cnt:>6}  ({pct:.1f}%)")

    # --- Step 7: Algorithmic zones ---
    print(f"\n{'='*70}")
    print("  ALGORITHMIC ZONE SEARCH")
    print(f"{'='*70}")
    print(f"  Criteria: hit_8h > {MIN_HIT_8H}%  AND  magnet_8h > {MIN_MAGNET_8H}  AND  N >= {MIN_ZONE_N}")

    zones = find_algorithmic_zones(matrix, MIN_ZONE_N, MIN_HIT_8H, MIN_MAGNET_8H)

    if zones:
        print(f"\n  Found {len(zones)} qualifying zone(s):")
        for z in zones:
            print(
                f"    distance={z['distance']}, strength={z['strength']}:  "
                f"hit={z['hit_rate_8h']:.1f}%, N={z['n']}, magnet={z['magnet_8h']:.2f}"
            )
    else:
        print("\n  No zones meet full criteria.")
        # Show top 3 most promising
        ranked = sorted(matrix, key=lambda c: c.get("magnet_8h", 0), reverse=True)
        top = [c for c in ranked if c["n"] >= 5][:3]
        if top:
            print("  Most promising cells (lower bar):")
            for c in top:
                print(
                    f"    distance={c['distance']}, strength={c['strength']}:  "
                    f"hit_8h={c['hit_rate_8h']:.1f}%, N={c['n']}, magnet_8h={c['magnet_8h']:.2f}"
                )

    # --- Step 8: PASS / FAIL / INSUFFICIENT DATA ---
    print(f"\n{'='*70}")
    print("  VERDICT")
    print(f"{'='*70}")

    total_n = len(all_results)
    if total_n < 50:
        # Estimate when we'll have enough data
        coins_in_data = set(r["coin"] for r in all_results)
        snap_times = set(r["snapshot_time"] for r in all_results)
        if len(snap_times) >= 2:
            snap_list = sorted(snap_times)
            span = (snap_list[-1] - snap_list[0]).total_seconds() / 86400
            if span > 0:
                rate = total_n / span
                days_needed = max(1, int((50 - total_n) / rate) + 1) if rate > 0 else 999
                target_date = datetime.now(timezone.utc) + timedelta(days=days_needed)
                print(f"\n  INSUFFICIENT DATA")
                print(f"  Total clusters: {total_n} (need >= 50 for meaningful analysis)")
                print(f"  Span: {span:.1f} days, rate: {rate:.1f} clusters/day")
                print(f"  Projected ready date: {target_date.strftime('%Y-%m-%d')}")
            else:
                print(f"\n  INSUFFICIENT DATA — only {total_n} clusters, all from same snapshot")
        else:
            print(f"\n  INSUFFICIENT DATA — only {total_n} clusters found")

    elif zones:
        total_zone_n = sum(z["n"] for z in zones)
        # Check multi-coin coverage
        zone_coins: set[str] = set()
        for z in zones:
            # Find which coins contribute to this zone
            for r in all_results:
                dist = fine_distance_bucket_label(r["distance_pct"])
                tier = classify_strength(r["strength_pct"])
                if dist == z["distance"] and tier == z["strength"]:
                    zone_coins.add(r["coin"])

        print(f"\n  PASS")
        print(f"  Qualifying zones: {len(zones)}")
        print(f"  Total observations in zones: {total_zone_n}")
        print(f"  Coins represented: {len(zone_coins)} ({', '.join(sorted(zone_coins))})")

        # Print TP_CLUSTER_CONFIG
        best = zones[0]
        tier_lo = STRENGTH_TIERS[best["strength"]][0]
        tier_hi = STRENGTH_TIERS[best["strength"]][1]
        dist_hi = float(best["distance"].split("-")[1].rstrip("%"))

        # Wilson 95% CI for hit rate
        n = best["n"]
        p_hat = best["hit_rate_8h"] / 100
        z_val = 1.96
        denom = 1 + z_val**2 / n
        center = (p_hat + z_val**2 / (2 * n)) / denom
        margin = z_val * math.sqrt((p_hat * (1 - p_hat) + z_val**2 / (4 * n)) / n) / denom
        ci_lo = max(0, center - margin)
        ci_hi = min(1, center + margin)

        print(f"\n  Recommended TP_CLUSTER_CONFIG:")
        print(f"  # Generated by L6b analysis on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
        print(f"  # Based on {n} cluster events across {len(zone_coins)} coins")
        print(f"  TP_CLUSTER_CONFIG = {{")
        print(f"      'min_strength_pct': {tier_lo},")
        print(f"      'max_distance_pct': {dist_hi},")
        print(f"      'expected_hit_rate': {p_hat:.2f},")
        print(f"      'confidence_interval': ({ci_lo:.2f}, {ci_hi:.2f}),")
        print(f"      'sample_size': {n},")
        print(f"  }}")

    else:
        print(f"\n  FAIL")
        print(f"  Total clusters analyzed: {total_n}")
        print(f"  No (distance, strength) zone meets all criteria:")
        print(f"    hit_rate_8h > {MIN_HIT_8H}%, magnet_8h > {MIN_MAGNET_8H}, N >= {MIN_ZONE_N}")

        # Show what fell short
        close = [
            c for c in matrix
            if c["n"] >= 10 and (c["hit_rate_8h"] > 40 or c["magnet_8h"] > 1.2)
        ]
        if close:
            print(f"\n  Near-miss cells:")
            for c in sorted(close, key=lambda x: x["magnet_8h"], reverse=True)[:5]:
                print(
                    f"    ({c['distance']}, {c['strength']}): "
                    f"hit_8h={c['hit_rate_8h']:.1f}%, magnet={c['magnet_8h']:.2f}, N={c['n']}"
                )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    from collectors.config import get_config
    cfg = get_config()
    init_pool(cfg)

    print("=" * 70)
    print("  L6b: Liquidation Cluster Analysis v2 (OI-Normalized Strength)")
    print("=" * 70)

    # Step 0: Schema exploration
    print("\n--- Step 0: OI schema exploration ---")
    explore_oi_schema()

    # Step 1: Load data
    print("\n--- Step 1: Loading data ---")
    t0 = time.time()

    liq_df = load_all_liq_map()
    if liq_df.empty:
        print("  ERROR: hl_liquidation_map is empty. Run collectors first.")
        return
    print(f"  hl_liquidation_map: {len(liq_df)} rows")

    coins_in_data = sorted(liq_df["coin"].unique())
    print(f"  Coins in data: {coins_in_data}")

    oi_df = load_oi_data(coins_in_data)
    print(f"  OI data: {len(oi_df)} rows, coins: {sorted(oi_df['symbol'].unique()) if not oi_df.empty else []}")

    if not oi_df.empty:
        for coin in coins_in_data:
            coin_oi = oi_df[oi_df["symbol"] == coin]
            if not coin_oi.empty:
                print(f"    {coin:<6}  {len(coin_oi)} OI rows,  range: {coin_oi['timestamp'].min()} — {coin_oi['timestamp'].max()}")

    # Fetch klines
    print("\n  Fetching Binance 1H klines...")
    klines_cache: dict[str, pd.DataFrame] = {}
    liq_min_ts = liq_df["snapshot_time"].min()
    since_ms = int(liq_min_ts.timestamp() * 1000) - 3600_000  # 1h before first snapshot

    for coin in coins_in_data:
        canon = canonical_coin(coin)
        ccxt_sym = binance_ccxt_symbol(canon)
        try:
            kl = fetch_klines_1h_ohlc(ccxt_sym, since_ms)
            klines_cache[canon] = kl
            print(f"    {canon:<6}  {len(kl)} bars")
        except Exception as e:
            print(f"    {canon:<6}  FAILED: {e}")

    t1 = time.time()
    print(f"\n  Data loading took {t1 - t0:.1f}s")

    # Step 2-8: Analysis
    print("\n--- Step 2-8: Analysis ---")
    run_analysis_v2(liq_df, oi_df, klines_cache)

    print(f"\n{'='*70}")
    print("  Done.")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
