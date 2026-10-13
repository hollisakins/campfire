-- =============================================================================
-- Data-release freeze core (P4a, docs/design-data-releases-impl-plan.md §6).
-- =============================================================================
-- Proves, on a fixture with one public and one private program:
--   1. dr_enqueue_freeze validates: bad tag, unknown component, NIRCam field
--      without published mosaics, photometry component without rows, a live
--      deploy lease (refused; a stale lease is not), non-admin refused.
--   2. the freeze writes every frozen table from the published rows only:
--      draft and revoked spectra excluded (and their line fits / members),
--      inactive objects excluded, objects without a published member
--      excluded, evidence_programs and z_source_spectrum_id correct for a
--      mixed object, line-fit staleness computed in-snapshot, members =
--      finals + sidecars (including the zfit / lines rows registered without
--      a spectrum_id), published mosaics only, P(z) sidecars of the
--      photometry field; counts and snapshot_xmin recorded; a deploy_events
--      row logged.
--   3. idempotence: re-freezing converges (same rows, no duplicates); a
--      failed attempt lands in state failed with the error text and the next
--      attempt succeeds.
--   4. RLS matrix over every dr_* table: anon denied; while the tag is frozen
--      only admins see rows; once published a user without grants sees the
--      public rows, a grantee both, a link account nothing; dr_exports stays
--      admin-only.
--   5. purge: refused for a published tag, allowed before, deletes everything.
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_dr_freeze.sql
-- Everything inside one rolled-back transaction.
\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------------------
-- Fixture
-- ---------------------------------------------------------------------------
INSERT INTO programs (slug, program_name, is_public) VALUES
  ('zzz_dr_pub',  'DR Freeze Public Program',  true),
  ('zzz_dr_priv', 'DR Freeze Private Program', false);
INSERT INTO fields (name) VALUES ('zzz_drf'), ('zzz_drf_nomosaic');
INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
  ('zzz_dr_obs_pub',  'zzz_dr_pub',  9401, 'zzz_drf'),
  ('zzz_dr_obs_priv', 'zzz_dr_priv', 9402, 'zzz_drf');

INSERT INTO auth.users (id, email) VALUES
  ('00000000-0000-0000-0000-0000000000d1', 'zzz-dr-admin@test.invalid'),
  ('00000000-0000-0000-0000-0000000000d2', 'zzz-dr-grantee@test.invalid'),
  ('00000000-0000-0000-0000-0000000000d3', 'zzz-dr-nogrant@test.invalid'),
  ('00000000-0000-0000-0000-0000000000d4', 'zzz-dr-link@test.invalid');
INSERT INTO user_profiles (user_id, username, full_name, is_admin, is_link_account, can_comment, can_inspect) VALUES
  ('00000000-0000-0000-0000-0000000000d1', 'zzz-dr-admin',   'ZZZ DR Admin',   true,  false, true, true),
  ('00000000-0000-0000-0000-0000000000d2', 'zzz-dr-grantee', 'ZZZ DR Grantee', false, false, true, false),
  ('00000000-0000-0000-0000-0000000000d3', 'zzz-dr-nogrant', 'ZZZ DR NoGrant', false, false, true, false),
  ('00000000-0000-0000-0000-0000000000d4', 'zzz-dr-link',    'ZZZ DR Link',    false, true,  false, false);
INSERT INTO user_program_access (user_id, program_slug) VALUES
  ('00000000-0000-0000-0000-0000000000d2', 'zzz_dr_priv');
INSERT INTO share_links (token, label, observation, link_user_id, link_password, include_drafts, allow_download, created_by)
VALUES ('zzz_dr_tok', 'dr link', 'zzz_dr_obs_pub', '00000000-0000-0000-0000-0000000000d4', 'x', false, true,
        '00000000-0000-0000-0000-0000000000d1');

-- Objects: PUB (one public member), MIX (public + private members, inspected q3),
-- PRIV (private only), DRAFTONLY (its only member is a draft), INACTIVE.
INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum, is_active,
                     redshift_quality, redshift_inspected, last_inspected_at, version, needs_review) VALUES
  ('ZZZ-DR-PUB',       'zzz_drf', 170.00, 4.00, ARRAY['zzz_dr_pub'],               ARRAY['zzz_dr_obs_pub'],  true,  true,  4, 1.0,  now(), 1, false),
  ('ZZZ-DR-MIX',       'zzz_drf', 170.01, 4.01, ARRAY['zzz_dr_pub','zzz_dr_priv'], ARRAY['zzz_dr_obs_pub','zzz_dr_obs_priv'], true, true, 3, 2.5, now(), 4, true),
  ('ZZZ-DR-PRIV',      'zzz_drf', 170.02, 4.02, ARRAY['zzz_dr_priv'],              ARRAY['zzz_dr_obs_priv'], true,  true,  0, NULL, NULL, 1, false),
  ('ZZZ-DR-DRAFTONLY', 'zzz_drf', 170.03, 4.03, ARRAY['zzz_dr_pub'],               ARRAY['zzz_dr_obs_pub'],  false, true,  0, NULL, NULL, 1, false),
  ('ZZZ-DR-INACTIVE',  'zzz_drf', 170.04, 4.04, ARRAY['zzz_dr_pub'],               ARRAY['zzz_dr_obs_pub'],  true,  false, 0, NULL, NULL, 1, false);

INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id, has_published_spectrum)
SELECT v.tid, 'zzz_drf', v.ra, 4.0, v.prog, v.obs, o.id, v.pub
FROM (VALUES
  ('zzz-dr-t-pub',   170.00, 'zzz_dr_pub',  'zzz_dr_obs_pub',  'ZZZ-DR-PUB',       true),
  ('zzz-dr-t-mixp',  170.01, 'zzz_dr_pub',  'zzz_dr_obs_pub',  'ZZZ-DR-MIX',       true),
  ('zzz-dr-t-mixq',  170.01, 'zzz_dr_priv', 'zzz_dr_obs_priv', 'ZZZ-DR-MIX',       true),
  ('zzz-dr-t-priv',  170.02, 'zzz_dr_priv', 'zzz_dr_obs_priv', 'ZZZ-DR-PRIV',      true),
  ('zzz-dr-t-draft', 170.03, 'zzz_dr_pub',  'zzz_dr_obs_pub',  'ZZZ-DR-DRAFTONLY', false),
  ('zzz-dr-t-inact', 170.04, 'zzz_dr_pub',  'zzz_dr_obs_pub',  'ZZZ-DR-INACTIVE',  true)
) AS v(tid, ra, prog, obs, oid, pub)
JOIN objects o ON o.object_id = v.oid;

