#!/usr/bin/env python3
"""
Offline tests for scripts/analyze_liq_clusters_v2.py (L6b).

Eight blocks exercising OI-normalized cluster strength functions with
synthetic data.  No DB or network calls.

Run directly: .venv/bin/python scripts/test_liq_analyzer_v2.py
Exit code 0 on all-pass, 1 otherwise.
"""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from analyze_liq_clusters_v2 import (
    attach_oi_to_snapshots,
    build_strength_matrix,
    classify_strength,
    compute_cluster_strength,
    find_algorithmic_zones,
    fine_distance_bucket_label,
)

passed = 0
failed = 0


def report(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")


# ---------------------------------------------------------------------------
# Block 1: OI normalization
# ---------------------------------------------------------------------------

def test_oi_normalization() -> None:
    print("\n--- Block 1: OI normalization ---")

    s = compute_cluster_strength(1_000_000, 100_000_000)
    report("$1M / $100M OI → 1.0%", abs(s - 1.0) < 1e-6, f"got {s}")

    s2 = compute_cluster_strength(2_000_000, 100_000_000)
    report("$2M / $100M OI → 2.0%", abs(s2 - 2.0) < 1e-6, f"got {s2}")

    s3 = compute_cluster_strength(1_000_000, 0)
    report("OI=0 → 0.0%", abs(s3) < 1e-6, f"got {s3}")

    s4 = compute_cluster_strength(1_000_000, float("nan"))
    report("OI=NaN → 0.0%", abs(s4) < 1e-6, f"got {s4}")

    s5 = compute_cluster_strength(0, 100_000_000)
    report("cluster=0 → 0.0%", abs(s5) < 1e-6, f"got {s5}")


# ---------------------------------------------------------------------------
# Block 2: Strength classification
# ---------------------------------------------------------------------------

def test_strength_classification() -> None:
    print("\n--- Block 2: Strength classification ---")

    report("0.1% → weak", classify_strength(0.1) == "weak")
    report("0.49% → weak", classify_strength(0.49) == "weak")
    report("0.5% → medium", classify_strength(0.5) == "medium")
    report("1.5% → medium", classify_strength(1.5) == "medium")
    report("1.99% → medium", classify_strength(1.99) == "medium")
    report("2.0% → strong", classify_strength(2.0) == "strong")
    report("4.9% → strong", classify_strength(4.9) == "strong")
    report("5.0% → mega", classify_strength(5.0) == "mega")


# ---------------------------------------------------------------------------
# Block 3: Fine distance buckets
# ---------------------------------------------------------------------------

def test_fine_distance_buckets() -> None:
    print("\n--- Block 3: Fine distance buckets ---")

    report("0.3% → '0-1%'", fine_distance_bucket_label(0.3) == "0-1%")
    report("0.99% → '0-1%'", fine_distance_bucket_label(0.99) == "0-1%")
    report("1.0% → '1-2%'", fine_distance_bucket_label(1.0) == "1-2%")
    report("2.5% → '2-3%'", fine_distance_bucket_label(2.5) == "2-3%")
    report("3.0% → '3-4%'", fine_distance_bucket_label(3.0) == "3-4%")
    report("4.5% → '4-5%'", fine_distance_bucket_label(4.5) == "4-5%")
    report("5.0% → '' (excluded)", fine_distance_bucket_label(5.0) == "")


# ---------------------------------------------------------------------------
# Block 4: Matrix aggregation
# ---------------------------------------------------------------------------

def test_matrix_aggregation() -> None:
    print("\n--- Block 4: Matrix aggregation ---")

    # 30 results: distance 0.5% (→ "0-1%"), strength 3.0% (→ "strong")
    # 20 hits, 10 misses on 8h
    results = []
    for i in range(30):
        results.append({
            "distance_pct": 0.5,
            "strength_pct": 3.0,
            "hit_1h": i < 10,
            "hit_4h": i < 15,
            "hit_8h": i < 20,
            "hit_24h": i < 25,
        })

    # 30 random results at same distance: 10 hits on 8h
    randoms = []
    for i in range(30):
        randoms.append({
            "distance_pct": 0.5,
            "hit_1h": i < 5,
            "hit_4h": i < 8,
            "hit_8h": i < 10,
            "hit_24h": i < 15,
        })

    matrix = build_strength_matrix(results, randoms)
    # Should have exactly 1 cell: ("0-1%", "strong")
    cell = [c for c in matrix if c["distance"] == "0-1%" and c["strength"] == "strong"]
    report(
        "single cell found",
        len(cell) == 1,
        f"got {len(cell)} cells, total matrix size={len(matrix)}",
    )
    if cell:
        c = cell[0]
        report("N=30", c["n"] == 30, f"got {c['n']}")
        # hit_rate_8h = 20/30 = 66.67%
        report(
            "hit_rate_8h ≈ 66.7%",
            abs(c["hit_rate_8h"] - 66.667) < 0.1,
            f"got {c['hit_rate_8h']:.1f}%",
        )
        # random_hr_8h = 10/30 = 33.33%, magnet = 66.67 / 33.33 ≈ 2.0
        report(
            "magnet_8h ≈ 2.0",
            abs(c["magnet_8h"] - 2.0) < 0.1,
            f"got {c['magnet_8h']:.2f}",
        )


# ---------------------------------------------------------------------------
# Block 5: Insufficient cell
# ---------------------------------------------------------------------------

def test_insufficient_cell() -> None:
    print("\n--- Block 5: Insufficient cell ---")

    # 5 results → cell exists in matrix but NOT in zones (N < 20)
    results = [
        {"distance_pct": 1.5, "strength_pct": 3.0,
         "hit_1h": True, "hit_4h": True, "hit_8h": True, "hit_24h": True}
        for _ in range(5)
    ]
    randoms = [
        {"distance_pct": 1.5,
         "hit_1h": False, "hit_4h": False, "hit_8h": False, "hit_24h": False}
        for _ in range(5)
    ]

    matrix = build_strength_matrix(results, randoms)
    cell = [c for c in matrix if c["distance"] == "1-2%" and c["strength"] == "strong"]
    report("cell with N=5 exists in matrix", len(cell) == 1)

    zones = find_algorithmic_zones(matrix, min_n=20, min_hit_8h=50.0, min_magnet_8h=1.5)
    report("N=5 cell NOT in algorithmic zones", len(zones) == 0)


# ---------------------------------------------------------------------------
# Block 6: Zone detection
# ---------------------------------------------------------------------------

def test_zone_detection() -> None:
    print("\n--- Block 6: Zone detection ---")

    matrix = [
        # Cell A: qualifies (n>=20, hit>50, magnet>1.5)
        {"distance": "0-1%", "strength": "strong", "n": 25,
         "hit_rate_8h": 60.0, "magnet_8h": 2.0},
        # Cell B: fails hit_rate
        {"distance": "1-2%", "strength": "medium", "n": 25,
         "hit_rate_8h": 45.0, "magnet_8h": 1.8},
        # Cell C: fails magnet
        {"distance": "2-3%", "strength": "strong", "n": 25,
         "hit_rate_8h": 60.0, "magnet_8h": 1.2},
        # Cell D: fails n
        {"distance": "3-4%", "strength": "mega", "n": 10,
         "hit_rate_8h": 60.0, "magnet_8h": 2.0},
    ]

    zones = find_algorithmic_zones(matrix, min_n=20, min_hit_8h=50.0, min_magnet_8h=1.5)
    report("1 zone qualifies", len(zones) == 1, f"got {len(zones)}")
    if zones:
        report(
            "qualifying zone is Cell A",
            zones[0]["distance"] == "0-1%" and zones[0]["strength"] == "strong",
            f"got ({zones[0]['distance']}, {zones[0]['strength']})",
        )
        report("sorted by magnet_8h desc", zones[0]["magnet_8h"] == 2.0)


# ---------------------------------------------------------------------------
# Block 7: Empty OI
# ---------------------------------------------------------------------------

def test_empty_oi() -> None:
    print("\n--- Block 7: Empty OI ---")

    snap_df = pd.DataFrame({
        "snapshot_time": pd.to_datetime(["2026-04-14 12:00", "2026-04-14 16:00"]).tz_localize("UTC"),
        "coin": ["BTC", "ETH"],
    })
    oi_df = pd.DataFrame(columns=["timestamp", "symbol", "open_interest"])

    result = attach_oi_to_snapshots(snap_df, oi_df)
    report("result has oi_usd column", "oi_usd" in result.columns)
    report("all oi_usd are NaN", result["oi_usd"].isna().all(), f"got {result['oi_usd'].tolist()}")


# ---------------------------------------------------------------------------
# Block 8: OI staleness
# ---------------------------------------------------------------------------

def test_oi_staleness() -> None:
    print("\n--- Block 8: OI staleness ---")

    snap_df = pd.DataFrame({
        "snapshot_time": pd.to_datetime([
            "2026-04-14 12:00",
            "2026-04-14 12:00",
        ]).tz_localize("UTC"),
        "coin": ["BTC", "ETH"],
    })

    oi_df = pd.DataFrame({
        "timestamp": pd.to_datetime([
            "2026-04-14 10:00",  # 2h stale → within 4h tolerance → BTC matches
            "2026-04-14 06:00",  # 6h stale → beyond 4h tolerance → ETH gets NaN
        ]).tz_localize("UTC"),
        "symbol": ["BTC", "ETH"],
        "open_interest": [50_000_000_000.0, 20_000_000_000.0],
    })

    result = attach_oi_to_snapshots(snap_df, oi_df, max_staleness_hours=4)
    btc_row = result[result["coin"] == "BTC"].iloc[0]
    eth_row = result[result["coin"] == "ETH"].iloc[0]

    report(
        "BTC OI 2h stale → matched",
        not math.isnan(btc_row["oi_usd"]),
        f"got {btc_row['oi_usd']}",
    )
    report(
        "BTC OI value correct",
        abs(btc_row["oi_usd"] - 50_000_000_000.0) < 1,
        f"got {btc_row['oi_usd']}",
    )
    report(
        "ETH OI 6h stale → NaN",
        math.isnan(eth_row["oi_usd"]),
        f"got {eth_row['oi_usd']}",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    test_oi_normalization()
    test_strength_classification()
    test_fine_distance_buckets()
    test_matrix_aggregation()
    test_insufficient_cell()
    test_zone_detection()
    test_empty_oi()
    test_oi_staleness()

    print(f"\nPASS: {passed} | FAIL: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
