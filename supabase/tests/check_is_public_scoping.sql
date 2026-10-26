-- Guard: the stored is_public columns are exact, and the read policies that
-- test them first give every caller the same rows as the scope array alone.
--
-- Catalog Read Model phase 1 (#533). objects.is_public (any member program
-- public), targets.is_public and spectra.is_public (the row's program) are
-- trigger-maintained copies of programs.is_public, and select_objects /
-- targets / spectra_by_access test `(NOT link AND is_public) OR <scope>` so
-- the planner sees a real column's statistics. Two things can go wrong and
-- this asserts both:
--   * drift: a trigger path that leaves a row's flag stale (insert, program
--     change on the row, a program flipping public or private, a program row
--     created after rows already name it);
--   * widening: a caller seeing a row through the is_public disjunct that the
--     scope array would not have given them (a viewer and a private-program
--     object, a grantee, an admin). Share links are covered by
--     check_share_link_scoping.sql against the same policies.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_is_public_scoping.sql
BEGIN;

DO $$
DECLARE
  v_a INTEGER; v_b INTEGER; v_c INTEGER; v_d INTEGER;
  v_ids INTEGER[];
  v_pub BOOLEAN;
  v_n   BIGINT;
  c_all CONSTANT TEXT := 'zzz_ip_%';
