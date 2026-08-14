-- Guard: a share-link account must see EXACTLY its own scope and nothing else.
--
-- Share links (docs/design-public-mirror.md) back each link with a synthetic
-- but fully real authenticated principal, so that every existing reader in the
-- portal works unchanged. The flip side is that a link account inherits every
-- policy written as "any logged-in user may read this" -- and a missed policy
-- is a silent successful read, not an error. The narrowing is ~20 hand-added
-- conjuncts across policies.sql; this asserts each one, because that is exactly
-- the kind of change where one gets forgotten.
--
-- Covers both scope axes (a NIRSpec observation and a NIRCam field), because
-- they narrow through different mechanisms: the observation axis rides
-- accessible_program_slugs() plus an observation match, while NIRCam has no
-- program gating at all and is held solely by the field name.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_share_link_scoping.sql
BEGIN;

-- ---------------------------------------------------------------------------
-- Fixtures (inserted as the owner, which bypasses RLS)
-- ---------------------------------------------------------------------------

-- Two programs. zzz_sl_open is PUBLIC: an ordinary user sees it via the
-- is_public union in accessible_program_slugs(), and a link account must not.
INSERT INTO programs (slug, program_name, is_public) VALUES
  ('zzz_sl_prog', 'Share Link Test Program', false),
  ('zzz_sl_open', 'Share Link Public Program', true);

-- Two observations in the SAME program: sharing one must not expose its
-- sibling. This is the case a program-scoped link could never express.
INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
  ('zzz_obs_shared',  'zzz_sl_prog', 9101, 'zzz_sl_field'),
  ('zzz_obs_sibling', 'zzz_sl_prog', 9102, 'zzz_sl_field'),
  ('zzz_obs_open',    'zzz_sl_open', 9103, 'zzz_sl_field');

INSERT INTO fields (name) VALUES ('zzz_sl_field'), ('zzz_sl_other');

INSERT INTO auth.users (id, email) VALUES
  ('00000000-0000-0000-0000-0000000000a1', 'zzz-admin@test.invalid'),
  ('00000000-0000-0000-0000-0000000000a2', 'zzz-user@test.invalid'),
  ('00000000-0000-0000-0000-0000000000b1', 'zzz-link-obs@test.invalid'),
  ('00000000-0000-0000-0000-0000000000b2', 'zzz-link-field@test.invalid'),
  ('00000000-0000-0000-0000-0000000000b3', 'zzz-link-drafts@test.invalid'),
  ('00000000-0000-0000-0000-0000000000b4', 'zzz-link-revoked@test.invalid'),
  ('00000000-0000-0000-0000-0000000000b5', 'zzz-link-nodl@test.invalid'),
  ('00000000-0000-0000-0000-0000000000b6', 'zzz-link-flddrf@test.invalid');

-- Link accounts must carry can_comment/can_inspect false; the
-- user_profiles_link_account_readonly CHECK enforces it (asserted below).
INSERT INTO user_profiles (user_id, username, full_name, is_admin, is_link_account, can_comment, can_inspect) VALUES
  ('00000000-0000-0000-0000-0000000000a1', 'zzz-admin', 'ZZZ Admin', true,  false, true,  true),
  ('00000000-0000-0000-0000-0000000000a2', 'zzz-user',  'ZZZ User',  false, false, true,  false),
  ('00000000-0000-0000-0000-0000000000b1', 'zzz-l-obs', 'ZZZ Link Obs',   false, true, false, false),
  ('00000000-0000-0000-0000-0000000000b2', 'zzz-l-fld', 'ZZZ Link Field', false, true, false, false),
  ('00000000-0000-0000-0000-0000000000b3', 'zzz-l-drf', 'ZZZ Link Draft', false, true, false, false),
  ('00000000-0000-0000-0000-0000000000b4', 'zzz-l-rev', 'ZZZ Link Revkd', false, true, false, false),
  ('00000000-0000-0000-0000-0000000000b5', 'zzz-l-ndl', 'ZZZ Link NoDL',  false, true, false, false),
  ('00000000-0000-0000-0000-0000000000b6', 'zzz-l-fdr', 'ZZZ Link FldDrf', false, true, false, false);

