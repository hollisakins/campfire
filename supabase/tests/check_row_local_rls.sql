-- Guard: row-local RLS scope columns (perf T2-A, #504, decision D-A).
--
-- spectra.program_slug / spectra.observation and shutters.has_published_spectrum
-- are trigger-owned copies of parent-target state that the read policies on
-- those tables (and, through spectra, on storage_objects) test directly instead
-- of materializing every accessible target per read. A copy that drifts from
-- its parent is an access-control bug in whichever direction it drifts: stale
-- = a private or draft row leaks, over-eager = published data goes dark. This
-- asserts the copies are (a) written by the triggers regardless of what the
-- client sent, (b) cascaded when the parent changes -- including through the
-- real publish/revoke primitive -- and (c) that the rewritten policies still
-- draw the same visibility line for an ordinary user and for an admin.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_row_local_rls.sql
BEGIN;

-- ---------------------------------------------------------------------------
-- Fixtures (inserted as the owner, which bypasses RLS)
-- ---------------------------------------------------------------------------

INSERT INTO programs (slug, program_name, is_public) VALUES
  ('zzz_rl_pub',  'Row-local Public Program',  true),
  ('zzz_rl_priv', 'Row-local Private Program', false);

INSERT INTO fields (name) VALUES ('zzz_rl_field');

INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
  ('zzz_rl_obs_pub',  'zzz_rl_pub',  9201, 'zzz_rl_field'),
  ('zzz_rl_obs_pub2', 'zzz_rl_pub',  9202, 'zzz_rl_field'),
  ('zzz_rl_obs_priv', 'zzz_rl_priv', 9203, 'zzz_rl_field');

INSERT INTO auth.users (id, email) VALUES
  ('00000000-0000-0000-0000-0000000000c1', 'zzz-rl-admin@test.invalid'),
  ('00000000-0000-0000-0000-0000000000c2', 'zzz-rl-user@test.invalid');

-- The ordinary user can comment and inspect but has NO grant on the private
-- program: the only thing separating them from the admin is program access.
INSERT INTO user_profiles (user_id, username, full_name, is_admin, can_comment, can_inspect) VALUES
  ('00000000-0000-0000-0000-0000000000c1', 'zzz-rl-admin', 'ZZZ RL Admin', true,  true, true),
  ('00000000-0000-0000-0000-0000000000c2', 'zzz-rl-user',  'ZZZ RL User',  false, true, true);

-- Three targets: published in the public program, UNPUBLISHED in the public
-- program, published in the private program. Objects are their namesakes.
INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum) VALUES
  ('zzz_rl_pub_t1',  'zzz_rl_field', 10.0, 10.0, '{zzz_rl_pub}',  '{zzz_rl_obs_pub}',  true),
  ('zzz_rl_pub_t2',  'zzz_rl_field', 10.1, 10.0, '{zzz_rl_pub}',  '{zzz_rl_obs_pub}',  false),
  ('zzz_rl_priv_t1', 'zzz_rl_field', 10.2, 10.0, '{zzz_rl_priv}', '{zzz_rl_obs_priv}', true);

INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id, has_published_spectrum) VALUES
  ('zzz_rl_pub_t1',  'zzz_rl_field', 10.0, 10.0, 'zzz_rl_pub',  'zzz_rl_obs_pub',
     (SELECT id FROM objects WHERE object_id = 'zzz_rl_pub_t1'),  true),
  ('zzz_rl_pub_t2',  'zzz_rl_field', 10.1, 10.0, 'zzz_rl_pub',  'zzz_rl_obs_pub',
     (SELECT id FROM objects WHERE object_id = 'zzz_rl_pub_t2'),  false),
  ('zzz_rl_priv_t1', 'zzz_rl_field', 10.2, 10.0, 'zzz_rl_priv', 'zzz_rl_obs_priv',
     (SELECT id FROM objects WHERE object_id = 'zzz_rl_priv_t1'), true);