-- Spectra. MIX: a PRISM (z=2.50, 1000 s) and a G395M whose auto agrees
-- (2.505) -> the refined grating anchors z_source; a G140M that disagrees.
INSERT INTO spectra (target_id, grating, fits_path, deploy_status, redshift_auto, exposure_time, file_hash, science_hash) VALUES
  ('zzz-dr-t-pub',   'PRISM', '/tmp/zzz_dr_pub_prism_spec.fits',  'published', 1.0,   500,  'sha256:' || repeat('1', 64), 'sha256:' || repeat('a', 64)),
  ('zzz-dr-t-pub',   'G395M', '/tmp/zzz_dr_pub_g395m_spec.fits',  'revoked',   1.0,   500,  'sha256:' || repeat('2', 64), 'sha256:' || repeat('b', 64)),
  ('zzz-dr-t-mixp',  'PRISM', '/tmp/zzz_dr_mixp_prism_spec.fits', 'published', 2.50,  1000, 'sha256:' || repeat('3', 64), 'sha256:' || repeat('c', 64)),
  ('zzz-dr-t-mixq',  'G395M', '/tmp/zzz_dr_mixq_g395m_spec.fits', 'published', 2.505, 2000, 'sha256:' || repeat('4', 64), 'sha256:' || repeat('d', 64)),
  ('zzz-dr-t-mixq',  'G140M', '/tmp/zzz_dr_mixq_g140m_spec.fits', 'published', 3.1,   2000, 'sha256:' || repeat('5', 64), 'sha256:' || repeat('e', 64)),
  ('zzz-dr-t-priv',  'PRISM', '/tmp/zzz_dr_priv_prism_spec.fits', 'published', 4.0,   500,  'sha256:' || repeat('6', 64), 'sha256:' || repeat('f', 64)),
  ('zzz-dr-t-draft', 'PRISM', '/tmp/zzz_dr_draft_prism_spec.fits','draft',     5.0,   500,  'sha256:' || repeat('7', 64), 'sha256:' || repeat('0', 64)),
  ('zzz-dr-t-inact', 'PRISM', '/tmp/zzz_dr_inact_prism_spec.fits','published', 6.0,   500,  'sha256:' || repeat('8', 64), 'sha256:' || repeat('9', 64));

-- Registry rows: spec + json + 1d (with spectrum_id) and zfit + lines.json
-- WITHOUT spectrum_id (how deploy registered them), for every spectrum.
INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, science_hash, size_bytes, content_type,
                             product_type, instrument, status, observation, program_slug, spectrum_id)
SELECT 'osn', 'data', 'data/products/nirspec/' || s.observation || '/' || s.spectrum_id || k.sfx,
       -- the final carries the spectrum's file hash (as deploy registers it)
       CASE WHEN k.pt = 'nirspec_spec' THEN s.file_hash
            ELSE 'sha256:' || md5(s.spectrum_id || k.sfx) || md5(k.sfx) END,
       s.science_hash, 1000 + k.i, 'application/octet-stream',
       k.pt, 'nirspec', 'active', s.observation, s.program_slug,
       CASE WHEN k.with_sid THEN s.spectrum_id END
FROM spectra s
JOIN targets t ON t.target_id = s.target_id
CROSS JOIN (VALUES
  ('_spec.fits',    'nirspec_spec',       true,  1),
  ('_spec.json',    'spectrum_json',      true,  2),
  ('_spec_1d.json', 'spectrum_1d_json',   true,  3),
  ('_zfit.json',    'zfit',               false, 4),
  ('_lines.json',   'nirspec_lines_json', false, 5)
) AS k(sfx, pt, with_sid, i)
WHERE t.target_id LIKE 'zzz-dr-t-%';
-- a superseded (retained) copy of a spec that must NOT become a member
INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type, product_type,
                             instrument, status, observation, program_slug, spectrum_id)
SELECT 'osn', 'data', 'data/products/nirspec/zzz_dr_obs_pub/old_' || s.spectrum_id || '_spec.fits',
       'sha256:' || repeat('5', 64), 10, 'application/octet-stream', 'nirspec_spec', 'nirspec', 'superseded',
       s.observation, s.program_slug, s.spectrum_id
FROM spectra s WHERE s.fits_path = '/tmp/zzz_dr_pub_prism_spec.fits';

-- Line fits: PUB's fit at the current object state (q4, z=1.0, version 1: fresh); MIX's fit made
-- at object version 2 (stale_redshift) and against an older spectrum hash
-- (stale_spectrum); the revoked spectrum's fit (must not freeze).
INSERT INTO spectrum_line_fits (spectrum_id, target_id, grating, program_slug, observation, z_used, z_source, z_quality,
                                object_id, object_version, fit_version, spectrum_hash, spectrum_science_hash)
SELECT s.id, s.target_id, s.grating, t.program_slug, t.observation,
       CASE WHEN t.target_id = 'zzz-dr-t-mixp' THEN 2.5 ELSE 1.0 END, 'inspected',
       CASE WHEN t.target_id = 'zzz-dr-t-mixp' THEN 3 ELSE 4 END,
       o.object_id,
       CASE WHEN t.target_id = 'zzz-dr-t-mixp' THEN 2 ELSE o.version END,
       'v1',
       CASE WHEN t.target_id = 'zzz-dr-t-mixp' THEN 'sha256:' || repeat('x', 64) ELSE s.file_hash END,
       CASE WHEN t.target_id = 'zzz-dr-t-mixp' THEN 'sha256:' || repeat('0', 64) ELSE s.science_hash END
FROM spectra s JOIN targets t ON t.target_id = s.target_id JOIN objects o ON o.id = t.object_id
WHERE s.fits_path IN ('/tmp/zzz_dr_pub_prism_spec.fits', '/tmp/zzz_dr_pub_g395m_spec.fits', '/tmp/zzz_dr_mixp_prism_spec.fits');
-- the PUB fit's denormalized program is stale (a metadata correction after the
-- fit): the frozen fit must take its spectrum's program (Codex on #599)
UPDATE spectrum_line_fits f SET program_slug = 'zzz_dr_priv'
  FROM spectra s WHERE s.id = f.spectrum_id AND s.fits_path = '/tmp/zzz_dr_pub_prism_spec.fits';

