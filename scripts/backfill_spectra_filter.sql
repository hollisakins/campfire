-- One-time backfill for spectra.filter / dr_spectra.filter (#683).
--
-- The migration adds the columns nullable; deploy writes the filter from the
-- summary and set_spectrum_filter fills it from fits_path on every insert and
-- fits_path update, but rows that predate the column are NULL until this runs.
-- The unique key moves to (target_id, grating, filter) only after this has run
-- and the verification below reads 0 everywhere (a NULL filter would not match
-- the new conflict key, so a redeploy would insert a second row).
--
-- Idempotent: only NULL rows are written. `filter` is in
-- bump_spectra_updated_at_trigger's column list, so every backfilled spectrum
-- enters the incremental sync delta once: the mirrors pick up the column
-- without a forced resync. dr_spectra has no updated_at trigger and no
-- immutability guard; its rows are copies of the same files, so the same
-- derivation applies.
--
-- Run once on the VM after the deploy that carries the migration, as the
-- database superuser:
--   docker exec -i campfire-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < scripts/backfill_spectra_filter.sql
-- ~80 k spectra plus the frozen copies: seconds.
\timing on
BEGIN;
SET LOCAL statement_timeout = '5min';

UPDATE spectra
   SET filter = public.spectrum_filter_from_path(fits_path)
 WHERE filter IS NULL
   AND public.spectrum_filter_from_path(fits_path) IS NOT NULL;

UPDATE dr_spectra
   SET filter = public.spectrum_filter_from_path(fits_path)
 WHERE filter IS NULL
   AND public.spectrum_filter_from_path(fits_path) IS NOT NULL;

COMMIT;

ANALYZE spectra;
ANALYZE dr_spectra;

-- Verification. The first two must be 0 (a non-zero count is a fits_path the
-- parser cannot read: inspect those rows before the unique-key swap). The
-- third is the swap's precondition: no (target_id, grating, filter) collides.
SELECT 'spectra without a filter' AS check, count(*) FROM spectra WHERE filter IS NULL
UNION ALL
SELECT 'dr_spectra without a filter', count(*) FROM dr_spectra WHERE filter IS NULL
UNION ALL
SELECT 'duplicate (target_id, grating, filter)', count(*) FROM (
  SELECT 1 FROM spectra GROUP BY target_id, grating, filter HAVING count(*) > 1) d;

-- What the archive holds, for the record.
SELECT grating, filter, count(*) FROM spectra GROUP BY 1, 2 ORDER BY 1, 2;
