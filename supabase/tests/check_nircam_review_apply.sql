-- Guard: nircam_review_apply, the guarded write of the NIRCam reviewer masks and
-- triage status (`campfire config push --masks`, the field deploy) to nircam_exposures.
--
-- What must hold:
--   1. Admin only: anon cannot call it; a signed-in non-admin is refused and writes
--      nothing.
--   2. A change lands only while the column still holds the value the client read:
--      one changed in between is refused with its current value, never overwritten,
--      and the other changes of the same call still land. A change already in place
--      is 'unchanged'; an exposure the table lacks is reported under 'missing'.
--   3. A status write stamps review_decided_at (so the portal outbox's
--      last-writer-wins guard skips an older decision); clearing a mask NULLs it.
--   4. Malformed changes are rejected whole (one transaction).
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_nircam_review_apply.sql
BEGIN;

-- ---------------------------------------------------------------------------
-- Fixtures (as the owner: RLS bypassed)
-- ---------------------------------------------------------------------------
INSERT INTO auth.users (id, email) VALUES
  ('00000000-0000-0000-0000-0000000f1b01', 'zzz-nr-admin@test.invalid'),
  ('00000000-0000-0000-0000-0000000f1b02', 'zzz-nr-user@test.invalid');

INSERT INTO user_profiles (user_id, username, full_name, is_admin, is_link_account, can_comment, can_inspect) VALUES
  ('00000000-0000-0000-0000-0000000f1b01', 'zzz-nr-admin', 'ZZZ NR Admin', true,  false, true, true),
  ('00000000-0000-0000-0000-0000000f1b02', 'zzz-nr-user',  'ZZZ NR User',  false, false, true, true);

INSERT INTO nircam_exposures (field, filter, detector, filename, mask_regions, review_status) VALUES
  ('zzz_nr_field', 'f444w', 'nrcalong', 'zzz_a', '{"version": 1, "polygons": [{"id": "p", "vertices": [[1, 1], [5, 1], [5, 5]]}]}', 'pending'),
  ('zzz_nr_field', 'f444w', 'nrcalong', 'zzz_b', NULL, 'excluded'),
  ('zzz_nr_field', 'f444w', 'nrcalong', 'zzz_c', NULL, 'pending');

