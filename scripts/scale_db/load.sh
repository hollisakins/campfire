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
# The migration chain seeds a few rows itself (system object_lists, flag
# definitions, ...), which a data-only restore would collide with: every table
# the dump carries is truncated first, in the same transaction, so the result
# is exactly the dump's data over the chain's schema. pg_restore >= 17 writes
# to stdout only with an explicit `-f -`; a failed restore fails the load
# (the pipeline would otherwise load nothing and exit 0 on psql's success).
TABLES=$(pg_restore -l "$DUMP" | awk '/ TABLE DATA /{printf "%s%s.%s", sep, $(NF-2), $(NF-1); sep=", "}')
[ -n "$TABLES" ] || { echo "no TABLE DATA entries in $DUMP" >&2; exit 2; }
{
  echo "SET session_replication_role = replica;"
  echo "SET statement_timeout = 0;"
  echo "TRUNCATE TABLE $TABLES CASCADE;"
  pg_restore --data-only --no-owner --no-privileges --format=custom -f - "$DUMP" || { echo "pg_restore failed" >&2; exit 1; }
} | psql "$DB_URL" -v ON_ERROR_STOP=1 -q --single-transaction || { echo "restore failed" >&2; exit 1; }
t2=$(date +%s); echo "restore: $((t2-t1)) s"

psql "$DB_URL" -v ON_ERROR_STOP=1 -q -f "$here/scrub.sql"
# Statistics before anything plans against the load: the first freeze
# rehearsal (2026-10-12) ran for 10+ minutes on a catastrophic no-stats plan
# because autoanalyze only caught up a minute after it began (the same
# statement takes 13 s with statistics).
psql "$DB_URL" -v ON_ERROR_STOP=1 -q -c "ANALYZE"
t3=$(date +%s); echo "scrub + analyze: $((t3-t2)) s"

psql "$DB_URL" -tA -c "SELECT 'objects '||count(*) FROM objects UNION ALL SELECT 'spectra '||count(*) FROM spectra UNION ALL SELECT 'storage_objects '||count(*) FROM storage_objects UNION ALL SELECT 'line_fits '||count(*) FROM spectrum_line_fits UNION ALL SELECT 'db_size '||pg_size_pretty(pg_database_size(current_database()))"
echo "total: $((t3-t0)) s"
