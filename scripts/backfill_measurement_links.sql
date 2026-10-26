-- One-time backfill for the phase 2a object links (Catalog Read Model):
--   spectrum_lines.object_id, object_photometry_bands.object_id,
--   spectrum_line_fits.stale_redshift,
--   and the same links inside releases frozen BEFORE the columns existed
--   (dr_spectrum_lines.object_id, dr_photometry_bands.object_id), each
--   resolved from the release's own frozen rows by tag, never from the live
--   catalog (a frozen release is a snapshot; dr_freeze_job copies the links
--   for every freeze after this migration).
--
-- The migration adds the columns NULL / false and the triggers keep new and
-- changed rows exact; rows that predate the columns are filled here. Until it
-- runs, the filter helpers (which now read these columns) see NULL object
-- links and treat every pre-existing fit as not stale, so run it right after
-- the deploy. Idempotent: each statement writes only rows whose value would
-- change. The fits UPDATE fires compute_line_fit_stale per row (same value;
-- harmless) and no updated_at bump exists on these three tables.
--
-- Run once on the VM after the phase 2a deploy, as the database superuser:
--   docker exec -i campfire-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < scripts/backfill_measurement_links.sql
\timing on
BEGIN;
SET LOCAL statement_timeout = '10min';

UPDATE spectrum_lines l SET object_id = t.object_id
  FROM spectra s
  JOIN targets t ON t.target_id = s.target_id
 WHERE s.id = l.spectrum_id
   AND l.object_id IS DISTINCT FROM t.object_id;

UPDATE object_photometry_bands b SET object_id = p.object_id
  FROM object_photometry p
 WHERE p.id = b.photometry_id
   AND b.object_id IS DISTINCT FROM p.object_id;

UPDATE spectrum_line_fits f SET stale_redshift = v.stale
  FROM (SELECT f2.spectrum_id,
               COALESCE(o.id IS NOT NULL AND public.line_fit_stale_redshift(
                 o.version, o.redshift_quality, (o.redshift)::double precision,
                 f2.object_version, f2.z_quality, f2.z_used), false) AS stale
          FROM spectrum_line_fits f2
          JOIN spectra s ON s.id = f2.spectrum_id
          LEFT JOIN targets t ON t.target_id = s.target_id
          LEFT JOIN objects o ON o.id = t.object_id) v
 WHERE v.spectrum_id = f.spectrum_id
   AND f.stale_redshift IS DISTINCT FROM v.stale;

-- Frozen releases: a release's own dr_spectra -> dr_targets and dr_photometry
-- rows, joined by tag. No-op on a database with no releases.
UPDATE dr_spectrum_lines l SET object_id = t.object_id
  FROM dr_spectra s
  JOIN dr_targets t ON t.tag = s.tag AND t.target_id = s.target_id
 WHERE s.tag = l.tag AND s.id = l.spectrum_id
   AND l.object_id IS DISTINCT FROM t.object_id;

UPDATE dr_photometry_bands b SET object_id = p.object_id
  FROM dr_photometry p
 WHERE p.tag = b.tag AND p.id = b.photometry_id
   AND b.object_id IS DISTINCT FROM p.object_id;

COMMIT;

ANALYZE spectrum_lines;
ANALYZE object_photometry_bands;
ANALYZE spectrum_line_fits;

-- Verification: every row agrees with its source (all counts must be 0).
SELECT 'lines' AS t, count(*) AS stale FROM spectrum_lines l
  JOIN spectra s ON s.id = l.spectrum_id JOIN targets tg ON tg.target_id = s.target_id
 WHERE l.object_id IS DISTINCT FROM tg.object_id
UNION ALL
SELECT 'bands', count(*) FROM object_photometry_bands b JOIN object_photometry p ON p.id = b.photometry_id
 WHERE b.object_id IS DISTINCT FROM p.object_id
UNION ALL
SELECT 'fits', count(*) FROM spectrum_line_fits f
  JOIN spectra s ON s.id = f.spectrum_id
  LEFT JOIN targets tg ON tg.target_id = s.target_id
  LEFT JOIN objects o ON o.id = tg.object_id
 WHERE f.stale_redshift IS DISTINCT FROM COALESCE(o.id IS NOT NULL AND public.line_fit_stale_redshift(
         o.version, o.redshift_quality, (o.redshift)::double precision, f.object_version, f.z_quality, f.z_used), false)
UNION ALL
SELECT 'dr_lines', count(*) FROM dr_spectrum_lines l
  JOIN dr_spectra s ON s.tag = l.tag AND s.id = l.spectrum_id
  JOIN dr_targets tg ON tg.tag = s.tag AND tg.target_id = s.target_id
 WHERE l.object_id IS DISTINCT FROM tg.object_id
UNION ALL
SELECT 'dr_bands', count(*) FROM dr_photometry_bands b JOIN dr_photometry p ON p.tag = b.tag AND p.id = b.photometry_id
 WHERE b.object_id IS DISTINCT FROM p.object_id;