BEGIN
  -- Fixtures: P public, Q private; A{P}, B{Q}, C{P,Q}; D{R} before R exists.
  INSERT INTO programs (slug, program_name, is_public) VALUES
    ('zzz_ip_p', 'Is Public Test P', true),
    ('zzz_ip_q', 'Is Public Test Q', false);
  INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
    ('ip_obs_p', 'zzz_ip_p', 9984, 'zzz_ip_field'),
    ('ip_obs_q', 'zzz_ip_q', 9985, 'zzz_ip_field');
  INSERT INTO auth.users (id, email) VALUES
    ('00000000-0000-0000-0000-0000000000d1', 'zzz-ip-admin@test.invalid'),
    ('00000000-0000-0000-0000-0000000000d2', 'zzz-ip-viewer@test.invalid'),
    ('00000000-0000-0000-0000-0000000000d3', 'zzz-ip-grantee@test.invalid');
  INSERT INTO user_profiles (user_id, username, full_name, is_admin, can_comment, can_inspect) VALUES
    ('00000000-0000-0000-0000-0000000000d1', 'zzz-ip-admin',   'ZZZ IP Admin',   true,  true, true),
    ('00000000-0000-0000-0000-0000000000d2', 'zzz-ip-viewer',  'ZZZ IP Viewer',  false, true, true),
    ('00000000-0000-0000-0000-0000000000d3', 'zzz-ip-grantee', 'ZZZ IP Grantee', false, true, true);
  INSERT INTO user_program_access (user_id, program_slug) VALUES
    ('00000000-0000-0000-0000-0000000000d3', 'zzz_ip_q');

  INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum) VALUES
    ('TEST-IP-A', 'zzz_ip_field', 152.00, 2.00, '{zzz_ip_p}',          '{ip_obs_p}',          true),
    ('TEST-IP-B', 'zzz_ip_field', 152.01, 2.00, '{zzz_ip_q}',          '{ip_obs_q}',          true),
    ('TEST-IP-C', 'zzz_ip_field', 152.02, 2.00, '{zzz_ip_p,zzz_ip_q}', '{ip_obs_p,ip_obs_q}', true),
    ('TEST-IP-D', 'zzz_ip_field', 152.03, 2.00, '{zzz_ip_r}',          '{ip_obs_p}',          true);
  SELECT id INTO v_a FROM objects WHERE object_id = 'TEST-IP-A';
  SELECT id INTO v_b FROM objects WHERE object_id = 'TEST-IP-B';
  SELECT id INTO v_c FROM objects WHERE object_id = 'TEST-IP-C';
  SELECT id INTO v_d FROM objects WHERE object_id = 'TEST-IP-D';

  INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id) VALUES
    ('test-ip-a',  'zzz_ip_field', 152.00, 2.00, 'zzz_ip_p', 'ip_obs_p', v_a),
    ('test-ip-b',  'zzz_ip_field', 152.01, 2.00, 'zzz_ip_q', 'ip_obs_q', v_b),
    ('test-ip-cp', 'zzz_ip_field', 152.02, 2.00, 'zzz_ip_p', 'ip_obs_p', v_c),
    ('test-ip-cq', 'zzz_ip_field', 152.02, 2.00, 'zzz_ip_q', 'ip_obs_q', v_c),
    ('test-ip-a2', 'zzz_ip_field', 152.00, 2.01, 'zzz_ip_p', 'ip_obs_p', v_a);   -- P, no spectrum: the move target in 3b
  INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status) VALUES
    ('test-ip-a',  'PRISM', '/tmp/ip_a_prism.fits',  10.0, 1000.0, 'published'),
    ('test-ip-b',  'PRISM', '/tmp/ip_b_prism.fits',  10.0, 1000.0, 'published'),
    ('test-ip-cp', 'PRISM', '/tmp/ip_cp_prism.fits', 10.0, 1000.0, 'published'),
    ('test-ip-cq', 'PRISM', '/tmp/ip_cq_prism.fits', 10.0, 1000.0, 'published');

  -- 1) Insert-time values ------------------------------------------------------
  IF (SELECT array_agg(object_id ORDER BY object_id) FROM objects WHERE is_public AND object_id LIKE 'TEST-IP-%')
     <> ARRAY['TEST-IP-A', 'TEST-IP-C'] THEN
    RAISE EXCEPTION 'objects.is_public at insert: want {A, C}, got %',
      (SELECT array_agg(object_id ORDER BY object_id) FROM objects WHERE is_public AND object_id LIKE 'TEST-IP-%');
  END IF;
  IF (SELECT array_agg(target_id ORDER BY target_id) FROM targets WHERE is_public AND target_id LIKE 'test-ip-%')
     <> ARRAY['test-ip-a', 'test-ip-a2', 'test-ip-cp'] THEN
    RAISE EXCEPTION 'targets.is_public at insert: want {a, a2, cp}';
  END IF;
  IF (SELECT array_agg(s.target_id ORDER BY s.target_id) FROM spectra s WHERE s.is_public AND s.target_id LIKE 'test-ip-%')
     <> ARRAY['test-ip-a', 'test-ip-cp'] THEN
    RAISE EXCEPTION 'spectra.is_public at insert: want {a, cp}';
  END IF;

  -- 2) A row's program reference changes ---------------------------------------
  UPDATE objects SET programs = '{zzz_ip_q}' WHERE id = v_a;
  SELECT is_public INTO v_pub FROM objects WHERE id = v_a;
  IF v_pub THEN RAISE EXCEPTION 'objects.is_public must follow programs[] (A -> {Q})'; END IF;
  UPDATE objects SET programs = '{zzz_ip_p}' WHERE id = v_a;
  SELECT is_public INTO v_pub FROM objects WHERE id = v_a;
  IF NOT v_pub THEN RAISE EXCEPTION 'objects.is_public must follow programs[] (A -> {P})'; END IF;
  UPDATE targets SET program_slug = 'zzz_ip_q', observation = 'ip_obs_q' WHERE target_id = 'test-ip-a';
  SELECT is_public INTO v_pub FROM targets WHERE target_id = 'test-ip-a';
  IF v_pub THEN RAISE EXCEPTION 'targets.is_public must follow program_slug'; END IF;
  UPDATE targets SET program_slug = 'zzz_ip_p', observation = 'ip_obs_p' WHERE target_id = 'test-ip-a';

  -- 3) A program flips, and a program row arrives after rows named it ---------
  UPDATE programs SET is_public = true WHERE slug = 'zzz_ip_q';
  IF (SELECT count(*) FROM objects WHERE object_id LIKE 'TEST-IP-%' AND NOT is_public) <> 1   -- only D{R}
     OR (SELECT count(*) FROM targets WHERE target_id LIKE 'test-ip-%' AND NOT is_public) <> 0
     OR (SELECT count(*) FROM spectra WHERE target_id LIKE 'test-ip-%' AND NOT is_public) <> 0 THEN
    RAISE EXCEPTION 'flipping Q public must set is_public on every Q row (objects, targets, spectra)';
  END IF;
  UPDATE programs SET is_public = false WHERE slug = 'zzz_ip_q';
  IF (SELECT array_agg(object_id ORDER BY object_id) FROM objects WHERE is_public AND object_id LIKE 'TEST-IP-%')
     <> ARRAY['TEST-IP-A', 'TEST-IP-C']
     OR (SELECT is_public FROM targets WHERE target_id = 'test-ip-b')
     OR (SELECT is_public FROM spectra WHERE target_id = 'test-ip-cq') THEN
    RAISE EXCEPTION 'flipping Q private must clear is_public on the Q-only rows and keep C (still in P)';
  END IF;
  INSERT INTO programs (slug, program_name, is_public) VALUES ('zzz_ip_r', 'Is Public Test R', true);
  SELECT is_public INTO v_pub FROM objects WHERE id = v_d;
  IF NOT v_pub THEN RAISE EXCEPTION 'a program row created after an object named it must set the object public'; END IF;

  -- 3b) The flag is trigger-owned: an explicit write is recomputed away, and a
  --     spectrum that moves to another target follows the target's program
  --     (the scope trigger rewrites program_slug in a BEFORE trigger, which an
  --     UPDATE OF program_slug event alone would not see).
  UPDATE objects SET is_public = true WHERE id = v_b;
  SELECT is_public INTO v_pub FROM objects WHERE id = v_b;
  IF v_pub THEN RAISE EXCEPTION 'an explicit write of objects.is_public must be recomputed from programs'; END IF;
  UPDATE targets SET is_public = true WHERE target_id = 'test-ip-b';
  SELECT is_public INTO v_pub FROM targets WHERE target_id = 'test-ip-b';
  IF v_pub THEN RAISE EXCEPTION 'an explicit write of targets.is_public must be recomputed from programs'; END IF;
  UPDATE spectra SET is_public = true WHERE fits_path = '/tmp/ip_b_prism.fits';
  SELECT is_public INTO v_pub FROM spectra WHERE fits_path = '/tmp/ip_b_prism.fits';
  IF v_pub THEN RAISE EXCEPTION 'an explicit write of spectra.is_public must be recomputed from programs'; END IF;
  UPDATE spectra SET target_id = 'test-ip-a2' WHERE fits_path = '/tmp/ip_b_prism.fits';  -- Q target -> P target
  SELECT is_public INTO v_pub FROM spectra WHERE fits_path = '/tmp/ip_b_prism.fits';
  IF NOT v_pub THEN RAISE EXCEPTION 'a spectrum moved to a public-program target must become public'; END IF;
  UPDATE spectra SET target_id = 'test-ip-b' WHERE fits_path = '/tmp/ip_b_prism.fits';   -- and back
  SELECT is_public INTO v_pub FROM spectra WHERE fits_path = '/tmp/ip_b_prism.fits';
  IF v_pub THEN RAISE EXCEPTION 'a spectrum moved back to a private-program target must become private'; END IF;

  -- 3c) A non-admin inspector (the grantee holds Q and can_inspect) cannot
  --     write the flag at all: the update-scope enforcers reject it.
  PERFORM set_config('role', 'authenticated', true);
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000d3', 'role', 'authenticated')::text, true);
  BEGIN
    UPDATE objects SET is_public = true WHERE id = v_b;
    RAISE EXCEPTION 'inspector: writing objects.is_public must be rejected';
  EXCEPTION WHEN OTHERS THEN
    IF SQLERRM NOT LIKE 'Non-admin updates to objects%' THEN RAISE; END IF;
  END;
  BEGIN
    UPDATE spectra SET is_public = true WHERE fits_path = '/tmp/ip_b_prism.fits';
    RAISE EXCEPTION 'inspector: writing spectra.is_public must be rejected';
  EXCEPTION WHEN OTHERS THEN
    IF SQLERRM NOT LIKE 'Non-admin updates to spectra%' THEN RAISE; END IF;
  END;
  PERFORM set_config('role', 'postgres', true);
  IF (SELECT is_public FROM objects WHERE id = v_b) OR (SELECT is_public FROM spectra WHERE fits_path = '/tmp/ip_b_prism.fits') THEN
    RAISE EXCEPTION 'inspector: a rejected write must leave the flag false';
  END IF;

  -- 4) No widening: the policy gives each caller the scope array''s rows -----
  PERFORM set_config('role', 'authenticated', true);
  -- viewer: public programs only
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000d2', 'role', 'authenticated')::text, true);
  v_ids := ARRAY(SELECT id FROM objects WHERE object_id LIKE 'TEST-IP-%' ORDER BY object_id);
  IF v_ids <> ARRAY[v_a, v_c, v_d] THEN
    RAISE EXCEPTION 'viewer: objects must be {A, C, D}, got %', v_ids;
  END IF;
  IF (SELECT array_agg(target_id ORDER BY target_id) FROM targets WHERE target_id LIKE 'test-ip-%')
     <> ARRAY['test-ip-a', 'test-ip-a2', 'test-ip-cp'] THEN
    RAISE EXCEPTION 'viewer: targets must be the P rows only';
  END IF;
  IF (SELECT array_agg(target_id ORDER BY target_id) FROM spectra WHERE target_id LIKE 'test-ip-%')
     <> ARRAY['test-ip-a', 'test-ip-cp'] THEN
    RAISE EXCEPTION 'viewer: spectra must be the P rows only';
  END IF;
  -- the viewer must not reach B through anything: its flag is false and Q is not in scope
  SELECT count(*) INTO v_n FROM objects WHERE id = v_b;
  IF v_n <> 0 THEN RAISE EXCEPTION 'viewer: private-program object B leaked'; END IF;
  -- grantee: public + Q
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000d3', 'role', 'authenticated')::text, true);
  v_ids := ARRAY(SELECT id FROM objects WHERE object_id LIKE 'TEST-IP-%' ORDER BY object_id);
  IF v_ids <> ARRAY[v_a, v_b, v_c, v_d] THEN
    RAISE EXCEPTION 'grantee: objects must be {A, B, C, D}, got %', v_ids;
  END IF;
  IF (SELECT count(*) FROM spectra WHERE target_id LIKE 'test-ip-%') <> 4 THEN
    RAISE EXCEPTION 'grantee: must see all four spectra';
  END IF;
  -- admin: everything
  PERFORM set_config('request.jwt.claims',
    json_build_object('sub', '00000000-0000-0000-0000-0000000000d1', 'role', 'authenticated')::text, true);
  IF (SELECT count(*) FROM objects WHERE object_id LIKE 'TEST-IP-%') <> 4
     OR (SELECT count(*) FROM targets WHERE target_id LIKE 'test-ip-%') <> 5 THEN
    RAISE EXCEPTION 'admin: must see every fixture row';
  END IF;

  -- 5) The flag never widens a non-admin past the scope array (the invariant
  --    the policy relies on): no visible row has is_public without a public
  --    program, catalog-wide.
  PERFORM set_config('role', 'postgres', true);
  SELECT count(*) INTO v_n FROM objects o
   WHERE o.is_public <> EXISTS (SELECT 1 FROM programs p WHERE p.is_public AND p.slug = ANY(o.programs));
  IF v_n <> 0 THEN RAISE EXCEPTION '% objects rows have a stale is_public', v_n; END IF;
  SELECT count(*) INTO v_n FROM targets t JOIN programs p ON p.slug = t.program_slug WHERE t.is_public <> p.is_public;
  IF v_n <> 0 THEN RAISE EXCEPTION '% targets rows have a stale is_public', v_n; END IF;
  SELECT count(*) INTO v_n FROM spectra s JOIN programs p ON p.slug = s.program_slug WHERE s.is_public <> p.is_public;
  IF v_n <> 0 THEN RAISE EXCEPTION '% spectra rows have a stale is_public', v_n; END IF;

  RAISE NOTICE 'check_is_public_scoping: OK';
END $$;

ROLLBACK;