-- Spectra inserted WITHOUT the scope columns (the deploy CLI sends them, but
-- the trigger must not depend on that) ...
INSERT INTO spectra (target_id, grating, fits_path, deploy_status) VALUES
  ('zzz_rl_pub_t1',  'prism_clear', 'spectra/zzz_rl_obs_pub/zzz_rl_pub_t1_prism_clear_spec.fits',   'published'),
  ('zzz_rl_pub_t2',  'prism_clear', 'spectra/zzz_rl_obs_pub/zzz_rl_pub_t2_prism_clear_spec.fits',   'draft'),
  ('zzz_rl_priv_t1', 'prism_clear', 'spectra/zzz_rl_obs_priv/zzz_rl_priv_t1_prism_clear_spec.fits', 'published');
-- ... and one with a deliberately WRONG scope, which the trigger must overwrite.
INSERT INTO spectra (target_id, grating, fits_path, deploy_status, program_slug, observation) VALUES
  ('zzz_rl_pub_t1', 'g395m_f290lp', 'spectra/zzz_rl_obs_pub/zzz_rl_pub_t1_g395m_f290lp_spec.fits', 'published',
   'zzz_rl_priv', 'zzz_rl_obs_priv');

-- One shutter per target, plus an orphan whose object_id matches no target.
INSERT INTO shutters (field, observation, object_id, source_id, center_ra, center_dec, position_angle, shutter_idx) VALUES
  ('zzz_rl_field', 'zzz_rl_obs_pub',  'zzz_rl_pub_t1',  1, 10.0, 10.0, 0, 0),
  ('zzz_rl_field', 'zzz_rl_obs_pub',  'zzz_rl_pub_t2',  2, 10.1, 10.0, 0, 0),
  ('zzz_rl_field', 'zzz_rl_obs_priv', 'zzz_rl_priv_t1', 3, 10.2, 10.0, 0, 0),
  ('zzz_rl_field', 'zzz_rl_obs_pub',  'zzz_rl_orphan',  4, 10.3, 10.0, 0, 0);

-- A target-level and an object-level comment on each of the three.
INSERT INTO comments (target_id, object_id, user_id, content)
SELECT t.id, NULL, '00000000-0000-0000-0000-0000000000c1'::uuid, 'zzz_rl note' FROM targets t WHERE t.target_id LIKE 'zzz\_rl\_%'
UNION ALL
SELECT NULL, o.id, '00000000-0000-0000-0000-0000000000c1'::uuid, 'zzz_rl note' FROM objects o WHERE o.object_id LIKE 'zzz\_rl\_%';

INSERT INTO object_photometry (object_id, field, ra, dec, catalog_name, photometry)
SELECT o.id, 'zzz_rl_field', o.ra, o.dec, 'zzz_rl_cat', '{}'::jsonb FROM objects o WHERE o.object_id LIKE 'zzz\_rl\_%';

-- Storage rows: one active per spectrum, plus a superseded one on the
-- published public spectrum (admin-only under the folded policy).
INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type, product_type, instrument, spectrum_id, status) VALUES
  ('osn', 'data', '/zzz_rl/pub_t1_prism.fits',     'sha256:a1', 1, 'application/fits', 'nirspec_spec', 'nirspec', 'zzz_rl_pub_t1_prism_clear',  'active'),
  ('osn', 'data', '/zzz_rl/pub_t1_prism_old.fits', 'sha256:a2', 1, 'application/fits', 'nirspec_spec', 'nirspec', 'zzz_rl_pub_t1_prism_clear',  'superseded'),
  ('osn', 'data', '/zzz_rl/pub_t2_prism.fits',     'sha256:a3', 1, 'application/fits', 'nirspec_spec', 'nirspec', 'zzz_rl_pub_t2_prism_clear',  'active'),
  ('osn', 'data', '/zzz_rl/priv_t1_prism.fits',    'sha256:a4', 1, 'application/fits', 'nirspec_spec', 'nirspec', 'zzz_rl_priv_t1_prism_clear', 'active');

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION pg_temp.zzz_rl_as(p_uid text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', 'authenticated')::text, true);
END $$;

CREATE OR REPLACE FUNCTION pg_temp.zzz_rl_visible() RETURNS jsonb
LANGUAGE sql STABLE AS $$
  SELECT jsonb_build_object(
    'spectra',    (SELECT count(*) FROM spectra           WHERE target_id LIKE 'zzz\_rl\_%'),
    'comments',   (SELECT count(*) FROM comments          WHERE content = 'zzz_rl note'),
    'photometry', (SELECT count(*) FROM object_photometry WHERE catalog_name = 'zzz_rl_cat'),
    'shutters',   (SELECT count(*) FROM shutters          WHERE field = 'zzz_rl_field'),
    'storage',    (SELECT count(*) FROM storage_objects   WHERE storage_key LIKE '/zzz\_rl/%'),
    -- The RPC the object page calls (SECURITY INVOKER: this policy is its gate).
    'nearby_t2',  (SELECT count(*) FROM get_nearby_shutters(10.1, 10.0, 2.0, 'zzz_rl_field'))
  );
$$;

-- ---------------------------------------------------------------------------
-- 1) The copies are trigger-owned: written on insert, client value overridden.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  n_drift int;
  v_slug text;
  v_obs text;
