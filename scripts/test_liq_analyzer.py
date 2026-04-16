#!/usr/bin/env python3
"""
Offline tests for scripts/analyze_liq_clusters.py (L6).

Seven blocks exercising the pure analysis functions with synthetic data.
No DB or network calls.

Run directly: .venv/bin/python scripts/test_liq_analyzer.py
Exit code 0 on all-pass, 1 otherwise.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from analyze_liq_clusters import (
    build_buckets,
    detect_clusters,
    check_hit,
    compute_hit_rate,
    compute_magnet_score,
    distance_bucket_label,
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
# Block 1: Bucket grouping
# ---------------------------------------------------------------------------

def test_bucket_grouping() -> None:
    print("\n--- Block 1: Bucket grouping ---")

    mid = 100_000.0
    # bucket_width = 100000 * 0.5 / 100 = 500

    # 3 levels within 0.3% of each other above mid → one bucket
    rows_close = [
        {"price_level": 100_100, "long_liq_usd": 0, "short_liq_usd": 300_000},
        {"price_level": 100_200, "long_liq_usd": 0, "short_liq_usd": 200_000},
        {"price_level": 100_300, "long_liq_usd": 0, "short_liq_usd": 100_000},
    ]
    buckets = build_buckets(rows_close, mid, bucket_pct=0.5)
    report(
        "3 close levels → 1 bucket",
        len(buckets) == 1,
        f"got {len(buckets)} buckets",
    )
    if buckets:
        report(
            "bucket total_usd = 600K",
            abs(buckets[0]["total_usd"] - 600_000) < 1,
            f"got {buckets[0]['total_usd']}",
        )
        report(
            "bucket side = short_liq_above",
            buckets[0]["side"] == "short_liq_above",
            f"got {buckets[0]['side']}",
        )

    # 2 levels 1% apart above mid → 2 buckets
    rows_far = [
        {"price_level": 100_500, "long_liq_usd": 0, "short_liq_usd": 400_000},
        {"price_level": 101_500, "long_liq_usd": 0, "short_liq_usd": 500_000},
    ]
    buckets2 = build_buckets(rows_far, mid, bucket_pct=0.5)
    report(
        "2 levels 1% apart → 2 buckets",
        len(buckets2) == 2,
        f"got {len(buckets2)} buckets",
    )

    # Levels below mid → long_liq_below side
    rows_below = [
        {"price_level": 99_000, "long_liq_usd": 1_000_000, "short_liq_usd": 0},
    ]
    buckets3 = build_buckets(rows_below, mid, bucket_pct=0.5)
    report(
        "level below mid → side = long_liq_below",
        len(buckets3) == 1 and buckets3[0]["side"] == "long_liq_below",
        f"got {buckets3[0]['side'] if buckets3 else 'no bucket'}",
    )

    # Level at exactly mid_price → skipped
    rows_at_mid = [
        {"price_level": 100_000, "long_liq_usd": 500_000, "short_liq_usd": 500_000},
    ]
    buckets4 = build_buckets(rows_at_mid, mid, bucket_pct=0.5)
    report(
        "level at exactly mid_price → 0 buckets",
        len(buckets4) == 0,
        f"got {len(buckets4)} buckets",
    )


# ---------------------------------------------------------------------------
# Block 2: Cluster detection (threshold filtering)
# ---------------------------------------------------------------------------

def test_cluster_detection() -> None:
    print("\n--- Block 2: Cluster detection ---")

    mid = 50_000.0
    # bucket_width = 50000 * 0.5 / 100 = 250
    # Big cluster: 3 levels in one bucket (offsets 1000-1100, all idx=4)
    # Small cluster: 1 level in a different bucket (offset 2000, idx=8)
    rows = [
        {"price_level": 51_000, "long_liq_usd": 0, "short_liq_usd": 800_000},
        {"price_level": 51_050, "long_liq_usd": 0, "short_liq_usd": 700_000},
        {"price_level": 51_100, "long_liq_usd": 0, "short_liq_usd": 500_000},
        {"price_level": 52_000, "long_liq_usd": 0, "short_liq_usd": 100_000},
    ]
    clusters = detect_clusters(rows, mid, threshold=1_000_000, bucket_pct=0.5)
    report(
        "threshold $1M → 1 cluster found",
        len(clusters) == 1,
        f"got {len(clusters)}",
    )
    if clusters:
        report(
            "cluster total_usd = $2M",
            abs(clusters[0]["total_usd"] - 2_000_000) < 1,
            f"${clusters[0]['total_usd']:,.0f}",
        )

    # Lower threshold catches both
    clusters_low = detect_clusters(rows, mid, threshold=50_000, bucket_pct=0.5)
    report(
        "threshold $50K → 2 clusters found",
        len(clusters_low) == 2,
        f"got {len(clusters_low)}",
    )

    # Very high threshold → no clusters
    clusters_none = detect_clusters(rows, mid, threshold=10_000_000, bucket_pct=0.5)
    report(
        "threshold $10M → 0 clusters",
        len(clusters_none) == 0,
        f"got {len(clusters_none)}",
    )


# ---------------------------------------------------------------------------
# Block 3: Side classification
# ---------------------------------------------------------------------------

def test_side_classification() -> None:
    print("\n--- Block 3: Side classification ---")

    mid = 100.0

    # Above mid → short_liq_above
    rows_above = [
        {"price_level": 105, "long_liq_usd": 0, "short_liq_usd": 1_000_000},
    ]
    clusters_above = detect_clusters(rows_above, mid, threshold=500_000, bucket_pct=0.5)
    report(
        "cluster above mid → short_liq_above",
        len(clusters_above) == 1 and clusters_above[0]["side"] == "short_liq_above",
        f"side={clusters_above[0]['side'] if clusters_above else 'none'}",
    )

    # Below mid → long_liq_below
    rows_below = [
        {"price_level": 95, "long_liq_usd": 1_000_000, "short_liq_usd": 0},
    ]
    clusters_below = detect_clusters(rows_below, mid, threshold=500_000, bucket_pct=0.5)
    report(
        "cluster below mid → long_liq_below",
        len(clusters_below) == 1 and clusters_below[0]["side"] == "long_liq_below",
        f"side={clusters_below[0]['side'] if clusters_below else 'none'}",
    )

    # Both sides present
    rows_both = rows_above + rows_below
    clusters_both = detect_clusters(rows_both, mid, threshold=500_000, bucket_pct=0.5)
    sides = sorted(c["side"] for c in clusters_both)
    report(
        "both sides → 2 clusters with correct sides",
        sides == ["long_liq_below", "short_liq_above"],
        f"sides={sides}",
    )


# ---------------------------------------------------------------------------
# Block 4: Hit rate calculation
# ---------------------------------------------------------------------------

def test_hit_rate() -> None:
    print("\n--- Block 4: Hit rate calculation ---")

    results = [{"hit_8h": True}] * 6 + [{"hit_8h": False}] * 4
    hr = compute_hit_rate(results, "hit_8h")
    report(
        "6/10 hit → 60%",
        abs(hr - 60.0) < 0.01,
        f"got {hr:.1f}%",
    )

    results_all = [{"hit_4h": True}] * 5
    hr_all = compute_hit_rate(results_all, "hit_4h")
    report(
        "5/5 hit → 100%",
        abs(hr_all - 100.0) < 0.01,
        f"got {hr_all:.1f}%",
    )

    results_none = [{"hit_24h": False}] * 8
    hr_none = compute_hit_rate(results_none, "hit_24h")
    report(
        "0/8 hit → 0%",
        abs(hr_none) < 0.01,
        f"got {hr_none:.1f}%",
    )

    # Empty list → 0%
    hr_empty = compute_hit_rate([], "hit_1h")
    report(
        "empty results → 0%",
        abs(hr_empty) < 0.01,
        f"got {hr_empty:.1f}%",
    )


# ---------------------------------------------------------------------------
# Block 5: Magnet score
# ---------------------------------------------------------------------------

def test_magnet_score() -> None:
    print("\n--- Block 5: Magnet score ---")

    ms = compute_magnet_score(60.0, 40.0)
    report(
        "60/40 → 1.5",
        abs(ms - 1.5) < 0.01,
        f"got {ms:.2f}",
    )

    ms2 = compute_magnet_score(80.0, 80.0)
    report(
        "80/80 → 1.0",
        abs(ms2 - 1.0) < 0.01,
        f"got {ms2:.2f}",
    )

    # Random hit rate = 0 → return inf or large number safely
    ms_zero = compute_magnet_score(50.0, 0.0)
    report(
        "random=0 → returns float('inf')",
        ms_zero == float("inf"),
        f"got {ms_zero}",
    )

    # Both zero → return 0 (no data)
    ms_both = compute_magnet_score(0.0, 0.0)
    report(
        "both=0 → 0.0",
        abs(ms_both) < 0.01,
        f"got {ms_both:.2f}",
    )


# ---------------------------------------------------------------------------
# Block 6: Empty data → no crash
# ---------------------------------------------------------------------------

def test_empty_data() -> None:
    print("\n--- Block 6: Empty data ---")

    mid = 50_000.0

    buckets = build_buckets([], mid, bucket_pct=0.5)
    report("empty rows → 0 buckets", len(buckets) == 0)

    clusters = detect_clusters([], mid, threshold=1_000_000, bucket_pct=0.5)
    report("empty rows → 0 clusters", len(clusters) == 0)

    hr = compute_hit_rate([], "hit_8h")
    report("empty results → hit_rate 0%", abs(hr) < 0.01)

    ms = compute_magnet_score(0.0, 0.0)
    report("zero/zero magnet → 0.0", abs(ms) < 0.01)


# ---------------------------------------------------------------------------
# Block 7: Distance bucket labels
# ---------------------------------------------------------------------------

def test_distance_buckets() -> None:
    print("\n--- Block 7: Distance bucket labels ---")

    report("0.5% → '0-2%'", distance_bucket_label(0.5) == "0-2%")
    report("1.9% → '0-2%'", distance_bucket_label(1.9) == "0-2%")
    report("2.0% → '2-4%'", distance_bucket_label(2.0) == "2-4%")
    report("3.5% → '2-4%'", distance_bucket_label(3.5) == "2-4%")
    report("4.0% → '4-6%'", distance_bucket_label(4.0) == "4-6%")
    report("5.9% → '4-6%'", distance_bucket_label(5.9) == "4-6%")
    report("6.0% → '6%+'", distance_bucket_label(6.0) == "6%+")
    report("15.0% → '6%+'", distance_bucket_label(15.0) == "6%+")


# ---------------------------------------------------------------------------
# Bonus Block: check_hit logic
# ---------------------------------------------------------------------------

def test_check_hit() -> None:
    print("\n--- Bonus: check_hit logic ---")

    # short_liq_above at 105: price needs to go UP to 105
    # future highs: 1h=103, 4h=106, 8h=107, 24h=110
    hits = check_hit(
        side="short_liq_above",
        cluster_price=105.0,
        future_highs={"1h": 103.0, "4h": 106.0, "8h": 107.0, "24h": 110.0},
        future_lows={"1h": 99.0, "4h": 98.0, "8h": 97.0, "24h": 95.0},
    )
    report("short above: 1h high=103 < 105 → miss", hits["hit_1h"] is False)
    report("short above: 4h high=106 >= 105 → hit", hits["hit_4h"] is True)
    report("short above: 8h high=107 >= 105 → hit", hits["hit_8h"] is True)
    report("short above: 24h high=110 >= 105 → hit", hits["hit_24h"] is True)

    # long_liq_below at 95: price needs to go DOWN to 95
    # future lows: 1h=97, 4h=96, 8h=95, 24h=93
    hits2 = check_hit(
        side="long_liq_below",
        cluster_price=95.0,
        future_highs={"1h": 102.0, "4h": 103.0, "8h": 104.0, "24h": 105.0},
        future_lows={"1h": 97.0, "4h": 96.0, "8h": 95.0, "24h": 93.0},
    )
    report("long below: 1h low=97 > 95 → miss", hits2["hit_1h"] is False)
    report("long below: 4h low=96 > 95 → miss", hits2["hit_4h"] is False)
    report("long below: 8h low=95 <= 95 → hit", hits2["hit_8h"] is True)
    report("long below: 24h low=93 <= 95 → hit", hits2["hit_24h"] is True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    test_bucket_grouping()
    test_cluster_detection()
    test_side_classification()
    test_hit_rate()
    test_magnet_score()
    test_empty_data()
    test_distance_buckets()
    test_check_hit()

    print(f"\nPASS: {passed} | FAIL: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
