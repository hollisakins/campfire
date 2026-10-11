-- =============================================================================
-- dr_readiness() harness (data releases P1, docs/design-data-releases-impl-plan.md §3).
-- =============================================================================
-- Proves the readiness report:
--   1. runs for service_role and for an authenticated admin, with every key
--      present and the stage-1 counters consistent with the seed;
--   2. detects fixture rows: a re-drafted NIRCam field (published deployment +
--      a mosaic row flipped to draft) but NOT a field that has only ever been
--      draft; a dangling active nirspec_spec registry row (no spectra row);
--      an etag: hash; a live deploy lease (and its stale flag);
--   3. raises for a non-admin and is not executable by anon.
--
-- One transaction, ROLLBACK'd. Seed-anchored uuids: admin@=1111…, user@=2222….
-- =============================================================================
\set ON_ERROR_STOP on
\set ADMIN '11111111-1111-1111-1111-111111111111'
\set USER '22222222-2222-2222-2222-222222222222'
BEGIN;

-- ---------------------------------------------------------------------------
-- Fixture (as postgres: FKs need a program + observation)
-- ---------------------------------------------------------------------------
INSERT INTO programs (slug, program_name, is_public) VALUES ('zzz_dr_prog', 'DR Readiness Program', true);
INSERT INTO observations (name, program_slug, jwst_program_id, field)
  VALUES ('zzz_dr_obs', 'zzz_dr_prog', 9301, 'zzz_dr_field');

-- a field that was published and now has one mosaic back in draft
INSERT INTO deployments (field, status, published_at) VALUES ('zzz_dr_field', 'published', now());
INSERT INTO nircam_images (field, tile, filter, pixel_scale, extension, epoch, file_path, deploy_status) VALUES
  ('zzz_dr_field', 'main', 'f444w', '30mas', 'sci', '', '/tmp/zzz_dr_f444w.fits', 'published'),
  ('zzz_dr_field', 'main', 'f277w', '30mas', 'sci', '', '/tmp/zzz_dr_f277w.fits', 'draft');
-- a field that has only ever been draft (must NOT be listed)
INSERT INTO deployments (field, status) VALUES ('zzz_dr_draftonly', 'draft');
INSERT INTO nircam_images (field, tile, filter, pixel_scale, extension, epoch, file_path, deploy_status) VALUES
  ('zzz_dr_draftonly', 'main', 'f444w', '30mas', 'sci', '', '/tmp/zzz_dr_draftonly.fits', 'draft');

-- a registry row for a spectrum that no longer exists + a legacy etag: preview
INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type,
                             product_type, instrument, status, observation, spectrum_id) VALUES
  ('osn', 'data', 'nirspec/zzz_dr_obs/zzz_dr_obs_prism_clear_999999_spec.fits', 'sha256:' || repeat('a', 64),
   1000, 'application/fits', 'nirspec_spec', 'nirspec', 'active', 'zzz_dr_obs', 'zzz_dr_obs_prism_clear_999999'),
  ('r2', 'data', 'nircam/zzz_dr_field/previews/zzz_dr_preview.png', 'etag:' || repeat('b', 32),
   2000, 'image/png', 'nircam_exposure_preview', 'nircam', 'active', NULL, NULL);

-- ---------------------------------------------------------------------------
-- 1 + 2. service_role: full report with the fixture visible
-- ---------------------------------------------------------------------------
SET LOCAL ROLE service_role;
SELECT set_config('request.jwt.claims', json_build_object('role', 'service_role')::text, true);

DO $$
DECLARE
  r jsonb;
  k text;
  n_pub bigint;
  n_obs bigint;
  n_nr bigint;
  n_pins bigint;
  v_sum bigint;
  lease jsonb;
