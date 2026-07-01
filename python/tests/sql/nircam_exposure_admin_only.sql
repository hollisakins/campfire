-- =============================================================================
-- NIRCam exposure admin-only leak harness (epic #261, N1) — the exit gate for
-- deploying canonical NIRCam exposures to the registry.
-- =============================================================================
-- A NIRCam canonical exposure is a reduction intermediate, never public science.
-- N1 registers it in storage_objects with spectrum_id=NULL AND deployment_id=NULL,
-- so the existing #210/#217 two-surface enforcement keeps it admin-only WITHOUT any
-- new policy — the "backfilled NIRCam is admin-only until a deployment lands" case
-- the storage_objects RLS explicitly anticipates. This harness proves that a
-- NON-ADMIN authenticated user (with full public-program access) gets ZERO NIRCam
-- exposure objects through every reader — RLS, get_storage_objects_for_sync, and
-- filter_accessible_storage_keys — while an ADMIN sees them, and that the same is
-- true for the admin-only nircam_exposures triage table.
--
-- Runs in one ROLLBACK'd transaction. Seed-anchored: user@=2222…, admin@=1111….
--
-- Run:  docker exec -i supabase_db_campfire psql -U postgres -d postgres \
--          -v ON_ERROR_STOP=1 -f python/tests/sql/nircam_exposure_admin_only.sql
-- =============================================================================
\set ON_ERROR_STOP on
BEGIN;

-- ---- fixtures (bootstrap superuser, bypasses RLS) ---------------------------
-- One canonical exposure storage object + expmap, admin-only by construction
-- (no spectrum_id, no deployment_id), and one nircam_exposures triage row.
CREATE TEMP TABLE _nc_keys (storage_key text);
INSERT INTO _nc_keys VALUES
  ('data/products/nircam/leaktest/f444w/jw01727028001_04101_00003_nrcalong.fits'),
  ('data/products/nircam/leaktest/expmaps/leaktest_f444w_expmap.fits');

INSERT INTO storage_objects
  (backend, bucket, storage_key, content_hash, sci_dq_hash, size_bytes,
   content_type, product_type, instrument, status, field,
   spectrum_id, deployment_id)
VALUES
  ('osn', 'data', 'data/products/nircam/leaktest/f444w/jw01727028001_04101_00003_nrcalong.fits',
   'sha256:1111111111111111111111111111111111111111111111111111111111111111',
   'sha256:2222222222222222222222222222222222222222222222222222222222222222',
   1024, 'application/fits', 'nircam_exposure', 'nircam', 'active', 'leaktest',
   NULL, NULL),
  ('osn', 'data', 'data/products/nircam/leaktest/expmaps/leaktest_f444w_expmap.fits',
   'sha256:3333333333333333333333333333333333333333333333333333333333333333',
   NULL,
   2048, 'application/fits', 'nircam_expmap', 'nircam', 'active', 'leaktest',
   NULL, NULL);

INSERT INTO nircam_exposures (field, filter, detector, filename, stage)
VALUES ('leaktest', 'f444w', 'nrcalong', 'jw01727028001_04101_00003_nrcalong', 'outlier');

GRANT SELECT ON _nc_keys TO authenticated, service_role;

-- =========================================================================
-- 1. RLS as NON-ADMIN (user@, full public-program access) -> zero leak
-- =========================================================================
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub', '22222222-2222-2222-2222-222222222222', 'role', 'authenticated')::text, true);

DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM storage_objects
    WHERE storage_key IN (SELECT storage_key FROM _nc_keys);
  IF n <> 0 THEN
    RAISE EXCEPTION 'RLS LEAK: non-admin sees % NIRCam exposure storage object(s) (expected 0)', n;
  END IF;

  SELECT count(*) INTO n FROM nircam_exposures WHERE field = 'leaktest';
  IF n <> 0 THEN
    RAISE EXCEPTION 'RLS LEAK: non-admin sees % nircam_exposures triage row(s) (expected 0)', n;
  END IF;
END $$;

-- =========================================================================
-- 2. RLS as ADMIN (admin@) -> the objects ARE visible
-- =========================================================================
SELECT set_config('request.jwt.claims',
  json_build_object('sub', '11111111-1111-1111-1111-111111111111', 'role', 'authenticated')::text, true);

DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM storage_objects
    WHERE storage_key IN (SELECT storage_key FROM _nc_keys);
  IF n <> 2 THEN RAISE EXCEPTION 'ADMIN BLOCKED: admin sees %/2 NIRCam exposure objects', n; END IF;

  SELECT count(*) INTO n FROM nircam_exposures WHERE field = 'leaktest';
  IF n <> 1 THEN RAISE EXCEPTION 'ADMIN BLOCKED: admin sees %/1 nircam_exposures row', n; END IF;
END $$;

-- =========================================================================
-- 3. Service-role RPC predicates (RLS bypassed) -> admin-only both ways
-- =========================================================================
RESET ROLE;
SET LOCAL ROLE service_role;
SELECT set_config('request.jwt.claims', '', true);

DO $$
DECLARE pubs text[];
        keys text[];
        leaked int; shown int;
BEGIN
  pubs := ARRAY(SELECT slug FROM programs WHERE is_public);
  keys := ARRAY(SELECT storage_key FROM _nc_keys);

  -- filter_accessible_storage_keys (presign gate): a member is authorized for NONE
  -- of the exposure keys (no spectrum, no published deployment), admin for ALL.
  SELECT count(*) INTO leaked FROM public.filter_accessible_storage_keys(keys, pubs, false);
  IF leaked <> 0 THEN
    RAISE EXCEPTION 'PRESIGN LEAK: % NIRCam exposure key(s) authorized for a member (expected 0)', leaked;
  END IF;

  SELECT count(*) INTO shown FROM public.filter_accessible_storage_keys(keys, pubs, true);
  IF shown <> 2 THEN
    RAISE EXCEPTION 'PRESIGN ADMIN: include_unpublished=true authorized %/2 exposure keys', shown;
  END IF;

  -- get_storage_objects_for_sync: absent from a member sync, present for admin.
  SELECT count(*) INTO leaked FROM (
    SELECT jsonb_array_elements(objects)->>'storage_key' AS k
    FROM public.get_storage_objects_for_sync(pubs, NULL, 100000, 0, false, false)
  ) q WHERE k IN (SELECT storage_key FROM _nc_keys);
  IF leaked <> 0 THEN
    RAISE EXCEPTION 'SYNC LEAK: % NIRCam exposure row(s) in a member sync (expected 0)', leaked;
  END IF;

  SELECT count(*) INTO shown FROM (
    SELECT jsonb_array_elements(objects)->>'storage_key' AS k
    FROM public.get_storage_objects_for_sync(pubs, NULL, 100000, 0, false, true)
  ) q WHERE k IN (SELECT storage_key FROM _nc_keys);
  IF shown <> 2 THEN
    RAISE EXCEPTION 'SYNC ADMIN: admin sync returns %/2 NIRCam exposure rows', shown;
  END IF;
END $$;

RESET ROLE;
SELECT '✅ NIRCAM EXPOSURE ADMIN-ONLY HARNESS: ALL ASSERTIONS PASSED' AS result;

ROLLBACK;
