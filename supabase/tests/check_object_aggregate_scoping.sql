-- Guard: object aggregate columns must be scoped to the viewer's accessible
-- programs.
--
-- Object rows are visible by program-array overlap (RLS: programs && accessible),
-- but the denormalized aggregate columns (programs, gratings, observations,
-- n_targets, n_spectra, max_snr, max_exposure_time) are computed across ALL
-- member programs at deploy time. A mixed public+proprietary object is therefore
-- visible to a viewer who can access only the public member, and the stored
-- aggregates would leak the proprietary program's name / counts / snr / exposure
-- / gratings. This asserts the canonical helper object_scoped_aggregates() and
-- the primary catalog RPC get_filtered_objects_paginated() scope those columns.
-- The other read paths (CSV export, Python sync, map markers, object detail)
-- reuse the same helper or mirror its logic.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_object_aggregate_scoping.sql
BEGIN;

DO $$
DECLARE
  v_obj_id   INTEGER;
  v_programs TEXT[];
  v_gratings TEXT[];
  v_obs      TEXT[];
  v_nt       INTEGER;
  v_ns       INTEGER;
  v_snr      DOUBLE PRECISION;
  v_exp      DOUBLE PRECISION;
  v_json     JSONB;
  v_row      JSONB;
BEGIN
  -- Fixtures: one public + one proprietary program, one object spanning both,
  -- one target (with one spectrum) per program. Global stored aggregates on the
  -- object deliberately span both programs.
  INSERT INTO programs (slug, program_name, is_public) VALUES
    ('zzz_test_pub',  'Scoping Test Public',  true),
    ('zzz_test_prop', 'Scoping Test Private', false);

  INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
    ('obs_pub',  'zzz_test_pub',  9991, 'zzz_test_field'),
    ('obs_prop', 'zzz_test_prop', 9992, 'zzz_test_field');

  INSERT INTO objects (object_id, field, ra, dec,
                       n_targets, n_spectra, programs, gratings, observations,
                       max_snr, max_exposure_time)
  VALUES ('TEST-OBJ-SCOPING', 'zzz_test_field', 150.0, 2.0,
          2, 2, ARRAY['zzz_test_prop','zzz_test_pub'],
          ARRAY['G395M','PRISM'], ARRAY['obs_prop','obs_pub'],
          30.0, 2000.0)
  RETURNING id INTO v_obj_id;

  INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id) VALUES
    ('test-scoping-pub',  'zzz_test_field', 150.0, 2.0, 'zzz_test_pub',  'obs_pub',  v_obj_id),
    ('test-scoping-prop', 'zzz_test_field', 150.0, 2.0, 'zzz_test_prop', 'obs_prop', v_obj_id);

  INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time) VALUES
    ('test-scoping-pub',  'PRISM', '/tmp/pub_prism_spec.fits',  10.0, 1000.0),
    ('test-scoping-prop', 'G395M', '/tmp/prop_g395m_spec.fits', 30.0, 2000.0);

  -- 1) Helper, public-only viewer: the proprietary member must be fully excluded.
  SELECT programs, gratings, observations, n_targets, n_spectra, max_snr, max_exposure_time
    INTO v_programs, v_gratings, v_obs, v_nt, v_ns, v_snr, v_exp
    FROM public.object_scoped_aggregates(v_obj_id, ARRAY['zzz_test_pub']);

  IF v_programs <> ARRAY['zzz_test_pub'] THEN
    RAISE EXCEPTION 'scoped programs leaked: got %', v_programs;
  END IF;
  IF v_gratings <> ARRAY['PRISM'] THEN
    RAISE EXCEPTION 'scoped gratings leaked: got %', v_gratings;
  END IF;
  IF v_obs <> ARRAY['obs_pub'] THEN
    RAISE EXCEPTION 'scoped observations leaked: got %', v_obs;
  END IF;
  IF v_nt <> 1 OR v_ns <> 1 THEN
    RAISE EXCEPTION 'scoped counts leaked: n_targets=% n_spectra=%', v_nt, v_ns;
  END IF;
  IF v_snr <> 10.0 OR v_exp <> 1000.0 THEN
    RAISE EXCEPTION 'scoped max_snr/exposure leaked: snr=% exp=%', v_snr, v_exp;
  END IF;

  -- 2) Helper, full access: must reproduce the global stored aggregates exactly.
  SELECT programs, n_targets, n_spectra, max_snr, max_exposure_time
    INTO v_programs, v_nt, v_ns, v_snr, v_exp
    FROM public.object_scoped_aggregates(v_obj_id, ARRAY['zzz_test_pub','zzz_test_prop']);

  IF v_programs <> ARRAY['zzz_test_prop','zzz_test_pub'] THEN
    RAISE EXCEPTION 'full-access programs mismatch: got %', v_programs;
  END IF;
  IF v_nt <> 2 OR v_ns <> 2 OR v_snr <> 30.0 OR v_exp <> 2000.0 THEN
    RAISE EXCEPTION 'full-access aggregates mismatch: nt=% ns=% snr=% exp=%', v_nt, v_ns, v_snr, v_exp;
  END IF;

  -- 3) Primary catalog RPC, public-only viewer: the returned row must be scoped.
  SELECT targets INTO v_json
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_test_pub'],
      p_fields        => ARRAY['zzz_test_field']
    );

  v_row := v_json -> 0;
  IF v_row IS NULL THEN
    RAISE EXCEPTION 'object not returned to public-access viewer';
  END IF;
  IF v_row -> 'programs' @> '["zzz_test_prop"]'::jsonb THEN
    RAISE EXCEPTION 'list RPC leaked proprietary program: %', v_row -> 'programs';
  END IF;
  IF NOT (v_row -> 'programs' @> '["zzz_test_pub"]'::jsonb) THEN
    RAISE EXCEPTION 'list RPC dropped accessible program: %', v_row -> 'programs';
  END IF;
  IF (v_row ->> 'n_targets')::int <> 1 THEN
    RAISE EXCEPTION 'list RPC leaked n_targets: %', v_row ->> 'n_targets';
  END IF;
  IF (v_row ->> 'max_snr')::float8 <> 10.0 THEN
    RAISE EXCEPTION 'list RPC leaked max_snr: %', v_row ->> 'max_snr';
  END IF;

  RAISE NOTICE 'OK: object aggregate scoping holds (helper + list RPC).';
END $$;

ROLLBACK;
