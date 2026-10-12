-- Guard: the needs-review rule (docs/design-needs-review.md, data releases P3).
--
-- An object signed off at quality 1-3 needs review iff its published member
-- spectrum set differs from the set the inspector saw at sign-off. Quality 4
-- never badges, reprocessing never badges, a draft spectrum is not "new data",
-- and every sign-off path re-stamps the reviewed set through the trigger.
--
-- Seed-independent: builds its own field with two objects.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_needs_review.sql
BEGIN;

INSERT INTO programs (slug, program_name, is_public) VALUES ('zzz_nr_pub', 'NR pub', true)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
  ('zzz_nr_obs', 'zzz_nr_pub', 9401, 'zzz_nr_field');

INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum) VALUES
  ('ZZZ-NR-A', 'zzz_nr_field', 150.00, 2.0, '{zzz_nr_pub}', '{zzz_nr_obs}', true),
  ('ZZZ-NR-B', 'zzz_nr_field', 150.10, 2.0, '{zzz_nr_pub}', '{zzz_nr_obs}', true);

INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, has_published_spectrum, object_id) VALUES
  ('zzz-nr-a1', 'zzz_nr_field', 150.00, 2.0, 'zzz_nr_pub', 'zzz_nr_obs', true, (SELECT id FROM objects WHERE object_id = 'ZZZ-NR-A')),
  ('zzz-nr-a2', 'zzz_nr_field', 150.00, 2.0, 'zzz_nr_pub', 'zzz_nr_obs', true, (SELECT id FROM objects WHERE object_id = 'ZZZ-NR-A')),
  ('zzz-nr-b1', 'zzz_nr_field', 150.10, 2.0, 'zzz_nr_pub', 'zzz_nr_obs', true, (SELECT id FROM objects WHERE object_id = 'ZZZ-NR-B'));

INSERT INTO spectra (target_id, grating, fits_path, deploy_status) VALUES
  ('zzz-nr-a1', 'PRISM', '/tmp/zzz_nr_a1_prism.fits', 'published'),
  ('zzz-nr-a2', 'PRISM', '/tmp/zzz_nr_a2_prism.fits', 'published'),
  ('zzz-nr-b1', 'PRISM', '/tmp/zzz_nr_b1_prism.fits', 'published');

CREATE TEMP TABLE _nr AS
SELECT id AS a FROM objects WHERE object_id = 'ZZZ-NR-A';
ALTER TABLE _nr ADD COLUMN b integer;
UPDATE _nr SET b = (SELECT id FROM objects WHERE object_id = 'ZZZ-NR-B');

CREATE OR REPLACE FUNCTION pg_temp.nr_flag(p_oid text) RETURNS boolean LANGUAGE sql AS
$$ SELECT needs_review FROM objects WHERE object_id = p_oid $$;
CREATE OR REPLACE FUNCTION pg_temp.nr_reviewed(p_oid text) RETURNS integer[] LANGUAGE sql AS
$$ SELECT reviewed_spectrum_ids FROM objects WHERE object_id = p_oid $$;
CREATE OR REPLACE FUNCTION pg_temp.nr_signoff(p_oid text, p_quality integer) RETURNS void LANGUAGE sql AS
$$ UPDATE objects SET redshift_quality = p_quality, last_inspected_at = clock_timestamp() WHERE object_id = p_oid $$;

DO $$
DECLARE
  a integer := (SELECT a FROM _nr);
  b integer := (SELECT b FROM _nr);
  new_sid integer;
