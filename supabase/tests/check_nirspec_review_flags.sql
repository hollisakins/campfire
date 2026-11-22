-- Guard: nirspec_source_review, the cloud home of the NIRSpec stuck-shutter and
-- background-override lists (`campfire config push / pull / diff`), and its guarded
-- write nirspec_review_apply, which every writer uses (CLI, deploy, portal).
--
-- What must hold:
--   1. The table is admin-only. A signed-in non-admin reads ZERO rows and can write
--      nothing, directly or through the function. (The client never mistakes those
--      zero rows for an empty cloud: it checks is_admin() first. This guard pins the
--      behaviour that check exists for.) anon has no access at all.
--   2. nirspec_review_apply applies a change only while the entry still holds the
--      value the client read: an entry changed in between is refused and reported,
--      never overwritten, and the other changes of the same call still land.
--   3. An empty stuck list ([]) is stored as an entry, distinct from NULL; clearing
--      an entry sets its column NULL, drops its provenance and keeps the row and the
--      other channel.
--   4. stuck_source accepts hand / web / auto / NULL and nothing else.
--   5. Malformed changes are rejected whole (one transaction).
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_nirspec_review_flags.sql
BEGIN;

-- ---------------------------------------------------------------------------
-- Fixtures (as the owner: RLS bypassed)
-- ---------------------------------------------------------------------------
INSERT INTO auth.users (id, email) VALUES
  ('00000000-0000-0000-0000-0000000f1a01', 'zzz-fl-admin@test.invalid'),
  ('00000000-0000-0000-0000-0000000f1a02', 'zzz-fl-user@test.invalid');

INSERT INTO user_profiles (user_id, username, full_name, is_admin, is_link_account, can_comment, can_inspect) VALUES
  ('00000000-0000-0000-0000-0000000f1a01', 'zzz-fl-admin', 'ZZZ FL Admin', true,  false, true, true),
  ('00000000-0000-0000-0000-0000000f1a02', 'zzz-fl-user',  'ZZZ FL User',  false, false, true, true);

INSERT INTO nirspec_source_review (observation, exposure_root, source_id, stuck_shutters, stuck_source, bkg_overrides) VALUES
  ('zzz_fl_obs', 'jw00000001001_01101', 1, '[1, 2]', 'auto', NULL),
  ('zzz_fl_obs', 'jw00000001001_01101', 2, '[3]',    NULL,   '{"3": [1]}'),   -- unknown provenance
  ('zzz_fl_obs', 'jw00000001001_01101', 3, NULL,     NULL,   '{"4": []}');

