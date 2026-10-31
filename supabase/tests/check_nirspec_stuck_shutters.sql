-- Guard: nirspec_stuck_shutters (the physical stuck-closed shutter list) is
-- admin-only and only ever holds a well-formed physical key with a manual / web
-- origin.
--
--   1. anon has no privilege on the table;
--   2. a non-admin user sees no row and cannot insert, update or delete one;
--   3. an admin reads, inserts, updates and deletes; the trigger stamps
--      updated_by with the admin's uid and moves updated_at;
--   4. the shutter CHECK accepts "q{1-4}r{row}c{col}" only, the origin CHECK
--      manual / web only (auto calls stay local), and (observation,
--      exposure_root, shutter) is unique.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_nirspec_stuck_shutters.sql
BEGIN;

-- ---------------------------------------------------------------------------
-- Fixtures (as the owner: RLS bypassed)
-- ---------------------------------------------------------------------------
INSERT INTO auth.users (id, email) VALUES
  ('00000000-0000-0000-0000-0000000055a1', 'zzz-ss-admin@test.invalid'),
  ('00000000-0000-0000-0000-0000000055a2', 'zzz-ss-user@test.invalid');

INSERT INTO user_profiles (user_id, username, full_name, is_admin, is_link_account, can_comment, can_inspect) VALUES
  ('00000000-0000-0000-0000-0000000055a1', 'zzz-ss-admin', 'ZZZ SS Admin', true,  false, true, true),
  ('00000000-0000-0000-0000-0000000055a2', 'zzz-ss-user',  'ZZZ SS User',  false, false, true, true);

INSERT INTO nirspec_stuck_shutters (observation, exposure_root, shutter, origin, stuck, note) VALUES
  ('zzz_ss_obs', 'jw09999001001_03101', 'q2r195c64', 'manual', true,  NULL),
  ('zzz_ss_obs', 'jw09999001001_03101', 'q2r300c149', 'web',   false, 'veto');

