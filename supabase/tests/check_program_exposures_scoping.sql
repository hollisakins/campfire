-- Guard: get_program_exposures() hands program viewers the exposures table of
-- nirspec_rate_exposures (what was observed, which master background) and nothing more.
--
-- The table is admin-only under RLS (it carries the rate-review state: review_status,
-- mask_regions, notes). The metadata program page lists each observation's exposures to
-- everyone who can see the program, so the SECURITY DEFINER reader scopes its rows itself:
-- the program must be in accessible_program_slugs() (public programs, grants, admins), and
-- a share link sees only the observation it was minted for. This asserts that, for a viewer
-- without a grant, a viewer with one, an admin and an observation share link, and that anon
-- cannot call it and the table itself stays hidden from non-admins.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_program_exposures_scoping.sql
BEGIN;

DO $$
DECLARE
  v_obs   TEXT[];
  v_count BIGINT;
BEGIN
  -- Fixtures ------------------------------------------------------------------
  INSERT INTO programs (slug, program_name, is_public) VALUES
    ('zzz_mb_pub',  'Exposures Test Public',  true),
    ('zzz_mb_prop', 'Exposures Test Private', false);

  INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
    ('mb_obs_a',     'zzz_mb_pub',  9971, 'zzz_mb_field'),
    ('mb_obs_b',     'zzz_mb_pub',  9971, 'zzz_mb_field'),
    ('mb_obs_draft', 'zzz_mb_pub',  9971, 'zzz_mb_field'),
    ('mb_obs_prop',  'zzz_mb_prop', 9972, 'zzz_mb_field');

  -- An observation's exposures are listed once it has a published spectrum;
  -- mb_obs_draft has only a draft one (deployed, not yet published).
  INSERT INTO targets (target_id, field, ra, dec, program_slug, observation) VALUES
    ('zzz-mb-a',     'zzz_mb_field', 150.0, 2.0, 'zzz_mb_pub',  'mb_obs_a'),
    ('zzz-mb-b',     'zzz_mb_field', 150.0, 2.0, 'zzz_mb_pub',  'mb_obs_b'),
    ('zzz-mb-draft', 'zzz_mb_field', 150.0, 2.0, 'zzz_mb_pub',  'mb_obs_draft'),
    ('zzz-mb-prop',  'zzz_mb_field', 150.0, 2.0, 'zzz_mb_prop', 'mb_obs_prop');
  INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status) VALUES
    ('zzz-mb-a',     'PRISM', '/tmp/zzz_mb_a.fits',     10.0, 1000.0, 'published'),
    ('zzz-mb-b',     'G395M', '/tmp/zzz_mb_b.fits',     10.0, 1000.0, 'published'),
    ('zzz-mb-draft', 'PRISM', '/tmp/zzz_mb_draft.fits', 10.0, 1000.0, 'draft'),
    ('zzz-mb-prop',  'PRISM', '/tmp/zzz_mb_prop.fits',  10.0, 1000.0, 'published');

  INSERT INTO auth.users (id, email) VALUES
    ('00000000-0000-0000-0000-0000000000c1', 'zzz-mb-admin@test.invalid'),
    ('00000000-0000-0000-0000-0000000000c2', 'zzz-mb-user@test.invalid'),
    ('00000000-0000-0000-0000-0000000000c3', 'zzz-mb-granted@test.invalid'),
    ('00000000-0000-0000-0000-0000000000c4', 'zzz-mb-link@test.invalid');
  INSERT INTO user_profiles (user_id, username, full_name, is_admin, is_link_account, can_comment, can_inspect) VALUES
    ('00000000-0000-0000-0000-0000000000c1', 'zzz-mb-admin',   'ZZZ MB Admin',   true,  false, true,  true),
    ('00000000-0000-0000-0000-0000000000c2', 'zzz-mb-user',    'ZZZ MB User',    false, false, true,  false),
    ('00000000-0000-0000-0000-0000000000c3', 'zzz-mb-granted', 'ZZZ MB Granted', false, false, true,  false),
    ('00000000-0000-0000-0000-0000000000c4', 'zzz-mb-link',    'ZZZ MB Link',    false, true,  false, false);
  INSERT INTO user_program_access (user_id, program_slug) VALUES
    ('00000000-0000-0000-0000-0000000000c3', 'zzz_mb_prop');
  INSERT INTO share_links (token, label, observation, field, link_user_id, link_password,
                           include_drafts, allow_download, created_by, revoked_at) VALUES
    ('zzz_mb_tok', 'obs link', 'mb_obs_a', NULL, '00000000-0000-0000-0000-0000000000c4', 'x',
     false, true, '00000000-0000-0000-0000-0000000000c1', NULL);

  INSERT INTO nirspec_rate_exposures (observation, exposure_root, detector, filename, grating, filter,
                                      date_obs, exposure_time, readout_pattern, n_groups, n_integrations,
                                      dither_position, n_dither_positions,
                                      mbkg_storage_key, mbkg_source, mbkg_rate_source, mbkg_test,
                                      mbkg_exposures, mbkg_n_empty_shutters, mbkg_version,
                                      review_status, notes) VALUES
    ('mb_obs_a', 'jw99971001001_03101_00001', 'nrs1', 'a1_rate.fits', 'PRISM', 'CLEAR',
     '2025-11-06', 2830.0, 'NRSIRS2RAPID', 20, 2, 1, 3,
     'data/products/nirspec/mb_obs_a/a1_mbkg.fits', 'combined', 'combined', 'consistent',
     '{1/1,2/1}', 160, '1.5', 'excluded', 'reviewer note'),
    ('mb_obs_a', 'jw99971001001_03101_00001', 'nrs2', 'a2_rate.fits', 'PRISM', 'CLEAR',
     '2025-11-06', 2830.0, 'NRSIRS2RAPID', 20, 2, 1, 3,
     NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'pending', NULL),           -- no master background
    ('mb_obs_b', 'jw99971002001_03101_00001', 'nrs1', 'b1_rate.fits', 'G395M', 'F290LP',
     '2025-11-07', 8753.3, 'NRSIRS2', 20, 6, 2, 3,
     'data/products/nirspec/mb_obs_b/b1_mbkg.fits', 'exposure', 'exposure',
     'differs 3.10-3.40 um +4.2 % (5.1 sig)', '{}', 90, '1.5', 'pending', NULL),
    ('mb_obs_prop', 'jw99972001001_03101_00001', 'nrs1', 'p1_rate.fits', 'PRISM', 'CLEAR',
     '2025-11-08', 2830.0, 'NRSIRS2RAPID', 20, 2, 1, 3,
     'data/products/nirspec/mb_obs_prop/p1_mbkg.fits', 'combined', 'combined', 'consistent',
     '{1/1}', 120, '1.5', 'pending', NULL),
    ('mb_obs_draft', 'jw99971003001_03101_00001', 'nrs1', 'd1_rate.fits', 'PRISM', 'CLEAR',
     '2025-11-09', 2830.0, 'NRSIRS2RAPID', 20, 2, 1, 3,
     NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'pending', NULL);

  -- The labels are constrained to the portal's two words ----------------------
  BEGIN
    UPDATE nirspec_rate_exposures SET mbkg_source = 'pool' WHERE filename = 'a1_rate.fits';
    RAISE EXCEPTION 'mbkg_source accepted a label other than combined / exposure';
  EXCEPTION WHEN check_violation THEN NULL;
  END;

  -- anon cannot call it --------------------------------------------------------
  IF has_function_privilege('anon', 'public.get_program_exposures(text)', 'EXECUTE') THEN
    RAISE EXCEPTION 'anon can execute get_program_exposures';
  END IF;

  PERFORM set_config('role', 'authenticated', true);

  -- 1) Viewer without a grant: every exposure of the public program's published
  --    observations (with a master background or not), none of its draft-only
  --    observation, none of the private program, and no table rows -----------------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000c2', 'role', 'authenticated')::text, true);
  v_obs := ARRAY(SELECT m.observation || '/' || m.detector FROM public.get_program_exposures('zzz_mb_pub') m);
  IF v_obs <> ARRAY['mb_obs_a/nrs1', 'mb_obs_a/nrs2', 'mb_obs_b/nrs1'] THEN
    RAISE EXCEPTION 'viewer: want the three public exposures, got %', v_obs;
  END IF;
  SELECT count(*) INTO v_count FROM public.get_program_exposures('zzz_mb_prop');
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'viewer without a grant read % private exposure rows', v_count;
  END IF;
  SELECT count(*) INTO v_count FROM nirspec_rate_exposures WHERE observation LIKE 'mb_obs_%';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'non-admin read % nirspec_rate_exposures rows directly', v_count;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.get_program_exposures('zzz_mb_pub') m
                 WHERE m.observation = 'mb_obs_b' AND m.filter = 'F290LP' AND m.date_obs = '2025-11-07'
                   AND m.exposure_time = 8753.3 AND m.readout_pattern = 'NRSIRS2'
                   AND m.n_groups = 20 AND m.n_integrations = 6
                   AND m.dither_position = 2 AND m.n_dither_positions = 3
                   AND m.mbkg_source = 'exposure') THEN
    RAISE EXCEPTION 'viewer: the exposure columns did not come through';
  END IF;

  -- 2) Viewer with a grant on the private program --------------------------------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000c3', 'role', 'authenticated')::text, true);
  SELECT count(*) INTO v_count FROM public.get_program_exposures('zzz_mb_prop');
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'granted viewer: want the private exposure, got % rows', v_count;
  END IF;

  -- 3) Admin: the private exposure, and the draft-only observation's too ----------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000c1', 'role', 'authenticated')::text, true);
  SELECT count(*) INTO v_count FROM public.get_program_exposures('zzz_mb_prop');
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'admin: want the private exposure, got % rows', v_count;
  END IF;
  SELECT count(*) INTO v_count FROM public.get_program_exposures('zzz_mb_pub') m
   WHERE m.observation = 'mb_obs_draft';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'admin: want the draft-only observation''s exposure, got % rows', v_count;
  END IF;

  -- 4) Share link for observation a: that observation only -----------------------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000c4', 'role', 'authenticated')::text, true);
  v_obs := ARRAY(SELECT DISTINCT m.observation FROM public.get_program_exposures('zzz_mb_pub') m);
  IF v_obs <> ARRAY['mb_obs_a'] THEN
    RAISE EXCEPTION 'share link: want observation a only, got %', v_obs;
  END IF;
  SELECT count(*) INTO v_count FROM public.get_program_exposures('zzz_mb_prop');
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'share link read % rows of another program', v_count;
  END IF;

  PERFORM set_config('request.jwt.claims', '', true);
  PERFORM set_config('role', 'none', true);

  RAISE NOTICE 'OK: get_program_exposures scoping holds (viewer, granted viewer, admin, share link, anon, draft-only observation).';
END $$;

ROLLBACK;