-- The ordinary user is granted the private program, so the ONLY difference
-- between them and the link accounts below is link-ness -- which is what makes
-- the "ordinary users are unaffected" assertions at the end meaningful.
INSERT INTO user_program_access (user_id, program_slug) VALUES
  ('00000000-0000-0000-0000-0000000000a2', 'zzz_sl_prog');

INSERT INTO share_links (token, label, observation, field, link_user_id, link_password,
                         include_drafts, allow_download, created_by, revoked_at) VALUES
  ('zzz_tok_obs',   'obs link',     'zzz_obs_shared', NULL,           '00000000-0000-0000-0000-0000000000b1', 'x', false, true,  '00000000-0000-0000-0000-0000000000a1', NULL),
  ('zzz_tok_field', 'field link',   NULL,             'zzz_sl_field', '00000000-0000-0000-0000-0000000000b2', 'x', false, true,  '00000000-0000-0000-0000-0000000000a1', NULL),
  ('zzz_tok_draft', 'drafts link',  'zzz_obs_shared', NULL,           '00000000-0000-0000-0000-0000000000b3', 'x', true,  true,  '00000000-0000-0000-0000-0000000000a1', NULL),
  ('zzz_tok_revkd', 'revoked link', 'zzz_obs_shared', NULL,           '00000000-0000-0000-0000-0000000000b4', 'x', false, true,  '00000000-0000-0000-0000-0000000000a1', now()),
  ('zzz_tok_nodl',  'no-dl link',   'zzz_obs_shared', NULL,           '00000000-0000-0000-0000-0000000000b5', 'x', false, false, '00000000-0000-0000-0000-0000000000a1', NULL),
  ('zzz_tok_flddr', 'fld+drf link', NULL,             'zzz_sl_field', '00000000-0000-0000-0000-0000000000b6', 'x', true,  true,  '00000000-0000-0000-0000-0000000000a1', NULL);

-- Objects: one per observation, plus a draft-only object in the shared scope.
INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum) VALUES
  ('ZZZ-OBJ-SHARED',  'zzz_sl_field', 150.0, 2.0, ARRAY['zzz_sl_prog'], ARRAY['zzz_obs_shared'],  true),
  ('ZZZ-OBJ-SIBLING', 'zzz_sl_field', 150.1, 2.1, ARRAY['zzz_sl_prog'], ARRAY['zzz_obs_sibling'], true),
  ('ZZZ-OBJ-OPEN',    'zzz_sl_field', 150.2, 2.2, ARRAY['zzz_sl_open'], ARRAY['zzz_obs_open'],    true),
  ('ZZZ-OBJ-DRAFT',   'zzz_sl_field', 150.3, 2.3, ARRAY['zzz_sl_prog'], ARRAY['zzz_obs_shared'],  false);

INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id, has_published_spectrum)
SELECT v.tid, 'zzz_sl_field', v.ra, 2.0, v.prog, v.obs, o.id, v.pub
FROM (VALUES
  ('zzz-t-shared',  150.0, 'zzz_sl_prog', 'zzz_obs_shared',  'ZZZ-OBJ-SHARED',  true),
  ('zzz-t-sibling', 150.1, 'zzz_sl_prog', 'zzz_obs_sibling', 'ZZZ-OBJ-SIBLING', true),
  ('zzz-t-open',    150.2, 'zzz_sl_open', 'zzz_obs_open',    'ZZZ-OBJ-OPEN',    true),
  ('zzz-t-draft',   150.3, 'zzz_sl_prog', 'zzz_obs_shared',  'ZZZ-OBJ-DRAFT',   false)
) AS v(tid, ra, prog, obs, oid, pub)
JOIN objects o ON o.object_id = v.oid;

-- spectrum_id (the natural key storage_objects joins on) is GENERATED from
-- fits_path: basename minus the _spec.fits suffix.
INSERT INTO spectra (target_id, grating, fits_path, deploy_status) VALUES
  ('zzz-t-shared',  'PRISM', '/tmp/zzz_shared_spec.fits',  'published'),
  ('zzz-t-sibling', 'PRISM', '/tmp/zzz_sibling_spec.fits', 'published'),
  ('zzz-t-open',    'PRISM', '/tmp/zzz_open_spec.fits',    'published'),
  ('zzz-t-draft',   'PRISM', '/tmp/zzz_draft_spec.fits',   'draft');

