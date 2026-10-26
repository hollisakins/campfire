-- Catalog Read Model phase 2c: lines and photometry are the source of truth,
-- written as rows by the two writer RPCs; the jsonb blobs are gone and the
-- consumers that still speak them get the map rebuilt from the rows.
--   1. line_fits_upsert lands every field of a per-line record on its lines
--      row (members as text[], integers as integers, nulls as NULLs), sets the
--      trigger-maintained object link, and a re-fit replaces the whole set;
--   2. lines_jsonb_of rebuilds exactly the map that was written (members only
--      where present, doubles at full precision whatever the server's
--      extra_float_digits), and '{}' for a fit with no lines;
--   3. catalog_matches_upsert lands a band with and without edges, sets the
--      object link, replaces the set on re-match, and bands_jsonb_of rebuilds
--      the {flux_unit, bands} map (wavelength keys only where a pivot exists);
--   4. an object_id supplied on a lines / photometry row is recomputed away;
--   5. the frozen twins carry the same columns (check_dr_parity.sql owns the
--      full parity rule; this is the cheap early signal).
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_long_table_fields.sql
\set ON_ERROR_STOP on
BEGIN;
-- the fixture maps below carry doubles; serialize them shortest-precise so the
-- written map and the rebuilt map (which pins the same setting) can be compared
SET LOCAL extra_float_digits = 1;

INSERT INTO programs (slug, program_name, is_public) VALUES ('zzz_ltf', 'Long table fields', true);
INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES ('ltf_obs', 'zzz_ltf', 9985, 'zzz_ltf_field');
INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum, redshift_inspected, redshift_quality)
  VALUES ('TEST-LTF', 'zzz_ltf_field', 157.0, 2.0, '{zzz_ltf}', '{ltf_obs}', true, 5.0, 3),
         ('TEST-LTF2', 'zzz_ltf_field', 157.01, 2.0, '{zzz_ltf}', '{ltf_obs}', true, 2.0, 3);
INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id)
  VALUES ('test-ltf', 'zzz_ltf_field', 157.0, 2.0, 'zzz_ltf', 'ltf_obs', (SELECT id FROM objects WHERE object_id = 'TEST-LTF'));
INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status)
  VALUES ('test-ltf', 'PRISM', '/tmp/ltf.fits', 10.0, 1000.0, 'published');

-- 1 + 2. a doublet total with every field set, a component with a real
--        record's nulls; then a re-fit with one line
DO $$
DECLARE
  v_sid integer := (SELECT id FROM spectra WHERE fits_path = '/tmp/ltf.fits');
  v_obj integer := (SELECT id FROM objects WHERE object_id = 'TEST-LTF');
  v_map jsonb := jsonb_build_object(
    'OII3727', jsonb_build_object('label', '[O II]λλ3726,3729', 'component', 'doublet', 'wave_rest', 3728.4,
                                  'wave_obs', 2.237, 'flux', 1.2301908226564674e-37::double precision, 'flux_err', 1.0e-19, 'snr', 15.0,
                                  'ew_rest', 42.0, 'ew_rest_err', 3.0, 'cont', 3.5e-20, 'cont_err', 1.0e-21,
                                  'dv', 12.5, 'dv_err', 4.0, 'sigma_v', 10.000000000000002::double precision, 'sigma_v_err', 20.0,
                                  'sigma_lsf_kms', 33.0, 'complex', 2, 'chi2', 10.750175652700515::double precision, 'dof', 150, 'npix', 155,
                                  'flags', 2048, 'blend_into', NULL, 'tied_to', NULL,
                                  'members', jsonb_build_array('OII3726', 'OII3729')),
    'OII3726', jsonb_build_object('label', '[O II]λ3726', 'component', 'narrow', 'wave_rest', 3727.09,
                                  'wave_obs', 2.236, 'flux', NULL, 'flux_err', NULL, 'snr', NULL,
                                  'ew_rest', NULL, 'ew_rest_err', NULL, 'cont', NULL, 'cont_err', NULL,
                                  'dv', 12.5, 'dv_err', NULL, 'sigma_v', 10.000000000000002::double precision, 'sigma_v_err', NULL,
                                  'sigma_lsf_kms', 33.0, 'complex', 2, 'chi2', 10.750175652700515::double precision, 'dof', 150, 'npix', 155,
                                  'flags', 3, 'blend_into', 'OII3727', 'tied_to', 'OII3727'));
  v_fit jsonb;
  r record;
  n integer;
