#!/usr/bin/env bash
# Load the latest production data dump into the CURRENT branch's local stack
# and scrub it (P-1, PR 5). Rebuilds the database first (migration chain, no
# seed), so run it on a branch whose stack you are happy to wipe.
#
# Usage: scripts/scale_db/load.sh [dump_file]
#   needs: the native stack running for this branch (supabase start --runtime native),
#          psql / pg_restore >= 17 on PATH (e.g. ~/miniforge3/envs/pgvalidate/bin)
set -euo pipefail
export SUPABASE_EXPERIMENTAL_STACK=1
SB=${SB:-"npx -y supabase@2.120.0"}
OUT_DIR="${CAMPFIRE_SCALE_DB_DIR:-$HOME/campfire-scale-db}"
DUMP="${1:-$(cat "$OUT_DIR/LATEST" 2>/dev/null || true)}"
[ -r "$DUMP" ] || { echo "no dump file (run scripts/scale_db/dump_prod.sh first): $DUMP" >&2; exit 2; }
here="$(cd "$(dirname "$0")" && pwd)"

DB_URL=$($SB status --output-format json | python3 -c 'import sys,json; print(json.load(sys.stdin)["env"]["DB_URL"])')
echo "target: $DB_URL"
echo "dump:   $DUMP"

t0=$(date +%s)
$SB db reset --yes --no-seed >/dev/null
t1=$(date +%s); echo "db reset (chain, no seed): $((t1-t0)) s"

# Triggers (FKs included) off for the bulk load: the dump is a consistent
# snapshot and the chain's triggers would otherwise recompute derived rows for
# every insert. Generated columns are not in the dump and recompute themselves.
{
  echo "SET session_replication_role = replica;"
  echo "SET statement_timeout = 0;"
  pg_restore --data-only --no-owner --no-privileges --format=custom "$DUMP"
} | psql "$DB_URL" -v ON_ERROR_STOP=1 -q --single-transaction
t2=$(date +%s); echo "restore: $((t2-t1)) s"

psql "$DB_URL" -v ON_ERROR_STOP=1 -q -f "$here/scrub.sql"
t3=$(date +%s); echo "scrub + analyze: $((t3-t2)) s"

psql "$DB_URL" -tA -c "SELECT 'objects '||count(*) FROM objects UNION ALL SELECT 'spectra '||count(*) FROM spectra UNION ALL SELECT 'storage_objects '||count(*) FROM storage_objects UNION ALL SELECT 'line_fits '||count(*) FROM spectrum_line_fits UNION ALL SELECT 'db_size '||pg_size_pretty(pg_database_size(current_database()))"
echo "total: $((t3-t0)) s"