BEGIN
  -- 0. Never signed off: no badge, no reviewed set.
  IF pg_temp.nr_flag('ZZZ-NR-A') OR pg_temp.nr_reviewed('ZZZ-NR-A') IS NOT NULL THEN
    RAISE EXCEPTION 'uninspected object must carry neither a badge nor a reviewed set';
  END IF;

  -- 1. Sign-off at quality 3 stamps the two published members and clears the badge.
  PERFORM pg_temp.nr_signoff('ZZZ-NR-A', 3);
  IF cardinality(pg_temp.nr_reviewed('ZZZ-NR-A')) <> 2 THEN
    RAISE EXCEPTION 'sign-off must stamp the published member ids, got %', pg_temp.nr_reviewed('ZZZ-NR-A');
  END IF;
  IF pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'fresh sign-off must not badge'; END IF;

  -- 2. A draft spectrum is not new data.
  INSERT INTO spectra (target_id, grating, fits_path, deploy_status)
  VALUES ('zzz-nr-a1', 'G395M', '/tmp/zzz_nr_a1_g395m.fits', 'draft') RETURNING id INTO new_sid;
  IF pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'a draft spectrum must not badge'; END IF;

  -- 3. Publishing it is: the inspector never saw this spectrum.
  UPDATE spectra SET deploy_status = 'published' WHERE id = new_sid;
  IF NOT pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'a newly published member spectrum must badge a q3 object'; END IF;

  -- 4. Reprocessing (same spectrum, new bytes) is not a review need.
  PERFORM pg_temp.nr_signoff('ZZZ-NR-A', 3);
  UPDATE spectra SET file_hash = 'sha256:' || repeat('a', 64), science_hash = 'sha256:' || repeat('b', 64)
  WHERE target_id = 'zzz-nr-a1';
  IF pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'reprocessing must not badge'; END IF;

  -- 5. Revoking a spectrum the inspector saw: membership changed, badge.
  UPDATE spectra SET deploy_status = 'revoked' WHERE id = new_sid;
  IF NOT pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'a revoked member spectrum must badge'; END IF;
  -- ...and publishing it again restores the reviewed set exactly.
  UPDATE spectra SET deploy_status = 'published' WHERE id = new_sid;
  IF pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'restoring the reviewed set must clear the badge'; END IF;

  -- 6. "Mark as reviewed" (last_inspected_at only) clears a badge.
  UPDATE spectra SET deploy_status = 'revoked' WHERE id = new_sid;
  IF NOT pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'precondition: badge expected'; END IF;
  UPDATE objects SET last_inspected_at = clock_timestamp() WHERE id = a;
  IF pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'mark as reviewed must clear the badge'; END IF;
  IF cardinality(pg_temp.nr_reviewed('ZZZ-NR-A')) <> 2 THEN
    RAISE EXCEPTION 'mark as reviewed must re-stamp the current set, got %', pg_temp.nr_reviewed('ZZZ-NR-A');
  END IF;

  -- 7. Quality 4 never badges, whatever happens to its members.
  PERFORM pg_temp.nr_signoff('ZZZ-NR-A', 4);
  UPDATE spectra SET deploy_status = 'published' WHERE id = new_sid;
  INSERT INTO spectra (target_id, grating, fits_path, deploy_status)
  VALUES ('zzz-nr-a2', 'G140M', '/tmp/zzz_nr_a2_g140m.fits', 'published');
  IF pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'quality 4 must never badge'; END IF;
  -- Downgrading to quality 2 is itself a sign-off: it re-stamps, so no badge yet...
  PERFORM pg_temp.nr_signoff('ZZZ-NR-A', 2);
  IF pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'a sign-off at q2 re-stamps and must not badge'; END IF;

  -- 8. Membership via targets: a target (with its published spectrum) moves from B to A.
  PERFORM pg_temp.nr_signoff('ZZZ-NR-B', 1);        -- Impossible, one member
  UPDATE targets SET object_id = a WHERE target_id = 'zzz-nr-b1';
  IF NOT pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'a target joining must badge the receiving q2 object'; END IF;
  IF NOT pg_temp.nr_flag('ZZZ-NR-B') THEN RAISE EXCEPTION 'a target leaving must badge the losing q1 object'; END IF;

  -- 9. An inactive (soft-deleted) object never badges.
  UPDATE objects SET is_active = false WHERE id = b;
  IF pg_temp.nr_flag('ZZZ-NR-B') THEN RAISE EXCEPTION 'an inactive object must not badge'; END IF;

  -- 10. Deleting a spectrum the inspector saw badges (statement DELETE trigger).
  PERFORM pg_temp.nr_signoff('ZZZ-NR-A', 3);
  DELETE FROM spectra WHERE id = new_sid;
  IF NOT pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'deleting a reviewed member spectrum must badge'; END IF;

  -- 11. Seeding: a NULL reviewed set is filled from the members that existed at
  --     sign-off (created_at <= last_inspected_at); one deployed later is
  --     "unseen" and badges. The fixture spectra were created at transaction
  --     start; the step-10 sign-off is a later clock_timestamp().
  UPDATE objects SET reviewed_spectrum_ids = NULL WHERE id = a;
  PERFORM public.recompute_needs_review(ARRAY[a]);
  IF pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'a NULL reviewed set must not badge'; END IF;
  UPDATE spectra SET created_at = clock_timestamp() WHERE target_id = 'zzz-nr-b1';  -- deployed after sign-off
  PERFORM set_config('request.jwt.claims', json_build_object('role', 'service_role')::text, true);
  IF (public.seed_reviewed_spectra(0, 10, ARRAY[a], true)->>'scanned')::int <> 1 THEN RAISE EXCEPTION 'seed dry run must count the pending object'; END IF;
  IF pg_temp.nr_reviewed('ZZZ-NR-A') IS NOT NULL THEN RAISE EXCEPTION 'seed dry run must write nothing'; END IF;
  IF (public.seed_reviewed_spectra(0, 10, ARRAY[a])->>'seeded')::int <> 1 THEN RAISE EXCEPTION 'seed must fill one object'; END IF;
  IF NOT pg_temp.nr_flag('ZZZ-NR-A') THEN RAISE EXCEPTION 'seed: a member deployed after sign-off must badge'; END IF;
  IF cardinality(pg_temp.nr_reviewed('ZZZ-NR-A')) <> 3 THEN
    RAISE EXCEPTION 'seed must stamp only the members that existed at sign-off (expected 3), got %', pg_temp.nr_reviewed('ZZZ-NR-A');
  END IF;
  -- Seeding is idempotent: a filled set is never overwritten.
  IF (public.seed_reviewed_spectra(0, 10, ARRAY[a])->>'last_id') IS NOT NULL THEN RAISE EXCEPTION 'seed must not revisit a filled set'; END IF;

  RAISE NOTICE 'OK: needs-review rule (quality 1-3 + membership change only), stamping, triggers and seeding.';
END $$;

-- A non-admin cannot run the seed.
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims', json_build_object('sub', '00000000-0000-0000-0000-00000000bb02', 'role', 'authenticated')::text, true);
DO $$
BEGIN
  BEGIN
    PERFORM public.seed_reviewed_spectra(0, 1);
    RAISE EXCEPTION 'seed_reviewed_spectra did not refuse a non-admin';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM NOT LIKE 'Access denied%' THEN RAISE; END IF;
  END;
END $$;
RESET ROLE;

ROLLBACK;