-- Photometry: a row for MIX and one for the inactive object (excluded).
INSERT INTO object_photometry (object_id, field, ra, dec, catalog_name, catalog_id, photometry, photo_z)
SELECT o.id, 'zzz_drf', o.ra, o.dec, 'ZZZ CAT v1', o.object_id, '{"bands":{}}'::jsonb, 2.4
FROM objects o WHERE o.object_id IN ('ZZZ-DR-MIX', 'ZZZ-DR-INACTIVE');
-- a newer cross-match of MIX from a catalog the release does NOT select, and the
-- denormalized live object rows pointing at it / at nothing frozen (Codex on #599)
INSERT INTO object_photometry (object_id, field, ra, dec, catalog_name, catalog_id, photometry, photo_z, updated_at)
SELECT o.id, 'zzz_drf', o.ra, o.dec, 'ZZZ CAT v2', o.object_id, '{"bands":{}}'::jsonb, 9.9, now() + interval '1 minute'
FROM objects o WHERE o.object_id = 'ZZZ-DR-MIX';
UPDATE objects SET photo_z = 9.9, has_photometry = true WHERE object_id = 'ZZZ-DR-MIX';
UPDATE objects SET photo_z = 7.0, has_photometry = true WHERE object_id = 'ZZZ-DR-PUB';
INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type, product_type, status, field)
VALUES ('osn', 'data', 'data/products/photometry/zzz_drf/pz/zzz_1.json', 'sha256:' || repeat('a1', 32), 50, 'application/json', 'photometry_pz', 'active', 'zzz_drf');

-- NIRCam: two published mosaics + one draft, each with a registry row.
INSERT INTO deployments (id, field, status, published_at) VALUES (990101, 'zzz_drf', 'published', now());
INSERT INTO nircam_images (field, tile, filter, pixel_scale, extension, epoch, file_path, deploy_status, deployment_id) VALUES
  ('zzz_drf', 'main', 'f444w', '30mas', 'sci', '', 'data/products/nircam/zzz_drf/f444w/zzz_drf_f444w_sci.fits', 'published', 990101),
  ('zzz_drf', 'main', 'f444w', '30mas', 'wht', '', 'data/products/nircam/zzz_drf/f444w/zzz_drf_f444w_wht.fits', 'published', 990101),
  ('zzz_drf', 'main', 'f277w', '30mas', 'sci', '', 'data/products/nircam/zzz_drf/f277w/zzz_drf_f277w_sci.fits', 'draft', 990101);
INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type, product_type, instrument, status, field, filter, deployment_id)
SELECT 'osn', 'data', ni.file_path, 'sha256:' || md5(ni.file_path) || md5(ni.filter), 5000, 'application/fits', 'nircam_mosaic', 'nircam', 'active', ni.field, ni.filter, 990101
FROM nircam_images ni WHERE ni.field = 'zzz_drf';

