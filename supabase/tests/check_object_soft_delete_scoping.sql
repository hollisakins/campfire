-- Guard: a soft-deleted object (objects.is_active = false) is an ADMIN-ONLY
-- sight, at the table and everywhere the objects policy is mirrored.
--
-- Catalog Read Model, phase 0. Reconciliation leaves an orphan behind when it
-- regroups an object's targets and marks it is_active = false. The catalog
-- RPCs always filtered those out themselves, but select_objects_by_access did
-- not, so a direct read of the table (PostgREST, the object page's cat_objects
-- lookup, the derived tables' policy chains) still showed them to every
-- viewer. The policy now carries `is_active OR is_admin()`, as do the policies
-- that re-derive it inline (object_photometry, object_list_members, comments,
-- flag_audit_log) and the verbatim copies inside the two SECURITY DEFINER
-- filter helpers (object_line_values, object_band_values), which bypass RLS
-- and must stay in step by hand.
--
-- This asserts, with one active and one soft-deleted object in a public
-- program (both published, both with a cross-match and a line fit), that a
-- non-admin viewer sees exactly the active one through the table, the
-- cat_objects view, the derived tables, the two helpers called directly, and
-- the objects list RPC; that an admin sees both; and that the service-role
-- RPC path (which gates on explicit arguments) still filters the inactive one.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_object_soft_delete_scoping.sql
BEGIN;

DO $$
DECLARE
  v_live  INTEGER;   -- active, published
  v_gone  INTEGER;   -- is_active = false, otherwise identical
  v_ids   INTEGER[];
  v_n     BIGINT;
  v_json  JSONB;
  v_count BIGINT;
  r       RECORD;
  c_pub   CONSTANT TEXT[] := ARRAY['zzz_sd_pub'];
BEGIN
  -- Fixtures ------------------------------------------------------------------
  INSERT INTO programs (slug, program_name, is_public) VALUES
    ('zzz_sd_pub', 'Soft Delete Test Public', true);
  INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
    ('sd_obs_pub', 'zzz_sd_pub', 9983, 'zzz_sd_field');

  INSERT INTO auth.users (id, email) VALUES
    ('00000000-0000-0000-0000-0000000000c1', 'zzz-sd-admin@test.invalid'),
    ('00000000-0000-0000-0000-0000000000c2', 'zzz-sd-user@test.invalid');
  INSERT INTO user_profiles (user_id, username, full_name, is_admin, can_comment, can_inspect) VALUES
    ('00000000-0000-0000-0000-0000000000c1', 'zzz-sd-admin', 'ZZZ SD Admin', true,  true, true),
    ('00000000-0000-0000-0000-0000000000c2', 'zzz-sd-user',  'ZZZ SD User',  false, true, true);

  INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum,
                       is_active, redshift_inspected, redshift_quality) VALUES
    ('TEST-SD-LIVE', 'zzz_sd_field', 151.00, 2.00, '{zzz_sd_pub}', '{sd_obs_pub}', true, true,  5.0, 3),
    ('TEST-SD-GONE', 'zzz_sd_field', 151.01, 2.00, '{zzz_sd_pub}', '{sd_obs_pub}', true, false, 5.0, 3);
  SELECT id INTO v_live FROM objects WHERE object_id = 'TEST-SD-LIVE';
  SELECT id INTO v_gone FROM objects WHERE object_id = 'TEST-SD-GONE';

  INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id) VALUES
    ('test-sd-live', 'zzz_sd_field', 151.00, 2.00, 'zzz_sd_pub', 'sd_obs_pub', v_live),
    ('test-sd-gone', 'zzz_sd_field', 151.01, 2.00, 'zzz_sd_pub', 'sd_obs_pub', v_gone);
  INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status) VALUES
    ('test-sd-live', 'PRISM', '/tmp/sd_live_prism.fits', 10.0, 1000.0, 'published'),
    ('test-sd-gone', 'PRISM', '/tmp/sd_gone_prism.fits', 10.0, 1000.0, 'published');

  -- One cross-match each (the trigger unnests the band) and one non-stale
  -- line fit each (the trigger unnests the line).
  INSERT INTO object_photometry (object_id, field, ra, dec, catalog_name, photometry)
  SELECT o.id, o.field, o.ra, o.dec, 'zzz_sd_cat',
         jsonb_build_object('flux_unit', 'uJy', 'bands', jsonb_build_object(
           'f444w', jsonb_build_object('flux', 0.36308, 'flux_err', 0.018, 'wav', 4.4)))
  FROM objects o WHERE o.id IN (v_live, v_gone);

  FOR r IN
    SELECT s.id AS spectrum_id, s.target_id, s.grating, t.program_slug, t.observation,
           o.object_id, o.version, o.redshift_quality
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    JOIN objects o ON o.id = t.object_id
    WHERE o.id IN (v_live, v_gone)
  LOOP
    INSERT INTO spectrum_line_fits (spectrum_id, target_id, grating, program_slug, observation,
                                    z_used, z_source, z_quality, object_id, object_version,
                                    n_lines, n_detected, lines, fit_version)
    VALUES (r.spectrum_id, r.target_id, r.grating, r.program_slug, r.observation,
            5.0, 'inspected', r.redshift_quality, r.object_id, r.version,
            1, 1,
            jsonb_build_object('CIII1908', jsonb_build_object(
              'component', 'doublet', 'wave_rest', 1907.7,
              'flux', 10e-19, 'flux_err', 1e-19, 'snr', 10.0)),
            'test');
  END LOOP;

  IF (SELECT count(*) FROM object_photometry_bands b JOIN object_photometry p ON p.id = b.photometry_id
      WHERE p.object_id IN (v_live, v_gone)) <> 2
     OR (SELECT count(*) FROM spectrum_lines l JOIN spectra s ON s.id = l.spectrum_id
         WHERE s.fits_path LIKE '/tmp/sd\_%') <> 2 THEN
    RAISE EXCEPTION 'fixture: triggers did not unnest one band row and one line row per object';
  END IF;

  -- 1) Non-admin viewer (public program, so accessible with no grant) --------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000c2', 'role', 'authenticated')::text, true);
  PERFORM set_config('role', 'authenticated', true);

  v_ids := ARRAY(SELECT id FROM objects WHERE id IN (v_live, v_gone) ORDER BY 1);
  IF v_ids <> ARRAY[v_live] THEN
    RAISE EXCEPTION 'viewer: objects table must show only the active object, got %', v_ids;
  END IF;
  v_ids := ARRAY(SELECT id FROM cat_objects WHERE id IN (v_live, v_gone) ORDER BY 1);
  IF v_ids <> ARRAY[v_live] THEN
    RAISE EXCEPTION 'viewer: cat_objects must show only the active object, got %', v_ids;
  END IF;
  -- The derived tables reach the object through their own policy chain.
  v_ids := ARRAY(SELECT DISTINCT p.object_id FROM object_photometry p WHERE p.object_id IN (v_live, v_gone) ORDER BY 1);
  IF v_ids <> ARRAY[v_live] THEN
    RAISE EXCEPTION 'viewer: object_photometry must show only the active object''s cross-match, got %', v_ids;
  END IF;
  SELECT count(*) INTO v_n FROM object_photometry_bands b JOIN object_photometry p ON p.id = b.photometry_id
    WHERE p.object_id IN (v_live, v_gone);
  IF v_n <> 1 THEN
    RAISE EXCEPTION 'viewer: object_photometry_bands must show one band row (active object only), got %', v_n;
  END IF;
  -- The two SECURITY DEFINER helpers bypass RLS and carry a copy of the
  -- policy: called directly with the public slugs they must drop the orphan.
  v_ids := ARRAY(SELECT v.object_id FROM public.object_band_values('f444w', NULL, NULL, NULL, NULL, c_pub) v
                 WHERE v.object_id IN (v_live, v_gone) ORDER BY 1);
  IF v_ids <> ARRAY[v_live] THEN
    RAISE EXCEPTION 'viewer: object_band_values leaked the soft-deleted object: %', v_ids;
  END IF;
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', NULL, NULL, false, c_pub) v
                 WHERE v.object_id IN (v_live, v_gone) ORDER BY 1);
  IF v_ids <> ARRAY[v_live] THEN
    RAISE EXCEPTION 'viewer: object_line_values leaked the soft-deleted object: %', v_ids;
  END IF;
  -- The objects list RPC (no regression: it filtered is_active before too).
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(p_program_slugs => c_pub, p_fields => ARRAY['zzz_sd_field']);
  IF v_count <> 1 OR jsonb_array_length(v_json) <> 1 OR v_json -> 0 ->> 'object_id' <> 'TEST-SD-LIVE' THEN
    RAISE EXCEPTION 'viewer: objects list must return only TEST-SD-LIVE, got count=% rows=%', v_count, v_json;
  END IF;

  -- 2) Admin: sees both -----------------------------------------------------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000c1', 'role', 'authenticated')::text, true);
  v_ids := ARRAY(SELECT id FROM objects WHERE id IN (v_live, v_gone) ORDER BY 1);
  IF v_ids <> ARRAY(SELECT x FROM unnest(ARRAY[v_live, v_gone]) x ORDER BY 1) THEN
    RAISE EXCEPTION 'admin: objects table must show both objects, got %', v_ids;
  END IF;
  v_ids := ARRAY(SELECT DISTINCT p.object_id FROM object_photometry p WHERE p.object_id IN (v_live, v_gone) ORDER BY 1);
  IF v_ids <> ARRAY(SELECT x FROM unnest(ARRAY[v_live, v_gone]) x ORDER BY 1) THEN
    RAISE EXCEPTION 'admin: object_photometry must show both cross-matches, got %', v_ids;
  END IF;
  v_ids := ARRAY(SELECT v.object_id FROM public.object_line_values('CIII1908', NULL, NULL, false, c_pub) v
                 WHERE v.object_id IN (v_live, v_gone) ORDER BY 1);
  IF v_ids <> ARRAY(SELECT x FROM unnest(ARRAY[v_live, v_gone]) x ORDER BY 1) THEN
    RAISE EXCEPTION 'admin: object_line_values must include the soft-deleted object, got %', v_ids;
  END IF;

  -- 3) Service role (the /api/v1 bearer path): the RPC filters is_active
  --    itself, so the explicit-argument gate alone still hides the orphan.
  PERFORM set_config('request.jwt.claims', json_build_object('role', 'service_role')::text, true);
  PERFORM set_config('role', 'service_role', true);
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(p_program_slugs => c_pub, p_fields => ARRAY['zzz_sd_field']);
  IF v_count <> 1 OR v_json -> 0 ->> 'object_id' <> 'TEST-SD-LIVE' THEN
    RAISE EXCEPTION 'service role: objects list must return only TEST-SD-LIVE, got count=% rows=%', v_count, v_json;
  END IF;

  RAISE NOTICE 'check_object_soft_delete_scoping: OK';
END $$;

ROLLBACK;