INSERT INTO object_photometry (object_id, field, ra, dec, catalog_name, photometry)
SELECT o.id, 'zzz_sl_field', o.ra, o.dec, 'zzz_cat', '{}'::jsonb FROM objects o
WHERE o.object_id IN ('ZZZ-OBJ-SHARED', 'ZZZ-OBJ-SIBLING', 'ZZZ-OBJ-OPEN');

-- NIRCam: a published mosaic in each field, plus a draft AND a revoked one in
-- the shared field -- include_drafts must reach the former and never the latter.
INSERT INTO nircam_images (field, tile, filter, pixel_scale, extension, file_path, deploy_status) VALUES
  ('zzz_sl_field', 'A1', 'F444W', '30mas', 'sci', '/tmp/zzz_shared_sci.fits', 'published'),
  ('zzz_sl_field', 'A2', 'F444W', '30mas', 'sci', '/tmp/zzz_shared_drf.fits', 'draft'),
  ('zzz_sl_field', 'A3', 'F444W', '30mas', 'sci', '/tmp/zzz_shared_rvk.fits', 'revoked'),
  ('zzz_sl_other', 'A1', 'F444W', '30mas', 'sci', '/tmp/zzz_other_sci.fits',  'published');

INSERT INTO map_layers (field, filter, tile_base_url, min_zoom, max_zoom,
                        ra_min, ra_max, dec_min, dec_max, wcs_params, image_width, image_height) VALUES
  ('zzz_sl_field', 'F444W', 'https://tiles.invalid/a', 0, 8, 149.0, 151.0, 1.0, 3.0, '{}'::jsonb, 1024, 1024),
  ('zzz_sl_other', 'F444W', 'https://tiles.invalid/b', 0, 8, 149.0, 151.0, 1.0, 3.0, '{}'::jsonb, 1024, 1024);

INSERT INTO deployments (id, observation, field, status) VALUES
  (990001, 'zzz_obs_shared',  NULL,           'published'),
  (990002, 'zzz_obs_sibling', NULL,           'published'),
  (990003, NULL,              'zzz_sl_field', 'published'),
  (990004, NULL,              'zzz_sl_other', 'published'),
  (990005, NULL,              'zzz_sl_field', 'revoked');

INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes,
                             content_type, product_type, status, spectrum_id, deployment_id)
SELECT 'osn', 'data', '/zzz/'||s.target_id, 'sha256:'||md5(s.target_id), 1, 'application/fits', 'nirspec_spec', 'active', s.spectrum_id, NULL
FROM spectra s WHERE s.target_id IN ('zzz-t-shared', 'zzz-t-sibling', 'zzz-t-open', 'zzz-t-draft');

INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes,
                             content_type, product_type, status, spectrum_id, deployment_id) VALUES
  ('osn', 'data', '/zzz/mosaic_shared', 'sha256:h1', 1, 'application/fits', 'nircam_mosaic', 'active', NULL, 990003),
  ('osn', 'data', '/zzz/mosaic_other',  'sha256:h2', 1, 'application/fits', 'nircam_mosaic', 'active', NULL, 990004);

INSERT INTO shutters (field, observation, object_id, source_id, center_ra, center_dec, position_angle, shutter_idx) VALUES
  ('zzz_sl_field', 'zzz_obs_shared',  'ZZZ-OBJ-SHARED',  1, 150.0, 2.0, 0, 0),
  ('zzz_sl_field', 'zzz_obs_sibling', 'ZZZ-OBJ-SIBLING', 2, 150.1, 2.1, 0, 0);

INSERT INTO slit_regions (field, observation, object_id, center_ra, center_dec, position_angle, shutter_idx) VALUES
  ('zzz_sl_field', 'zzz_obs_shared',  'ZZZ-OBJ-SHARED',  150.0, 2.0, 0, 0),
  ('zzz_sl_field', 'zzz_obs_sibling', 'ZZZ-OBJ-SIBLING', 150.1, 2.1, 0, 0);