BEGIN
  SELECT count(*) INTO n_drift
  FROM spectra s JOIN targets t USING (target_id)
  WHERE s.target_id LIKE 'zzz\_rl\_%'
    AND (s.program_slug IS DISTINCT FROM t.program_slug OR s.observation IS DISTINCT FROM t.observation);
  IF n_drift <> 0 THEN
    RAISE EXCEPTION 'spectra scope drift after insert: % rows disagree with their target', n_drift;
  END IF;

  SELECT program_slug, observation INTO v_slug, v_obs
  FROM spectra WHERE target_id = 'zzz_rl_pub_t1' AND grating = 'g395m_f290lp';
  IF v_slug <> 'zzz_rl_pub' OR v_obs <> 'zzz_rl_obs_pub' THEN
    RAISE EXCEPTION 'sync_spectra_target_scope accepted a client-supplied scope (%, %)', v_slug, v_obs;
  END IF;
  RAISE NOTICE 'ok: spectra.program_slug / observation are trigger-owned';
END $$;

DO $$
DECLARE
  v jsonb;
BEGIN
  SELECT jsonb_object_agg(object_id, has_published_spectrum) INTO v
  FROM shutters WHERE field = 'zzz_rl_field';
  IF v <> '{"zzz_rl_pub_t1": true, "zzz_rl_pub_t2": false, "zzz_rl_priv_t1": true, "zzz_rl_orphan": true}'::jsonb THEN
    RAISE EXCEPTION 'shutters.has_published_spectrum after insert: %', v;
  END IF;
  RAISE NOTICE 'ok: shutters.has_published_spectrum follows the target on insert (orphan stays visible)';
END $$;

-- ---------------------------------------------------------------------------
-- 2) Cascades: a target that moves observation drags its spectra along.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  n_moved int;
BEGIN
  UPDATE targets SET observation = 'zzz_rl_obs_pub2' WHERE target_id = 'zzz_rl_pub_t1';
  SELECT count(*) INTO n_moved FROM spectra WHERE target_id = 'zzz_rl_pub_t1' AND observation = 'zzz_rl_obs_pub2';
  IF n_moved <> 2 THEN
    RAISE EXCEPTION 'propagate_target_scope_to_spectra: expected 2 spectra on zzz_rl_obs_pub2, got %', n_moved;
  END IF;
  UPDATE targets SET observation = 'zzz_rl_obs_pub' WHERE target_id = 'zzz_rl_pub_t1';
  RAISE NOTICE 'ok: target observation change cascades to spectra';
END $$;

-- ---------------------------------------------------------------------------
-- 3) Cascades through the real publish / revoke primitive:
--    set_spectra_deploy_status -> recompute_has_published_spectrum -> targets
--    -> propagate_target_publication_to_shutters.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  v_id int;
  v_flag boolean;
BEGIN
  PERFORM pg_temp.zzz_rl_as('00000000-0000-0000-0000-0000000000c1');   -- admin, for the RPC's gate
  SELECT id INTO v_id FROM spectra WHERE target_id = 'zzz_rl_pub_t2' AND grating = 'prism_clear';

  PERFORM set_spectra_deploy_status(ARRAY[v_id], 'published');
  SELECT has_published_spectrum INTO v_flag FROM shutters WHERE object_id = 'zzz_rl_pub_t2';
  IF NOT v_flag THEN
    RAISE EXCEPTION 'publishing zzz_rl_pub_t2 did not flip its shutter to visible';
  END IF;

  PERFORM set_spectra_deploy_status(ARRAY[v_id], 'draft', 'upload');
  SELECT has_published_spectrum INTO v_flag FROM shutters WHERE object_id = 'zzz_rl_pub_t2';
  IF v_flag THEN
    RAISE EXCEPTION 'un-publishing zzz_rl_pub_t2 did not flip its shutter back to hidden';
  END IF;
  RAISE NOTICE 'ok: publish / revoke cascades to shutters through recompute_has_published_spectrum';
