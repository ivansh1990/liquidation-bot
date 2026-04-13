#!/usr/bin/env python3
"""
Quick analysis — run as soon as you have 2+ days of data.

Answers:
1. How much data has been collected?
2. What does the current liquidation map look like?
3. Is there asymmetry in liquidation levels above/below current price?
4. What are the latest Binance metrics?
5. How is the liquidation map evolving? (if >1 day of data)

Output: text report to stdout + saved to analysis/report_YYYY-MM-DD.txt
"""

import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.config import get_config
from collectors.db import init_pool, get_conn

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
log = logging.getLogger(__name__)

REPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "analysis"
)


def section(title: str) -> str:
    return f"\n{'='*60}\n{title}\n{'='*60}\n"


def run_analysis() -> str:
    lines = []

    def out(text: str = "") -> None:
        lines.append(text)

    out(section("1. DATA COLLECTION STATISTICS"))

    with get_conn() as conn:
        cur = conn.cursor()

        # Address count
        cur.execute("SELECT COUNT(*) FROM hl_addresses")
        addr_count = cur.fetchone()[0]
        out(f"  Tracked addresses:     {addr_count}")

        # Position snapshots
        cur.execute("SELECT COUNT(*) FROM hl_position_snapshots")
        pos_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT snapshot_time) FROM hl_position_snapshots")
        snap_count = cur.fetchone()[0]
        out(f"  Position snapshots:    {pos_count} rows across {snap_count} snapshots")

        # Estimated vs real liquidation prices
        cur.execute(
            "SELECT is_liq_estimated, COUNT(*) FROM hl_position_snapshots "
            "GROUP BY is_liq_estimated"
        )
        for row in cur.fetchall():
            label = "estimated" if row[0] else "real (API)"
            out(f"    liquidation_px {label}: {row[1]}")

        # Liquidation map
        cur.execute("SELECT COUNT(*) FROM hl_liquidation_map")
        map_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT snapshot_time) FROM hl_liquidation_map")
        map_snaps = cur.fetchone()[0]
        out(f"  Liquidation map:       {map_count} rows across {map_snaps} snapshots")

        # Binance tables
        for table in ["binance_oi", "binance_funding", "binance_ls_ratio", "binance_taker"]:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
            out(f"  {table:24s} {count} rows")

        # Time range
        cur.execute(
            "SELECT MIN(snapshot_time), MAX(snapshot_time) FROM hl_position_snapshots"
        )
        row = cur.fetchone()
        if row[0]:
            out(f"\n  Data range: {row[0]} → {row[1]}")

    # --- Section 2: Current Liquidation Map ---
    out(section("2. CURRENT LIQUIDATION MAP (top 20 levels)"))

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT coin, price_level,
                   SUM(long_liq_usd) AS total_long,
                   SUM(short_liq_usd) AS total_short,
                   SUM(num_long_positions) AS n_long,
                   SUM(num_short_positions) AS n_short,
                   AVG(current_price) AS avg_price
            FROM hl_liquidation_map
            WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM hl_liquidation_map)
            GROUP BY coin, price_level
            ORDER BY (SUM(long_liq_usd) + SUM(short_liq_usd)) DESC
            LIMIT 20
        """)
        rows = cur.fetchall()
        if rows:
            out(f"  {'Coin':<6} {'Level':>12} {'Long $':>14} {'Short $':>14} {'#L':>4} {'#S':>4} {'Price':>12}")
            out(f"  {'-'*6} {'-'*12} {'-'*14} {'-'*14} {'-'*4} {'-'*4} {'-'*12}")
            for r in rows:
                out(
                    f"  {r[0]:<6} {r[1]:>12,.2f} {r[2]:>14,.0f} {r[3]:>14,.0f} "
                    f"{r[4]:>4} {r[5]:>4} {r[6]:>12,.2f}"
                )
        else:
            out("  No liquidation map data yet.")

    # --- Section 3: Asymmetry ---
    out(section("3. LIQUIDATION ASYMMETRY (BTC, ETH)"))

    with get_conn() as conn:
        cur = conn.cursor()
        for coin in ["BTC", "ETH"]:
            cur.execute("""
                SELECT
                    SUM(CASE WHEN price_level < current_price THEN long_liq_usd ELSE 0 END) AS liq_below,
                    SUM(CASE WHEN price_level > current_price THEN short_liq_usd ELSE 0 END) AS liq_above,
                    AVG(current_price) AS price
                FROM hl_liquidation_map
                WHERE coin = %s
                  AND snapshot_time = (SELECT MAX(snapshot_time) FROM hl_liquidation_map WHERE coin = %s)
            """, (coin, coin))
            row = cur.fetchone()
            if row and row[0] is not None and row[1] is not None:
                below = row[0]
                above = row[1]
                price = row[2]
                ratio = below / above if above > 0 else float("inf")
                out(f"  {coin} (price ${price:,.2f}):")
                out(f"    Long liq below price:  ${below:>14,.0f}")
                out(f"    Short liq above price: ${above:>14,.0f}")
                out(f"    Ratio (below/above):   {ratio:>14.2f}")
                if ratio > 2:
                    out(f"    ⚠ Strong downside liquidation cluster")
                elif ratio < 0.5:
                    out(f"    ⚠ Strong upside liquidation cluster")
            else:
                out(f"  {coin}: no data yet")

    # --- Section 4: Binance Quick Check ---
    out(section("4. LATEST BINANCE METRICS"))

    with get_conn() as conn:
        cur = conn.cursor()

        out(f"  {'Symbol':<8} {'OI ($)':>14} {'Funding':>10} {'L/S Ratio':>10} {'Taker B/S':>10}")
        out(f"  {'-'*8} {'-'*14} {'-'*10} {'-'*10} {'-'*10}")

        cur.execute("""
            SELECT DISTINCT symbol FROM binance_oi ORDER BY symbol
        """)
        symbols = [r[0] for r in cur.fetchall()]

        for sym in symbols:
            # Latest OI
            cur.execute(
                "SELECT open_interest_usd FROM binance_oi "
                "WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1", (sym,)
            )
            oi_row = cur.fetchone()
            oi_str = f"${oi_row[0]:>12,.0f}" if oi_row else "N/A".rjust(14)

            # Latest funding
            cur.execute(
                "SELECT funding_rate FROM binance_funding "
                "WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1", (sym,)
            )
            fr_row = cur.fetchone()
            fr_val = fr_row[0] if fr_row else None
            fr_str = f"{fr_val:>10.6f}" if fr_val is not None else "N/A".rjust(10)
            fr_flag = " !" if fr_val is not None and abs(fr_val) > 0.0001 else ""

            # Latest L/S ratio
            cur.execute(
                "SELECT long_short_ratio FROM binance_ls_ratio "
                "WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1", (sym,)
            )
            ls_row = cur.fetchone()
            ls_val = ls_row[0] if ls_row else None
            ls_str = f"{ls_val:>10.4f}" if ls_val is not None else "N/A".rjust(10)
            ls_flag = " !" if ls_val is not None and (ls_val > 2 or ls_val < 0.5) else ""

            # Latest taker
            cur.execute(
                "SELECT buy_sell_ratio FROM binance_taker "
                "WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1", (sym,)
            )
            tk_row = cur.fetchone()
            tk_str = f"{tk_row[0]:>10.4f}" if tk_row else "N/A".rjust(10)

            out(f"  {sym:<8} {oi_str} {fr_str}{fr_flag} {ls_str}{ls_flag} {tk_str}")

    # --- Section 5: Liquidation Map Dynamics ---
    out(section("5. LIQUIDATION MAP DYNAMICS (24h change)"))

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            WITH latest AS (
                SELECT coin, price_level, SUM(long_liq_usd + short_liq_usd) AS total_usd
                FROM hl_liquidation_map
                WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM hl_liquidation_map)
                GROUP BY coin, price_level
            ),
            day_ago AS (
                SELECT coin, price_level, SUM(long_liq_usd + short_liq_usd) AS total_usd
                FROM hl_liquidation_map
                WHERE snapshot_time = (
                    SELECT MAX(snapshot_time) FROM hl_liquidation_map
                    WHERE snapshot_time < NOW() - INTERVAL '24 hours'
                )
                GROUP BY coin, price_level
            )
            SELECT
                l.coin, l.price_level,
                l.total_usd AS now_usd,
                COALESCE(d.total_usd, 0) AS prev_usd,
                l.total_usd - COALESCE(d.total_usd, 0) AS change_usd
            FROM latest l
            LEFT JOIN day_ago d ON l.coin = d.coin AND l.price_level = d.price_level
            ORDER BY ABS(l.total_usd - COALESCE(d.total_usd, 0)) DESC
            LIMIT 10
        """)
        rows = cur.fetchall()
        if rows:
            out(f"  {'Coin':<6} {'Level':>12} {'Now $':>14} {'24h ago $':>14} {'Change $':>14}")
            out(f"  {'-'*6} {'-'*12} {'-'*14} {'-'*14} {'-'*14}")
            for r in rows:
                change = r[4]
                direction = "↑" if change > 0 else "↓" if change < 0 else "="
                out(
                    f"  {r[0]:<6} {r[1]:>12,.2f} {r[2]:>14,.0f} {r[3]:>14,.0f} "
                    f"{change:>+14,.0f} {direction}"
                )
        else:
            out("  Not enough data for 24h comparison yet.")

    return "\n".join(lines)


def main() -> None:
    cfg = get_config()
    init_pool(cfg)

    report_text = run_analysis()
    print(report_text)

    # Save to file
    os.makedirs(REPORT_DIR, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    report_path = os.path.join(REPORT_DIR, f"report_{date_str}.txt")
    with open(report_path, "w") as f:
        f.write(report_text)
    print(f"\nReport saved to {report_path}")


if __name__ == "__main__":
    main()