-- Community artifacts: a link account must see none of these, in scope or not.
INSERT INTO object_lists (id, name, slug, created_by, visibility) VALUES
  (990001, 'ZZZ Public List', 'zzz-public-list', '00000000-0000-0000-0000-0000000000a2', 'public_read');

INSERT INTO object_list_members (list_id, ra, dec, object_id)
SELECT 990001, o.ra, o.dec, o.id FROM objects o WHERE o.object_id = 'ZZZ-OBJ-SHARED';

INSERT INTO list_audit_log (list_id, action, user_id) VALUES
  (990001, 'add', '00000000-0000-0000-0000-0000000000a2');

INSERT INTO comments (user_id, content, object_id)
SELECT '00000000-0000-0000-0000-0000000000a2', 'internal note', o.id
FROM objects o WHERE o.object_id = 'ZZZ-OBJ-SHARED';

INSERT INTO flag_audit_log (field_name, object_id, user_id)
SELECT 'redshift_quality', o.id, '00000000-0000-0000-0000-0000000000a2'
FROM objects o WHERE o.object_id = 'ZZZ-OBJ-SHARED';


-- ---------------------------------------------------------------------------
-- Assertions
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION pg_temp.zzz_as(p_uid text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', 'authenticated')::text, true);
END $$;

-- Counts every table a link account could read, under the current principal.
-- Returned as one row so each assertion block reads as a single comparison.
CREATE OR REPLACE FUNCTION pg_temp.zzz_visible() RETURNS jsonb
LANGUAGE sql STABLE AS $$
  SELECT jsonb_build_object(
    'profiles',    (SELECT count(*) FROM user_profiles),
    'programs',    (SELECT count(*) FROM programs        WHERE slug LIKE 'zzz%'),
    'observations',(SELECT count(*) FROM observations    WHERE name LIKE 'zzz%'),
    'targets',     (SELECT count(*) FROM targets         WHERE target_id LIKE 'zzz%'),
    'spectra',     (SELECT count(*) FROM spectra         WHERE target_id LIKE 'zzz%'),
    'objects',     (SELECT count(*) FROM objects         WHERE object_id LIKE 'ZZZ%'),
    'photometry',  (SELECT count(*) FROM object_photometry WHERE catalog_name = 'zzz_cat'),
    'nircam',      (SELECT count(*) FROM nircam_images   WHERE field LIKE 'zzz%'),
    'fields',      (SELECT count(*) FROM fields          WHERE name LIKE 'zzz%'),
    'map_layers',  (SELECT count(*) FROM map_layers      WHERE field LIKE 'zzz%'),
    'deployments', (SELECT count(*) FROM deployments     WHERE id BETWEEN 990001 AND 990005),
    'storage',     (SELECT count(*) FROM storage_objects WHERE storage_key LIKE '/zzz/%'),
    'shutters',    (SELECT count(*) FROM shutters        WHERE field LIKE 'zzz%'),
    'slits',       (SELECT count(*) FROM slit_regions    WHERE field LIKE 'zzz%'),
    'lists',       (SELECT count(*) FROM object_lists    WHERE slug LIKE 'zzz%'),
    'list_members',(SELECT count(*) FROM object_list_members WHERE list_id = 990001),
    'list_audit',  (SELECT count(*) FROM list_audit_log  WHERE list_id = 990001),
    'comments',    (SELECT count(*) FROM comments        WHERE content = 'internal note'),
    'flag_audit',  (SELECT count(*) FROM flag_audit_log  WHERE field_name = 'redshift_quality'
                      AND object_id IN (SELECT id FROM objects WHERE object_id LIKE 'ZZZ%')),
    'share_links', (SELECT count(*) FROM share_links     WHERE token LIKE 'zzz%')
  );
$$;

SET LOCAL ROLE authenticated;

DO $$
DECLARE
  v jsonb;
  v_expected jsonb;
BEGIN
  -- -------------------------------------------------------------------------
  -- 1) Observation-scoped link: exactly one observation's data, nothing else.
  -- -------------------------------------------------------------------------
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000000b1');
  v := pg_temp.zzz_visible();

  v_expected := jsonb_build_object(
    'profiles', 1,        -- itself only: NOT every CAMPFIRE user
    'programs', 1,        -- its own program; NOT the is_public one
    'observations', 1,    -- NOT its sibling in the same program
    'targets', 1, 'spectra', 1, 'objects', 1, 'photometry', 1,
    'nircam', 0, 'fields', 0, 'map_layers', 0,   -- no field axis for an obs link
    'deployments', 1,     -- its own scope's deployment only
    'storage', 1,         -- its own spectrum's FITS only
    'shutters', 1, 'slits', 1,
    'lists', 0, 'list_members', 0, 'list_audit', 0,
    'comments', 0, 'flag_audit', 0,
    'share_links', 0      -- must never read the table backing it
  );
  IF v <> v_expected THEN
    RAISE EXCEPTION 'observation-scoped link leaked. got % expected %', v, v_expected;
  END IF;

  -- -------------------------------------------------------------------------
  -- 2) Field-scoped link: NIRCam only. The program axis is empty for it, so
  --    every NIRSpec table must be zero -- including the ones with no program
  --    gate of their own (shutters, slit_regions), which are held solely by
  --    the observation conjunct.
  -- -------------------------------------------------------------------------
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000000b2');
  v := pg_temp.zzz_visible();

  v_expected := jsonb_build_object(
    'profiles', 1, 'programs', 0, 'observations', 0,
    'targets', 0, 'spectra', 0, 'objects', 0, 'photometry', 0,
    'nircam', 1,          -- the published mosaic of its field; NOT the draft,
                          -- NOT the other field's
    'fields', 1, 'map_layers', 1,
    'deployments', 1, 'storage', 1,
    'shutters', 0, 'slits', 0,
    'lists', 0, 'list_members', 0, 'list_audit', 0,
    'comments', 0, 'flag_audit', 0, 'share_links', 0
  );
  IF v <> v_expected THEN
    RAISE EXCEPTION 'field-scoped link leaked. got % expected %', v, v_expected;
  END IF;

  -- -------------------------------------------------------------------------
  -- 3) include_drafts link: the draft target/spectrum inside its scope becomes
  --    visible, and nothing outside the scope follows it in.
  -- -------------------------------------------------------------------------
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000000b3');
  v := pg_temp.zzz_visible();

  IF (v->>'targets')::int <> 2 OR (v->>'spectra')::int <> 2 OR (v->>'objects')::int <> 2 THEN
    RAISE EXCEPTION 'include_drafts link did not see in-scope drafts: %', v;
  END IF;
  IF (v->>'observations')::int <> 1 OR (v->>'programs')::int <> 1
     OR (v->>'comments')::int <> 0 OR (v->>'profiles')::int <> 1 THEN
    RAISE EXCEPTION 'include_drafts link widened beyond drafts: %', v;
  END IF;
  -- Draft opt-in must not reach a REVOKED row -- it relaxes the gate to draft,
  -- never past it.
  IF EXISTS (SELECT 1 FROM spectra WHERE target_id LIKE 'zzz%' AND deploy_status = 'revoked') THEN
    RAISE EXCEPTION 'include_drafts link saw a revoked spectrum';
  END IF;

  -- -------------------------------------------------------------------------
  -- 3b) The same on the field axis, where deploy_status is three-valued:
  --     include_drafts relaxes 'published' to 'draft' and must stop there. A
  --     revoked mosaic and a revoked deployment sit in this link's own field,
  --     which is exactly where an unscoped OR link_sees_drafts() would leak.
  -- -------------------------------------------------------------------------
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000000b6');
  v := pg_temp.zzz_visible();

  IF (v->>'nircam')::int <> 2 THEN
    RAISE EXCEPTION 'field+drafts link should see published+draft mosaics only: %', v;
  END IF;
  IF (v->>'deployments')::int <> 1 THEN
    RAISE EXCEPTION 'field+drafts link saw a revoked deployment: %', v;
  END IF;
  IF (v->>'fields')::int <> 1 OR (v->>'targets')::int <> 0 THEN
    RAISE EXCEPTION 'field+drafts link widened beyond its field: %', v;
  END IF;

  -- -------------------------------------------------------------------------
  -- 4) Revoked link: reads nothing at all, immediately -- the helpers resolve
  --    to NULL on both axes, so revocation bites on the next query rather than
  --    waiting for the account deletion to propagate.
  -- -------------------------------------------------------------------------
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000000b4');
  v := pg_temp.zzz_visible();

  IF v <> jsonb_build_object(
       'profiles', 1, 'programs', 0, 'observations', 0, 'targets', 0, 'spectra', 0,
       'objects', 0, 'photometry', 0, 'nircam', 0, 'fields', 0, 'map_layers', 0,
       'deployments', 0, 'storage', 0, 'shutters', 0, 'slits', 0, 'lists', 0,
       'list_members', 0, 'list_audit', 0, 'comments', 0, 'flag_audit', 0,
       'share_links', 0) THEN
    RAISE EXCEPTION 'revoked link still reads data: %', v;
  END IF;

  -- -------------------------------------------------------------------------
  -- 5) allow_download = false: catalog stays visible, bytes do not. Every
  --    download path (portal and /api/v1) runs through storage_objects, so
  --    this one conjunct is the whole opt-out.
  -- -------------------------------------------------------------------------
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000000b5');
  v := pg_temp.zzz_visible();

  IF (v->>'storage')::int <> 0 THEN
    RAISE EXCEPTION 'allow_download=false link can still presign: %', v;
  END IF;
  IF (v->>'targets')::int <> 1 OR (v->>'spectra')::int <> 1 THEN
    RAISE EXCEPTION 'allow_download=false link lost its catalog: %', v;
  END IF;

  -- -------------------------------------------------------------------------
  -- 6) Writes. Link accounts have can_comment/can_inspect false, so the write
  --    policies already refuse them -- assert it rather than assume it.
  -- -------------------------------------------------------------------------
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000000b1');
  BEGIN
    INSERT INTO comments (user_id, content, object_id)
    VALUES ('00000000-0000-0000-0000-0000000000b1', 'link wuz here',
            (SELECT id FROM objects WHERE object_id = 'ZZZ-OBJ-SHARED'));
    RAISE EXCEPTION 'link account was able to comment';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;  -- expected
  END;

  -- -------------------------------------------------------------------------
  -- 7) Ordinary users are unaffected. The narrowing is ~20 conjuncts spliced
  --    into shared policies, so the regression risk runs in both directions:
  --    this user differs from the link accounts ONLY in is_link_account.
  -- -------------------------------------------------------------------------
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000000a2');
  v := pg_temp.zzz_visible();

  IF (v->>'profiles')::int < 7 THEN
    RAISE EXCEPTION 'ordinary user lost profile visibility: %', v;
  END IF;
  IF (v->>'programs')::int <> 2 OR (v->>'observations')::int <> 3 THEN
    RAISE EXCEPTION 'ordinary user lost program/observation access: %', v;
  END IF;
  IF (v->>'targets')::int <> 3 OR (v->>'spectra')::int <> 3 THEN
    RAISE EXCEPTION 'ordinary user lost catalog access (drafts should be the only gap): %', v;
  END IF;
  IF (v->>'nircam')::int <> 2 OR (v->>'map_layers')::int <> 2 OR (v->>'fields')::int <> 2 THEN
    RAISE EXCEPTION 'ordinary user lost NIRCam access: %', v;
  END IF;
  IF (v->>'deployments')::int <> 5 THEN
    RAISE EXCEPTION 'ordinary user lost the deployment log: %', v;
  END IF;
  IF (v->>'comments')::int <> 1 OR (v->>'lists')::int <> 1 OR (v->>'flag_audit')::int <> 1 THEN
    RAISE EXCEPTION 'ordinary user lost community artifacts: %', v;
  END IF;
  IF (v->>'shutters')::int <> 2 OR (v->>'slits')::int <> 2 THEN
    RAISE EXCEPTION 'ordinary user lost shutters/slits: %', v;
  END IF;
  IF (v->>'share_links')::int <> 0 THEN
    RAISE EXCEPTION 'non-admin user can read share_links: %', v;
  END IF;

  -- -------------------------------------------------------------------------
  -- 8) Admins keep full visibility, including drafts and share_links.
  -- -------------------------------------------------------------------------
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000000a1');
  v := pg_temp.zzz_visible();

  IF (v->>'targets')::int <> 4 OR (v->>'spectra')::int <> 4 OR (v->>'nircam')::int <> 4 THEN
    RAISE EXCEPTION 'admin lost draft visibility: %', v;
  END IF;
  IF (v->>'share_links')::int <> 6 THEN
    RAISE EXCEPTION 'admin cannot read share_links: %', v;
  END IF;

  RAISE NOTICE 'OK: share-link scoping holds across both axes.';