BEGIN
  -- a live lease, and a stale one, to show up in the report
  PERFORM public.begin_deploy_scope('observation', 'zzz_dr_obs', NULL, 'host-live');
  PERFORM public.begin_deploy_scope('field', 'zzz_dr_field', NULL, 'host-stale');
  UPDATE deploy_scope_state SET heartbeat_at = now() - interval '2 hours', in_progress_since = now() - interval '3 hours'
   WHERE scope_type = 'field' AND scope_key = 'zzz_dr_field';

  r := public.dr_readiness();

  FOREACH k IN ARRAY ARRAY['generated_at', 'spectra_by_version', 'published', 'drafts', 'revoked',
                           'leases', 'redrafted_nircam_fields', 'dangling_registry_rows',
                           'etag_hash_rows', 'line_fits', 'needs_review', 'implicit_pins',
                           'science_hash_missing'] LOOP
    IF NOT (r ? k) THEN
      RAISE EXCEPTION 'KEYS: report lacks %: %', k, r::text;
    END IF;
  END LOOP;

  -- published counters agree with direct counts; versions sum to the total
  SELECT count(*) INTO n_pub FROM spectra WHERE deploy_status = 'published';
  SELECT count(DISTINCT observation) INTO n_obs FROM spectra WHERE deploy_status = 'published';
  IF (r->'published'->>'spectra')::bigint <> n_pub OR (r->'published'->>'observations')::bigint <> n_obs THEN
    RAISE EXCEPTION 'PUBLISHED: expected % spectra / % observations, got %', n_pub, n_obs, r->'published';
  END IF;
  SELECT COALESCE(sum((v->>'spectra')::bigint), 0) INTO v_sum FROM jsonb_each(r->'spectra_by_version') AS e(k, v);
  IF v_sum <> n_pub THEN
    RAISE EXCEPTION 'VERSIONS: per-version spectra sum to %, published is %', v_sum, n_pub;
  END IF;
  SELECT count(*) INTO n_obs FROM objects WHERE is_active AND has_published_spectrum;
  IF (r->'published'->>'objects')::bigint <> n_obs THEN
    RAISE EXCEPTION 'OBJECTS: expected %, got %', n_obs, r->'published'->>'objects';
  END IF;

  -- needs_review: the catalog predicate over active objects
  SELECT count(*) INTO n_nr FROM objects
   WHERE is_active AND staleness_reason IS NOT NULL AND last_inspected_at IS NOT NULL
     AND (last_data_change_at IS NULL OR last_data_change_at > last_inspected_at);
  SELECT count(*) INTO n_pins FROM objects WHERE is_active AND inspected_used_auto;
  IF (r->>'needs_review')::bigint <> n_nr OR (r->>'implicit_pins')::bigint <> n_pins THEN
    RAISE EXCEPTION 'REVIEW: expected needs_review % / pins %, got % / %', n_nr, n_pins, r->>'needs_review', r->>'implicit_pins';
  END IF;

  -- drafts: the fixture's draft mosaic rows and draft deployment are counted
  IF (r->'drafts'->>'nircam_images')::int < 2 OR (r->'drafts'->>'deployments')::int < 1 THEN
    RAISE EXCEPTION 'DRAFTS: expected >= 2 draft mosaics and >= 1 draft deployment, got %', r->'drafts';
  END IF;

  -- re-drafted field detected, draft-only field not listed
  IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(r->'redrafted_nircam_fields') e
                 WHERE e->>'field' = 'zzz_dr_field'
                   AND (e->>'draft_images')::int = 1 AND (e->>'published_images')::int = 1
                   AND (e->>'published_deployments')::int = 1) THEN
    RAISE EXCEPTION 'REDRAFTED: zzz_dr_field not detected: %', r->'redrafted_nircam_fields';
  END IF;
  IF EXISTS (SELECT 1 FROM jsonb_array_elements(r->'redrafted_nircam_fields') e WHERE e->>'field' = 'zzz_dr_draftonly') THEN
    RAISE EXCEPTION 'REDRAFTED: draft-only field listed: %', r->'redrafted_nircam_fields';
  END IF;

  -- dangling registry row detected and attributed to its observation
  IF (r->'dangling_registry_rows'->>'count')::int < 1
     OR (r->'dangling_registry_rows'->'by_observation'->>'zzz_dr_obs')::int <> 1 THEN
    RAISE EXCEPTION 'DANGLING: fixture row not detected: %', r->'dangling_registry_rows';
  END IF;

  -- etag: row counted under its product type
  IF (r->'etag_hash_rows'->>'nircam_exposure_preview')::int < 1 THEN
    RAISE EXCEPTION 'ETAG: fixture row not counted: %', r->'etag_hash_rows';
  END IF;

  -- leases: the live one is not stale, the old one is
  SELECT e INTO lease FROM jsonb_array_elements(r->'leases') e WHERE e->>'scope_key' = 'zzz_dr_obs';
  IF lease IS NULL OR (lease->>'stale')::boolean OR lease->>'in_progress_host' <> 'host-live' THEN
    RAISE EXCEPTION 'LEASES: live lease missing or misreported: %', r->'leases';
  END IF;
  SELECT e INTO lease FROM jsonb_array_elements(r->'leases') e WHERE e->>'scope_key' = 'zzz_dr_field';
  IF lease IS NULL OR NOT (lease->>'stale')::boolean THEN
    RAISE EXCEPTION 'LEASES: stale lease missing or not flagged: %', r->'leases';
  END IF;

  -- line fits: counts are numbers (seed may carry none)
  IF (r->'line_fits'->>'total') IS NULL THEN
    RAISE EXCEPTION 'LINE FITS: total missing: %', r->'line_fits';
  END IF;

  -- science_hash_missing: null before PR #586, a number after — never an error
  IF NOT (r->'science_hash_missing' ? 'spectra') OR NOT (r->'science_hash_missing' ? 'storage_objects') THEN
    RAISE EXCEPTION 'SCIENCE HASH: keys missing: %', r->'science_hash_missing';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1b. authenticated admin: same report
-- ---------------------------------------------------------------------------
RESET ROLE;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub', :'ADMIN', 'role', 'authenticated')::text, true);

DO $$
DECLARE r jsonb;
BEGIN
  r := public.dr_readiness();
  IF (r->'dangling_registry_rows'->>'count')::int < 1 THEN
    RAISE EXCEPTION 'ADMIN: report differs from service_role: %', r->'dangling_registry_rows';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. non-admin raises; anon cannot execute
-- ---------------------------------------------------------------------------
RESET ROLE;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub', :'USER', 'role', 'authenticated')::text, true);

DO $$
DECLARE denied boolean := false;
BEGIN
  BEGIN
    PERFORM public.dr_readiness();
  EXCEPTION WHEN OTHERS THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'GATE: non-admin could run dr_readiness';
  END IF;
END $$;

RESET ROLE;
SET LOCAL ROLE anon;
SELECT set_config('request.jwt.claims', json_build_object('role', 'anon')::text, true);

DO $$
DECLARE denied boolean := false;
BEGIN
  BEGIN
    PERFORM public.dr_readiness();
  EXCEPTION WHEN insufficient_privilege THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'ANON: anon can execute dr_readiness (REVOKE missing)';
  END IF;
END $$;

RESET ROLE;
SELECT '✅ DR READINESS HARNESS: ALL ASSERTIONS PASSED' AS result;

ROLLBACK;
