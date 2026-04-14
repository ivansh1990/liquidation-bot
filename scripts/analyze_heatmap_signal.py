#!/usr/bin/env python3
"""
Hyperliquid heatmap overlay analysis (framework).

Question: when a CoinGlass long-flush signal fires, does the presence of a
short-liquidation cluster above price on the Hyperliquid heatmap predict a
better outcome? If yes, the heatmap could act as a confirmation filter / TP
target generator on top of H1.

HL snapshot collection started recently (mid-April 2026), so this script is
mostly a framework: it will usually emit "insufficient data, projected ready
date: ...". Re-run once the projected date has passed.

Method:
  1. Pull the hl_liquidation_map snapshot window.
  2. For each H1 flush event (long_vol_zscore > 2.0) inside that window:
     • Find the HL snapshot that IMMEDIATELY PRECEDES the flush close
       (no look-ahead). Skip if delta > 30 min.
     • From that snapshot, identify "clusters" via top-decile rule:
         - rank all rows by short_liq_usd → keep top 10% as short clusters
         - rank all rows by long_liq_usd  → keep top 10% as long clusters
     • Record nearest short cluster above current_price (distance + size)
       and nearest long cluster below (distance + size).
  3. If matched count ≥ 30 → split into Group A (short cluster within 5%
     above) vs Group B (no nearby cluster); compare return_8h.
  4. If matched count < 30 → print status and projected ready date.

Coin scope: SOL, DOGE, LINK, AVAX, SUI, ARB (same altcoins as walk-forward).

Usage:
    .venv/bin/python scripts/analyze_heatmap_signal.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

# Path setup (see walkforward_h1_flush.py).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _THIS_DIR)

from collectors.config import binance_ccxt_symbol, get_config, hl_coin  # noqa: E402
from collectors.db import get_conn, init_pool  # noqa: E402

from backtest_liquidation_flush import (  # noqa: E402
    compute_signals,
    fetch_klines_4h,
    load_liquidations,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COINS: list[str] = ["SOL", "DOGE", "LINK", "AVAX", "SUI", "ARB"]
FLUSH_Z_THRESHOLD: float = 2.0
NEAREST_CLUSTER_PCT: float = 5.0      # "nearby" cutoff for Group A split
TOP_DECILE: float = 0.10
HL_MATCH_TOLERANCE_MIN: int = 30      # max staleness for preceding HL snapshot
MIN_MATCHED_EVENTS: int = 30          # below this → framework-only output


# ---------------------------------------------------------------------------
# HL queries
# ---------------------------------------------------------------------------


def get_hl_window() -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """Return (earliest, latest) snapshot_time in hl_liquidation_map (UTC)."""
    with get_conn() as conn:
        row = pd.read_sql(
            "SELECT MIN(snapshot_time) AS mn, MAX(snapshot_time) AS mx FROM hl_liquidation_map",
            conn,
        ).iloc[0]
    if pd.isna(row["mn"]):
        return None, None
    return (pd.Timestamp(row["mn"]).tz_convert("UTC"), pd.Timestamp(row["mx"]).tz_convert("UTC"))


def fetch_preceding_snapshot(coin: str, flush_ts: pd.Timestamp) -> pd.DataFrame:
    """
    Return the single HL snapshot for `coin` whose snapshot_time is the
    largest value ≤ flush_ts. Empty DataFrame if none / too stale.
    """
    with get_conn() as conn:
        # Find the snapshot_time.
        df_ts = pd.read_sql(
            """
            SELECT MAX(snapshot_time) AS snapshot_time
            FROM hl_liquidation_map
            WHERE coin = %s AND snapshot_time <= %s
            """,
            conn,
            params=(coin, flush_ts.to_pydatetime()),
        )
        snap_ts = df_ts["snapshot_time"].iloc[0]
        if snap_ts is None:
            return pd.DataFrame()
        snap_ts = pd.Timestamp(snap_ts).tz_convert("UTC")
        if (flush_ts - snap_ts) > pd.Timedelta(minutes=HL_MATCH_TOLERANCE_MIN):
            return pd.DataFrame()

        df = pd.read_sql(
            """
            SELECT snapshot_time, price_level, long_liq_usd, short_liq_usd,
                   num_long_positions, num_short_positions, current_price
            FROM hl_liquidation_map
            WHERE coin = %s AND snapshot_time = %s
            """,
            conn,
            params=(coin, snap_ts.to_pydatetime()),
        )
    return df


# ---------------------------------------------------------------------------
# Cluster analysis
# ---------------------------------------------------------------------------


def analyze_clusters(snapshot: pd.DataFrame, current_price: float) -> dict:
    """
    Top-decile cluster extraction.

    Returns distance_above_pct / size_above_usd for the nearest top-decile
    short cluster strictly above current_price, and symmetrically for the
    nearest top-decile long cluster strictly below. Missing-side values
    come back as np.nan.
    """
    result = {
        "distance_above_pct": np.nan,
        "size_above_usd": np.nan,
        "distance_below_pct": np.nan,
        "size_below_usd": np.nan,
        "asymmetry": np.nan,  # total_short_above / total_long_below
    }
    if snapshot.empty or current_price <= 0:
        return result

    # Short clusters: top decile of rows by short_liq_usd (excluding zeros).
    shorts = snapshot[snapshot["short_liq_usd"] > 0].copy()
    if not shorts.empty:
        thr = shorts["short_liq_usd"].quantile(1 - TOP_DECILE)
        short_clusters = shorts[shorts["short_liq_usd"] >= thr]
        above = short_clusters[short_clusters["price_level"] > current_price]
        if not above.empty:
            nearest = above.sort_values("price_level").iloc[0]
            result["distance_above_pct"] = (
                (nearest["price_level"] - current_price) / current_price * 100
            )
            result["size_above_usd"] = float(nearest["short_liq_usd"])
        total_short_above = short_clusters.loc[
            short_clusters["price_level"] > current_price, "short_liq_usd"
        ].sum()
    else:
        total_short_above = 0.0

    # Long clusters: top decile by long_liq_usd.
    longs = snapshot[snapshot["long_liq_usd"] > 0].copy()
    if not longs.empty:
        thr = longs["long_liq_usd"].quantile(1 - TOP_DECILE)
        long_clusters = longs[longs["long_liq_usd"] >= thr]
        below = long_clusters[long_clusters["price_level"] < current_price]
        if not below.empty:
            nearest = below.sort_values("price_level", ascending=False).iloc[0]
            result["distance_below_pct"] = (
                (current_price - nearest["price_level"]) / current_price * 100
            )
            result["size_below_usd"] = float(nearest["long_liq_usd"])
        total_long_below = long_clusters.loc[
            long_clusters["price_level"] < current_price, "long_liq_usd"
        ].sum()
    else:
        total_long_below = 0.0

    if total_long_below > 0:
        result["asymmetry"] = total_short_above / total_long_below

    return result


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------


def collect_matched_events(
    hl_first: pd.Timestamp, hl_last: pd.Timestamp
) -> tuple[pd.DataFrame, int]:
    """
    Walk every altcoin's flush events within [hl_first, hl_last] and try to
    match an HL snapshot. Returns (DataFrame of matched events, total_flush_count).
    """
    matched: list[dict] = []
    total_flush = 0

    for coin in COINS:
        print(f"  Processing {coin}...", flush=True)
        liq_df = load_liquidations(coin)
        if liq_df.empty:
            continue
        since_ms = int(liq_df["timestamp"].min().timestamp() * 1000)
        try:
            price_df = fetch_klines_4h(binance_ccxt_symbol(coin), since_ms)
        except Exception as e:
            print(f"    failed klines fetch: {e}")
            continue
        if price_df.empty:
            continue
        sig = compute_signals(liq_df, price_df)
        if sig.empty:
            continue

        window = sig[(sig.index >= hl_first) & (sig.index <= hl_last)]
        flushes = window[window["long_vol_zscore"] > FLUSH_Z_THRESHOLD]
        total_flush += len(flushes)

        hl_coin_name = hl_coin(coin)  # PEPE → kPEPE etc.
        for ts, row in flushes.iterrows():
            snap = fetch_preceding_snapshot(hl_coin_name, ts)
            if snap.empty:
                continue
            current_price = float(snap["current_price"].dropna().iloc[0]) if not snap["current_price"].dropna().empty else float(row["price"])
            clusters = analyze_clusters(snap, current_price)
            matched.append(
                {
                    "coin": coin,
                    "flush_ts": ts,
                    "snapshot_ts": snap["snapshot_time"].iloc[0],
                    "current_price": current_price,
                    "long_vol_zscore": float(row["long_vol_zscore"]),
                    "return_8h": float(row.get("return_8h", np.nan)),
                    **clusters,
                }
            )

    df = pd.DataFrame(matched)
    return df, total_flush


def print_comparison(df: pd.DataFrame) -> None:
    """Compare Group A (nearby short cluster) vs Group B for return_8h."""
    df = df.dropna(subset=["return_8h"])
    a = df[df["distance_above_pct"] < NEAREST_CLUSTER_PCT]
    b = df[
        (df["distance_above_pct"] >= NEAREST_CLUSTER_PCT)
        | (df["distance_above_pct"].isna())
    ]

    def _stats(g: pd.DataFrame) -> tuple[int, float, float]:
        if g.empty:
            return 0, 0.0, 0.0
        return len(g), float(g["return_8h"].mean()), float((g["return_8h"] > 0).mean() * 100)

    an, aa, aw = _stats(a)
    bn, ba, bw = _stats(b)

    print()
    print("  Comparative analysis (return_8h after flush):")
    print(
        f"    Group A (short cluster within {NEAREST_CLUSTER_PCT:.1f}% above): "
        f"N={an}  avg={aa:+.2f}%  win%={aw:.1f}%"
    )
    print(
        f"    Group B (no nearby short cluster):          "
        f"N={bn}  avg={ba:+.2f}%  win%={bw:.1f}%"
    )
    if an > 0 and bn > 0:
        delta_avg = aa - ba
        delta_win = aw - bw
        print(f"    Delta: avg {delta_avg:+.2f}pp, win% {delta_win:+.1f}pp")
        if delta_avg > 0 and delta_win > 0:
            print("    → heatmap short-cluster-above filter improves H1 outcomes.")
        elif delta_avg < 0 and delta_win < 0:
            print("    → nearby short cluster worsens H1 outcomes (counterintuitive, recheck).")
        else:
            print("    → mixed signal; no clear directional effect.")


def main() -> None:
    init_pool(get_config())

    print("H1 FLUSH + HL HEATMAP OVERLAY (preliminary / framework)")
    print("=" * 78)

    hl_first, hl_last = get_hl_window()
    if hl_first is None:
        print("  hl_liquidation_map is empty — no HL snapshots yet.")
        print("  Run the HL snapshots collector for a few days, then re-run.")
        return
    span_days = (hl_last - hl_first).total_seconds() / 86400
    print(f"  HL snapshot window: {hl_first} → {hl_last} ({span_days:.2f} days)")
    print(f"  Coins: {', '.join(COINS)}   flush threshold: z > {FLUSH_Z_THRESHOLD}")
    print(f"  Cluster rule: top-{int(TOP_DECILE*100)}% of rows by respective liq_usd")
    print(f"  HL match: immediately-preceding snapshot, max stale = {HL_MATCH_TOLERANCE_MIN} min")
    print()

    df, total_flush = collect_matched_events(hl_first, hl_last)
    n = len(df)

    print()
    print(f"  Flush events in HL window (all coins):       {total_flush}")
    print(f"  Matched flush-events with HL snapshot:       {n}  (need ≥ {MIN_MATCHED_EVENTS})")

    if n < MIN_MATCHED_EVENTS:
        rate_per_day = n / span_days if span_days > 0 else 0.0
        if rate_per_day > 0:
            days_need = (MIN_MATCHED_EVENTS - n) / rate_per_day
            ready = datetime.now(timezone.utc) + timedelta(days=days_need)
            print(f"  Match rate: {rate_per_day:.2f} events/day")
            print(f"  Projected ready date (≥{MIN_MATCHED_EVENTS} matches): {ready.date()}")
        else:
            print("  Match rate: 0 — need more HL snapshots AND/OR more flush events.")
        print("  Framework ready. Re-run after the projected date.")
        return

    # Enough data — comparative analysis.
    print_comparison(df)

    # Also print a per-coin breakdown for context.
    print()
    print("  Per-coin matched counts:")
    for coin in COINS:
        sub = df[df["coin"] == coin]
        print(f"    {coin:<5} N={len(sub):>3}")


if __name__ == "__main__":
    main()