CREATE OR REPLACE FUNCTION pg_temp.zzz_as(p_uid text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', 'authenticated')::text, true);
END $$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_as(text) TO authenticated, anon;

CREATE OR REPLACE FUNCTION pg_temp.zzz_change(p_channel text, p_name text, p_expect jsonb, p_set jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT jsonb_build_object('channel', p_channel, 'filter', 'f444w', 'filename', p_name,
                            'expect', jsonb_build_object('value', p_expect),
                            'set', jsonb_build_object('value', p_set));
$$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_change(text, text, jsonb, jsonb) TO authenticated;

-- ---------------------------------------------------------------------------
-- 1. Admin-only
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF has_function_privilege('anon', 'public.nircam_review_apply(text, jsonb)', 'EXECUTE') THEN
    RAISE EXCEPTION 'anon can call nircam_review_apply';
  END IF;
END $$;

SET LOCAL ROLE authenticated;
SELECT pg_temp.zzz_as('00000000-0000-0000-0000-0000000f1b02');
DO $$
BEGIN
  BEGIN
    PERFORM public.nircam_review_apply('zzz_nr_field', jsonb_build_array(
      pg_temp.zzz_change('status', 'zzz_c', '"pending"', '"excluded"')));
    RAISE EXCEPTION 'a non-admin ran nircam_review_apply';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
END $$;
RESET ROLE;
DO $$
BEGIN
  IF (SELECT review_status FROM nircam_exposures WHERE field = 'zzz_nr_field' AND filename = 'zzz_c') <> 'pending' THEN
    RAISE EXCEPTION 'a non-admin write reached nircam_exposures';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2-4. The guarded write, as an admin
-- ---------------------------------------------------------------------------
SET LOCAL ROLE authenticated;
SELECT pg_temp.zzz_as('00000000-0000-0000-0000-0000000f1b01');

DO $$
DECLARE
  res jsonb;
  r   record;
BEGIN
  -- (a) one call: a mask edit whose expectation holds (jsonb equality: spelling of
  --     numbers does not matter), a status edit whose expectation is stale, one
  --     already in place, and one for an exposure that does not exist.
  res := public.nircam_review_apply('zzz_nr_field', jsonb_build_array(
    pg_temp.zzz_change('mask', 'zzz_a',
      '{"version": 1, "polygons": [{"id": "p", "vertices": [[1.0, 1], [5, 1], [5, 5.0]]}]}',
      '{"version": 1, "polygons": [{"id": "q", "vertices": [[2, 2], [9, 2], [9, 9]]}]}'),
    pg_temp.zzz_change('status', 'zzz_b', '"pending"', '"approved"'),
    pg_temp.zzz_change('status', 'zzz_c', '"approved"', '"pending"'),
    pg_temp.zzz_change('mask', 'zzz_nope', NULL, '{"polygons": []}')));
  IF (res ->> 'applied')::int <> 1 OR (res ->> 'unchanged')::int <> 1
     OR jsonb_array_length(res -> 'refused') <> 1 OR jsonb_array_length(res -> 'missing') <> 1 THEN
    RAISE EXCEPTION 'guarded write: expected 1 applied / 1 unchanged / 1 refused / 1 missing, got %', res;
  END IF;
  IF res -> 'refused' -> 0 ->> 'filename' <> 'zzz_b' OR res -> 'refused' -> 0 -> 'current' <> '"excluded"'::jsonb THEN
    RAISE EXCEPTION 'guarded write: the refusal must name the exposure and its current value, got %', res -> 'refused';
  END IF;
  IF res -> 'missing' -> 0 ->> 'filename' <> 'zzz_nope' THEN
    RAISE EXCEPTION 'guarded write: the missing exposure must be named, got %', res -> 'missing';
  END IF;
  SELECT * INTO r FROM nircam_exposures WHERE field = 'zzz_nr_field' AND filename = 'zzz_a';
  IF r.mask_regions -> 'polygons' -> 0 ->> 'id' <> 'q' THEN
    RAISE EXCEPTION 'guarded write: the matching mask edit did not land: %', r.mask_regions;
  END IF;
  IF (SELECT review_status FROM nircam_exposures WHERE field = 'zzz_nr_field' AND filename = 'zzz_b') <> 'excluded' THEN
    RAISE EXCEPTION 'guarded write: a stale status edit overwrote the row';
  END IF;

  -- (b) a status write stamps review_decided_at; clearing a mask NULLs it.
  res := public.nircam_review_apply('zzz_nr_field', jsonb_build_array(
    pg_temp.zzz_change('status', 'zzz_b', '"excluded"', '"pending"'),
    pg_temp.zzz_change('mask', 'zzz_a',
      '{"version": 1, "polygons": [{"id": "q", "vertices": [[2, 2], [9, 2], [9, 9]]}]}', NULL)));
  IF (res ->> 'applied')::int <> 2 THEN
    RAISE EXCEPTION 'guarded write: expected 2 applied, got %', res;
  END IF;
  SELECT * INTO r FROM nircam_exposures WHERE field = 'zzz_nr_field' AND filename = 'zzz_b';
  IF r.review_status <> 'pending' OR r.review_decided_at IS NULL THEN
    RAISE EXCEPTION 'a status write must set the status and stamp review_decided_at: % %', r.review_status, r.review_decided_at;
  END IF;
  IF (SELECT mask_regions FROM nircam_exposures WHERE field = 'zzz_nr_field' AND filename = 'zzz_a') IS NOT NULL THEN
    RAISE EXCEPTION 'clearing a mask must NULL mask_regions';
  END IF;

  -- (c) malformed changes are rejected, and the whole call with them.
  BEGIN
    PERFORM public.nircam_review_apply('zzz_nr_field', jsonb_build_array(
      pg_temp.zzz_change('status', 'zzz_c', '"pending"', '"excluded"'),
      pg_temp.zzz_change('status', 'zzz_a', '"pending"', '"rejected"')));
    RAISE EXCEPTION 'an unknown status was accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL;
  END;
  IF (SELECT review_status FROM nircam_exposures WHERE field = 'zzz_nr_field' AND filename = 'zzz_c') <> 'pending' THEN
    RAISE EXCEPTION 'a rejected call left a partial write';
  END IF;
  BEGIN
    PERFORM public.nircam_review_apply('zzz_nr_field', jsonb_build_array(
      pg_temp.zzz_change('mask', 'zzz_c', NULL, '[[1, 1], [2, 2], [3, 3]]')));
    RAISE EXCEPTION 'a mask that is not {"polygons": [...]} was accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL;
  END;
  BEGIN
    PERFORM public.nircam_review_apply('zzz_nr_field', '[{"channel": "notes", "filter": "f444w", "filename": "zzz_c"}]');
    RAISE EXCEPTION 'an unknown channel was accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL;
  END;
  BEGIN
    PERFORM public.nircam_review_apply('zzz_nr_field', '{"channel": "mask"}');
    RAISE EXCEPTION 'a non-array p_changes was accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL;
  END;

  RAISE NOTICE 'OK: nircam_review_apply is admin-only and never overwrites a column changed since it was read.';
END $$;

RESET ROLE;
ROLLBACK;
