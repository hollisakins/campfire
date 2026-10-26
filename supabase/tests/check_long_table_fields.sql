-- Catalog Read Model phase 2c-1: the long tables carry every field of the
-- jsonb records they are unnested from, so they can become the source of
-- truth and the blobs can be dropped (2c-2).
--   1. a line_fits row's full per-line record lands field for field on its
--      lines row (members as text[], integers as integers, nulls as NULLs);
--   2. a catalog_matches band entry lands with its wav_min / wav_max, and an
--      entry without edges leaves them NULL;
--   3. catalog-wide: no lines row lacks a label and no photometry row lacks
--      edges its blob entry has (after scripts/backfill_long_table_fields.sql
--      on a database that predates 2c-1; always true on a fresh reset);
--   4. the frozen twins carry the same columns (check_dr_parity.sql owns the
--      full parity rule; this is the cheap early signal).
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_long_table_fields.sql
\set ON_ERROR_STOP on
BEGIN;

INSERT INTO programs (slug, program_name, is_public) VALUES ('zzz_ltf', 'Long table fields', true);
INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES ('ltf_obs', 'zzz_ltf', 9985, 'zzz_ltf_field');
INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum, redshift_inspected, redshift_quality)
  VALUES ('TEST-LTF', 'zzz_ltf_field', 157.0, 2.0, '{zzz_ltf}', '{ltf_obs}', true, 5.0, 3);
INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id)
  VALUES ('test-ltf', 'zzz_ltf_field', 157.0, 2.0, 'zzz_ltf', 'ltf_obs', (SELECT id FROM objects WHERE object_id = 'TEST-LTF'));
INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status)
  VALUES ('test-ltf', 'PRISM', '/tmp/ltf.fits', 10.0, 1000.0, 'published');

-- 1. one doublet total with every field set, one component with the nulls a
--    real record carries
INSERT INTO line_fits (spectrum_id, target_id, grating, program_slug, observation, z_used, z_source, z_quality,
                       object_id, object_version, n_lines, n_detected, lines, fit_version)
SELECT s.id, 'test-ltf', 'PRISM', 'zzz_ltf', 'ltf_obs', 5.0, 'inspected', 3, 'TEST-LTF', o.version, 2, 1,
       jsonb_build_object(
         'OII3727', jsonb_build_object('label', '[O II]λλ3726,3729', 'component', 'doublet', 'wave_rest', 3728.4,
                                       'wave_obs', 2.237, 'flux', 1.5e-18, 'flux_err', 1.0e-19, 'snr', 15.0,
                                       'ew_rest', 42.0, 'ew_rest_err', 3.0, 'cont', 3.5e-20, 'cont_err', 1.0e-21,
                                       'dv', 12.5, 'dv_err', 4.0, 'sigma_v', 110.0, 'sigma_v_err', 20.0,
                                       'sigma_lsf_kms', 33.0, 'complex', 2, 'chi2', 41.5, 'dof', 150, 'npix', 155,
                                       'flags', 2048, 'blend_into', NULL, 'tied_to', NULL,
                                       'members', jsonb_build_array('OII3726', 'OII3729')),
         'OII3726', jsonb_build_object('label', '[O II]λ3726', 'component', 'narrow', 'wave_rest', 3727.09,
                                       'wave_obs', 2.236, 'flux', NULL, 'flux_err', NULL, 'snr', NULL,
                                       'ew_rest', NULL, 'ew_rest_err', NULL, 'cont', NULL, 'cont_err', NULL,
                                       'dv', 12.5, 'dv_err', NULL, 'sigma_v', 110.0, 'sigma_v_err', NULL,
                                       'sigma_lsf_kms', 33.0, 'complex', 2, 'chi2', 41.5, 'dof', 150, 'npix', 155,
                                       'flags', 3, 'blend_into', 'OII3727', 'tied_to', 'OII3727')),
       'test'
  FROM spectra s, objects o WHERE s.fits_path = '/tmp/ltf.fits' AND o.object_id = 'TEST-LTF';

DO $$
DECLARE
  r record;