-- A helper that runs SQL as a principal (claims + SET LOCAL ROLE).
CREATE FUNCTION pg_temp.zzz_dr_as(p_role text, p_uid uuid, p_sql text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE v text;
BEGIN
  IF p_uid IS NULL THEN
    PERFORM set_config('request.jwt.claims', json_build_object('role', p_role)::text, true);
  ELSE
    PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', p_role)::text, true);
  END IF;
  EXECUTE format('SET LOCAL ROLE %I', p_role);
  EXECUTE p_sql INTO v;
  RESET ROLE;
  PERFORM set_config('request.jwt.claims', '', true);
  RETURN v;
EXCEPTION WHEN OTHERS THEN
  RESET ROLE;
  PERFORM set_config('request.jwt.claims', '', true);
  RETURN 'ERR: ' || SQLERRM;
END $$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_dr_as(text, uuid, text) TO authenticated, anon;

-- ---------------------------------------------------------------------------
-- 1. Validation
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  admin uuid := '00000000-0000-0000-0000-0000000000d1';
  nogrant uuid := '00000000-0000-0000-0000-0000000000d3';
  got text;
BEGIN
  got := pg_temp.zzz_dr_as('authenticated', nogrant, $q$SELECT (public.dr_enqueue_freeze('DR-zzz', '["nirspec"]', NULL, true))::text$q$);
  IF got NOT LIKE 'ERR: Access denied%' THEN RAISE EXCEPTION 'non-admin enqueue should be refused, got %', got; END IF;

  got := pg_temp.zzz_dr_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('bad tag!', '["nirspec"]', NULL, true))::text$q$);
  IF got NOT LIKE 'ERR: invalid tag%' THEN RAISE EXCEPTION 'bad tag should be refused, got %', got; END IF;

  got := pg_temp.zzz_dr_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzz', '["miri"]', NULL, true))::text$q$);
  IF got NOT LIKE 'ERR: unknown component%' THEN RAISE EXCEPTION 'unknown component should be refused, got %', got; END IF;

  got := pg_temp.zzz_dr_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzz', '["nircam/zzz_drf_nomosaic"]', NULL, true))::text$q$);
  IF got NOT LIKE 'ERR: component nircam/zzz_drf_nomosaic: no published NIRCam images%' THEN RAISE EXCEPTION 'field without mosaics should be refused, got %', got; END IF;

  got := pg_temp.zzz_dr_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzz', '["photometry/zzz_drf/nope"]', NULL, true))::text$q$);
  IF got NOT LIKE 'ERR: component photometry/zzz_drf/nope: no photometry rows%' THEN RAISE EXCEPTION 'photometry without rows should be refused, got %', got; END IF;

  got := pg_temp.zzz_dr_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzz', '[]', NULL, true))::text$q$);
  IF got NOT LIKE 'ERR: components must be a non-empty%' THEN RAISE EXCEPTION 'empty components should be refused, got %', got; END IF;

  -- photometry rows hang off the frozen objects: no nirspec, no photometry (Codex on #599)
  got := pg_temp.zzz_dr_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzz', '["photometry/zzz_drf/ZZZ CAT v1"]', NULL, true))::text$q$);
  IF got NOT LIKE 'ERR: photometry components need the nirspec component%' THEN RAISE EXCEPTION 'photometry-only freeze should be refused, got %', got; END IF;

  -- interlock: while a release is `freezing`, a deploy cannot begin (CF409)
  INSERT INTO dr_releases (tag, state, components, freeze_started_at) VALUES ('DR-zzz-lock', 'freezing', '["nirspec"]', now());
  got := pg_temp.zzz_dr_as('authenticated', admin, $q$SELECT (public.begin_deploy_scope('observation', 'zzz_dr_obs_priv', NULL, 'zzz-host', false))::text$q$);
  IF got NOT LIKE 'ERR: deploy_scope_busy: a data-release freeze is running (DR-zzz-lock)%' THEN RAISE EXCEPTION 'deploy should be refused while freezing, got %', got; END IF;
  DELETE FROM dr_releases WHERE tag = 'DR-zzz-lock';
  -- and a lease taken after the enqueue (the pg_cron delay) fails the job cleanly
  INSERT INTO dr_releases (tag, state, components, freeze_started_at) VALUES ('DR-zzz-late', 'freezing', '["nirspec"]', now());
  INSERT INTO deploy_scope_state (scope_type, scope_key, in_progress_since, heartbeat_at, in_progress_host)
  VALUES ('observation', 'zzz_dr_obs_pub', now(), now(), 'zzz-host');
  PERFORM public.dr_freeze_job('DR-zzz-late');
  IF (SELECT state FROM dr_releases WHERE tag = 'DR-zzz-late') <> 'failed'
     OR (SELECT error FROM dr_releases WHERE tag = 'DR-zzz-late') NOT LIKE 'a deploy started after the freeze was enqueued%' THEN
    RAISE EXCEPTION 'late lease should fail the job: %', (SELECT to_jsonb(r) FROM dr_releases r WHERE tag = 'DR-zzz-late');
  END IF;
  DELETE FROM dr_releases WHERE tag = 'DR-zzz-late';
  DELETE FROM deploy_scope_state WHERE scope_key = 'zzz_dr_obs_pub';

  -- a live deploy lease blocks; a stale one does not
  INSERT INTO deploy_scope_state (scope_type, scope_key, in_progress_since, heartbeat_at, in_progress_host)
  VALUES ('observation', 'zzz_dr_obs_pub', now(), now(), 'zzz-host');
  got := pg_temp.zzz_dr_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzz', '["nirspec"]', NULL, true))::text$q$);
  IF got NOT LIKE 'ERR: a deploy is in progress%' THEN RAISE EXCEPTION 'live lease should block the freeze, got %', got; END IF;
  UPDATE deploy_scope_state SET in_progress_since = now() - interval '2 days', heartbeat_at = now() - interval '2 days'
   WHERE scope_key = 'zzz_dr_obs_pub';
  IF EXISTS (SELECT 1 FROM deploy_scope_state WHERE scope_key = 'zzz_dr_obs_pub'
              AND COALESCE(heartbeat_at, in_progress_since) >= now() - deploy_lease_ttl()) THEN
    RAISE EXCEPTION 'fixture: lease should be stale now';
  END IF;
  RAISE NOTICE 'OK 1: validation (non-admin, tag, components, lease)';
END $$;

-- ---------------------------------------------------------------------------
-- 2. The freeze (inline, as the admin)
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  admin uuid := '00000000-0000-0000-0000-0000000000d1';
  got text; r dr_releases%ROWTYPE; n int; n2 int;
  mix int; pub_id int; z_src int; g395m int;
BEGIN
  got := pg_temp.zzz_dr_as('authenticated', admin,
    $q$SELECT (public.dr_enqueue_freeze('DR-zzz', '["nirspec","nircam/zzz_drf","photometry/zzz_drf/ZZZ CAT v1"]', 'rehearsal', true))->>'state'$q$);
  IF got <> 'frozen' THEN RAISE EXCEPTION 'freeze did not land frozen: %', got; END IF;
  SELECT * INTO r FROM dr_releases WHERE tag = 'DR-zzz';
  IF r.snapshot_xmin IS NULL OR r.frozen_at IS NULL OR r.freeze_duration_ms IS NULL OR r.created_by <> admin OR r.frozen_by <> admin THEN
    RAISE EXCEPTION 'release row incomplete: %', to_jsonb(r);
  END IF;
  IF r.notes <> 'rehearsal' THEN RAISE EXCEPTION 'notes not stored'; END IF;

  -- spectra: the 5 published ones of active objects + the inactive object's
  -- published spectrum is NOT frozen (its object is inactive -> object_id NULL
  -- but the spectrum itself is published: it IS frozen, with object_id NULL).
  SELECT count(*) INTO n FROM dr_spectra WHERE tag = 'DR-zzz' AND fits_path LIKE '/tmp/zzz_dr_%';
  IF n <> 6 THEN RAISE EXCEPTION 'expected 6 frozen spectra (published only), got %', n; END IF;
  IF EXISTS (SELECT 1 FROM dr_spectra WHERE tag = 'DR-zzz' AND fits_path IN ('/tmp/zzz_dr_pub_g395m_spec.fits', '/tmp/zzz_dr_draft_prism_spec.fits')) THEN
    RAISE EXCEPTION 'revoked / draft spectrum frozen';
  END IF;
  SELECT object_id INTO n FROM dr_spectra WHERE tag = 'DR-zzz' AND fits_path = '/tmp/zzz_dr_inact_prism_spec.fits';
  IF n IS NOT NULL THEN RAISE EXCEPTION 'spectrum of an inactive object should carry object_id NULL'; END IF;
  -- ...but keeps its target's field (Codex on #600)
  IF (SELECT field FROM dr_spectra WHERE tag = 'DR-zzz' AND fits_path = '/tmp/zzz_dr_inact_prism_spec.fits') IS DISTINCT FROM 'zzz_drf' THEN
    RAISE EXCEPTION 'objectless spectrum lost its target field';
  END IF;
  IF EXISTS (SELECT 1 FROM dr_spectra WHERE tag = 'DR-zzz' AND fits_path LIKE '/tmp/zzz_dr_%' AND field IS NULL) THEN
    RAISE EXCEPTION 'a frozen spectrum has no field';
  END IF;

  -- objects: PUB, MIX, PRIV; not DRAFTONLY, not INACTIVE
  SELECT count(*) INTO n FROM dr_objects WHERE tag = 'DR-zzz' AND name LIKE 'ZZZ-DR-%';
  IF n <> 3 THEN RAISE EXCEPTION 'expected 3 frozen objects, got %', n; END IF;
  IF EXISTS (SELECT 1 FROM dr_objects WHERE tag = 'DR-zzz' AND name IN ('ZZZ-DR-DRAFTONLY', 'ZZZ-DR-INACTIVE')) THEN
    RAISE EXCEPTION 'draft-only or inactive object frozen';
  END IF;
  SELECT o.id INTO mix FROM objects o WHERE o.object_id = 'ZZZ-DR-MIX';
  IF (SELECT evidence_programs FROM dr_objects WHERE tag = 'DR-zzz' AND object_id = mix) <> ARRAY['zzz_dr_priv', 'zzz_dr_pub'] THEN
    RAISE EXCEPTION 'MIX evidence_programs wrong: %', (SELECT evidence_programs FROM dr_objects WHERE tag = 'DR-zzz' AND object_id = mix);
  END IF;
  SELECT s.id INTO g395m FROM spectra s WHERE s.fits_path = '/tmp/zzz_dr_mixq_g395m_spec.fits';
  SELECT z_source_spectrum_id INTO z_src FROM dr_objects WHERE tag = 'DR-zzz' AND object_id = mix;
  IF z_src <> g395m THEN RAISE EXCEPTION 'MIX z_source should be the PRISM-consistent G395M (%), got %', g395m, z_src; END IF;
  -- review_state mirrors the live flag in the snapshot (the P3 triggers
  -- recompute it on the fixture inserts, so compare rather than assume)
  IF ((SELECT review_state FROM dr_objects WHERE tag = 'DR-zzz' AND object_id = mix) ->> 'needs_review')::boolean
     IS DISTINCT FROM (SELECT needs_review FROM objects WHERE id = mix)
     OR NOT ((SELECT review_state FROM dr_objects WHERE tag = 'DR-zzz' AND object_id = mix) ? 'reviewed_spectrum_ids') THEN
    RAISE EXCEPTION 'MIX review_state not frozen';
  END IF;
  IF (SELECT redshift_quality FROM dr_objects WHERE tag = 'DR-zzz' AND object_id = mix) <> 3
     OR (SELECT redshift FROM dr_objects WHERE tag = 'DR-zzz' AND object_id = mix) <> 2.5 THEN
    RAISE EXCEPTION 'MIX inspection state not frozen';
  END IF;
  -- aggregates, programs and the auto redshift come from the PUBLISHED members
  -- (Codex on #599): MIX has 3 published members in 2 programs; its auto
  -- redshift is the PRISM-consistent G395M value, not the live column
  IF (SELECT (n_spectra, n_targets, gratings, programs, redshift_auto::numeric(10,4)) FROM dr_objects WHERE tag = 'DR-zzz' AND object_id = mix)
     IS DISTINCT FROM (3, 2, ARRAY['G140M', 'G395M', 'PRISM'], ARRAY['zzz_dr_priv', 'zzz_dr_pub'], 2.505::numeric(10,4)) THEN
    RAISE EXCEPTION 'MIX frozen aggregates wrong: %', (SELECT to_jsonb(o) - 'review_state' FROM dr_objects o WHERE tag = 'DR-zzz' AND object_id = mix);
  END IF;
  SELECT o.id INTO pub_id FROM objects o WHERE o.object_id = 'ZZZ-DR-PUB';
  -- PUB's G395M member is revoked: one published spectrum, one grating; its
  -- live row still counts two (the reconcile aggregates include every member)
  IF (SELECT (n_spectra, gratings) FROM dr_objects WHERE tag = 'DR-zzz' AND object_id = pub_id)
     IS DISTINCT FROM (1, ARRAY['PRISM']) THEN
    RAISE EXCEPTION 'PUB frozen aggregates should count the published member only: %', (SELECT to_jsonb(o) - 'review_state' FROM dr_objects o WHERE tag = 'DR-zzz' AND object_id = pub_id);
  END IF;

  -- line fits: PUB fresh, MIX stale both ways, revoked spectrum's fit absent
  SELECT count(*) INTO n FROM dr_line_fits WHERE tag = 'DR-zzz' AND target_id LIKE 'zzz-dr-t-%';
  IF n <> 2 THEN RAISE EXCEPTION 'expected 2 frozen line fits, got %', n; END IF;
  IF (SELECT stale_redshift OR stale_spectrum FROM dr_line_fits WHERE tag = 'DR-zzz' AND target_id = 'zzz-dr-t-pub') THEN
    RAISE EXCEPTION 'PUB line fit should be fresh';
  END IF;
  IF NOT (SELECT stale_redshift AND stale_spectrum FROM dr_line_fits WHERE tag = 'DR-zzz' AND target_id = 'zzz-dr-t-mixp') THEN
    RAISE EXCEPTION 'MIX line fit should be stale on both axes';
  END IF;
  -- the frozen fit carries its spectrum's program, not the fit's stale copy
  IF (SELECT program_slug FROM dr_line_fits WHERE tag = 'DR-zzz' AND target_id = 'zzz-dr-t-pub') <> 'zzz_dr_pub' THEN
    RAISE EXCEPTION 'frozen line fit kept a stale program: %',
      (SELECT program_slug FROM dr_line_fits WHERE tag = 'DR-zzz' AND target_id = 'zzz-dr-t-pub');
  END IF;

  -- photometry: MIX only, with the object's programs
  SELECT count(*) INTO n FROM dr_photometry WHERE tag = 'DR-zzz' AND catalog_name = 'ZZZ CAT v1';
  IF n <> 1 OR (SELECT programs FROM dr_photometry WHERE tag = 'DR-zzz' AND catalog_name = 'ZZZ CAT v1') <> ARRAY['zzz_dr_priv', 'zzz_dr_pub'] THEN
    RAISE EXCEPTION 'photometry freeze wrong (n=%)', n;
  END IF;
  -- object photo-z fields follow the SELECTED catalog, not the live row
  IF (SELECT (d.photo_z, d.has_photometry) FROM dr_objects d JOIN objects o ON o.id = d.object_id
      WHERE d.tag = 'DR-zzz' AND o.object_id = 'ZZZ-DR-MIX') IS DISTINCT FROM (2.4::double precision, true)
     OR (SELECT (d.photo_z, d.has_photometry) FROM dr_objects d JOIN objects o ON o.id = d.object_id
         WHERE d.tag = 'DR-zzz' AND o.object_id = 'ZZZ-DR-PUB') IS DISTINCT FROM (NULL::double precision, false) THEN
    RAISE EXCEPTION 'frozen photo-z fields wrong: %', (SELECT jsonb_agg(jsonb_build_object('o', o.object_id, 'z', d.photo_z, 'has', d.has_photometry))
                                                        FROM dr_objects d JOIN objects o ON o.id = d.object_id WHERE d.tag = 'DR-zzz');
  END IF;

  -- members: 6 spectra x 5 products (zfit + lines via the key rule) + 2 mosaics
  SELECT count(*) INTO n FROM dr_members WHERE tag = 'DR-zzz' AND component = 'nirspec' AND storage_key LIKE 'data/products/nirspec/zzz_dr_%';
  IF n <> 30 THEN RAISE EXCEPTION 'expected 30 nirspec members, got %', n; END IF;
  SELECT count(*) INTO n FROM dr_members WHERE tag = 'DR-zzz' AND product_type IN ('zfit', 'nirspec_lines_json') AND spectrum_id IS NULL;
  IF n <> 0 THEN RAISE EXCEPTION 'key-derived sidecar members should carry the spectrum_id'; END IF;
  IF EXISTS (SELECT 1 FROM dr_members WHERE tag = 'DR-zzz' AND storage_key LIKE '%/old_%') THEN
    RAISE EXCEPTION 'superseded registry row became a member';
  END IF;
  IF EXISTS (SELECT 1 FROM dr_members m JOIN spectra s ON s.spectrum_id = m.spectrum_id WHERE m.tag = 'DR-zzz' AND s.deploy_status <> 'published') THEN
    RAISE EXCEPTION 'member of an unpublished spectrum';
  END IF;
  SELECT count(*) INTO n FROM dr_members WHERE tag = 'DR-zzz' AND component = 'nircam/zzz_drf';
  IF n <> 2 THEN RAISE EXCEPTION 'expected 2 mosaic members (published only), got %', n; END IF;
  -- P(z) sidecars are key-only products the layout cannot archive: never members (D-P4-5)
  SELECT count(*) INTO n FROM dr_members WHERE tag = 'DR-zzz' AND component LIKE 'photometry/%';
  IF n <> 0 THEN RAISE EXCEPTION 'P(z) sidecars must not be members, got %', n; END IF;
  IF EXISTS (SELECT 1 FROM dr_members WHERE tag = 'DR-zzz' AND byte_state <> 'pending') THEN
    RAISE EXCEPTION 'members should start pending';
  END IF;

  -- counts + event
  IF (r.counts->>'members')::int <> (SELECT count(*) FROM dr_members WHERE tag = 'DR-zzz')
     OR (r.counts->>'spectra')::int <> (SELECT count(*) FROM dr_spectra WHERE tag = 'DR-zzz') THEN
    RAISE EXCEPTION 'counts do not match the rows: %', r.counts;
  END IF;
  SELECT count(*) INTO n FROM deploy_events WHERE action = 'freeze' AND metadata->>'dr_tag' = 'DR-zzz';
  IF n <> 1 THEN RAISE EXCEPTION 'expected one freeze event, got %', n; END IF;

  -- readiness lists it
  got := pg_temp.zzz_dr_as('authenticated', admin, $q$SELECT (SELECT r->>'state' FROM jsonb_array_elements(public.dr_readiness()->'releases') r WHERE r->>'tag' = 'DR-zzz')$q$);
  IF got <> 'frozen' THEN RAISE EXCEPTION 'dr_readiness should list the release, got %', got; END IF;
  RAISE NOTICE 'OK 2: freeze contents';
END $$;

-- ---------------------------------------------------------------------------
-- 3. Idempotence and failure
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  admin uuid := '00000000-0000-0000-0000-0000000000d1';
  got text; before jsonb; after jsonb; n int;
BEGIN
  SELECT counts INTO before FROM dr_releases WHERE tag = 'DR-zzz';
  got := pg_temp.zzz_dr_as('authenticated', admin,
    $q$SELECT (public.dr_enqueue_freeze('DR-zzz', '["nirspec","nircam/zzz_drf","photometry/zzz_drf/ZZZ CAT v1"]', NULL, true))->>'state'$q$);
  IF got <> 'frozen' THEN RAISE EXCEPTION 're-freeze failed: %', got; END IF;
  SELECT counts INTO after FROM dr_releases WHERE tag = 'DR-zzz';
  IF before <> after THEN RAISE EXCEPTION 're-freeze changed the counts: % -> %', before, after; END IF;
  SELECT count(*) INTO n FROM dr_members WHERE tag = 'DR-zzz';
  IF n <> (after->>'members')::int THEN RAISE EXCEPTION 're-freeze left duplicates'; END IF;

  -- a failing freeze: make the parse step raise by pointing the row at a
  -- component that no longer validates, then run the job directly.
  UPDATE dr_releases SET state = 'freezing', components = '["nircam/zzz_drf_nomosaic"]' WHERE tag = 'DR-zzz';
  PERFORM public.dr_freeze_job('DR-zzz');
  IF (SELECT state FROM dr_releases WHERE tag = 'DR-zzz') <> 'failed'
     OR (SELECT error FROM dr_releases WHERE tag = 'DR-zzz') NOT LIKE '%no published NIRCam images%' THEN
    RAISE EXCEPTION 'failed freeze not recorded: %', (SELECT to_jsonb(r) FROM dr_releases r WHERE tag = 'DR-zzz');
  END IF;
  -- the rows of the failed attempt are gone (the statement never ran) and a
  -- good attempt from the failed state succeeds again
  got := pg_temp.zzz_dr_as('authenticated', admin,
    $q$SELECT (public.dr_enqueue_freeze('DR-zzz', '["nirspec","nircam/zzz_drf","photometry/zzz_drf/ZZZ CAT v1"]', NULL, true))->>'state'$q$);
  IF got <> 'frozen' THEN RAISE EXCEPTION 'freeze after failure did not succeed: %', got; END IF;
  IF (SELECT error FROM dr_releases WHERE tag = 'DR-zzz') IS NOT NULL THEN RAISE EXCEPTION 'error not cleared'; END IF;

  -- the job does nothing (and does not raise: the cron job must still get
  -- unscheduled by the commit) on a tag that is not freezing
  PERFORM public.dr_freeze_job('DR-zzz');
  IF (SELECT state FROM dr_releases WHERE tag = 'DR-zzz') <> 'frozen' THEN
    RAISE EXCEPTION 'dr_freeze_job on a frozen tag should leave it frozen';
  END IF;

  -- a published mosaic without an active registry row fails the freeze
  -- instead of being dropped silently (Codex on #599)
  INSERT INTO nircam_images (field, tile, filter, pixel_scale, extension, epoch, file_path, deploy_status, deployment_id)
  VALUES ('zzz_drf', 'main', 'f150w', '30mas', 'sci', '', 'data/products/nircam/zzz_drf/f150w/zzz_drf_f150w_sci.fits', 'published', 990101);
  got := pg_temp.zzz_dr_as('authenticated', admin,
    $q$SELECT (public.dr_enqueue_freeze('DR-zzz-mos', '["nircam/zzz_drf"]', NULL, true))->>'state'$q$);
  IF got <> 'failed' OR (SELECT error FROM dr_releases WHERE tag = 'DR-zzz-mos')
       NOT LIKE 'published mosaics without an active registry row%zzz_drf_f150w_sci.fits%' THEN
    RAISE EXCEPTION 'unregistered mosaic should fail the freeze: % / %', got, (SELECT error FROM dr_releases WHERE tag = 'DR-zzz-mos');
  END IF;
  IF EXISTS (SELECT 1 FROM dr_members WHERE tag = 'DR-zzz-mos') THEN
    RAISE EXCEPTION 'the failed freeze left members behind';
  END IF;
  DELETE FROM dr_releases WHERE tag = 'DR-zzz-mos';
  DELETE FROM nircam_images WHERE file_path = 'data/products/nircam/zzz_drf/f150w/zzz_drf_f150w_sci.fits';

  -- likewise a published spectrum whose final FITS has no active registry row
  UPDATE storage_objects so SET status = 'revoked'
    FROM spectra s
   WHERE s.fits_path = '/tmp/zzz_dr_mixp_prism_spec.fits' AND so.spectrum_id = s.spectrum_id
     AND so.product_type = 'nirspec_spec' AND so.status = 'active';
  got := pg_temp.zzz_dr_as('authenticated', admin,
    $q$SELECT (public.dr_enqueue_freeze('DR-zzz-spec', '["nirspec"]', NULL, true))->>'state'$q$);
  IF got <> 'failed' OR (SELECT error FROM dr_releases WHERE tag = 'DR-zzz-spec')
       NOT LIKE 'published spectra without an active registered final%' THEN
    RAISE EXCEPTION 'unregistered final should fail the freeze: % / %', got, (SELECT error FROM dr_releases WHERE tag = 'DR-zzz-spec');
  END IF;
  IF EXISTS (SELECT 1 FROM dr_spectra WHERE tag = 'DR-zzz-spec') THEN
    RAISE EXCEPTION 'the failed freeze left spectra behind';
  END IF;
  UPDATE storage_objects so SET status = 'active'
    FROM spectra s
   WHERE s.fits_path = '/tmp/zzz_dr_mixp_prism_spec.fits' AND so.spectrum_id = s.spectrum_id
     AND so.product_type = 'nirspec_spec' AND so.status = 'revoked';
  DELETE FROM dr_releases WHERE tag = 'DR-zzz-spec';

  -- an active final whose hash is not the catalog row's (a --supabase-only
  -- redeploy left the old row) fails too (Codex on #599)
  UPDATE spectra SET file_hash = 'sha256:' || repeat('e', 64) WHERE fits_path = '/tmp/zzz_dr_mixp_prism_spec.fits';
  got := pg_temp.zzz_dr_as('authenticated', admin,
    $q$SELECT (public.dr_enqueue_freeze('DR-zzz-spec', '["nirspec"]', NULL, true))->>'state'$q$);
  IF got <> 'failed' OR (SELECT error FROM dr_releases WHERE tag = 'DR-zzz-spec')
       NOT LIKE 'published spectra without an active registered final of the same file hash%' THEN
    RAISE EXCEPTION 'stale final should fail the freeze: % / %', got, (SELECT error FROM dr_releases WHERE tag = 'DR-zzz-spec');
  END IF;
  UPDATE spectra SET file_hash = 'sha256:' || repeat('3', 64) WHERE fits_path = '/tmp/zzz_dr_mixp_prism_spec.fits';
  DELETE FROM dr_releases WHERE tag = 'DR-zzz-spec';

  -- a selected catalog whose rows are all orphans freezes nothing: refused
  -- rather than listed as an empty component (Codex on #599)
  INSERT INTO object_photometry (object_id, field, ra, dec, catalog_name, catalog_id, photometry, photo_z)
  VALUES (NULL, 'zzz_drf', 170.5, 4.5, 'ZZZ ORPHANS', 'orphan-1', '{"bands":{}}'::jsonb, 1.1);
  got := pg_temp.zzz_dr_as('authenticated', admin,
    $q$SELECT (public.dr_enqueue_freeze('DR-zzz-orph', '["nirspec","photometry/zzz_drf/ZZZ ORPHANS"]', NULL, true))->>'state'$q$);
  IF got <> 'failed' OR (SELECT error FROM dr_releases WHERE tag = 'DR-zzz-orph')
       NOT LIKE 'photometry components matching no frozen object%ZZZ ORPHANS%' THEN
    RAISE EXCEPTION 'an all-orphan catalog should fail the freeze: % / %', got, (SELECT error FROM dr_releases WHERE tag = 'DR-zzz-orph');
  END IF;
  DELETE FROM dr_releases WHERE tag = 'DR-zzz-orph';
  DELETE FROM object_photometry WHERE catalog_name = 'ZZZ ORPHANS';

  -- nirspec selected with nothing published: refused, not an empty release
  -- (Codex on #599). Triggers off so the objects are left exactly as they were.
  CREATE TEMP TABLE zzz_was_pub AS SELECT id FROM spectra WHERE deploy_status = 'published';
  SET LOCAL session_replication_role = replica;
  UPDATE spectra SET deploy_status = 'draft' WHERE id IN (SELECT id FROM zzz_was_pub);
  SET LOCAL session_replication_role = origin;
  got := pg_temp.zzz_dr_as('authenticated', admin,
    $q$SELECT (public.dr_enqueue_freeze('DR-zzz-none', '["nirspec"]', NULL, true))->>'state'$q$);
  SET LOCAL session_replication_role = replica;
  UPDATE spectra SET deploy_status = 'published' WHERE id IN (SELECT id FROM zzz_was_pub);
  SET LOCAL session_replication_role = origin;
  IF got <> 'failed' OR (SELECT error FROM dr_releases WHERE tag = 'DR-zzz-none')
       NOT LIKE 'the nirspec component froze no spectra%' THEN
    RAISE EXCEPTION 'an empty nirspec freeze should fail: % / %', got, (SELECT error FROM dr_releases WHERE tag = 'DR-zzz-none');
  END IF;
  DELETE FROM dr_releases WHERE tag = 'DR-zzz-none';
  DROP TABLE zzz_was_pub;
  RAISE NOTICE 'OK 3: idempotence + failure path';
END $$;

-- ---------------------------------------------------------------------------
-- 4. RLS matrix
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  admin   uuid := '00000000-0000-0000-0000-0000000000d1';
  grantee uuid := '00000000-0000-0000-0000-0000000000d2';
  nogrant uuid := '00000000-0000-0000-0000-0000000000d3';
  link    uuid := '00000000-0000-0000-0000-0000000000d4';
  surfaces text[][] := ARRAY[
    ['dr_releases',   $q$SELECT count(*)::text FROM dr_releases WHERE tag = 'DR-zzz'$q$],
    ['dr_spectra',    $q$SELECT count(*)::text FROM dr_spectra WHERE tag = 'DR-zzz' AND fits_path LIKE '/tmp/zzz_dr_%'$q$],
    ['dr_objects',    $q$SELECT count(*)::text FROM dr_objects WHERE tag = 'DR-zzz' AND name LIKE 'ZZZ-DR-%'$q$],
    ['dr_members',    $q$SELECT count(*)::text FROM dr_members WHERE tag = 'DR-zzz' AND (storage_key LIKE 'data/products/nirspec/zzz_dr_%' OR storage_key LIKE 'data/products/nircam/zzz_drf/%')$q$],
    ['dr_line_fits',  $q$SELECT count(*)::text FROM dr_line_fits WHERE tag = 'DR-zzz' AND target_id LIKE 'zzz-dr-t-%'$q$],
    ['dr_photometry', $q$SELECT count(*)::text FROM dr_photometry WHERE tag = 'DR-zzz' AND catalog_name = 'ZZZ CAT v1'$q$],
    ['dr_exports',    $q$SELECT count(*)::text FROM dr_exports WHERE tag = 'DR-zzz'$q$]
  ];
  -- frozen (unpublished): [anon, nogrant, grantee, link, admin]
  frozen_expected text[][] := ARRAY[
    ['ERR', '0', '0', '0', '1'],
    ['ERR', '0', '0', '0', '6'],
    ['ERR', '0', '0', '0', '3'],
    ['ERR', '0', '0', '0', '32'],
    ['ERR', '0', '0', '0', '2'],
    ['ERR', '0', '0', '0', '1'],
    ['ERR', '0', '0', '0', '1']
  ];
  -- published: public program rows for nogrant; both for grantee; link nothing
  -- spectra: pub 4 (pub prism, mixp prism, inactive-object prism... = pub,
  -- mixp, inact = 3 public) -- see the counts asserted below.
  pub_expected text[][] := ARRAY[
    ['ERR', '1', '1', '0', '1'],
    ['ERR', '3', '6', '0', '6'],
    -- objects: a reader must hold EVERY program of an object (its frozen
    -- aggregates span them all; Codex on #599): nogrant sees PUB only, not MIX
    ['ERR', '1', '3', '0', '3'],
    -- members: 3 public spectra x 5 = 15 + 2 mosaics = 17 ; grantee all 32
    ['ERR', '17', '32', '0', '32'],
    ['ERR', '2', '2', '0', '2'],   -- both fits are on public-program spectra
    ['ERR', '0', '1', '0', '1'],   -- MIX photometry: same all-programs rule
    ['ERR', '0', '0', '0', '1']    -- exports: admin only
  ];
  i int; got text; who text; uid uuid; role text; j int;
  principals uuid[] := ARRAY[NULL, nogrant, grantee, link, admin];
  names text[] := ARRAY['anon', 'nogrant', 'grantee', 'link', 'admin'];
BEGIN
  INSERT INTO dr_exports (tag, stream, partition, state) VALUES ('DR-zzz', 'spectra', 'zzz_dr_pub', 'pending');

  FOR i IN 1 .. array_length(surfaces, 1) LOOP
    FOR j IN 1 .. 5 LOOP
      role := CASE WHEN j = 1 THEN 'anon' ELSE 'authenticated' END;
      got := pg_temp.zzz_dr_as(role, principals[j], surfaces[i][2]);
      got := CASE WHEN got LIKE 'ERR:%' THEN 'ERR' ELSE got END;
      IF got IS DISTINCT FROM frozen_expected[i][j] THEN
        RAISE EXCEPTION 'MATRIX (frozen) % as %: got % expected %', surfaces[i][1], names[j], got, frozen_expected[i][j];
      END IF;
    END LOOP;
  END LOOP;

  UPDATE dr_releases SET state = 'published', published_at = now() WHERE tag = 'DR-zzz';
  FOR i IN 1 .. array_length(surfaces, 1) LOOP
    FOR j IN 1 .. 5 LOOP
      role := CASE WHEN j = 1 THEN 'anon' ELSE 'authenticated' END;
      got := pg_temp.zzz_dr_as(role, principals[j], surfaces[i][2]);
      got := CASE WHEN got LIKE 'ERR:%' THEN 'ERR' ELSE got END;
      IF got IS DISTINCT FROM pub_expected[i][j] THEN
        RAISE EXCEPTION 'MATRIX (published) % as %: got % expected %', surfaces[i][1], names[j], got, pub_expected[i][j];
      END IF;
    END LOOP;
  END LOOP;

  -- nobody but the RPCs writes: authenticated has no INSERT/UPDATE/DELETE
  got := pg_temp.zzz_dr_as('authenticated', admin, $q$UPDATE dr_releases SET notes = 'x' WHERE tag = 'DR-zzz' RETURNING 'wrote'$q$);
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'authenticated admin could write dr_releases directly'; END IF;
  -- anon cannot call the RPCs
  got := pg_temp.zzz_dr_as('anon', NULL, $q$SELECT (public.dr_enqueue_freeze('DR-anon', '["nirspec"]', NULL, true))::text$q$);
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'anon could call dr_enqueue_freeze'; END IF;
  got := pg_temp.zzz_dr_as('authenticated', nogrant, $q$SELECT public.dr_freeze_job('DR-zzz')::text$q$);
  IF got NOT LIKE 'ERR:%' THEN RAISE EXCEPTION 'authenticated could call dr_freeze_job'; END IF;
  RAISE NOTICE 'OK 4: RLS matrix (frozen: admin only; published: program-scoped, link excluded; exports admin-only; no direct writes)';
END $$;

-- ---------------------------------------------------------------------------
-- 5. Purge
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  admin uuid := '00000000-0000-0000-0000-0000000000d1';
  got text; n int;
BEGIN
  got := pg_temp.zzz_dr_as('authenticated', admin, $q$SELECT (public.dr_purge_release('DR-zzz'))::text$q$);
  IF got NOT LIKE 'ERR: release DR-zzz is published%' THEN RAISE EXCEPTION 'purge of a published tag should be refused, got %', got; END IF;
  got := pg_temp.zzz_dr_as('authenticated', admin, $q$SELECT (public.dr_enqueue_freeze('DR-zzz', '["nirspec"]', NULL, true))::text$q$);
  IF got NOT LIKE 'ERR: release DR-zzz is published%' THEN RAISE EXCEPTION 're-freeze of a published tag should be refused, got %', got; END IF;

  UPDATE dr_releases SET state = 'frozen', published_at = NULL WHERE tag = 'DR-zzz';
  got := pg_temp.zzz_dr_as('authenticated', '00000000-0000-0000-0000-0000000000d3', $q$SELECT (public.dr_purge_release('DR-zzz'))::text$q$);
  IF got NOT LIKE 'ERR: Access denied%' THEN RAISE EXCEPTION 'non-admin purge should be refused, got %', got; END IF;
  SELECT count(*) INTO n FROM dr_members WHERE tag = 'DR-zzz';
  got := pg_temp.zzz_dr_as('authenticated', admin, $q$SELECT (public.dr_purge_release('DR-zzz'))->'deleted'->>'members'$q$);
  IF got <> n::text THEN RAISE EXCEPTION 'purge should report % members, got %', n, got; END IF;
  SELECT count(*) INTO n FROM (
    SELECT tag FROM dr_releases WHERE tag = 'DR-zzz' UNION ALL SELECT tag FROM dr_spectra WHERE tag = 'DR-zzz'
    UNION ALL SELECT tag FROM dr_objects WHERE tag = 'DR-zzz' UNION ALL SELECT tag FROM dr_members WHERE tag = 'DR-zzz'
    UNION ALL SELECT tag FROM dr_line_fits WHERE tag = 'DR-zzz' UNION ALL SELECT tag FROM dr_photometry WHERE tag = 'DR-zzz'
    UNION ALL SELECT tag FROM dr_exports WHERE tag = 'DR-zzz') x;
  IF n <> 0 THEN RAISE EXCEPTION 'purge left % rows', n; END IF;
  IF (SELECT count(*) FROM deploy_events WHERE action = 'delete' AND metadata->>'dr_tag' = 'DR-zzz' AND (metadata->>'purge')::boolean) <> 1 THEN
    RAISE EXCEPTION 'purge event missing';
  END IF;
  got := pg_temp.zzz_dr_as('authenticated', admin, $q$SELECT (public.dr_purge_release('DR-zzz'))::text$q$);
  IF got NOT LIKE 'ERR: no release%' THEN RAISE EXCEPTION 'purge of a missing tag should say so, got %', got; END IF;
  RAISE NOTICE 'OK 5: purge';
END $$;

ROLLBACK;
