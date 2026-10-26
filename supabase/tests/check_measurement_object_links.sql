-- Guard: the measurement rows' object links and stored staleness are exact
-- on every path that can move them (Catalog Read Model phase 2a).
--
-- lines.object_id and photometry.object_id are copies
-- of the object the row belongs to; line_fits.stale_redshift is the
-- stored line_fit_stale_redshift(object, fit). The catalog filter helpers
-- read these columns directly, so a stale copy is a wrong filter result (a
-- detection attributed to the wrong object, or a stale fit shown as fresh).
-- This drives each maintenance path and checks the catalog-wide invariants:
--   1. insert: a fit's lines carry the object; a cross-match's bands carry it;
--      a fresh fit is not stale
--   2. inspection moves the object's redshift -> the fit is stale; a refit at
--      the new redshift and version -> fresh again
--   3. a target regroups to another object -> its lines follow, the fit goes
--      stale (other object's version / redshift)
--   4. a spectrum moves to another target -> its lines follow
--   5. a cross-match is re-matched -> its bands follow
--   6. an explicit write of stale_redshift is recomputed away
--   7. catalog-wide: no line / band row disagrees with its parent, no fit
--      disagrees with the function, and the helpers agree with the stored
--      columns (object_line_values excludes the stale fit)
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_measurement_object_links.sql
BEGIN;

DO $$
DECLARE
  v_a INTEGER; v_b INTEGER;
  v_sa INTEGER; v_sb INTEGER;
  v_ver INTEGER;
  v_n BIGINT;
  v_ids INTEGER[];
  v_obj INTEGER;
  v_stale BOOLEAN;
BEGIN
  INSERT INTO programs (slug, program_name, is_public) VALUES ('zzz_ml_p', 'Links Test', true);
  INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES ('ml_obs', 'zzz_ml_p', 9986, 'zzz_ml_field');
  INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum, redshift_inspected, redshift_quality) VALUES
    ('TEST-ML-A', 'zzz_ml_field', 154.00, 2.00, '{zzz_ml_p}', '{ml_obs}', true, 5.0, 3),
    ('TEST-ML-B', 'zzz_ml_field', 154.01, 2.00, '{zzz_ml_p}', '{ml_obs}', true, 2.0, 3);
  SELECT id INTO v_a FROM objects WHERE object_id = 'TEST-ML-A';
  SELECT id INTO v_b FROM objects WHERE object_id = 'TEST-ML-B';
  INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id) VALUES
    ('test-ml-a', 'zzz_ml_field', 154.00, 2.00, 'zzz_ml_p', 'ml_obs', v_a),
    ('test-ml-b', 'zzz_ml_field', 154.01, 2.00, 'zzz_ml_p', 'ml_obs', v_b);
  INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status) VALUES
    ('test-ml-a', 'PRISM', '/tmp/ml_a_prism.fits', 10.0, 1000.0, 'published'),
    ('test-ml-b', 'G395M', '/tmp/ml_b_g395m.fits', 10.0, 1000.0, 'published');
  SELECT id INTO v_sa FROM spectra WHERE fits_path = '/tmp/ml_a_prism.fits';
  SELECT id INTO v_sb FROM spectra WHERE fits_path = '/tmp/ml_b_g395m.fits';
  SELECT version INTO v_ver FROM objects WHERE id = v_a;

  PERFORM public.line_fits_upsert(jsonb_build_array(
    jsonb_build_object('spectrum_id', v_sa, 'target_id', 'test-ml-a', 'grating', 'PRISM', 'program_slug', 'zzz_ml_p', 'observation', 'ml_obs',
                       'z_used', 5.0, 'z_source', 'inspected', 'z_quality', 3, 'object_id', 'TEST-ML-A', 'object_version', v_ver, 'n_lines', 1, 'n_detected', 1,
                       'lines', jsonb_build_object('CIII1908', jsonb_build_object('component', 'doublet', 'wave_rest', 1907.7, 'flux', 1e-18, 'flux_err', 1e-19, 'snr', 10.0)), 'fit_version', 'test'),
    jsonb_build_object('spectrum_id', v_sb, 'target_id', 'test-ml-b', 'grating', 'G395M', 'program_slug', 'zzz_ml_p', 'observation', 'ml_obs',
                       'z_used', 2.0, 'z_source', 'inspected', 'z_quality', 3, 'object_id', 'TEST-ML-B', 'object_version', (SELECT version FROM objects WHERE id = v_b), 'n_lines', 1, 'n_detected', 1,
                       'lines', jsonb_build_object('CIII1908', jsonb_build_object('component', 'doublet', 'wave_rest', 1907.7, 'flux', 1e-18, 'flux_err', 1e-19, 'snr', 20.0)), 'fit_version', 'test')));
  PERFORM public.catalog_matches_upsert(jsonb_build_array(jsonb_build_object(
    'object_id', v_a, 'field', 'zzz_ml_field', 'ra', 154.00, 'dec', 2.00, 'catalog_name', 'zzz_ml_cat',
    'photometry', jsonb_build_object('flux_unit', 'uJy', 'bands', jsonb_build_object('f444w', jsonb_build_object('flux', 0.36308, 'flux_err', 0.018, 'wav', 4.4))))));

  -- 1) insert-time links --------------------------------------------------------
  SELECT object_id INTO v_obj FROM lines WHERE spectrum_id = v_sa AND line = 'CIII1908';
  IF v_obj IS DISTINCT FROM v_a THEN RAISE EXCEPTION 'lines.object_id at insert: want A (%), got %', v_a, v_obj; END IF;
  SELECT b.object_id INTO v_obj FROM photometry b JOIN catalog_matches p ON p.id = b.match_id WHERE p.catalog_name = 'zzz_ml_cat';
  IF v_obj IS DISTINCT FROM v_a THEN RAISE EXCEPTION 'bands.object_id at insert: want A, got %', v_obj; END IF;
  IF (SELECT bool_or(stale_redshift) FROM line_fits WHERE spectrum_id IN (v_sa, v_sb)) THEN
    RAISE EXCEPTION 'a fit at the object''s current redshift and version must not be stale';
  END IF;

  -- 2) inspection moves the redshift; a refit clears it --------------------------
  UPDATE objects SET redshift_inspected = 5.1 WHERE id = v_a;
  SELECT stale_redshift INTO v_stale FROM line_fits WHERE spectrum_id = v_sa;
  IF NOT v_stale THEN RAISE EXCEPTION 'inspecting the object must mark its fit stale'; END IF;
  SELECT stale_redshift INTO v_stale FROM line_fits WHERE spectrum_id = v_sb;
  IF v_stale THEN RAISE EXCEPTION 'inspecting A must not touch B''s fit'; END IF;
  SELECT version INTO v_ver FROM objects WHERE id = v_a;
  UPDATE line_fits SET z_used = 5.1, object_version = v_ver WHERE spectrum_id = v_sa;
  SELECT stale_redshift INTO v_stale FROM line_fits WHERE spectrum_id = v_sa;
  IF v_stale THEN RAISE EXCEPTION 'a refit at the new redshift and version must be fresh'; END IF;

  -- 3) a target regroups ---------------------------------------------------------
  UPDATE targets SET object_id = v_b WHERE target_id = 'test-ml-a';
  SELECT object_id INTO v_obj FROM lines WHERE spectrum_id = v_sa AND line = 'CIII1908';
  IF v_obj IS DISTINCT FROM v_b THEN RAISE EXCEPTION 'regrouping the target must re-point its lines to B, got %', v_obj; END IF;
  SELECT stale_redshift INTO v_stale FROM line_fits WHERE spectrum_id = v_sa;
  IF NOT v_stale THEN RAISE EXCEPTION 'a fit whose target regrouped to another object must be stale'; END IF;
  UPDATE targets SET object_id = v_a WHERE target_id = 'test-ml-a';
  SELECT object_id INTO v_obj FROM lines WHERE spectrum_id = v_sa AND line = 'CIII1908';
  IF v_obj IS DISTINCT FROM v_a THEN RAISE EXCEPTION 'regrouping back must re-point the lines to A'; END IF;
  SELECT stale_redshift INTO v_stale FROM line_fits WHERE spectrum_id = v_sa;
  IF v_stale THEN RAISE EXCEPTION 'back on its own object at its own version the fit is fresh again'; END IF;

  -- 4) a spectrum moves to another target ----------------------------------------
  UPDATE spectra SET target_id = 'test-ml-b' WHERE id = v_sa;   -- PRISM is free on test-ml-b
  SELECT object_id INTO v_obj FROM lines WHERE spectrum_id = v_sa AND line = 'CIII1908';
  IF v_obj IS DISTINCT FROM v_b THEN RAISE EXCEPTION 'moving the spectrum must re-point its lines to B, got %', v_obj; END IF;
  UPDATE spectra SET target_id = 'test-ml-a' WHERE id = v_sa;
  SELECT object_id INTO v_obj FROM lines WHERE spectrum_id = v_sa AND line = 'CIII1908';
  IF v_obj IS DISTINCT FROM v_a THEN RAISE EXCEPTION 'moving the spectrum back must re-point the lines to A'; END IF;

  -- 5) a cross-match is re-matched -----------------------------------------------
  UPDATE catalog_matches SET object_id = v_b WHERE catalog_name = 'zzz_ml_cat';
  SELECT b.object_id INTO v_obj FROM photometry b JOIN catalog_matches p ON p.id = b.match_id WHERE p.catalog_name = 'zzz_ml_cat';
  IF v_obj IS DISTINCT FROM v_b THEN RAISE EXCEPTION 're-matching the cross-match must re-point its bands to B'; END IF;
  UPDATE catalog_matches SET object_id = NULL WHERE catalog_name = 'zzz_ml_cat';
  SELECT b.object_id INTO v_obj FROM photometry b JOIN catalog_matches p ON p.id = b.match_id WHERE p.catalog_name = 'zzz_ml_cat';
  IF v_obj IS NOT NULL THEN RAISE EXCEPTION 'an unmatched cross-match must leave its bands unlinked'; END IF;
  UPDATE catalog_matches SET object_id = v_a WHERE catalog_name = 'zzz_ml_cat';

  -- 6) an explicit write of the stored flag is recomputed --------------------------
  UPDATE line_fits SET stale_redshift = true WHERE spectrum_id = v_sa;
  SELECT stale_redshift INTO v_stale FROM line_fits WHERE spectrum_id = v_sa;
  IF v_stale THEN RAISE EXCEPTION 'an explicit write of stale_redshift must be recomputed from the object'; END IF;

  -- 7) catalog-wide invariants and helper agreement ---------------------------------
  SELECT count(*) INTO v_n FROM lines l
    JOIN spectra s ON s.id = l.spectrum_id JOIN targets tg ON tg.target_id = s.target_id
   WHERE l.object_id IS DISTINCT FROM tg.object_id;
  IF v_n <> 0 THEN RAISE EXCEPTION '% lines rows disagree with their target''s object', v_n; END IF;
  SELECT count(*) INTO v_n FROM photometry b JOIN catalog_matches p ON p.id = b.match_id
   WHERE b.object_id IS DISTINCT FROM p.object_id;
  IF v_n <> 0 THEN RAISE EXCEPTION '% photometry rows disagree with their cross-match', v_n; END IF;
  SELECT count(*) INTO v_n FROM line_fits f
    JOIN spectra s ON s.id = f.spectrum_id
    LEFT JOIN targets tg ON tg.target_id = s.target_id
    LEFT JOIN objects o ON o.id = tg.object_id
   WHERE f.stale_redshift IS DISTINCT FROM COALESCE(o.id IS NOT NULL AND public.line_fit_stale_redshift(
           o.version, o.redshift_quality, (o.redshift)::double precision, f.object_version, f.z_quality, f.z_used), false);
  IF v_n <> 0 THEN RAISE EXCEPTION '% line_fits rows disagree with line_fit_stale_redshift', v_n; END IF;
  -- the status view (live rule) and the stored column agree
  SELECT count(*) INTO v_n FROM line_fits_status st JOIN line_fits f ON f.spectrum_id = st.spectrum_id
   WHERE st.stale_redshift IS DISTINCT FROM f.stale_redshift;
  IF v_n <> 0 THEN RAISE EXCEPTION '% rows: line_fits_status disagrees with the stored stale_redshift', v_n; END IF;

  -- helpers: make A's fit stale again and check it drops out of the non-stale set
  PERFORM set_config('request.jwt.claims', json_build_object('role', 'service_role')::text, true);
  UPDATE objects SET redshift_inspected = 5.2 WHERE id = v_a;
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', NULL, NULL, false, ARRAY['zzz_ml_p']) v
                 WHERE v.object_id IN (v_a, v_b) ORDER BY 1);
  IF v_ids <> ARRAY[v_b] THEN RAISE EXCEPTION 'object_line_values must exclude the stale fit: want {B}, got %', v_ids; END IF;
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', NULL, NULL, true, ARRAY['zzz_ml_p']) v
                 WHERE v.object_id IN (v_a, v_b) ORDER BY 1);
  IF v_ids <> ARRAY(SELECT x FROM unnest(ARRAY[v_a, v_b]) x ORDER BY 1) THEN RAISE EXCEPTION 'object_line_values with stale included must return both, got %', v_ids; END IF;
  IF ARRAY(SELECT public.spectra_matching_line_filter('CIII1908', NULL, NULL, false) INTERSECT SELECT unnest(ARRAY[v_sa, v_sb])) <> ARRAY[v_sb] THEN
    RAISE EXCEPTION 'spectra_matching_line_filter must exclude the stale fit';
  END IF;
  v_ids := ARRAY(SELECT v.object_id FROM public.object_band_values('f444w', NULL, NULL, NULL, NULL, ARRAY['zzz_ml_p']) v WHERE v.object_id IN (v_a, v_b));
  IF v_ids <> ARRAY[v_a] THEN RAISE EXCEPTION 'object_band_values must read the band row''s object link: want {A}, got %', v_ids; END IF;

  RAISE NOTICE 'check_measurement_object_links: OK';
END $$;

ROLLBACK;
