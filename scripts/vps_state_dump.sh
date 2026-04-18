#!/usr/bin/env bash
# vps_state_dump.sh — read-only liquidation-bot state inspector for the VPS.
#
# Run on the production VPS (Hetzner, Linux) and paste the output back to the
# architect / planning session. This script:
#   - Lists OS / systemd / docker / tmux state relevant to liquidation-bot.
#   - Counts rows + min/max timestamp for every Postgres table named in
#     collectors/db.py SCHEMA_SQL plus the inline-created tables (coinglass_*,
#     coinglass_*_h1/h2/30m, coinglass_netposition_*, coinglass_cvd_*).
#   - Prints disk usage on the project + Postgres data dirs.
#   - Tails the last 30 journalctl lines per liquidation systemd unit.
#
# It performs ZERO writes, ZERO restarts, ZERO migrations. Safe to run any time.
#
# Usage:
#   bash scripts/vps_state_dump.sh
#   bash scripts/vps_state_dump.sh > /tmp/vps_state_$(date -u +%Y%m%dT%H%M%SZ).txt 2>&1
#
# Env-var expectations (read from .env or shell):
#   LIQ_DB_HOST, LIQ_DB_PORT, LIQ_DB_NAME, LIQ_DB_USER, LIQ_DB_PASSWORD
# If a .env exists at the project root it is sourced for these vars.

set -u

PROJECT_ROOT="${PROJECT_ROOT:-$HOME/liquidation-bot}"
ENV_FILE="$PROJECT_ROOT/.env"

print_section() {
  printf '\n=========================================================================\n'
  printf '== %s\n' "$1"
  printf '=========================================================================\n'
}

run_cmd() {
  printf '$ %s\n' "$*"
  "$@" 2>&1 || printf '(exit=%d)\n' "$?"
  printf '\n'
}

# --- Source .env if present (export so child shells see the vars) -----------
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

LIQ_DB_HOST="${LIQ_DB_HOST:-localhost}"
LIQ_DB_PORT="${LIQ_DB_PORT:-5432}"
LIQ_DB_NAME="${LIQ_DB_NAME:-liquidation}"
LIQ_DB_USER="${LIQ_DB_USER:-postgres}"
export PGPASSWORD="${LIQ_DB_PASSWORD:-}"

PSQL=(psql -X -h "$LIQ_DB_HOST" -p "$LIQ_DB_PORT" -U "$LIQ_DB_USER" -d "$LIQ_DB_NAME" -A -F '|' -t)

# --- Section 1: host / OS ---------------------------------------------------
print_section "Host & OS"
run_cmd uname -a
run_cmd date -u
run_cmd uptime
run_cmd whoami
run_cmd cat /etc/os-release

# --- Section 2: systemd state ----------------------------------------------
print_section "Systemd units (liquidation-bot scope)"
run_cmd systemctl list-units --type=service --all --no-pager --plain --no-legend
echo "--- timers (active + dormant) ---"
run_cmd systemctl list-timers --all --no-pager
echo "--- per-unit is-active / is-enabled ---"
for unit in liq-hl-websocket.service liq-hl-snapshots.timer liq-binance.timer \
            liq-coinglass-oi.timer liq-paper-bot.service \
            liq-showcase-bot.service liq-telegram-bot.service; do
  printf '%s  active=%s  enabled=%s\n' \
    "$unit" \
    "$(systemctl is-active "$unit" 2>&1)" \
    "$(systemctl is-enabled "$unit" 2>&1)"
done

# --- Section 3: docker / tmux (defensive — neither expected on this stack) -
print_section "Docker & tmux (expected: none — pure systemd deploy)"
if command -v docker >/dev/null 2>&1; then
  run_cmd docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
else
  echo "(docker not installed — expected)"
fi
if command -v tmux >/dev/null 2>&1; then
  tmux list-sessions 2>&1 || echo "(no tmux sessions — expected)"
