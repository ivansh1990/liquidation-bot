#!/usr/bin/env python3
"""
L6: LiqMapAnalyzer — liquidation cluster magnet-effect analysis.

Hypothesis: price tends to move TOWARD large liquidation clusters.
  - Large SHORT-liq clusters ABOVE price attract price upward.
  - Large LONG-liq clusters BELOW price attract price downward.

Uses hl_liquidation_map (15-min snapshots, ~13 Apr 2026+) plus Binance 1H
klines (via ccxt, public) for forward-looking high/low verification.

Method:
  1. Load all hl_liquidation_map snapshots + Binance 1H OHLC per coin.
  2. For each (snapshot_time, coin): bucket nearby price_levels, detect clusters
     above a USD threshold.
  3. For each cluster: check whether price reached the cluster level within
     1h / 4h / 8h / 24h (using Binance kline highs/lows).
  4. Compare hit rate vs a random baseline (mirror-distance phantom levels).
  5. Print per-threshold / per-coin / per-distance tables + PASS/FAIL verdict.

Usage:
    .venv/bin/python scripts/analyze_liq_clusters.py
"""
from __future__ import annotations

import math
import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import ccxt
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import COINS, binance_ccxt_symbol, canonical_coin
from collectors.db import get_conn, init_pool

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

THRESHOLDS: list[float] = [500_000, 1_000_000, 2_000_000, 5_000_000]
HORIZONS: list[str] = ["1h", "4h", "8h", "24h"]
HORIZON_HOURS: dict[str, int] = {"1h": 1, "4h": 4, "8h": 8, "24h": 24}
BUCKET_PCT: float = 0.5
SNAPSHOT_SAMPLE_INTERVAL: int = 4  # use every Nth snapshot to cut work
RANDOM_SEED: int = 42

# PASS criteria
MIN_CLUSTERS: int = 100
MIN_MAGNET_8H: float = 1.3
MIN_HIT_RATE_8H: float = 50.0


# ---------------------------------------------------------------------------
# Pure analysis functions (importable, tested by test_liq_analyzer.py)
# ---------------------------------------------------------------------------

def build_buckets(
    rows: list[dict],
    mid_price: float,
    bucket_pct: float = 0.5,
) -> list[dict]:
    """
    Group price_level rows into buckets of width ``bucket_pct``% of *mid_price*.

    For levels above *mid_price*: use ``short_liq_usd`` → side ``"short_liq_above"``.
    For levels below *mid_price*: use ``long_liq_usd``  → side ``"long_liq_below"``.
    Levels exactly at *mid_price* are skipped.

    Returns list of ``{bucket_center, side, total_usd, distance_pct, count}``.
    """
    if not rows or mid_price <= 0:
        return []

    bucket_width = mid_price * bucket_pct / 100.0
    if bucket_width <= 0:
        return []

    agg: dict[tuple[int, str], dict] = {}

    for r in rows:
        pl = r["price_level"]
        if pl > mid_price:
            usd = r.get("short_liq_usd", 0) or 0
            side = "short_liq_above"
            distance = pl - mid_price
        elif pl < mid_price:
            usd = r.get("long_liq_usd", 0) or 0
            side = "long_liq_below"
            distance = mid_price - pl
        else:
            continue

        idx = int(distance / bucket_width)
        key = (idx, side)

        if key not in agg:
            if side == "short_liq_above":
                center = mid_price + (idx + 0.5) * bucket_width
            else:
                center = mid_price - (idx + 0.5) * bucket_width
            dist_pct = (idx + 0.5) * bucket_pct
            agg[key] = {
                "bucket_center": center,
                "side": side,
                "total_usd": 0.0,
                "distance_pct": dist_pct,
                "count": 0,
            }

        agg[key]["total_usd"] += usd
        agg[key]["count"] += 1

    return list(agg.values())


