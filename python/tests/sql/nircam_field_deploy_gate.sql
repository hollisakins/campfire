-- =============================================================================
-- NIRCam field-deployment visibility gate (epic #261, N1) — the exit gate for
-- making NIRCam deploy first-class with the draft->published lifecycle.
-- =============================================================================
-- A NIRCam field spans multiple JWST programs, so there is no per-program scope:
-- a *published* field-scoped deployment is public to EVERYONE; a *draft* one is
-- admin-only. This proves, in one ROLLBACK'd transaction, that the storage_objects
-- gate (RLS + filter_accessible_storage_keys + get_storage_objects_for_sync)
-- enforces exactly that for a nircam_exposure object, and that set_deployment_status
-- flips a field deployment without the "not found" raise (observation is NULL).
--
-- Seed-anchored: user@=2222… (non-admin, public-program access), admin@=1111….
-- Run:  docker exec -i supabase_db_campfire psql -U postgres -d postgres \
--          -v ON_ERROR_STOP=1 -f python/tests/sql/nircam_field_deploy_gate.sql
-- =============================================================================
\set ON_ERROR_STOP on
BEGIN;

-- ---- fixtures (bootstrap superuser, bypasses RLS) ---------------------------
-- A DRAFT field deployment + a nircam_exposure storage object linked to it.
CREATE TEMP TABLE _dep AS
  WITH ins AS (
    INSERT INTO deployments (field, status) VALUES ('leaktest', 'draft') RETURNING id)
  SELECT id FROM ins;

INSERT INTO storage_objects
  (backend, bucket, storage_key, content_hash, size_bytes, content_type,
   product_type, instrument, status, field, spectrum_id, deployment_id)
VALUES
  ('osn', 'data', 'data/products/nircam/leaktest/f444w/jw_gate_nrcalong.fits',
   'sha256:1111111111111111111111111111111111111111111111111111111111111111',
   1024, 'application/fits', 'nircam_exposure', 'nircam', 'active', 'leaktest',
   NULL, (SELECT id FROM _dep));

GRANT SELECT ON _dep TO authenticated, service_role;

CREATE OR REPLACE FUNCTION pg_temp.gate_key() RETURNS text LANGUAGE sql AS
  $f$ SELECT 'data/products/nircam/leaktest/f444w/jw_gate_nrcalong.fits'::text $f$;

-- =========================================================================
-- 1. DRAFT field deployment -> NON-ADMIN sees ZERO (RLS)
-- =========================================================================
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub','22222222-2222-2222-2222-222222222222','role','authenticated')::text, true);
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM storage_objects WHERE storage_key = pg_temp.gate_key();
  IF n <> 0 THEN RAISE EXCEPTION 'RLS LEAK: non-admin sees a DRAFT field-deploy object (expected 0)'; END IF;
END $$;

-- =========================================================================
-- 2. DRAFT -> ADMIN sees it (RLS)
-- =========================================================================
SELECT set_config('request.jwt.claims',
  json_build_object('sub','11111111-1111-1111-1111-111111111111','role','authenticated')::text, true);
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM storage_objects WHERE storage_key = pg_temp.gate_key();
  IF n <> 1 THEN RAISE EXCEPTION 'ADMIN BLOCKED: admin does not see the draft field-deploy object (got %)', n; END IF;
END $$;

-- =========================================================================
-- 3. Service-role RPCs: DRAFT hidden from member view, shown to admin view
-- =========================================================================
RESET ROLE;
SET LOCAL ROLE service_role;
SELECT set_config('request.jwt.claims', '', true);
DO $$
DECLARE pubs text[]; k text[]; n int;
BEGIN
  pubs := ARRAY(SELECT slug FROM programs WHERE is_public);
  k := ARRAY[pg_temp.gate_key()];
  SELECT count(*) INTO n FROM public.filter_accessible_storage_keys(k, pubs, false);
  IF n <> 0 THEN RAISE EXCEPTION 'PRESIGN LEAK: draft field object authorized for member (got %)', n; END IF;
  SELECT count(*) INTO n FROM (
    SELECT jsonb_array_elements(objects)->>'storage_key' AS s
    FROM public.get_storage_objects_for_sync(pubs, NULL, 100000, 0, false, false)) q
    WHERE s = pg_temp.gate_key();
  IF n <> 0 THEN RAISE EXCEPTION 'SYNC LEAK: draft field object in member sync (got %)', n; END IF;
END $$;

-- =========================================================================
-- 4. PUBLISH via set_deployment_status AS ADMIN (must NOT raise on NULL observation)
-- =========================================================================
RESET ROLE;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub','11111111-1111-1111-1111-111111111111','role','authenticated')::text, true);
DO $$
DECLARE r json;
BEGIN
  r := public.set_deployment_status((SELECT id FROM _dep), 'published', NULL, 'testhost');
  IF (r->>'status') <> 'published' THEN
    RAISE EXCEPTION 'set_deployment_status(field) did not publish (got %)', r;
  END IF;
  IF (SELECT status FROM deployments WHERE id = (SELECT id FROM _dep)) <> 'published' THEN
    RAISE EXCEPTION 'deployment row not flipped to published';
  END IF;
END $$;

-- =========================================================================
-- 5. PUBLISHED field deployment -> PUBLIC to everyone (member RPCs + non-admin RLS)
-- =========================================================================
RESET ROLE;
SET LOCAL ROLE service_role;
SELECT set_config('request.jwt.claims', '', true);
DO $$
DECLARE pubs text[]; k text[]; n int;
BEGIN
  pubs := ARRAY(SELECT slug FROM programs WHERE is_public);
  k := ARRAY[pg_temp.gate_key()];
  SELECT count(*) INTO n FROM public.filter_accessible_storage_keys(k, pubs, false);
  IF n <> 1 THEN RAISE EXCEPTION 'PRESIGN: published field object NOT public (got %/1)', n; END IF;
  SELECT count(*) INTO n FROM (
    SELECT jsonb_array_elements(objects)->>'storage_key' AS s
    FROM public.get_storage_objects_for_sync(pubs, NULL, 100000, 0, false, false)) q
    WHERE s = pg_temp.gate_key();
  IF n <> 1 THEN RAISE EXCEPTION 'SYNC: published field object NOT in member sync (got %/1)', n; END IF;
END $$;

RESET ROLE;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub','22222222-2222-2222-2222-222222222222','role','authenticated')::text, true);
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM storage_objects WHERE storage_key = pg_temp.gate_key();
  IF n <> 1 THEN RAISE EXCEPTION 'RLS: non-admin cannot see PUBLISHED field-deploy object (got %/1)', n; END IF;
END $$;

RESET ROLE;
SELECT '✅ NIRCAM FIELD-DEPLOY GATE HARNESS: ALL ASSERTIONS PASSED' AS result;

ROLLBACK;