else
  echo "(tmux not installed)"
fi

# --- Section 4: Postgres connectivity probe --------------------------------
print_section "Postgres connectivity"
echo "DSN: host=$LIQ_DB_HOST port=$LIQ_DB_PORT db=$LIQ_DB_NAME user=$LIQ_DB_USER"
if ! "${PSQL[@]}" -c 'SELECT 1' >/dev/null 2>&1; then
  echo "ERROR: psql cannot connect — sections 5-7 will be skipped."
  echo "Diagnostics:"
  run_cmd "${PSQL[@]}" -c 'SELECT 1'
  PG_OK=0
else
  echo "OK"
  PG_OK=1
fi

# --- Section 5: row counts per table ---------------------------------------
print_section "Postgres tables — row counts + timestamp range"
if [ "$PG_OK" -eq 1 ]; then
  TABLES=(
    "hl_addresses:first_seen"
    "hl_position_snapshots:snapshot_time"
    "hl_liquidation_map:snapshot_time"
    "binance_oi:timestamp"
    "binance_funding:timestamp"
    "binance_ls_ratio:timestamp"
    "binance_taker:timestamp"
    "coinglass_liquidations:timestamp"
    "coinglass_liquidations_h1:timestamp"
    "coinglass_liquidations_h2:timestamp"
    "coinglass_liquidations_30m:timestamp"
    "coinglass_oi:timestamp"
    "coinglass_oi_h1:timestamp"
    "coinglass_oi_h2:timestamp"
    "coinglass_oi_30m:timestamp"
    "coinglass_funding:timestamp"
    "coinglass_netposition_h1:timestamp"
    "coinglass_netposition_h2:timestamp"
    "coinglass_netposition_h4:timestamp"
    "coinglass_cvd_h1:timestamp"
    "coinglass_cvd_h2:timestamp"
    "coinglass_cvd_h4:timestamp"
  )
  printf '%-32s | %12s | %25s | %25s\n' "table" "count" "min_ts" "max_ts"
  printf '%-32s-+-%12s-+-%25s-+-%25s\n' "$(printf -- '-%.0s' {1..32})" "$(printf -- '-%.0s' {1..12})" "$(printf -- '-%.0s' {1..25})" "$(printf -- '-%.0s' {1..25})"
  for entry in "${TABLES[@]}"; do
    table="${entry%%:*}"
    ts_col="${entry##*:}"
    exists=$("${PSQL[@]}" -c "SELECT to_regclass('public.$table') IS NOT NULL" 2>/dev/null | tr -d '[:space:]')
    if [ "$exists" != "t" ]; then
      printf '%-32s | %12s | %25s | %25s\n' "$table" "MISSING" "-" "-"
      continue
    fi
    row=$("${PSQL[@]}" -c "SELECT COUNT(*), COALESCE(MIN($ts_col)::text,''), COALESCE(MAX($ts_col)::text,'') FROM $table" 2>/dev/null)
    cnt=$(echo "$row" | cut -d'|' -f1 | tr -d '[:space:]')
    mn=$(echo "$row"  | cut -d'|' -f2)
    mx=$(echo "$row"  | cut -d'|' -f3)
    printf '%-32s | %12s | %25s | %25s\n' "$table" "${cnt:-?}" "${mn:-?}" "${mx:-?}"
  done
else
  echo "(skipped — psql failed)"
fi

# --- Section 6: per-coin liquidation-map cardinality (L6b prerequisite) ----
print_section "hl_liquidation_map per-coin (L6b retest pre-flight)"
if [ "$PG_OK" -eq 1 ]; then
  "${PSQL[@]}" -c "
SELECT coin, COUNT(*) AS snapshots,
       MIN(snapshot_time)::date AS first_day,
       MAX(snapshot_time)::date AS last_day,
       MAX(snapshot_time) - MIN(snapshot_time) AS span
