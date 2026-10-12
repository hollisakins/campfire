-- Guard: archive registry rows (data releases P2, root = 'archive') are
-- readable by EXACTLY the principals who may read the live spectrum they were
-- retained from -- and the two API-path filter functions agree with the policy.
--
-- The retain step copies a superseded NIRSpec final / sidecar to
-- archive/<sha256>/<relpath> and inserts a row with the live row's program_slug,
-- observation and spectrum_id. The policy (select_storage_objects_by_access),
-- filter_accessible_storage_keys and filter_link_storage_keys each carry an
-- archive branch: program accessible (row-local program_slug) AND the live
-- spectrum published (draft for a drafts link), links narrowed on the row's own
-- observation, 'collected' rows (no bytes) never, mosaic freeze copies (no
-- spectrum_id) admin-only until P4. Three restatements of one rule is exactly
-- where one drifts, so every branch is asserted against a fixed expected set
-- per principal: anon, user without grants, grantee, admin, and share links in
-- and out of scope.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_archive_authz.sql
BEGIN;

-- ---------------------------------------------------------------------------
-- Fixtures (as the owner: RLS bypassed)
-- ---------------------------------------------------------------------------
INSERT INTO programs (slug, program_name, is_public) VALUES
  ('zzz_aa_pub', 'Archive authz public program', true),
  ('zzz_aa_prv', 'Archive authz private program', false);

INSERT INTO fields (name) VALUES ('zzz_aa_field');

INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
  ('zzz_aa_obs_pub',  'zzz_aa_pub', 9301, 'zzz_aa_field'),
  ('zzz_aa_obs_pub2', 'zzz_aa_pub', 9302, 'zzz_aa_field'),
  ('zzz_aa_obs_prv',  'zzz_aa_prv', 9303, 'zzz_aa_field');

INSERT INTO auth.users (id, email) VALUES
  ('00000000-0000-0000-0000-00000000aa01', 'zzz-aa-admin@test.invalid'),
  ('00000000-0000-0000-0000-00000000aa02', 'zzz-aa-nogrant@test.invalid'),
  ('00000000-0000-0000-0000-00000000aa03', 'zzz-aa-grantee@test.invalid'),
  ('00000000-0000-0000-0000-00000000aa11', 'zzz-aa-link-obs@test.invalid'),
  ('00000000-0000-0000-0000-00000000aa12', 'zzz-aa-link-nodl@test.invalid'),
  ('00000000-0000-0000-0000-00000000aa13', 'zzz-aa-link-drafts@test.invalid');

INSERT INTO user_profiles (user_id, username, full_name, is_admin, is_link_account, can_comment, can_inspect) VALUES
  ('00000000-0000-0000-0000-00000000aa01', 'zzz-aa-admin',   'ZZZ AA Admin',   true,  false, true,  true),
  ('00000000-0000-0000-0000-00000000aa02', 'zzz-aa-nogrant', 'ZZZ AA NoGrant', false, false, true,  false),
  ('00000000-0000-0000-0000-00000000aa03', 'zzz-aa-grantee', 'ZZZ AA Grantee', false, false, true,  false),
  ('00000000-0000-0000-0000-00000000aa11', 'zzz-aa-l-obs',   'ZZZ AA Link',    false, true,  false, false),
  ('00000000-0000-0000-0000-00000000aa12', 'zzz-aa-l-nodl',  'ZZZ AA NoDL',    false, true,  false, false),
  ('00000000-0000-0000-0000-00000000aa13', 'zzz-aa-l-drf',   'ZZZ AA Drafts',  false, true,  false, false);

INSERT INTO user_program_access (user_id, program_slug) VALUES
  ('00000000-0000-0000-0000-00000000aa03', 'zzz_aa_prv');

INSERT INTO share_links (token, label, observation, field, link_user_id, link_password,
                         include_drafts, allow_download, created_by, revoked_at) VALUES
  ('zzz_aa_tok_obs',  'obs link',    'zzz_aa_obs_pub', NULL, '00000000-0000-0000-0000-00000000aa11', 'x', false, true,  '00000000-0000-0000-0000-00000000aa01', NULL),
  ('zzz_aa_tok_nodl', 'no-dl link',  'zzz_aa_obs_pub', NULL, '00000000-0000-0000-0000-00000000aa12', 'x', false, false, '00000000-0000-0000-0000-00000000aa01', NULL),
  ('zzz_aa_tok_drf',  'drafts link', 'zzz_aa_obs_pub', NULL, '00000000-0000-0000-0000-00000000aa13', 'x', true,  true,  '00000000-0000-0000-0000-00000000aa01', NULL);

