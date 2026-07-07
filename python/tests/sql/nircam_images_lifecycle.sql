-- =============================================================================
-- NIRCam mosaic (nircam_images) lifecycle gate (epic #261, N2).
-- =============================================================================
-- The public NIRCam page reads nircam_images. A published mosaic is public to
-- everyone (a field spans multiple programs); draft/revoked are admin-only. This
-- proves the nircam_images RLS enforces that, and that set_deployment_status on a
-- NIRCam field deployment flips its mosaics' deploy_status (publish -> revoke).
--
-- Seed-anchored: user@=2222… (non-admin), admin@=1111….
-- Run:  docker exec -i supabase_db_campfire psql -U postgres -d postgres \
--          -v ON_ERROR_STOP=1 -f python/tests/sql/nircam_images_lifecycle.sql
-- =============================================================================
\set ON_ERROR_STOP on
BEGIN;

-- ---- fixtures (superuser) ---------------------------------------------------
CREATE TEMP TABLE _dep AS
  WITH ins AS (
    INSERT INTO deployments (field, status) VALUES ('leaktest', 'draft') RETURNING id)
  SELECT id FROM ins;

INSERT INTO nircam_images
  (field, tile, filter, pixel_scale, extension, file_path, deploy_status, deployment_id)
VALUES
  ('leaktest', 'A1', 'f444w', '30mas', 'i2d',
   'data/products/nircam/leaktest/f444w/mosaic_nircam_f444w_leaktest_30mas_A1_i2d.fits',
   'draft', (SELECT id FROM _dep));

GRANT SELECT ON _dep TO authenticated, service_role;

-- 1. DRAFT -> non-admin sees ZERO
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub','22222222-2222-2222-2222-222222222222','role','authenticated')::text, true);
DO $$ DECLARE n int; BEGIN
  SELECT count(*) INTO n FROM nircam_images WHERE field='leaktest';
  IF n <> 0 THEN RAISE EXCEPTION 'RLS LEAK: non-admin sees a DRAFT mosaic (expected 0)'; END IF;
END $$;

-- 2. DRAFT -> admin sees it
SELECT set_config('request.jwt.claims',
  json_build_object('sub','11111111-1111-1111-1111-111111111111','role','authenticated')::text, true);
DO $$ DECLARE n int; BEGIN
  SELECT count(*) INTO n FROM nircam_images WHERE field='leaktest';
  IF n <> 1 THEN RAISE EXCEPTION 'ADMIN BLOCKED: admin does not see the draft mosaic (got %)', n; END IF;
END $$;

-- 3. PUBLISH via set_deployment_status (as admin) flips nircam_images.deploy_status
DO $$ DECLARE r json; BEGIN
  r := public.set_deployment_status((SELECT id FROM _dep), 'published', NULL, 'testhost');
  IF ((r->'nircam_images')->>'updated')::int <> 1 THEN
    RAISE EXCEPTION 'set_deployment_status did not flip 1 mosaic (got %)', r;
  END IF;
END $$;

-- 4. PUBLISHED -> non-admin sees it
SELECT set_config('request.jwt.claims',
  json_build_object('sub','22222222-2222-2222-2222-222222222222','role','authenticated')::text, true);
DO $$ DECLARE n int; BEGIN
  SELECT count(*) INTO n FROM nircam_images WHERE field='leaktest';
  IF n <> 1 THEN RAISE EXCEPTION 'RLS: non-admin cannot see PUBLISHED mosaic (got %/1)', n; END IF;
END $$;

-- 5. REVOKE (as admin) -> non-admin sees ZERO again
SELECT set_config('request.jwt.claims',
  json_build_object('sub','11111111-1111-1111-1111-111111111111','role','authenticated')::text, true);
DO $$ BEGIN PERFORM public.set_deployment_status((SELECT id FROM _dep), 'revoked', NULL, 'testhost'); END $$;
SELECT set_config('request.jwt.claims',
  json_build_object('sub','22222222-2222-2222-2222-222222222222','role','authenticated')::text, true);
DO $$ DECLARE n int; BEGIN
  SELECT count(*) INTO n FROM nircam_images WHERE field='leaktest';
  IF n <> 0 THEN RAISE EXCEPTION 'RLS LEAK: non-admin sees a REVOKED mosaic (expected 0)'; END IF;
END $$;

RESET ROLE;
SELECT '✅ NIRCAM IMAGES LIFECYCLE HARNESS: ALL ASSERTIONS PASSED' AS result;

ROLLBACK;