CREATE OR REPLACE FUNCTION pg_temp.zzz_as(p_uid text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', 'authenticated')::text, true);
END $$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_as(text) TO authenticated;

-- ---------------------------------------------------------------------------
-- 1. anon: no privilege at all.
-- ---------------------------------------------------------------------------
SET LOCAL ROLE anon;
DO $$
BEGIN
  BEGIN
    PERFORM count(*) FROM nirspec_stuck_shutters;
    RAISE EXCEPTION 'anon can read nirspec_stuck_shutters';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END $$;
RESET ROLE;

-- ---------------------------------------------------------------------------
-- 2. A non-admin user: nothing visible, nothing writable.
-- ---------------------------------------------------------------------------
SET LOCAL ROLE authenticated;
DO $$
DECLARE n int;
BEGIN
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000055a2');
  SELECT count(*) INTO n FROM nirspec_stuck_shutters WHERE observation = 'zzz_ss_obs';
  IF n <> 0 THEN RAISE EXCEPTION 'non-admin sees % stuck-shutter rows', n; END IF;

  BEGIN
    INSERT INTO nirspec_stuck_shutters (observation, exposure_root, shutter, origin, stuck)
    VALUES ('zzz_ss_obs', 'jw09999001001_03101', 'q1r1c1', 'web', true);
    RAISE EXCEPTION 'non-admin inserted a stuck-shutter row';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;  -- RLS WITH CHECK violation
  END;

  UPDATE nirspec_stuck_shutters SET stuck = false WHERE observation = 'zzz_ss_obs';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'non-admin updated % rows', n; END IF;
  DELETE FROM nirspec_stuck_shutters WHERE observation = 'zzz_ss_obs';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'non-admin deleted % rows', n; END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. An admin: full read / write, the trigger stamps the writer.
-- ---------------------------------------------------------------------------
DO $$
DECLARE n int; who uuid; at timestamptz;
BEGIN
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000055a1');
  SELECT count(*) INTO n FROM nirspec_stuck_shutters WHERE observation = 'zzz_ss_obs';
  IF n <> 2 THEN RAISE EXCEPTION 'admin sees % rows, expected 2', n; END IF;

  INSERT INTO nirspec_stuck_shutters (observation, exposure_root, shutter, origin, stuck)
  VALUES ('zzz_ss_obs', 'jw09999001001_03101', 'q4r12c7', 'web', true);
  SELECT updated_by INTO who FROM nirspec_stuck_shutters
   WHERE observation = 'zzz_ss_obs' AND shutter = 'q4r12c7';
  IF who IS DISTINCT FROM '00000000-0000-0000-0000-0000000055a1'::uuid THEN
    RAISE EXCEPTION 'insert stamped updated_by = %', who;
  END IF;

  -- the upsert the web and the deploy push send
  INSERT INTO nirspec_stuck_shutters (observation, exposure_root, shutter, origin, stuck)
  VALUES ('zzz_ss_obs', 'jw09999001001_03101', 'q4r12c7', 'web', false)
  ON CONFLICT (observation, exposure_root, shutter) DO UPDATE SET stuck = EXCLUDED.stuck;
  SELECT count(*) INTO n FROM nirspec_stuck_shutters
   WHERE observation = 'zzz_ss_obs' AND shutter = 'q4r12c7' AND stuck = false;
  IF n <> 1 THEN RAISE EXCEPTION 'upsert did not update in place'; END IF;

  DELETE FROM nirspec_stuck_shutters WHERE observation = 'zzz_ss_obs' AND shutter = 'q4r12c7';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 1 THEN RAISE EXCEPTION 'admin delete removed % rows', n; END IF;
END $$;
RESET ROLE;

-- A service-role style write (no auth.uid()) keeps the updated_by it sends.
DO $$
DECLARE who uuid;
BEGIN
  PERFORM set_config('request.jwt.claims', '', true);
  UPDATE nirspec_stuck_shutters SET updated_by = '00000000-0000-0000-0000-0000000055a2', note = 'x'
   WHERE observation = 'zzz_ss_obs' AND shutter = 'q2r195c64';
  SELECT updated_by INTO who FROM nirspec_stuck_shutters
   WHERE observation = 'zzz_ss_obs' AND shutter = 'q2r195c64';
  IF who IS DISTINCT FROM '00000000-0000-0000-0000-0000000055a2'::uuid THEN
    RAISE EXCEPTION 'service write lost its updated_by: %', who;
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. CHECKs and the unique key.
-- ---------------------------------------------------------------------------
DO $$
DECLARE k text;
BEGIN
  FOREACH k IN ARRAY ARRAY['q5r1c1', 'q0r1c1', 'Q2r195c64', 'q2r195', 'q2-r195-c64', 'q2r195c64 ', '', 'q2rc64']
  LOOP
    BEGIN
      INSERT INTO nirspec_stuck_shutters (observation, exposure_root, shutter, origin, stuck)
      VALUES ('zzz_ss_obs', 'jw09999001001_03101', k, 'manual', true);
      RAISE EXCEPTION 'shutter key % was accepted', quote_literal(k);
    EXCEPTION WHEN check_violation THEN
      NULL;
    END;
  END LOOP;

  BEGIN
    INSERT INTO nirspec_stuck_shutters (observation, exposure_root, shutter, origin, stuck)
    VALUES ('zzz_ss_obs', 'jw09999001001_03101', 'q1r2c3', 'auto', true);
    RAISE EXCEPTION 'origin auto was accepted';
  EXCEPTION WHEN check_violation THEN
    NULL;
  END;

  BEGIN
    INSERT INTO nirspec_stuck_shutters (observation, exposure_root, shutter, origin, stuck)
    VALUES ('zzz_ss_obs', 'jw09999001001_03101', 'q2r195c64', 'web', true);
    RAISE EXCEPTION 'duplicate (observation, exposure_root, shutter) was accepted';
  EXCEPTION WHEN unique_violation THEN
    NULL;
  END;

  BEGIN
    INSERT INTO nirspec_stuck_shutters (observation, exposure_root, shutter, origin)
    VALUES ('zzz_ss_obs', 'jw09999001001_03101', 'q1r2c3', 'web');
    RAISE EXCEPTION 'a row without stuck was accepted';
  EXCEPTION WHEN not_null_violation THEN
    NULL;
  END;
END $$;

ROLLBACK;
