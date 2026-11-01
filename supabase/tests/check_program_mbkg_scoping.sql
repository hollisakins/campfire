-- Guard: get_program_mbkg() hands program viewers the master-background columns of
-- nirspec_rate_exposures and nothing more.
--
-- The table is admin-only under RLS (it carries the rate-review state: review_status,
-- mask_regions, notes). The metadata program page shows each exposure's master
-- background to everyone who can see the program, so the SECURITY DEFINER reader
-- scopes its rows itself: the program must be in accessible_program_slugs() (public
-- programs, grants, admins), and a share link sees only the observation it was minted
-- for. This asserts that, for a viewer without a grant, a viewer with one, an admin and
-- an observation share link, and that anon cannot call it and the table itself stays
-- hidden from non-admins.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_program_mbkg_scoping.sql
BEGIN;

DO $$
DECLARE
  v_obs   TEXT[];
  v_count BIGINT;
BEGIN
  -- Fixtures ------------------------------------------------------------------
  INSERT INTO programs (slug, program_name, is_public) VALUES
    ('zzz_mb_pub',  'Mbkg Test Public',  true),
    ('zzz_mb_prop', 'Mbkg Test Private', false);

  INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
    ('mb_obs_a',    'zzz_mb_pub',  9971, 'zzz_mb_field'),
    ('mb_obs_b',    'zzz_mb_pub',  9971, 'zzz_mb_field'),
    ('mb_obs_prop', 'zzz_mb_prop', 9972, 'zzz_mb_field');

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

  INSERT INTO nirspec_rate_exposures (observation, exposure_root, detector, filename, grating,
                                      mbkg_storage_key, mbkg_source, mbkg_rate_source, mbkg_test,
                                      mbkg_exposures, mbkg_n_empty_shutters, mbkg_version,
                                      review_status, notes) VALUES
    ('mb_obs_a', 'jw99971001001_03101_00001', 'nrs1', 'a1_rate.fits', 'PRISM',
     'data/products/nirspec/mb_obs_a/a1_mbkg.fits', 'combined', 'combined', 'consistent',
     '{1/1,2/1}', 160, '1.5', 'excluded', 'reviewer note'),
    ('mb_obs_a', 'jw99971001001_03101_00001', 'nrs2', 'a2_rate.fits', 'PRISM',
     NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'pending', NULL),           -- no master background
    ('mb_obs_b', 'jw99971002001_03101_00001', 'nrs1', 'b1_rate.fits', 'G395M',
     'data/products/nirspec/mb_obs_b/b1_mbkg.fits', 'exposure', 'exposure',
     'differs 3.10-3.40 um +4.2 % (5.1 sig)', '{}', 90, '1.5', 'pending', NULL),
    ('mb_obs_prop', 'jw99972001001_03101_00001', 'nrs1', 'p1_rate.fits', 'PRISM',
     'data/products/nirspec/mb_obs_prop/p1_mbkg.fits', 'combined', 'combined', 'consistent',
     '{1/1}', 120, '1.5', 'pending', NULL);

  -- The labels are constrained to the portal's two words ----------------------
  BEGIN
    UPDATE nirspec_rate_exposures SET mbkg_source = 'pool' WHERE filename = 'a1_rate.fits';
    RAISE EXCEPTION 'mbkg_source accepted a label other than combined / exposure';
  EXCEPTION WHEN check_violation THEN NULL;
  END;

  -- anon cannot call it --------------------------------------------------------
  IF has_function_privilege('anon', 'public.get_program_mbkg(text)', 'EXECUTE') THEN
    RAISE EXCEPTION 'anon can execute get_program_mbkg';
  END IF;

  PERFORM set_config('role', 'authenticated', true);

  -- 1) Viewer without a grant: the public program's two exposures with a master
  --    background, none of the private program, and no table rows ------------------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000c2', 'role', 'authenticated')::text, true);
  v_obs := ARRAY(SELECT m.observation || '/' || m.detector FROM public.get_program_mbkg('zzz_mb_pub') m);
  IF v_obs <> ARRAY['mb_obs_a/nrs1', 'mb_obs_b/nrs1'] THEN
    RAISE EXCEPTION 'viewer: want the two public exposures with a master background, got %', v_obs;
  END IF;
  SELECT count(*) INTO v_count FROM public.get_program_mbkg('zzz_mb_prop');
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'viewer without a grant read % private master-background rows', v_count;
  END IF;
  SELECT count(*) INTO v_count FROM nirspec_rate_exposures WHERE observation LIKE 'mb_obs_%';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'non-admin read % nirspec_rate_exposures rows directly', v_count;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.get_program_mbkg('zzz_mb_pub') m
                 WHERE m.observation = 'mb_obs_a' AND m.mbkg_source = 'combined' AND m.has_product
                   AND m.mbkg_exposures = '{1/1,2/1}' AND m.mbkg_n_empty_shutters = 160) THEN
    RAISE EXCEPTION 'viewer: the master-background columns did not come through';
  END IF;

  -- 2) Viewer with a grant on the private program --------------------------------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000c3', 'role', 'authenticated')::text, true);
  SELECT count(*) INTO v_count FROM public.get_program_mbkg('zzz_mb_prop');
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'granted viewer: want the private exposure, got % rows', v_count;
  END IF;

  -- 3) Admin ---------------------------------------------------------------------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000c1', 'role', 'authenticated')::text, true);
  SELECT count(*) INTO v_count FROM public.get_program_mbkg('zzz_mb_prop');
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'admin: want the private exposure, got % rows', v_count;
  END IF;

  -- 4) Share link for observation a: that observation only -----------------------
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000c4', 'role', 'authenticated')::text, true);
  v_obs := ARRAY(SELECT m.observation FROM public.get_program_mbkg('zzz_mb_pub') m);
  IF v_obs <> ARRAY['mb_obs_a'] THEN
    RAISE EXCEPTION 'share link: want observation a only, got %', v_obs;
  END IF;
  SELECT count(*) INTO v_count FROM public.get_program_mbkg('zzz_mb_prop');
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'share link read % rows of another program', v_count;
  END IF;

  PERFORM set_config('request.jwt.claims', '', true);
  PERFORM set_config('role', 'none', true);

  RAISE NOTICE 'OK: get_program_mbkg scoping holds (viewer, granted viewer, admin, share link, anon).';
END $$;

ROLLBACK;