def detect_clusters(
    rows: list[dict],
    mid_price: float,
    threshold: float,
    bucket_pct: float = 0.5,
) -> list[dict]:
    """Return buckets whose ``total_usd >= threshold``."""
    return [
        b for b in build_buckets(rows, mid_price, bucket_pct)
        if b["total_usd"] >= threshold
    ]


def check_hit(
    side: str,
    cluster_price: float,
    future_highs: dict[str, float],
    future_lows: dict[str, float],
) -> dict[str, bool]:
    """
    Check whether price reached *cluster_price* within each horizon.

    * ``short_liq_above``: price must rise → ``future_highs[h] >= cluster_price``.
    * ``long_liq_below``:  price must fall → ``future_lows[h] <= cluster_price``.
    """
    result: dict[str, bool] = {}
    for h in HORIZONS:
        if side == "short_liq_above":
            high = future_highs.get(h)
            result[f"hit_{h}"] = high is not None and high >= cluster_price
        else:
            low = future_lows.get(h)
            result[f"hit_{h}"] = low is not None and low <= cluster_price
    return result


def compute_hit_rate(results: list[dict], key: str) -> float:
    """Percentage of *results* where ``result[key]`` is truthy."""
    if not results:
        return 0.0
    hits = sum(1 for r in results if r.get(key))
    return hits / len(results) * 100.0


def compute_magnet_score(cluster_hr: float, random_hr: float) -> float:
    """``cluster_hr / random_hr``, with safe zero handling."""
    if random_hr == 0 and cluster_hr == 0:
        return 0.0
    if random_hr == 0:
        return float("inf")
    return cluster_hr / random_hr


def distance_bucket_label(pct: float) -> str:
    """Map a distance percentage to a human-readable bucket label."""
    if pct < 2.0:
        return "0-2%"
    if pct < 4.0:
        return "2-4%"
    if pct < 6.0:
        return "4-6%"
    return "6%+"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def explore_schema() -> None:
    """Print hl_liquidation_map schema + data summary (Step 0)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'hl_liquidation_map'
            ORDER BY ordinal_position
            """
        )
        cols = cur.fetchall()
        print("  Schema:")
        for name, dtype in cols:
            print(f"    {name:<25} {dtype}")

        cur.execute(
            """
            SELECT coin, COUNT(*) AS rows,
                   MIN(snapshot_time) AS first_snap,
                   MAX(snapshot_time) AS last_snap,
                   COUNT(DISTINCT snapshot_time) AS snapshots
            FROM hl_liquidation_map
            GROUP BY coin ORDER BY coin
            """
        )
        rows = cur.fetchall()
        print("\n  Data per coin:")
        print(f"    {'coin':<8} {'rows':>8} {'snaps':>7} {'first':>22} {'last':>22}")
        total_rows = 0
        total_snaps = 0
        for coin, cnt, first, last, snaps in rows:
            print(f"    {coin:<8} {cnt:>8} {snaps:>7} {str(first):>22} {str(last):>22}")
            total_rows += cnt
            total_snaps = max(total_snaps, snaps)
        print(f"    {'TOTAL':<8} {total_rows:>8} {total_snaps:>7}")


def load_all_liq_map() -> pd.DataFrame:
    """Load the entire hl_liquidation_map into a DataFrame."""
    with get_conn() as conn:
        df = pd.read_sql(
            """
            SELECT snapshot_time, coin, price_level,
                   long_liq_usd, short_liq_usd, current_price
            FROM hl_liquidation_map
            ORDER BY snapshot_time, coin
            """,
            conn,
            parse_dates=["snapshot_time"],
        )
    if not df.empty and df["snapshot_time"].dt.tz is None:
        df["snapshot_time"] = df["snapshot_time"].dt.tz_localize("UTC")
    return df


# ---------------------------------------------------------------------------
# Binance kline helpers
# ---------------------------------------------------------------------------