END $$;

RESET ROLE;

-- ---------------------------------------------------------------------------
-- 9) A writable link account must be impossible to create at all.
--
-- Assertion 6 above checks that the write policies refuse a link account, but
-- that only holds because its profile carries can_comment = false -- and
-- can_comment DEFAULTS TO TRUE. So the guarantee cannot rest on every mint path
-- remembering to pass it; the CHECK constraint is what actually makes a
-- read-only link account structural. Assert the constraint itself, since it is
-- the thing standing between a forgotten column and a share link whose holder
-- can comment on the archive.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  BEGIN
    INSERT INTO user_profiles (user_id, username, full_name, is_link_account)
    VALUES ('00000000-0000-0000-0000-0000000000b9', 'zzz-l-bad', 'ZZZ Link Bad', true);
    RAISE EXCEPTION 'a link account was created with the default can_comment = true';
  EXCEPTION WHEN check_violation THEN
    NULL;  -- expected
  END;

  BEGIN
    INSERT INTO user_profiles (user_id, username, full_name, is_link_account, can_comment, is_admin)
    VALUES ('00000000-0000-0000-0000-0000000000b9', 'zzz-l-bad', 'ZZZ Link Bad', true, false, true);
    RAISE EXCEPTION 'an admin link account was created';
  EXCEPTION WHEN check_violation THEN
    NULL;  -- expected
  END;

  RAISE NOTICE 'OK: link accounts cannot be granted write or admin rights.';
