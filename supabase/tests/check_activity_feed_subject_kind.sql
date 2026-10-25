-- Guard: get_activity_feed labels every row's subject kind and keeps
-- object-parented comments.
--
-- The admin activity page links each row by subject_kind: 'object' rows to
-- /nirspec/objects/<id>, 'target' rows to the /nirspec/targets/<id> shim,
-- 'spectrum' rows (display "target/GRATING") as text. Before subject_kind the
-- page sent every row to the targets route, which 404s for everything written
-- after Phase D. Comments were also INNER JOINed to targets, so comments on an
-- object never reached the feed.
--
-- Fixtures build their own program; the actor is the seed admin
-- (11111111-...), present after every `db reset`.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_activity_feed_subject_kind.sql
BEGIN;

INSERT INTO programs (slug, program_name, is_public) VALUES ('zzz_af_pub', 'AF pub', true)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
  ('zzz_af_obs', 'zzz_af_pub', 9402, 'zzz_af_field');

INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum) VALUES
  ('ZZZ-AF-O', 'zzz_af_field', 151.00, 2.0, '{zzz_af_pub}', '{zzz_af_obs}', true);

INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, has_published_spectrum, object_id) VALUES
  ('zzz-af-t', 'zzz_af_field', 151.00, 2.0, 'zzz_af_pub', 'zzz_af_obs', true,
   (SELECT id FROM objects WHERE object_id = 'ZZZ-AF-O'));

INSERT INTO spectra (target_id, grating, fits_path, deploy_status) VALUES
  ('zzz-af-t', 'PRISM', 'zzz/af/t_prism.fits', 'published');

-- Comments: one per parent kind, one deleted, one orphaned (no parent at all).
INSERT INTO comments (target_id, object_id, user_id, content, created_at) VALUES
  ((SELECT id FROM targets WHERE target_id = 'zzz-af-t'), NULL,
   '11111111-1111-1111-1111-111111111111', 'zzz-af target comment', now() AT TIME ZONE 'utc'),
  (NULL, (SELECT id FROM objects WHERE object_id = 'ZZZ-AF-O'),
   '11111111-1111-1111-1111-111111111111', 'zzz-af object comment', now() AT TIME ZONE 'utc'),
  (NULL, NULL,
   '11111111-1111-1111-1111-111111111111', 'zzz-af orphan comment', now() AT TIME ZONE 'utc');
INSERT INTO comments (target_id, object_id, user_id, content, created_at, is_deleted) VALUES
  (NULL, (SELECT id FROM objects WHERE object_id = 'ZZZ-AF-O'),
   '11111111-1111-1111-1111-111111111111', 'zzz-af deleted comment', now() AT TIME ZONE 'utc', true);

-- Inspection rows: one per subject kind.
INSERT INTO flag_audit_log (target_id, object_id, spectrum_id, user_id, field_name, old_value, new_value, changed_at) VALUES
  ((SELECT id FROM targets WHERE target_id = 'zzz-af-t'), NULL, NULL,
   '11111111-1111-1111-1111-111111111111', 'zzz_af_flag', 0, 1, now() AT TIME ZONE 'utc'),
  (NULL, (SELECT id FROM objects WHERE object_id = 'ZZZ-AF-O'), NULL,
   '11111111-1111-1111-1111-111111111111', 'zzz_af_flag', 0, 2, now() AT TIME ZONE 'utc'),
  (NULL, NULL, (SELECT id FROM spectra WHERE target_id = 'zzz-af-t' AND grating = 'PRISM'),
   '11111111-1111-1111-1111-111111111111', 'zzz_af_flag', 0, 3, now() AT TIME ZONE 'utc');

-- Read as the seed admin.
SET LOCAL role authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub', '11111111-1111-1111-1111-111111111111', 'role', 'authenticated')::text, true);

CREATE TEMP TABLE af_feed AS
SELECT * FROM public.get_activity_feed(true, true, NULL, false, NULL, 1, 10000)
WHERE content LIKE 'zzz-af %' OR field_name = 'zzz_af_flag';

DO $$
DECLARE
  r record;
  n int;
BEGIN
  -- 1. the object-parented comment reaches the feed, labelled 'object'.
  SELECT * INTO r FROM af_feed WHERE content = 'zzz-af object comment';
  IF r.id IS NULL THEN RAISE EXCEPTION 'object comment missing from the feed'; END IF;
  IF r.subject_kind <> 'object' OR r.target_display_id <> 'ZZZ-AF-O' THEN
    RAISE EXCEPTION 'object comment labelled (%, %)', r.subject_kind, r.target_display_id;
  END IF;

  -- 2. the target comment is labelled 'target'.
  SELECT * INTO r FROM af_feed WHERE content = 'zzz-af target comment';
  IF r.id IS NULL OR r.subject_kind <> 'target' OR r.target_display_id <> 'zzz-af-t' THEN
    RAISE EXCEPTION 'target comment labelled (%, %)', r.subject_kind, r.target_display_id;
  END IF;

  -- 3. orphaned and deleted comments stay out.
  SELECT count(*) INTO n FROM af_feed WHERE content IN ('zzz-af orphan comment', 'zzz-af deleted comment');
  IF n <> 0 THEN RAISE EXCEPTION '% orphan/deleted comment(s) leaked into the feed', n; END IF;

  -- 4. inspection rows: kind follows the subject column; a spectrum displays as target/GRATING.
  SELECT * INTO r FROM af_feed WHERE type = 'inspection' AND new_value = 1;
  IF r.subject_kind <> 'target' OR r.target_display_id <> 'zzz-af-t' THEN
    RAISE EXCEPTION 'target inspection labelled (%, %)', r.subject_kind, r.target_display_id;
  END IF;
  SELECT * INTO r FROM af_feed WHERE type = 'inspection' AND new_value = 2;
  IF r.subject_kind <> 'object' OR r.target_display_id <> 'ZZZ-AF-O' THEN
    RAISE EXCEPTION 'object inspection labelled (%, %)', r.subject_kind, r.target_display_id;
  END IF;
  SELECT * INTO r FROM af_feed WHERE type = 'inspection' AND new_value = 3;
  IF r.subject_kind <> 'spectrum' OR r.target_display_id <> 'zzz-af-t/PRISM' THEN
    RAISE EXCEPTION 'spectrum inspection labelled (%, %)', r.subject_kind, r.target_display_id;
  END IF;

  -- 5. every fixture row carries a kind (no NULLs for a row with a subject).
  SELECT count(*) INTO n FROM af_feed WHERE subject_kind IS NULL;
  IF n <> 0 THEN RAISE EXCEPTION '% feed row(s) without subject_kind', n; END IF;

  RAISE NOTICE 'check_activity_feed_subject_kind: ok';
END $$;

-- 6. a non-admin is refused.
SELECT set_config('request.jwt.claims',
  json_build_object('sub', '22222222-2222-2222-2222-222222222222', 'role', 'authenticated')::text, true);
DO $$
BEGIN
  PERFORM * FROM public.get_activity_feed(true, true, NULL, false, NULL, 1, 10);
  RAISE EXCEPTION 'non-admin read the activity feed';
EXCEPTION WHEN insufficient_privilege OR raise_exception THEN
  IF SQLERRM = 'non-admin read the activity feed' THEN RAISE; END IF;
END $$;

ROLLBACK;