BEGIN
  SELECT l.* INTO r FROM lines l JOIN spectra s ON s.id = l.spectrum_id WHERE s.fits_path = '/tmp/ltf.fits' AND l.line = 'OII3727';
  IF r IS NULL THEN RAISE EXCEPTION 'doublet total not unnested'; END IF;
  IF r.label <> '[O II]λλ3726,3729' OR r.wave_obs <> 2.237 OR r.cont <> 3.5e-20 OR r.cont_err <> 1.0e-21
     OR r.dv <> 12.5 OR r.dv_err <> 4.0 OR r.sigma_v <> 110.0 OR r.sigma_v_err <> 20.0 OR r.sigma_lsf_kms <> 33.0
     OR r.complex <> 2 OR r.chi2 <> 41.5 OR r.dof <> 150 OR r.npix <> 155
     OR r.tied_to IS NOT NULL OR r.members <> ARRAY['OII3726', 'OII3729'] THEN
    RAISE EXCEPTION 'doublet total fields do not match the record: %', to_jsonb(r);
  END IF;
  SELECT l.* INTO r FROM lines l JOIN spectra s ON s.id = l.spectrum_id WHERE s.fits_path = '/tmp/ltf.fits' AND l.line = 'OII3726';
  IF r IS NULL THEN RAISE EXCEPTION 'component not unnested'; END IF;
  IF r.label <> '[O II]λ3726' OR r.flux IS NOT NULL OR r.cont IS NOT NULL OR r.dv_err IS NOT NULL OR r.sigma_v_err IS NOT NULL
     OR r.tied_to <> 'OII3727' OR r.blend_into <> 'OII3727' OR r.members IS NOT NULL OR r.complex <> 2 OR r.npix <> 155 THEN
    RAISE EXCEPTION 'component fields do not match the record: %', to_jsonb(r);
  END IF;
END $$;

-- 2. bands with and without edges
INSERT INTO catalog_matches (object_id, field, ra, dec, catalog_name, catalog_id, photometry)
VALUES ((SELECT id FROM objects WHERE object_id = 'TEST-LTF'), 'zzz_ltf_field', 157.0, 2.0, 'ltf_cat', 1,
        jsonb_build_object('flux_unit', 'uJy', 'bands', jsonb_build_object(
          'f444w', jsonb_build_object('flux', 1.0, 'flux_err', 0.1, 'wav', 4.4, 'wav_min', 3.9, 'wav_max', 4.9),
          'f150w', jsonb_build_object('flux', 0.5, 'flux_err', 0.1, 'wav', 1.5))));

DO $$
DECLARE
  r record;
BEGIN
  SELECT * INTO r FROM photometry WHERE match_id = (SELECT id FROM catalog_matches WHERE catalog_name = 'ltf_cat') AND band = 'f444w';
  IF r IS NULL OR r.wav_min <> 3.9 OR r.wav_max <> 4.9 THEN RAISE EXCEPTION 'band edges not copied: %', to_jsonb(r); END IF;
  SELECT * INTO r FROM photometry WHERE match_id = (SELECT id FROM catalog_matches WHERE catalog_name = 'ltf_cat') AND band = 'f150w';
  IF r IS NULL OR r.wav_min IS NOT NULL OR r.wav_max IS NOT NULL THEN RAISE EXCEPTION 'band without edges got edges: %', to_jsonb(r); END IF;
END $$;

-- 3. catalog-wide
DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM lines WHERE label IS NULL;
  IF n > 0 THEN RAISE EXCEPTION '% lines rows carry no label (unfilled)', n; END IF;
  SELECT count(*) INTO n FROM photometry b JOIN catalog_matches m ON m.id = b.match_id
   WHERE b.wav_min IS NULL AND (m.photometry -> 'bands' -> b.band) ? 'wav_min';
  IF n > 0 THEN RAISE EXCEPTION '% photometry rows lack edges their blob entry has (unfilled)', n; END IF;
END $$;

-- 4. frozen twins carry the same columns
DO $$
DECLARE missing text;
BEGIN
  SELECT string_agg(a.attname, ', ') INTO missing
    FROM pg_attribute a
   WHERE a.attrelid = 'public.lines'::regclass AND a.attnum > 0 AND NOT a.attisdropped
     AND NOT EXISTS (SELECT 1 FROM pg_attribute d WHERE d.attrelid = 'public.dr_lines'::regclass AND d.attname = a.attname AND d.attnum > 0 AND NOT d.attisdropped);
  IF missing IS NOT NULL THEN RAISE EXCEPTION 'dr_lines lacks: %', missing; END IF;
  SELECT string_agg(a.attname, ', ') INTO missing
    FROM pg_attribute a
   WHERE a.attrelid = 'public.photometry'::regclass AND a.attnum > 0 AND NOT a.attisdropped
     AND NOT EXISTS (SELECT 1 FROM pg_attribute d WHERE d.attrelid = 'public.dr_photometry'::regclass AND d.attname = a.attname AND d.attnum > 0 AND NOT d.attisdropped);
  IF missing IS NOT NULL THEN RAISE EXCEPTION 'dr_photometry lacks: %', missing; END IF;
END $$;

ROLLBACK;
