-- One-time backfill for the is_public columns (Catalog Read Model phase 1, #533).
--
-- The migration adds objects / targets / spectra.is_public as NOT NULL DEFAULT
-- false and the triggers keep new and changed rows exact, but rows that
-- predate the column are false until this runs. The read policies are correct
-- either way (`is_public OR <scope>` falls through to the scope array); only
-- the planner's estimate is wrong until the flags are real, so there is no
-- rush and no data risk. Idempotent: every statement writes only rows whose
-- value would change, and none of the touched columns fire an updated_at bump
-- (bump_spectra_updated_at is column-scoped; objects.updated_at is stamped by
-- inspection and reconcile only), so sync clients see nothing.
--
-- Run once on the VM after the phase 1 deploy, as the database superuser:
--   docker exec -i campfire-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < scripts/backfill_is_public.sql
-- ~45 k targets, ~80 k spectra, ~43 k objects: a few seconds in all.
\timing on
BEGIN;
SET LOCAL statement_timeout = '5min';

UPDATE targets t SET is_public = p.is_public
  FROM programs p
 WHERE p.slug = t.program_slug
   AND t.is_public IS DISTINCT FROM p.is_public;

UPDATE spectra s SET is_public = p.is_public
  FROM programs p
 WHERE p.slug = s.program_slug
   AND s.is_public IS DISTINCT FROM p.is_public;

UPDATE objects o SET is_public = v.pub
  FROM (SELECT o2.id,
               EXISTS (SELECT 1 FROM programs p WHERE p.is_public AND p.slug = ANY(o2.programs)) AS pub
          FROM objects o2) v
 WHERE v.id = o.id
   AND o.is_public IS DISTINCT FROM v.pub;

COMMIT;

-- Fresh statistics are the whole point.
ANALYZE objects;
ANALYZE targets;
ANALYZE spectra;

-- Verification: every row agrees with programs (all three counts must be 0).
SELECT 'objects' AS t, count(*) AS stale FROM objects o
 WHERE o.is_public <> EXISTS (SELECT 1 FROM programs p WHERE p.is_public AND p.slug = ANY(o.programs))
UNION ALL
SELECT 'targets', count(*) FROM targets t JOIN programs p ON p.slug = t.program_slug WHERE t.is_public <> p.is_public
UNION ALL
SELECT 'spectra', count(*) FROM spectra s JOIN programs p ON p.slug = s.program_slug WHERE s.is_public <> p.is_public;