def fetch_klines_1h_ohlc(ccxt_symbol: str, since_ms: int) -> pd.DataFrame:
    """
    Fetch paginated 1H OHLC from Binance futures via ccxt.

    Returns DF indexed by UTC timestamp with [open, high, low, close].
    """
    exchange = ccxt.binance({"options": {"defaultType": "swap"}})
    all_rows: list[list] = []
    cursor = since_ms
    while True:
        batch = exchange.fetch_ohlcv(
            ccxt_symbol, timeframe="1h", since=cursor, limit=1000,
        )
        if not batch:
            break
        all_rows.extend(batch)
        cursor = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    if not all_rows:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "vol"])
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("timestamp")[["open", "high", "low", "close"]]


def compute_future_extremes(
    klines: pd.DataFrame,
    snap_time: pd.Timestamp,
) -> tuple[dict[str, float], dict[str, float]]:
    """
    Given 1H klines, compute the maximum high and minimum low within
    [snap_time, snap_time + horizon] for each horizon.

    Returns (future_highs, future_lows) dicts keyed by horizon string.
    """
    highs: dict[str, float] = {}
    lows: dict[str, float] = {}
    for h_str, hours in HORIZON_HOURS.items():
        end = snap_time + pd.Timedelta(hours=hours)
        window = klines[(klines.index > snap_time) & (klines.index <= end)]
        if window.empty:
            continue
        highs[h_str] = float(window["high"].max())
        lows[h_str] = float(window["low"].min())
    return highs, lows


# ---------------------------------------------------------------------------
# Main analysis pipeline
# ---------------------------------------------------------------------------