-- One target + spectrum per case.
INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, has_published_spectrum) VALUES
  ('zzz-aa-pub',     'zzz_aa_field', 150.00, 2.0, 'zzz_aa_pub', 'zzz_aa_obs_pub',  true),
  ('zzz-aa-pub2',    'zzz_aa_field', 150.01, 2.0, 'zzz_aa_pub', 'zzz_aa_obs_pub2', true),
  ('zzz-aa-draft',   'zzz_aa_field', 150.02, 2.0, 'zzz_aa_pub', 'zzz_aa_obs_pub',  false),
  ('zzz-aa-revoked', 'zzz_aa_field', 150.03, 2.0, 'zzz_aa_pub', 'zzz_aa_obs_pub',  false),
  ('zzz-aa-prv',     'zzz_aa_field', 150.04, 2.0, 'zzz_aa_prv', 'zzz_aa_obs_prv',  true),
  ('zzz-aa-coll',    'zzz_aa_field', 150.05, 2.0, 'zzz_aa_pub', 'zzz_aa_obs_pub',  true);

INSERT INTO spectra (target_id, grating, fits_path, deploy_status) VALUES
  ('zzz-aa-pub',     'PRISM', '/tmp/zzz_aa_pub_spec.fits',     'published'),
  ('zzz-aa-pub2',    'PRISM', '/tmp/zzz_aa_pub2_spec.fits',    'published'),
  ('zzz-aa-draft',   'PRISM', '/tmp/zzz_aa_draft_spec.fits',   'draft'),
  ('zzz-aa-revoked', 'PRISM', '/tmp/zzz_aa_revoked_spec.fits', 'revoked'),
  ('zzz-aa-prv',     'PRISM', '/tmp/zzz_aa_prv_spec.fits',     'published'),
  ('zzz-aa-coll',    'PRISM', '/tmp/zzz_aa_coll_spec.fits',    'published');

-- Live rows (the pre-P2 behaviour must survive the policy restructure).
INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type,
                             product_type, instrument, status, root, observation, program_slug, spectrum_id)
SELECT 'osn', 'data', 'zzz_aa/live/' || s.target_id, 'sha256:' || md5('live' || s.target_id), 1,
       'application/fits', 'nirspec_spec', 'nirspec', 'active', 'live',
       s.observation, s.program_slug, s.spectrum_id
FROM spectra s WHERE s.target_id LIKE 'zzz-aa-%';

-- Archive rows: what retain-on-supersede inserts (row-local scope copied from
-- the live row), one per case, named by case so the assertions read.
INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type,
                             product_type, instrument, status, root, observation, program_slug,
                             spectrum_id, archived_from_key, archived_at, collected_at)
SELECT 'osn', 'data',
       'archive/' || md5('arch' || s.target_id) || md5(s.target_id) || '/products/nirspec/' || s.observation || '/' || s.target_id || '_spec.fits',
       'sha256:' || md5('arch' || s.target_id) || md5(s.target_id), 1,
       'application/fits', 'nirspec_spec', 'nirspec',
       CASE WHEN s.target_id = 'zzz-aa-coll' THEN 'collected' ELSE 'archived' END, 'archive',
       s.observation, s.program_slug, s.spectrum_id,
       'zzz_aa/live/' || s.target_id, now() - interval '1 day',
       CASE WHEN s.target_id = 'zzz-aa-coll' THEN now() END
FROM spectra s WHERE s.target_id LIKE 'zzz-aa-%';

-- A mosaic freeze copy: no spectrum_id, no program (P4 will scope these).
INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type,
                             product_type, instrument, status, root, field, archived_at)