BEGIN
  v_fit := jsonb_build_object('spectrum_id', v_sid, 'target_id', 'test-ltf', 'grating', 'PRISM',
                              'program_slug', 'zzz_ltf', 'observation', 'ltf_obs', 'z_used', 5.0,
                              'z_source', 'inspected', 'z_quality', 3, 'object_id', 'TEST-LTF',
                              'object_version', (SELECT version FROM objects WHERE id = v_obj),
                              'n_lines', 2, 'n_detected', 1, 'fit_version', 'test', 'lines', v_map);
  n := public.line_fits_upsert(jsonb_build_array(v_fit));
  IF n <> 1 THEN RAISE EXCEPTION 'line_fits_upsert returned % for one fit', n; END IF;
  IF (SELECT count(*) FROM line_fits WHERE spectrum_id = v_sid) <> 1 THEN RAISE EXCEPTION 'fit row not written'; END IF;

  SELECT l.* INTO r FROM lines l WHERE l.spectrum_id = v_sid AND l.line = 'OII3727';
  IF r IS NULL THEN RAISE EXCEPTION 'doublet total not written'; END IF;
  IF r.label <> '[O II]λλ3726,3729' OR r.wave_obs <> 2.237 OR r.cont <> 3.5e-20 OR r.cont_err <> 1.0e-21
     OR r.dv <> 12.5 OR r.dv_err <> 4.0 OR r.sigma_v <> 10.000000000000002 OR r.sigma_v_err <> 20.0 OR r.sigma_lsf_kms <> 33.0
     OR r.complex <> 2 OR r.chi2 <> 10.750175652700515 OR r.dof <> 150 OR r.npix <> 155 OR r.flags <> 2048
     OR r.tied_to IS NOT NULL OR r.members <> ARRAY['OII3726', 'OII3729'] OR r.object_id IS DISTINCT FROM v_obj THEN
    RAISE EXCEPTION 'doublet total fields do not match the record: %', to_jsonb(r);
  END IF;
  SELECT l.* INTO r FROM lines l WHERE l.spectrum_id = v_sid AND l.line = 'OII3726';
  IF r IS NULL THEN RAISE EXCEPTION 'component not written'; END IF;
  IF r.label <> '[O II]λ3726' OR r.flux IS NOT NULL OR r.cont IS NOT NULL OR r.dv_err IS NOT NULL OR r.sigma_v_err IS NOT NULL
     OR r.tied_to <> 'OII3727' OR r.blend_into <> 'OII3727' OR r.members IS NOT NULL OR r.complex <> 2 OR r.npix <> 155
     OR r.object_id IS DISTINCT FROM v_obj THEN
    RAISE EXCEPTION 'component fields do not match the record: %', to_jsonb(r);
  END IF;

  -- the rebuilt map is the written map (jsonb equality ignores key order)
  IF public.lines_jsonb_of(v_sid) <> v_map THEN
    RAISE EXCEPTION 'lines_jsonb_of differs from the map written: %', public.lines_jsonb_of(v_sid);
  END IF;
  IF public.lines_jsonb_of(v_sid, 'no-such-release') <> '{}'::jsonb THEN
    RAISE EXCEPTION 'lines_jsonb_of by an unknown tag must be empty';
  END IF;

  -- a re-fit replaces the set: the component is gone, the total updated
  v_fit := v_fit || jsonb_build_object('n_lines', 1, 'lines', jsonb_build_object('OII3727', (v_map -> 'OII3727') || '{"snr": 16.0}'::jsonb));
  PERFORM public.line_fits_upsert(jsonb_build_array(v_fit));
  IF (SELECT count(*) FROM lines WHERE spectrum_id = v_sid) <> 1
     OR (SELECT snr FROM lines WHERE spectrum_id = v_sid AND line = 'OII3727') <> 16.0
     OR (SELECT n_lines FROM line_fits WHERE spectrum_id = v_sid) <> 1 THEN
    RAISE EXCEPTION 're-fit did not replace the lines set';
  END IF;
  -- and a fit with no lines rebuilds to an empty map
  PERFORM public.line_fits_upsert(jsonb_build_array(v_fit - 'lines'));
  IF (SELECT count(*) FROM lines WHERE spectrum_id = v_sid) <> 0 OR public.lines_jsonb_of(v_sid) <> '{}'::jsonb THEN
    RAISE EXCEPTION 'a fit without lines must have no rows and an empty map';
  END IF;
END $$;

-- 3. bands with and without edges, re-match, rebuilt map
DO $$
DECLARE
  v_obj integer := (SELECT id FROM objects WHERE object_id = 'TEST-LTF');
  v_map jsonb := jsonb_build_object('flux_unit', 'uJy', 'bands', jsonb_build_object(
    'f444w', jsonb_build_object('flux', 1.0, 'flux_err', 0.1, 'wav', 4.4, 'wav_min', 3.9, 'wav_max', 4.9),
    'f150w', jsonb_build_object('flux', 0.5, 'flux_err', 0.1)));
  v_rec jsonb;
  v_id integer;
  r record;