CREATE OR REPLACE FUNCTION pg_temp.zzz_as(p_uid text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', 'authenticated')::text, true);
END $$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_as(text) TO authenticated, anon;

-- The entry of one channel as the function would report it.
CREATE OR REPLACE FUNCTION pg_temp.zzz_entry(p_channel text, p_sid integer) RETURNS jsonb
LANGUAGE sql STABLE AS $$
  SELECT CASE p_channel
           WHEN 'stuck' THEN CASE WHEN stuck_shutters IS NULL THEN NULL
                                  ELSE jsonb_build_object('value', stuck_shutters, 'source', stuck_source) END
           ELSE CASE WHEN bkg_overrides IS NULL THEN NULL ELSE jsonb_build_object('value', bkg_overrides) END
         END
    FROM public.nirspec_source_review
   WHERE observation = 'zzz_fl_obs' AND exposure_root = 'jw00000001001_01101' AND source_id = p_sid;
$$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_entry(text, integer) TO authenticated;

CREATE OR REPLACE FUNCTION pg_temp.zzz_change(p_channel text, p_sid integer, p_expect jsonb, p_set jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT jsonb_build_object('channel', p_channel, 'exposure_root', 'jw00000001001_01101',
                            'source_id', p_sid, 'expect', p_expect, 'set', p_set);
$$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_change(text, integer, jsonb, jsonb) TO authenticated;

-- ---------------------------------------------------------------------------
-- 1. Admin-only: anon and a non-admin
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF has_table_privilege('anon', 'public.nirspec_source_review', 'SELECT')
     OR has_function_privilege('anon', 'public.nirspec_review_apply(text, jsonb)', 'EXECUTE') THEN
    RAISE EXCEPTION 'anon can reach nirspec_source_review or nirspec_review_apply';
  END IF;
END $$;

SET LOCAL ROLE authenticated;
SELECT pg_temp.zzz_as('00000000-0000-0000-0000-0000000f1a02');

DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM public.nirspec_source_review WHERE observation = 'zzz_fl_obs';
  IF n <> 0 THEN
    RAISE EXCEPTION 'a non-admin reads % row(s) of nirspec_source_review, expected 0', n;
  END IF;

  UPDATE public.nirspec_source_review SET stuck_shutters = '[9]' WHERE observation = 'zzz_fl_obs';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN
    RAISE EXCEPTION 'a non-admin updated % row(s) of nirspec_source_review', n;
  END IF;

  BEGIN
    INSERT INTO public.nirspec_source_review (observation, exposure_root, source_id, stuck_shutters)
    VALUES ('zzz_fl_obs', 'jw00000001001_01101', 99, '[1]');
    RAISE EXCEPTION 'a non-admin inserted into nirspec_source_review';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;

  BEGIN
    PERFORM public.nirspec_review_apply('zzz_fl_obs', jsonb_build_array(
      pg_temp.zzz_change('stuck', 1, '{"value": [1, 2], "source": "auto"}', '{"value": [9], "source": "hand"}')));
    RAISE EXCEPTION 'a non-admin ran nirspec_review_apply';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
END $$;

RESET ROLE;
DO $$
BEGIN
  IF (SELECT stuck_shutters FROM nirspec_source_review
       WHERE observation = 'zzz_fl_obs' AND source_id = 1) <> '[1, 2]'::jsonb THEN
    RAISE EXCEPTION 'a non-admin write reached nirspec_source_review';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2-5. The guarded write, as an admin
-- ---------------------------------------------------------------------------
SET LOCAL ROLE authenticated;
SELECT pg_temp.zzz_as('00000000-0000-0000-0000-0000000f1a01');

DO $$
DECLARE
  res jsonb;
  n   integer;
BEGIN
  -- an admin reads the rows
  SELECT count(*) INTO n FROM public.nirspec_source_review WHERE observation = 'zzz_fl_obs';
  IF n <> 3 THEN RAISE EXCEPTION 'an admin reads % row(s), expected 3', n; END IF;

  -- (a) one call: an edit whose expectation holds, one whose expectation is stale,
  --     a new entry on a new row, and one already at the wanted value.
  res := public.nirspec_review_apply('zzz_fl_obs', jsonb_build_array(
    -- holds (the list is compared as jsonb: spacing and number spelling do not matter)
    pg_temp.zzz_change('stuck', 1, '{"value":[1,2.0],"source":"auto"}', '{"value": [1, 2, 3], "source": "hand"}'),
    -- stale: the client read [4] but the row holds [3] (someone edited it)
    pg_temp.zzz_change('stuck', 2, '{"value": [4], "source": null}', '{"value": [5], "source": "hand"}'),
    -- new row, new entry (the client saw nothing)
    pg_temp.zzz_change('bkg', 7, NULL, '{"value": {"3": [1, 2]}}'),
    -- already there: unchanged, whatever was expected
    pg_temp.zzz_change('bkg', 3, '{"value": {"9": [9]}}', '{"value": {"4": []}}')));
  IF (res ->> 'applied')::int <> 2 OR (res ->> 'unchanged')::int <> 1
     OR jsonb_array_length(res -> 'refused') <> 1 THEN
    RAISE EXCEPTION 'guarded write: expected 2 applied / 1 unchanged / 1 refused, got %', res;
  END IF;
  IF (res -> 'refused' -> 0 ->> 'source_id')::int <> 2
     OR res -> 'refused' -> 0 -> 'current' <> '{"value": [3], "source": null}'::jsonb THEN
    RAISE EXCEPTION 'guarded write: the refusal must name the entry and its current value, got %', res -> 'refused';
  END IF;
  IF pg_temp.zzz_entry('stuck', 1) <> '{"value": [1, 2, 3], "source": "hand"}'::jsonb THEN
    RAISE EXCEPTION 'guarded write: the matching edit did not land: %', pg_temp.zzz_entry('stuck', 1);
  END IF;
  IF pg_temp.zzz_entry('stuck', 2) <> '{"value": [3], "source": null}'::jsonb THEN
    RAISE EXCEPTION 'guarded write: a stale edit overwrote the entry: %', pg_temp.zzz_entry('stuck', 2);
  END IF;
  IF pg_temp.zzz_entry('bkg', 2) <> '{"value": {"3": [1]}}'::jsonb THEN
    RAISE EXCEPTION 'guarded write: the other channel of a refused entry changed';
  END IF;
  IF pg_temp.zzz_entry('bkg', 7) <> '{"value": {"3": [1, 2]}}'::jsonb
     OR pg_temp.zzz_entry('stuck', 7) IS NOT NULL THEN
    RAISE EXCEPTION 'guarded write: the new entry did not land on a row of its own';
  END IF;

  -- (b) a new entry is refused when one appeared since the client looked
  res := public.nirspec_review_apply('zzz_fl_obs', jsonb_build_array(
    pg_temp.zzz_change('bkg', 7, NULL, '{"value": {"3": [9]}}')));
  IF (res ->> 'applied')::int <> 0 OR jsonb_array_length(res -> 'refused') <> 1 THEN
    RAISE EXCEPTION 'guarded write: an insert over an entry the client never saw must be refused, got %', res;
  END IF;

  -- (c) an empty stuck list is an entry ("vetted, none stuck"), not NULL; a tag-only
  --     change is a change (unknown provenance -> auto).
  res := public.nirspec_review_apply('zzz_fl_obs', jsonb_build_array(
    pg_temp.zzz_change('stuck', 3, NULL, '{"value": [], "source": "web"}'),
    pg_temp.zzz_change('stuck', 2, '{"value": [3], "source": null}', '{"value": [3], "source": "auto"}')));
  IF (res ->> 'applied')::int <> 2 THEN
    RAISE EXCEPTION 'guarded write: veto + tag update expected 2 applied, got %', res;
  END IF;
  IF pg_temp.zzz_entry('stuck', 3) <> '{"value": [], "source": "web"}'::jsonb THEN
    RAISE EXCEPTION 'an empty stuck list must be stored as an entry, got %', pg_temp.zzz_entry('stuck', 3);
  END IF;
  IF pg_temp.zzz_entry('stuck', 2) <> '{"value": [3], "source": "auto"}'::jsonb THEN
    RAISE EXCEPTION 'a tag-only change did not land: %', pg_temp.zzz_entry('stuck', 2);
  END IF;

  -- (d) clearing an entry: column NULL, provenance dropped, row and other channel kept.
  res := public.nirspec_review_apply('zzz_fl_obs', jsonb_build_array(
    pg_temp.zzz_change('stuck', 2, '{"value": [3], "source": "auto"}', NULL)));
  IF (res ->> 'applied')::int <> 1 THEN
    RAISE EXCEPTION 'guarded write: clear expected 1 applied, got %', res;
  END IF;
  SELECT count(*) INTO n FROM public.nirspec_source_review
   WHERE observation = 'zzz_fl_obs' AND source_id = 2
     AND stuck_shutters IS NULL AND stuck_source IS NULL AND bkg_overrides = '{"3": [1]}'::jsonb;
  IF n <> 1 THEN
    RAISE EXCEPTION 'clearing a stuck entry must NULL the list and its source and keep the row and the bkg entry';
  END IF;

  -- clearing an entry of a row that does not exist creates nothing
  res := public.nirspec_review_apply('zzz_fl_obs', jsonb_build_array(
    pg_temp.zzz_change('stuck', 555, NULL, NULL)));
  SELECT count(*) INTO n FROM public.nirspec_source_review WHERE observation = 'zzz_fl_obs' AND source_id = 555;
  IF (res ->> 'unchanged')::int <> 1 OR n <> 0 THEN
    RAISE EXCEPTION 'clearing a missing entry must be a no-op, got % (% row)', res, n;
  END IF;

  -- (e) malformed changes are rejected, and the whole call with them.
  BEGIN
    PERFORM public.nirspec_review_apply('zzz_fl_obs', jsonb_build_array(
      pg_temp.zzz_change('stuck', 1, '{"value": [1, 2, 3], "source": "hand"}', '{"value": [7], "source": "hand"}'),
      pg_temp.zzz_change('stuck', 1, NULL, '{"value": [1], "source": "robot"}')));
    RAISE EXCEPTION 'a change with an unknown source was accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL;
  END;
  IF pg_temp.zzz_entry('stuck', 1) <> '{"value": [1, 2, 3], "source": "hand"}'::jsonb THEN
    RAISE EXCEPTION 'a rejected call left a partial write: %', pg_temp.zzz_entry('stuck', 1);
  END IF;
  BEGIN
    PERFORM public.nirspec_review_apply('zzz_fl_obs', jsonb_build_array(
      pg_temp.zzz_change('stuck', 1, NULL, '{"value": {"3": [1]}, "source": "hand"}')));
    RAISE EXCEPTION 'a stuck entry that is not a list was accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL;
  END;
  BEGIN
    PERFORM public.nirspec_review_apply('zzz_fl_obs', jsonb_build_array(
      pg_temp.zzz_change('bkg', 1, NULL, '{"value": [1]}')));
    RAISE EXCEPTION 'a bkg entry that is not a map was accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL;
  END;
  BEGIN
    PERFORM public.nirspec_review_apply('zzz_fl_obs', '[{"channel": "notes", "exposure_root": "r", "source_id": 1}]');
    RAISE EXCEPTION 'an unknown channel was accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL;
  END;
  BEGIN
    PERFORM public.nirspec_review_apply('zzz_fl_obs', '{"channel": "stuck"}');
    RAISE EXCEPTION 'a non-array p_changes was accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL;
  END;

  -- 4. the CHECK on stuck_source (a direct write, should anyone make one)
  BEGIN
    UPDATE public.nirspec_source_review SET stuck_source = 'robot'
     WHERE observation = 'zzz_fl_obs' AND source_id = 1;
    RAISE EXCEPTION 'stuck_source accepted a value outside hand / web / auto';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  UPDATE public.nirspec_source_review SET stuck_shutters = '[]', stuck_source = 'web'
   WHERE observation = 'zzz_fl_obs' AND source_id = 1;

  RAISE NOTICE 'OK: nirspec_source_review is admin-only; nirspec_review_apply never overwrites an entry changed since it was read.';
END $$;

RESET ROLE;
ROLLBACK;
