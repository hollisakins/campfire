-- Guard: line_fit_stale_spectrum decides "the spectrum changed since the fit"
-- on the SCIENCE hash when both sides carry one, and on the whole-file hash
-- otherwise (P0c, docs/design-data-releases-impl-plan.md §2) -- and the
-- spectrum_line_fits_status view and get_line_fits_for_sync agree, because
-- both call the same function.
--
-- Why it matters: a redeploy that only rewrote volatile header cards (CMPFRTIM,
-- the DR stamp) moves spectra.file_hash but not spectra.science_hash. Before
-- P0c every such redeploy marked every fit of the observation stale_spectrum
-- and queued a pointless refit; after it, only a real change in science content
-- does. Products fitted before linefit wrote SCIHASH (spectrum_science_hash
-- NULL) and spectra rows without a science hash keep the old whole-file rule,
-- so nothing silently reads as fresh for lack of a column.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_line_fit_stale_spectrum.sql
BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The function alone, every branch.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  f1 CONSTANT text := 'sha256:' || repeat('1', 64);
  f2 CONSTANT text := 'sha256:' || repeat('2', 64);
  s1 CONSTANT text := 'sha256:' || repeat('a', 64);
  s2 CONSTANT text := 'sha256:' || repeat('b', 64);
BEGIN
  -- both science hashes present: the science hash decides
  IF public.line_fit_stale_spectrum(f2, s1, f1, s1) THEN
    RAISE EXCEPTION 'same science hash with different file hashes must NOT be stale';
  END IF;
  IF NOT public.line_fit_stale_spectrum(f1, s2, f1, s1) THEN
    RAISE EXCEPTION 'different science hashes must be stale even with equal file hashes';
  END IF;
  -- either science hash missing: the whole-file hash decides
  IF public.line_fit_stale_spectrum(f1, NULL, f1, s1) THEN
    RAISE EXCEPTION 'spectrum without a science hash, same file hash: must NOT be stale';
  END IF;
  IF NOT public.line_fit_stale_spectrum(f2, NULL, f1, s1) THEN
    RAISE EXCEPTION 'spectrum without a science hash, different file hash: must be stale';
  END IF;
  IF public.line_fit_stale_spectrum(f1, s1, f1, NULL) THEN
    RAISE EXCEPTION 'fit without a science hash, same file hash: must NOT be stale';
  END IF;
  IF NOT public.line_fit_stale_spectrum(f2, s1, f1, NULL) THEN
    RAISE EXCEPTION 'fit without a science hash, different file hash: must be stale';
  END IF;
  -- a bare digest from an older product compares with a prefixed one
  IF public.line_fit_stale_spectrum(f1, s1, f1, repeat('a', 64)) THEN
    RAISE EXCEPTION 'bare science digest must compare equal to its sha256: form';
  END IF;
  IF public.line_fit_stale_spectrum(f1, NULL, repeat('1', 64), NULL) THEN
    RAISE EXCEPTION 'bare file digest must compare equal to its sha256: form';
  END IF;
  -- no hash anywhere on the fit side: stale (a fit with no provenance)
  IF NOT public.line_fit_stale_spectrum(f1, s1, NULL, NULL) THEN
    RAISE EXCEPTION 'a fit with neither hash must be stale';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Fixtures: four spectra, one fit each, covering the four combinations the
--    ledger must tell apart.
-- ---------------------------------------------------------------------------
INSERT INTO programs (slug, program_name, is_public) VALUES
  ('zzz_lfs_prog', 'Line-fit staleness test', true);
INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
  ('zzz_lfs_obs', 'zzz_lfs_prog', 9201, 'zzz_lfs_field');
INSERT INTO fields (name) VALUES ('zzz_lfs_field');

INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, has_published_spectrum)
VALUES
  ('zzz-lfs-cards',   'zzz_lfs_field', 150.00, 2.0, 'zzz_lfs_prog', 'zzz_lfs_obs', true),
  ('zzz-lfs-science', 'zzz_lfs_field', 150.01, 2.0, 'zzz_lfs_prog', 'zzz_lfs_obs', true),
  ('zzz-lfs-oldfit',  'zzz_lfs_field', 150.02, 2.0, 'zzz_lfs_prog', 'zzz_lfs_obs', true),
  ('zzz-lfs-fresh',   'zzz_lfs_field', 150.03, 2.0, 'zzz_lfs_prog', 'zzz_lfs_obs', true);