VALUES ('osn', 'data', 'archive/' || repeat('9', 64) || '/products/nircam/zzz_aa_field/zzz_aa_mosaic.fits.gz',
        'sha256:' || repeat('9', 64), 1, 'application/fits', 'nircam_mosaic', 'nircam',
        'archived', 'archive', 'zzz_aa_field', now());

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION pg_temp.zzz_as(p_uid text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', 'authenticated')::text, true);
END $$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_as(text) TO authenticated;

-- A fixture key -> its case name ('pub', 'draft', ..., 'mosaic').
CREATE OR REPLACE FUNCTION pg_temp.zzz_case(p_key text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN p_key LIKE '%nircam%' THEN 'mosaic'
              WHEN p_key LIKE 'zzz_aa/live/%' THEN regexp_replace(p_key, '^zzz_aa/live/zzz-aa-', '')
              ELSE regexp_replace(p_key, '^.*/zzz-aa-([a-z0-9]+)_spec\.fits$', '\1') END;
$$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_case(text) TO authenticated;

-- The archive rows the current principal can read, as sorted case names.
CREATE OR REPLACE FUNCTION pg_temp.zzz_archive_cases() RETURNS text[]
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(array_agg(c ORDER BY c), '{}')
  FROM (SELECT pg_temp.zzz_case(storage_key) AS c
        FROM storage_objects
        WHERE root = 'archive' AND (storage_key LIKE '%zzz-aa-%' OR storage_key LIKE '%zzz_aa_%')) x;
$$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_archive_cases() TO authenticated;

-- The live rows the current principal can read, likewise.
CREATE OR REPLACE FUNCTION pg_temp.zzz_live_cases() RETURNS text[]
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(array_agg(c ORDER BY c), '{}')
  FROM (SELECT pg_temp.zzz_case(storage_key) AS c
        FROM storage_objects
        WHERE root = 'live' AND storage_key LIKE 'zzz_aa/live/%') x;
$$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_live_cases() TO authenticated;

CREATE OR REPLACE FUNCTION pg_temp.zzz_expect(p_who text, p_got text[], p_want text[]) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF p_got IS DISTINCT FROM p_want THEN
    RAISE EXCEPTION '%: expected %, got %', p_who, p_want, p_got;
  END IF;
END $$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_expect(text, text[], text[]) TO authenticated;

-- ---------------------------------------------------------------------------
-- 1. anon: no SELECT privilege on the registry at all.
-- ---------------------------------------------------------------------------
SET LOCAL ROLE anon;
DO $$
BEGIN
  BEGIN
    PERFORM count(*) FROM storage_objects;
    RAISE EXCEPTION 'anon can read storage_objects';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END $$;
RESET ROLE;

-- ---------------------------------------------------------------------------
-- 2. Policy, per principal.
-- ---------------------------------------------------------------------------
SET LOCAL ROLE authenticated;
DO $$
BEGIN
  -- user without grants: public program, published parents only; the row of
  -- the second observation is visible too (users are program-scoped, not
  -- observation-scoped); never draft / revoked / collected / private / mosaic
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-00000000aa02');
  PERFORM pg_temp.zzz_expect('user without grants (archive)', pg_temp.zzz_archive_cases(), ARRAY['pub', 'pub2']);
  PERFORM pg_temp.zzz_expect('user without grants (live)', pg_temp.zzz_live_cases(), ARRAY['coll', 'pub', 'pub2']);

  -- grantee of the private program: plus the private row
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-00000000aa03');
  PERFORM pg_temp.zzz_expect('grantee (archive)', pg_temp.zzz_archive_cases(), ARRAY['prv', 'pub', 'pub2']);
  PERFORM pg_temp.zzz_expect('grantee (live)', pg_temp.zzz_live_cases(), ARRAY['coll', 'prv', 'pub', 'pub2']);

  -- admin: everything, the collected row and the mosaic copy included
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-00000000aa01');
  PERFORM pg_temp.zzz_expect('admin (archive)', pg_temp.zzz_archive_cases(),
                             ARRAY['coll', 'draft', 'mosaic', 'prv', 'pub', 'pub2', 'revoked']);

  -- observation link, downloads on: its observation only (pub2 is the same
  -- program, other observation: hidden)
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-00000000aa11');
  PERFORM pg_temp.zzz_expect('obs link (archive)', pg_temp.zzz_archive_cases(), ARRAY['pub']);
  PERFORM pg_temp.zzz_expect('obs link (live)', pg_temp.zzz_live_cases(), ARRAY['coll', 'pub']);

  -- the same link with downloads off: nothing in the registry at all
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-00000000aa12');
  PERFORM pg_temp.zzz_expect('no-download link (archive)', pg_temp.zzz_archive_cases(), '{}');
  PERFORM pg_temp.zzz_expect('no-download link (live)', pg_temp.zzz_live_cases(), '{}');

  -- a link that sees drafts: the draft parent's archive row too, never revoked
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-00000000aa13');
  PERFORM pg_temp.zzz_expect('drafts link (archive)', pg_temp.zzz_archive_cases(), ARRAY['draft', 'pub']);
END $$;
RESET ROLE;

-- ---------------------------------------------------------------------------
-- 3. The API-path functions (called under service_role, RLS off) give the
--    same answers as the policy for the matching principal.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION pg_temp.zzz_cases_of(p_keys text[]) RETURNS text[]
LANGUAGE sql IMMUTABLE AS $$
  SELECT COALESCE(array_agg(c ORDER BY c), '{}')
  FROM (SELECT pg_temp.zzz_case(k) AS c FROM unnest(p_keys) k) x;
$$;

DO $$
DECLARE
  all_keys text[] := ARRAY(SELECT storage_key FROM storage_objects
                           WHERE root = 'archive' AND (storage_key LIKE '%zzz-aa-%' OR storage_key LIKE '%zzz_aa_%'));
BEGIN
  -- user without grants ≙ public programs only
  PERFORM pg_temp.zzz_expect('filter_accessible_storage_keys (public programs)',
    pg_temp.zzz_cases_of(ARRAY(SELECT k FROM public.filter_accessible_storage_keys(all_keys, ARRAY['zzz_aa_pub'], false) k)),
    ARRAY['pub', 'pub2']);

  -- grantee ≙ both programs
  PERFORM pg_temp.zzz_expect('filter_accessible_storage_keys (grantee)',
    pg_temp.zzz_cases_of(ARRAY(SELECT k FROM public.filter_accessible_storage_keys(all_keys, ARRAY['zzz_aa_pub', 'zzz_aa_prv'], false) k)),
    ARRAY['prv', 'pub', 'pub2']);

  -- admin / service caller (p_include_unpublished): every ARCHIVED row, the
  -- mosaic copy included, but never a collected one (no bytes to presign)
  PERFORM pg_temp.zzz_expect('filter_accessible_storage_keys (include_unpublished)',
    pg_temp.zzz_cases_of(ARRAY(SELECT k FROM public.filter_accessible_storage_keys(all_keys, '{}'::text[], true) k)),
    ARRAY['draft', 'mosaic', 'prv', 'pub', 'pub2', 'revoked']);

  -- a program list that does not include the row's program: nothing
  PERFORM pg_temp.zzz_expect('filter_accessible_storage_keys (wrong program)',
    pg_temp.zzz_cases_of(ARRAY(SELECT k FROM public.filter_accessible_storage_keys(all_keys, ARRAY['zzz_aa_prv'], false) k
                               WHERE k LIKE '%zzz-aa-pub%')),
    '{}');

  -- observation link, published only
  PERFORM pg_temp.zzz_expect('filter_link_storage_keys (obs link)',
    pg_temp.zzz_cases_of(ARRAY(SELECT k FROM public.filter_link_storage_keys(all_keys, ARRAY['zzz_aa_pub'], 'zzz_aa_obs_pub', NULL, false) k)),
    ARRAY['pub']);

  -- drafts link
  PERFORM pg_temp.zzz_expect('filter_link_storage_keys (drafts link)',
    pg_temp.zzz_cases_of(ARRAY(SELECT k FROM public.filter_link_storage_keys(all_keys, ARRAY['zzz_aa_pub'], 'zzz_aa_obs_pub', NULL, true) k)),
    ARRAY['draft', 'pub']);

  -- a field link never reaches a NIRSpec archive row
  PERFORM pg_temp.zzz_expect('filter_link_storage_keys (field link)',
    pg_temp.zzz_cases_of(ARRAY(SELECT k FROM public.filter_link_storage_keys(all_keys, '{}'::text[], NULL, 'zzz_aa_field', true) k)),
    '{}');

  RAISE NOTICE 'OK: archive rows are readable by exactly the readers of their live spectrum; policy and filter functions agree.';
END $$;

ROLLBACK;