FROM hl_liquidation_map
GROUP BY coin
ORDER BY coin;
" 2>&1
else
  echo "(skipped)"
fi

# --- Section 7: top tables by size -----------------------------------------
print_section "Top 15 tables by total relation size"
if [ "$PG_OK" -eq 1 ]; then
  "${PSQL[@]}" -c "
SELECT relname AS table_name,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname = 'public'
ORDER BY pg_total_relation_size(c.oid) DESC
LIMIT 15;
" 2>&1
else
  echo "(skipped)"
fi

# --- Section 8: disk usage --------------------------------------------------
print_section "Disk usage"
run_cmd df -h /
run_cmd df -h "$PROJECT_ROOT"
if [ -d "$PROJECT_ROOT" ]; then
  run_cmd du -sh "$PROJECT_ROOT"
  for sub in state analysis logs; do
    [ -d "$PROJECT_ROOT/$sub" ] && run_cmd du -sh "$PROJECT_ROOT/$sub"
  done
fi
# Postgres data dir (path varies by distro; try the common ones)
for pgdata in /var/lib/postgresql /var/lib/pgsql; do
  [ -d "$pgdata" ] && run_cmd sudo -n du -sh "$pgdata" 2>&1 || true
done

# --- Section 9: paper-bot state file ---------------------------------------
print_section "Paper bot state file"
STATE_FILE="$PROJECT_ROOT/state/paper_state.json"
if [ -f "$STATE_FILE" ]; then
  run_cmd ls -la "$STATE_FILE"
  echo "--- summary ---"
  python3 - <<EOF 2>&1 || echo "(python summary failed)"
import json, sys
try:
    s = json.load(open("$STATE_FILE"))
    print(f"capital            = {s.get('capital'):.2f}")
    print(f"open_positions     = {len(s.get('positions', []))}")
    print(f"closed_trades      = {len(s.get('closed_trades', []))}")
    print(f"equity_history len = {len(s.get('equity_history', []))}")
    print(f"last_summary_date  = {s.get('last_summary_date')}")
    if s.get('positions'):
        print('open_positions sample:')
        for p in s['positions'][:3]:
            print(f"  {p.get('coin')} entry={p.get('entry_px')} due={p.get('exit_due')}")
except Exception as e:
    print(f"ERROR reading state: {e}")
EOF
else
  echo "MISSING: $STATE_FILE — paper bot has not produced state on this VPS."
fi

# --- Section 10: recent journal logs per liquidation unit ------------------
print_section "Recent journalctl per liquidation unit (last 30 lines)"
for unit in liq-hl-websocket.service liq-hl-snapshots.service \
            liq-binance.service liq-coinglass-oi.service \
            liq-paper-bot.service liq-showcase-bot.service \
            liq-telegram-bot.service; do
  printf '\n--- %s ---\n' "$unit"
  journalctl -u "$unit" --no-pager -n 30 2>&1 | tail -n 35
done

# --- Section 11: errors in the last hour (any liquidation unit) ------------
print_section "Errors in the last hour (any liquidation unit)"
journalctl --since '1 hour ago' -p err --no-pager \
  -u liq-hl-websocket.service \
  -u liq-hl-snapshots.service \
  -u liq-binance.service \
  -u liq-coinglass-oi.service \
  -u liq-paper-bot.service \
  -u liq-showcase-bot.service \
  -u liq-telegram-bot.service 2>&1 | head -200

# --- Section 12: git state --------------------------------------------------
print_section "Git state"
if [ -d "$PROJECT_ROOT/.git" ]; then
  ( cd "$PROJECT_ROOT" && \
    run_cmd git rev-parse --abbrev-ref HEAD && \
    run_cmd git log --oneline -10 && \
    run_cmd git status --short --branch )
else
  echo "(no git checkout at $PROJECT_ROOT)"
fi

print_section "End of dump"
unset PGPASSWORD