def run_analysis(liq_df: pd.DataFrame, klines_cache: dict[str, pd.DataFrame]) -> None:
    """Run the full cluster + hit-rate analysis across all thresholds."""

    # Unique (snapshot_time, coin) pairs — sample every Nth to manage load
    snap_coins = (
        liq_df[["snapshot_time", "coin", "current_price"]]
        .drop_duplicates(subset=["snapshot_time", "coin"])
        .sort_values("snapshot_time")
    )
    # Deduplicate snapshot times and take every Nth
    unique_times = sorted(snap_coins["snapshot_time"].unique())
    sampled_times = set(unique_times[::SNAPSHOT_SAMPLE_INTERVAL])
    snap_coins = snap_coins[snap_coins["snapshot_time"].isin(sampled_times)]
    print(f"\n  Snapshots total: {len(unique_times)}, sampled (every {SNAPSHOT_SAMPLE_INTERVAL}th): {len(sampled_times)}")
    print(f"  Unique (snapshot, coin) pairs to process: {len(snap_coins)}")

    # Pre-group liq_df rows by (snapshot_time, coin) for fast lookup
    grouped = liq_df.groupby(["snapshot_time", "coin"])

    # For each threshold, accumulate cluster results
    all_results: dict[float, list[dict]] = {t: [] for t in THRESHOLDS}
    random_results: dict[float, list[dict]] = {t: [] for t in THRESHOLDS}

    rng = random.Random(RANDOM_SEED)
    processed = 0

    for _, row in snap_coins.iterrows():
        snap_time = row["snapshot_time"]
        coin = row["coin"]
        mid_price = row["current_price"]

        if mid_price is None or mid_price <= 0 or np.isnan(mid_price):
            continue

        # Map hl_liquidation_map coin to canonical name for Binance klines
        canon = canonical_coin(coin)
        klines = klines_cache.get(canon)
        if klines is None or klines.empty:
            continue

        # Get all rows for this snapshot
        try:
            snap_group = grouped.get_group((snap_time, coin))
        except KeyError:
            continue

        rows_list = snap_group[["price_level", "long_liq_usd", "short_liq_usd"]].to_dict("records")

        # Pre-compute future extremes (same for all thresholds)
        future_highs, future_lows = compute_future_extremes(klines, snap_time)
        if not future_highs:
            continue  # no kline data after this snapshot

        for threshold in THRESHOLDS:
            clusters = detect_clusters(rows_list, mid_price, threshold, BUCKET_PCT)
            for cl in clusters:
                hit = check_hit(cl["side"], cl["bucket_center"], future_highs, future_lows)
                result = {
                    "coin": canon,
                    "snapshot_time": snap_time,
                    "side": cl["side"],
                    "distance_pct": cl["distance_pct"],
                    "total_usd": cl["total_usd"],
                    **hit,
                }
                all_results[threshold].append(result)

                # Random baseline: mirror the cluster to the opposite side
                if cl["side"] == "short_liq_above":
                    phantom_price = mid_price - (cl["bucket_center"] - mid_price)
                    phantom_side = "long_liq_below"
                else:
                    phantom_price = mid_price + (mid_price - cl["bucket_center"])
                    phantom_side = "short_liq_above"
                phantom_hit = check_hit(phantom_side, phantom_price, future_highs, future_lows)
                random_results[threshold].append({
                    "coin": canon,
                    "side": phantom_side,
                    "distance_pct": cl["distance_pct"],
                    **phantom_hit,
                })

        processed += 1

    print(f"  Processed {processed} (snapshot, coin) pairs\n")

    # --- Print results per threshold ---
    best_threshold = None
    best_magnet_8h = 0.0

    for threshold in THRESHOLDS:
        results = all_results[threshold]
        randoms = random_results[threshold]
        n = len(results)

        print(f"{'='*60}")
        print(f"  Threshold: ${threshold:,.0f}")
        print(f"  Total clusters found: {n}")
        if n == 0:
            print("  (no clusters at this threshold)\n")
            continue

        n_above = sum(1 for r in results if r["side"] == "short_liq_above")
        n_below = sum(1 for r in results if r["side"] == "long_liq_below")
        print(f"    SHORT above: {n_above}")
        print(f"    LONG below:  {n_below}")

        # Hit rates
        print(f"\n  Hit rates:")
        header = f"  {'':12}"
        for h in HORIZONS:
            header += f"  {h:>7}"
        print(header)

        line_c = f"  {'Clusters':12}"
        line_r = f"  {'Random':12}"
        line_m = f"  {'Magnet':12}"
        for h in HORIZONS:
            key = f"hit_{h}"
            c_hr = compute_hit_rate(results, key)
            r_hr = compute_hit_rate(randoms, key)
            ms = compute_magnet_score(c_hr, r_hr)
            line_c += f"  {c_hr:>6.1f}%"
            line_r += f"  {r_hr:>6.1f}%"
            if ms == float("inf"):
                line_m += f"  {'inf':>7}"
            else:
                line_m += f"  {ms:>7.2f}"
        print(line_c)
        print(line_r)
        print(line_m)

        # Per-coin breakdown at 8h
        print(f"\n  By coin (8h):")
        coins_in_results = sorted(set(r["coin"] for r in results))
        for coin in coins_in_results:
            coin_res = [r for r in results if r["coin"] == coin]
            coin_hr = compute_hit_rate(coin_res, "hit_8h")
            coin_rand = [r for r in randoms if r["coin"] == coin]
            coin_rhr = compute_hit_rate(coin_rand, "hit_8h")
            coin_ms = compute_magnet_score(coin_hr, coin_rhr)
            ms_str = f"{coin_ms:.2f}" if coin_ms != float("inf") else "inf"
            print(f"    {coin:<6} clusters={len(coin_res):>4}, hit_8h={coin_hr:>5.1f}%, magnet={ms_str}")

        # By distance
        print(f"\n  By distance (8h):")
        dist_groups: dict[str, list[dict]] = defaultdict(list)
        for r in results:
            lbl = distance_bucket_label(r["distance_pct"])
            dist_groups[lbl].append(r)
        for lbl in ["0-2%", "2-4%", "4-6%", "6%+"]:
            grp = dist_groups.get(lbl, [])
            if grp:
                hr = compute_hit_rate(grp, "hit_8h")
                print(f"    {lbl:<6} clusters={len(grp):>4}, hit_8h={hr:>5.1f}%")
            else:
                print(f"    {lbl:<6} clusters=   0")

        print()

        # Track best threshold for verdict
        c_hr_8h = compute_hit_rate(results, "hit_8h")
        r_hr_8h = compute_hit_rate(randoms, "hit_8h")
        ms_8h = compute_magnet_score(c_hr_8h, r_hr_8h)
        if ms_8h > best_magnet_8h and n >= MIN_CLUSTERS:
            best_magnet_8h = ms_8h
            best_threshold = threshold

    # --- PASS/FAIL verdict ---
    print(f"{'='*60}")
    print("  VERDICT")
    print(f"{'='*60}")

    # Find the best threshold that meets all criteria
    verdict = "INSUFFICIENT DATA"
    verdict_detail = ""

    for threshold in THRESHOLDS:
        results = all_results[threshold]
        randoms = random_results[threshold]
        n = len(results)
        if n < MIN_CLUSTERS:
            continue

        c_hr_8h = compute_hit_rate(results, "hit_8h")
        r_hr_8h = compute_hit_rate(randoms, "hit_8h")
        ms_8h = compute_magnet_score(c_hr_8h, r_hr_8h)

        if ms_8h > MIN_MAGNET_8H and c_hr_8h > MIN_HIT_RATE_8H:
            verdict = "PASS"
            verdict_detail = (
                f"threshold=${threshold:,.0f}, N={n}, "
                f"hit_8h={c_hr_8h:.1f}%, magnet_8h={ms_8h:.2f}"
            )
            break
        elif verdict == "INSUFFICIENT DATA":
            verdict = "FAIL"
            verdict_detail = (
                f"best threshold=${threshold:,.0f}, N={n}, "
                f"hit_8h={c_hr_8h:.1f}%, magnet_8h={ms_8h:.2f}"
            )

    # Check if truly insufficient data across all thresholds
    max_clusters = max(len(all_results[t]) for t in THRESHOLDS)

    if max_clusters < MIN_CLUSTERS:
        verdict = "INSUFFICIENT DATA"
        # Estimate collection rate
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT MIN(snapshot_time), MAX(snapshot_time) FROM hl_liquidation_map"
            )
            first, last = cur.fetchone()
        if first and last:
            span_days = max((last - first).total_seconds() / 86400, 0.01)
            # Use the lowest threshold (most clusters) for projection
            rate = max_clusters / span_days
            if rate > 0:
                days_needed = (MIN_CLUSTERS - max_clusters) / rate
                projected = datetime.now(timezone.utc) + timedelta(days=days_needed)
                print(f"\n  Current clusters (lowest threshold): {max_clusters}")
                print(f"  Required: {MIN_CLUSTERS}")
                print(f"  Collection span: {span_days:.1f} days")
                print(f"  Rate: ~{rate:.0f} clusters/day")
                print(f"  Projected ready date: {projected.date()}")
            else:
                print(f"\n  Current clusters: {max_clusters}")
                print(f"  Required: {MIN_CLUSTERS}")
                print(f"  Collection rate: 0 — need more data.")
        print(f"\n  Re-run: .venv/bin/python scripts/analyze_liq_clusters.py")

    print(f"\n  Result: {verdict}")
    if verdict_detail:
        print(f"  Detail: {verdict_detail}")
    print()

    # --- Step 7: Additional analysis if PASS and enough data ---
    if verdict == "PASS" and best_threshold is not None:
        results = all_results[best_threshold]
        if len(results) >= 200:
            print(f"{'='*60}")
            print(f"  ADDITIONAL ANALYSIS (threshold=${best_threshold:,.0f})")
            print(f"{'='*60}")

            # Correlation: cluster size vs hit rate
            size_bins = [
                ("small", lambda r: r["total_usd"] < 2_000_000),
                ("medium", lambda r: 2_000_000 <= r["total_usd"] < 5_000_000),
                ("large", lambda r: r["total_usd"] >= 5_000_000),
            ]
            print("\n  Cluster size vs hit rate (8h):")
            for label, pred in size_bins:
                grp = [r for r in results if pred(r)]
                if grp:
                    hr = compute_hit_rate(grp, "hit_8h")
                    print(f"    {label:<8} N={len(grp):>4}, hit_8h={hr:>5.1f}%")

            # Average time to hit (approximate: first horizon that hits)
            times = []
            for r in results:
                for h in HORIZONS:
                    if r.get(f"hit_{h}"):
                        times.append(HORIZON_HOURS[h])
                        break
            if times:
                avg_time = sum(times) / len(times)
                print(f"\n  Average first-hit horizon: {avg_time:.1f}h")
                print(f"  (of {len(times)} clusters that hit at any horizon)")

            # Best parameters summary
            print(f"\n  Recommended runtime parameters:")
            # Optimal distance: which bucket has highest hit rate?
            dist_groups2: dict[str, list[dict]] = defaultdict(list)
            for r in results:
                lbl = distance_bucket_label(r["distance_pct"])
                dist_groups2[lbl].append(r)
            best_dist = max(
                dist_groups2.keys(),
                key=lambda k: compute_hit_rate(dist_groups2[k], "hit_8h")
                if dist_groups2[k] else 0,
            )
            print(f"    min_cluster_threshold = ${best_threshold:,.0f}")
            print(f"    best_distance_range   = {best_dist}")
            print()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    from collectors.config import get_config

    cfg = get_config()
    init_pool(cfg)

    print("L6: LIQUIDATION CLUSTER MAGNET-EFFECT ANALYSIS")
    print("=" * 60)
    print(f"Hypothesis: price moves toward large liquidation clusters")
    print(f"Data: hl_liquidation_map (15-min snapshots)")
    print(f"Forward prices: Binance 1H klines (ccxt)")
    print(f"Thresholds: {', '.join(f'${t:,.0f}' for t in THRESHOLDS)}")
    print(f"Bucket width: {BUCKET_PCT}% of mid-price")
    print(f"Horizons: {', '.join(HORIZONS)}")

    # Step 0: Schema exploration
    print(f"\n{'='*60}")
    print("  Step 0: Schema exploration")
    print(f"{'='*60}")
    explore_schema()

    # Step 1: Load data
    print(f"\n{'='*60}")
    print("  Step 1: Loading data")
    print(f"{'='*60}")

    t0 = time.time()
    liq_df = load_all_liq_map()
    if liq_df.empty:
        print("  hl_liquidation_map is empty. Run hl_snapshots collector first.")
        return
    print(f"  Loaded {len(liq_df):,} rows in {time.time()-t0:.1f}s")

    # Determine coins present in the data
    coins_in_data = sorted(liq_df["coin"].unique())
    print(f"  Coins in data: {', '.join(coins_in_data)}")

    # Date range
    first_snap = liq_df["snapshot_time"].min()
    last_snap = liq_df["snapshot_time"].max()
    span_days = (last_snap - first_snap).total_seconds() / 86400
    print(f"  Date range: {first_snap} → {last_snap} ({span_days:.2f} days)")

    # Fetch Binance 1H klines for each coin
    print(f"\n  Fetching Binance 1H klines...")
    klines_cache: dict[str, pd.DataFrame] = {}
    # Need klines starting from first snapshot, extending 24h past last snapshot
    since_ms = int((first_snap - pd.Timedelta(hours=1)).timestamp() * 1000)

    for coin in coins_in_data:
        canon = canonical_coin(coin)
        ccxt_sym = binance_ccxt_symbol(canon)
        try:
            kl = fetch_klines_1h_ohlc(ccxt_sym, since_ms)
            klines_cache[canon] = kl
            print(f"    {canon:<6} {len(kl):>5} bars ({kl.index.min()} → {kl.index.max()})" if not kl.empty else f"    {canon:<6} empty")
        except Exception as e:
            print(f"    {canon:<6} FAILED: {e}")

    # Step 2-6: Run analysis
    print(f"\n{'='*60}")
    print("  Steps 2-6: Cluster detection + hit-rate analysis")
    print(f"{'='*60}")

    run_analysis(liq_df, klines_cache)

    print("Done.")


if __name__ == "__main__":
    main()