END $$;

-- ---------------------------------------------------------------------------
-- 10) Role columns are locked against self-service updates.
--
-- self_update_profile is row-level and `authenticated` holds table-wide
-- UPDATE, so without the enforce_profile_role_update_scope trigger a link
-- account could clear its own is_link_account -- stepping out of every
-- narrowing conjunct at once -- and any user could set their own is_admin.
-- ---------------------------------------------------------------------------
SET LOCAL ROLE authenticated;

DO $$
BEGIN
  -- A link account must not be able to clear its own discriminator.
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000000b1');
  BEGIN
    UPDATE user_profiles SET is_link_account = false
    WHERE user_id = '00000000-0000-0000-0000-0000000000b1';
    RAISE EXCEPTION 'link account cleared its own is_link_account';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;  -- expected
  END;

  -- An ordinary user must not be able to self-escalate.
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000000a2');
  BEGIN
    UPDATE user_profiles SET is_admin = true
    WHERE user_id = '00000000-0000-0000-0000-0000000000a2';
    RAISE EXCEPTION 'ordinary user set their own is_admin';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;  -- expected
  END;

  -- ...while ordinary self-service profile edits still work.
  UPDATE user_profiles SET full_name = 'ZZZ Renamed'
  WHERE user_id = '00000000-0000-0000-0000-0000000000a2';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'ordinary user can no longer edit their own profile';
  END IF;

  RAISE NOTICE 'OK: role columns are admin-set only.';
END $$;

-- ---------------------------------------------------------------------------
-- 11) link_password never reaches `authenticated` -- not even an admin's
--     browser session. The grant is an explicit column list because a
--     table-level GRANT SELECT would cover every column regardless of any
--     column-level REVOKE.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  PERFORM pg_temp.zzz_as('00000000-0000-0000-0000-0000000000a1');

  BEGIN
    PERFORM count(link_password) FROM share_links;
    RAISE EXCEPTION 'authenticated (admin) can read link_password';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;  -- expected
  END;

  -- The non-secret columns stay readable for the admin table.
  IF (SELECT count(token) FROM share_links WHERE token LIKE 'zzz%') <> 6 THEN
    RAISE EXCEPTION 'admin lost the share-links table listing';
  END IF;

  RAISE NOTICE 'OK: link_password is invisible to authenticated.';
END $$;

RESET ROLE;

ROLLBACK;
