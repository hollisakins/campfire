#!/usr/bin/env bash
# Freeze-at-scale harness (data releases P4a, impl plan §6 "Tests" / gate L).
#
# Runs dr_freeze_job on a production-shaped database (scripts/scale_db/load.sh)
# and reports what sizes the executor and the statement_timeout: wall time,
# WAL written, temp bytes, the frozen tables' sizes, and -- with --with-reader --
# whether a concurrent sync reader and a simulated redeploy touch raised any
# conflict. Then a second freeze is cut short (statement_timeout) and re-run to
# show the retry converges on the same counts.
#
# Usage: DB_URL=postgresql://... scripts/measure_dr_freeze.sh [TAG] [--with-reader]
#   TAG defaults to DR-harness; the tag is purged at the end.
set -euo pipefail
DB_URL="${DB_URL:?set DB_URL to the scale-DB stack}"
TAG="${1:-DR-harness}"
WITH_READER="${2:-}"
PSQL=(psql "$DB_URL" -X -q -v ON_ERROR_STOP=1 -tA)

echo "== catalog"
"${PSQL[@]}" -c "SELECT 'published spectra '||count(*) FROM spectra WHERE deploy_status='published'
UNION ALL SELECT 'active objects '||count(*) FROM objects WHERE is_active
UNION ALL SELECT 'line fits '||count(*) FROM spectrum_line_fits
UNION ALL SELECT 'registry rows '||count(*) FROM storage_objects
UNION ALL SELECT 'db size '||pg_size_pretty(pg_database_size(current_database()))"

"${PSQL[@]}" -c "DELETE FROM dr_manifest WHERE tag='$TAG'; DELETE FROM dr_line_fits WHERE tag='$TAG'; DELETE FROM dr_photometry WHERE tag='$TAG'; DELETE FROM dr_spectra WHERE tag='$TAG'; DELETE FROM dr_objects WHERE tag='$TAG'; DELETE FROM dr_exports WHERE tag='$TAG'; DELETE FROM dr_releases WHERE tag='$TAG';" >/dev/null
# nirspec + every NIRCam field with published mosaics
COMPONENTS=$("${PSQL[@]}" -c "SELECT jsonb_agg(c)::text FROM (SELECT 'nirspec' AS c UNION ALL SELECT DISTINCT 'nircam/'||field FROM nircam_images WHERE deploy_status='published') x")
echo "== components: $COMPONENTS"

reader_pid=""
if [ "$WITH_READER" = "--with-reader" ]; then
  echo "== concurrent load: sync pages (get_objects_for_sync) + a redeploy-like touch of 50 spectra per round"
  (
    for _ in $(seq 1 200); do
      psql "$DB_URL" -X -q -tA -c "SELECT count(*) FROM get_objects_for_sync((SELECT array_agg(slug) FROM programs), NULL, NULL, 2000, false, false, NULL, NULL)" >/dev/null 2>&1 || true
      psql "$DB_URL" -X -q -tA -c "UPDATE spectra SET updated_at = now() WHERE id IN (SELECT id FROM spectra WHERE deploy_status='published' ORDER BY random() LIMIT 50)" >/dev/null 2>&1 || true
    done
  ) &
  reader_pid=$!
fi

echo "== freeze 1 (full)"
"${PSQL[@]}" <<SQL
INSERT INTO dr_releases (tag, state, components, freeze_started_at) VALUES ('$TAG', 'freezing', '$COMPONENTS'::jsonb, now());
CREATE TEMP TABLE h0 AS SELECT pg_current_wal_lsn() AS lsn, clock_timestamp() AS t0,
  (SELECT sum(temp_bytes) FROM pg_stat_database WHERE datname = current_database()) AS temp0;
SELECT public.dr_freeze_job('$TAG');
SELECT 'state '||state||' error '||coalesce(error,'-')||' duration_ms '||coalesce(freeze_duration_ms::text,'-') FROM dr_releases WHERE tag='$TAG';
SELECT 'counts '||counts::text FROM dr_releases WHERE tag='$TAG';
SELECT 'wal written '||pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), lsn)) FROM h0;
SELECT 'temp bytes delta '||pg_size_pretty(((SELECT sum(temp_bytes) FROM pg_stat_database WHERE datname = current_database()) - temp0)::bigint) FROM h0;
SELECT 'wall '||round(extract(epoch FROM clock_timestamp() - t0)::numeric, 1)||' s' FROM h0;
SELECT 'size '||relname||' '||pg_size_pretty(pg_total_relation_size('public.'||relname)) FROM (VALUES ('dr_spectra'),('dr_objects'),('dr_manifest'),('dr_line_fits'),('dr_photometry')) v(relname);
SELECT 'frozen tables total '||pg_size_pretty(sum(pg_total_relation_size('public.'||relname))::bigint) FROM (VALUES ('dr_spectra'),('dr_objects'),('dr_manifest'),('dr_line_fits'),('dr_photometry')) v(relname);
SQL

if [ -n "$reader_pid" ]; then
  kill "$reader_pid" 2>/dev/null || true
  wait "$reader_pid" 2>/dev/null || true
  echo "== conflicts / deadlocks seen by the database"
  "${PSQL[@]}" -c "SELECT 'conflicts '||conflicts||' deadlocks '||deadlocks FROM pg_stat_database WHERE datname=current_database()"
fi

echo "== freeze 2: cut short by statement_timeout, then re-run converges"
"${PSQL[@]}" <<SQL
UPDATE dr_releases SET state='freezing' WHERE tag='$TAG';
SQL
BEFORE=$("${PSQL[@]}" -c "SELECT counts::text FROM dr_releases WHERE tag='$TAG'")
set +e
psql "$DB_URL" -X -q -tA -c "SET statement_timeout='2s'; SELECT public.dr_freeze_job('$TAG');" 2>&1 | tail -1
set -e
"${PSQL[@]}" <<SQL
SELECT 'after the cut: state '||state||' error '||left(coalesce(error,'-'), 80) FROM dr_releases WHERE tag='$TAG';
SELECT 'partial rows after the cut: '||(SELECT count(*) FROM dr_spectra WHERE tag='$TAG');
UPDATE dr_releases SET state='freezing' WHERE tag='$TAG';
SELECT public.dr_freeze_job('$TAG');
SELECT 'rerun state '||state||' duration_ms '||coalesce(freeze_duration_ms::text,'-') FROM dr_releases WHERE tag='$TAG';
SQL
AFTER=$("${PSQL[@]}" -c "SELECT counts::text FROM dr_releases WHERE tag='$TAG'")
if [ "$BEFORE" = "$AFTER" ]; then echo "converged: counts identical to freeze 1"; else echo "MISMATCH: $BEFORE vs $AFTER"; fi

echo "== purge"
"${PSQL[@]}" -c "SELECT set_config('request.jwt.claims', '{\"role\":\"service_role\"}', true); SELECT (public.dr_purge_release('$TAG'))->'deleted';"