END $$;

-- ---------------------------------------------------------------------------
-- 4) Visibility under the rewritten policies.
-- ---------------------------------------------------------------------------
SET LOCAL ROLE authenticated;

DO $$
DECLARE
  v jsonb;
  v_expected jsonb;
  n int;
BEGIN
  -- Ordinary user: public program only, published only.
  PERFORM pg_temp.zzz_rl_as('00000000-0000-0000-0000-0000000000c2');
  v := pg_temp.zzz_rl_visible();
  v_expected := jsonb_build_object(
    'spectra',    2,   -- pub_t1 prism + g395m; not the draft, not the private
    'comments',   2,   -- target-level + object-level on pub_t1 only
    'photometry', 1,   -- pub_t1's object
    -- shutters carry no program gate (#229): pub_t1 + priv_t1 + orphan; NOT
    -- the unpublished pub_t2.
    'shutters',   3,
    'storage',    1,   -- pub_t1's ACTIVE row; the superseded one is admin-only
    'nearby_t2',  0    -- the RPC path hides the unpublished target's shutter too
  );
  IF v <> v_expected THEN
    RAISE EXCEPTION 'ordinary user sees % (expected %)', v, v_expected;
  END IF;

  -- DQ update path: allowed on a visible published spectrum ...
  UPDATE spectra SET dq_flags = 1 WHERE target_id = 'zzz_rl_pub_t1' AND grating = 'prism_clear';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 1 THEN
    RAISE EXCEPTION 'inspector could not set dq_flags on a visible published spectrum (rows=%)', n;
  END IF;
  -- ... a no-op on a private one (invisible, so 0 rows rather than an error) ...
  UPDATE spectra SET dq_flags = 1 WHERE target_id = 'zzz_rl_priv_t1';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN
    RAISE EXCEPTION 'inspector updated % private spectra', n;
  END IF;
  -- ... and the scope columns are not theirs to touch.
  BEGIN
    UPDATE spectra SET program_slug = 'zzz_rl_priv' WHERE target_id = 'zzz_rl_pub_t1' AND grating = 'prism_clear';
    RAISE EXCEPTION 'inspector rewrote spectra.program_slug';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;

  -- Comment insert: on an accessible target yes, on a private one no.
  INSERT INTO comments (target_id, user_id, content)
  VALUES ((SELECT id FROM targets WHERE target_id = 'zzz_rl_pub_t1'), '00000000-0000-0000-0000-0000000000c2', 'zzz_rl user note');
  BEGIN
    INSERT INTO comments (target_id, user_id, content)
    VALUES ((SELECT id FROM targets WHERE target_id = 'zzz_rl_priv_t1'), '00000000-0000-0000-0000-0000000000c2', 'zzz_rl user note');
    RAISE EXCEPTION 'ordinary user commented on a private target';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
  RAISE NOTICE 'ok: ordinary user sees exactly the published public rows; writes are scoped the same way';

  -- Admin: everything, including the draft, the private program and the
  -- superseded storage row (the folded admin branch). comments counts the six
  -- fixture rows; the user's own insert above carries different content.
  PERFORM pg_temp.zzz_rl_as('00000000-0000-0000-0000-0000000000c1');
  v := pg_temp.zzz_rl_visible();
  v_expected := jsonb_build_object(
    'spectra', 4, 'comments', 6, 'photometry', 3, 'shutters', 4, 'storage', 4, 'nearby_t2', 1
  );
  IF v <> v_expected THEN
    RAISE EXCEPTION 'admin sees % (expected %)', v, v_expected;
  END IF;
  RAISE NOTICE 'ok: admin sees every row, superseded storage included';
END $$;

ROLLBACK;