-- cards:   file hash moved, science hash did not (a CMPFRTIM-only redeploy)
-- science: science hash moved (a real re-reduction)
-- oldfit:  the fit predates SCIHASH; file hash moved -> stale by the old rule
-- fresh:   nothing moved
INSERT INTO spectra (target_id, grating, fits_path, deploy_status, file_hash, science_hash) VALUES
  ('zzz-lfs-cards',   'PRISM', '/tmp/zzz_lfs_cards_spec.fits',   'published',
     'sha256:' || repeat('2', 64), 'sha256:' || repeat('a', 64)),
  ('zzz-lfs-science', 'PRISM', '/tmp/zzz_lfs_science_spec.fits', 'published',
     'sha256:' || repeat('2', 64), 'sha256:' || repeat('b', 64)),
  ('zzz-lfs-oldfit',  'PRISM', '/tmp/zzz_lfs_oldfit_spec.fits',  'published',
     'sha256:' || repeat('2', 64), 'sha256:' || repeat('a', 64)),
  ('zzz-lfs-fresh',   'PRISM', '/tmp/zzz_lfs_fresh_spec.fits',   'published',
     'sha256:' || repeat('1', 64), 'sha256:' || repeat('a', 64));

INSERT INTO spectrum_line_fits
  (spectrum_id, target_id, grating, program_slug, observation, z_used, z_source, z_quality,
   fit_version, spectrum_hash, spectrum_science_hash)
SELECT s.id, s.target_id, s.grating, 'zzz_lfs_prog', 'zzz_lfs_obs', 5.0, 'inspected', 4,
       '2', 'sha256:' || repeat('1', 64),
       CASE WHEN s.target_id = 'zzz-lfs-oldfit' THEN NULL ELSE 'sha256:' || repeat('a', 64) END
FROM spectra s
WHERE s.target_id LIKE 'zzz-lfs-%';

-- ---------------------------------------------------------------------------
-- 3. The ledger view.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  v_stale text[];
BEGIN
  SELECT COALESCE(array_agg(target_id ORDER BY target_id), '{}')
  INTO v_stale
  FROM spectrum_line_fits_status
  WHERE target_id LIKE 'zzz-lfs-%' AND stale_spectrum;
  IF v_stale <> ARRAY['zzz-lfs-oldfit', 'zzz-lfs-science'] THEN
    RAISE EXCEPTION 'spectrum_line_fits_status: expected stale_spectrum on {oldfit, science}, got %', v_stale;
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. The sync RPC agrees with the view, fit by fit.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  v_disagree int;
BEGIN
  WITH rpc AS (
    SELECT (r->>'spectrum_id')::int AS spectrum_id,
           (r->>'stale_spectrum')::boolean AS stale_spectrum,
           (r->>'stale_redshift')::boolean AS stale_redshift
    FROM public.get_line_fits_for_sync(ARRAY['zzz_lfs_prog'], NULL, 1000, false, false, NULL) g,
         jsonb_array_elements(g.line_fit_records) r
  )
  SELECT count(*) INTO v_disagree
  FROM spectrum_line_fits_status v
  JOIN rpc ON rpc.spectrum_id = v.spectrum_id
  WHERE v.target_id LIKE 'zzz-lfs-%'
    AND (v.stale_spectrum IS DISTINCT FROM rpc.stale_spectrum
         OR v.stale_redshift IS DISTINCT FROM rpc.stale_redshift);
  IF v_disagree <> 0 THEN
    RAISE EXCEPTION 'get_line_fits_for_sync disagrees with spectrum_line_fits_status on % fit(s)', v_disagree;
  END IF;
  -- and the RPC saw all four
  SELECT 4 - count(*) INTO v_disagree
  FROM public.get_line_fits_for_sync(ARRAY['zzz_lfs_prog'], NULL, 1000, false, false, NULL) g,
       jsonb_array_elements(g.line_fit_records) r;
  IF v_disagree <> 0 THEN
    RAISE EXCEPTION 'get_line_fits_for_sync returned the wrong number of fixture fits';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 5. The column contract: a malformed science hash is refused.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  BEGIN
    UPDATE spectrum_line_fits SET spectrum_science_hash = 'abc'
    WHERE target_id = 'zzz-lfs-fresh';
    RAISE EXCEPTION 'spectrum_line_fits_spectrum_science_hash_check did not fire';
  EXCEPTION WHEN check_violation THEN
    NULL;
  END;
END $$;

ROLLBACK;