BEGIN
  v_rec := jsonb_build_object('object_id', v_obj, 'field', 'zzz_ltf_field', 'ra', 157.0, 'dec', 2.0,
                              'catalog_name', 'ltf_cat', 'catalog_id', '1', 'match_distance_arcsec', 0.1,
                              'photometry', v_map, 'photo_z', 4.9, 'has_pz', false);
  IF public.catalog_matches_upsert(jsonb_build_array(v_rec)) <> 1 THEN RAISE EXCEPTION 'catalog_matches_upsert count'; END IF;
  SELECT id INTO v_id FROM catalog_matches WHERE catalog_name = 'ltf_cat' AND catalog_id = '1';
  SELECT * INTO r FROM photometry WHERE match_id = v_id AND band = 'f444w';
  IF r IS NULL OR r.wav_min <> 3.9 OR r.wav_max <> 4.9 OR r.wav <> 4.4 OR r.object_id IS DISTINCT FROM v_obj THEN
    RAISE EXCEPTION 'band with edges not written as sent: %', to_jsonb(r);
  END IF;
  SELECT * INTO r FROM photometry WHERE match_id = v_id AND band = 'f150w';
  IF r IS NULL OR r.wav IS NOT NULL OR r.wav_min IS NOT NULL OR r.object_id IS DISTINCT FROM v_obj THEN
    RAISE EXCEPTION 'band without a pivot got wavelengths: %', to_jsonb(r);
  END IF;
  IF public.bands_jsonb_of(v_id) <> v_map THEN
    RAISE EXCEPTION 'bands_jsonb_of differs from the map written: %', public.bands_jsonb_of(v_id);
  END IF;
  -- re-match on the same (field, catalog_name, catalog_id): one band now, same id
  PERFORM public.catalog_matches_upsert(jsonb_build_array(
    v_rec || jsonb_build_object('photometry', jsonb_build_object('flux_unit', 'uJy', 'bands', jsonb_build_object(
      'f444w', jsonb_build_object('flux', 2.0, 'flux_err', 0.1, 'wav', 4.4, 'wav_min', 3.9, 'wav_max', 4.9))))));
  IF (SELECT count(*) FROM catalog_matches WHERE catalog_name = 'ltf_cat') <> 1
     OR (SELECT count(*) FROM photometry WHERE match_id = v_id) <> 1
     OR (SELECT flux FROM photometry WHERE match_id = v_id) <> 2.0 THEN
    RAISE EXCEPTION 're-match did not replace the band set in place';
  END IF;
END $$;

-- 4. a supplied object link is recomputed away
DO $$
DECLARE
  v_sid integer := (SELECT id FROM spectra WHERE fits_path = '/tmp/ltf.fits');
  v_obj integer := (SELECT id FROM objects WHERE object_id = 'TEST-LTF');
  v_obj2 integer := (SELECT id FROM objects WHERE object_id = 'TEST-LTF2');
  v_id integer := (SELECT id FROM catalog_matches WHERE catalog_name = 'ltf_cat');
BEGIN
  INSERT INTO lines (spectrum_id, line, object_id) VALUES (v_sid, 'Halpha', v_obj2);
  IF (SELECT object_id FROM lines WHERE spectrum_id = v_sid AND line = 'Halpha') IS DISTINCT FROM v_obj THEN
    RAISE EXCEPTION 'lines.object_id took the client value';
  END IF;
  UPDATE lines SET object_id = v_obj2 WHERE spectrum_id = v_sid AND line = 'Halpha';
  IF (SELECT object_id FROM lines WHERE spectrum_id = v_sid AND line = 'Halpha') IS DISTINCT FROM v_obj THEN
    RAISE EXCEPTION 'lines.object_id kept an explicit update';
  END IF;
  INSERT INTO photometry (match_id, band, flux, flux_err, object_id) VALUES (v_id, 'f090w', 1.0, 0.1, v_obj2);
  IF (SELECT object_id FROM photometry WHERE match_id = v_id AND band = 'f090w') IS DISTINCT FROM v_obj THEN
    RAISE EXCEPTION 'photometry.object_id took the client value';
  END IF;
END $$;

-- 5. frozen twins carry the same columns
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
  IF EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid IN ('public.line_fits'::regclass, 'public.dr_line_fits'::regclass) AND attname = 'lines' AND NOT attisdropped)
     OR EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid IN ('public.catalog_matches'::regclass, 'public.dr_catalog_matches'::regclass) AND attname = 'photometry' AND NOT attisdropped) THEN
    RAISE EXCEPTION 'a jsonb blob column is back';
  END IF;
END $$;

ROLLBACK;
