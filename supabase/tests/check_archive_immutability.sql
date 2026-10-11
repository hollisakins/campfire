-- Archive registry rows are write-once (P2, data releases design §4.2 / §7.8).
--
-- Asserts protect_archive_rows: an archive row cannot be deleted, cannot have
-- its identity/provenance columns changed, may only move archived -> collected
-- (which stamps collected_at), and a live row cannot be turned into an archive
-- row in place; live rows keep their normal update/delete behaviour; the
-- root/status CHECKs reject inconsistent rows.
--
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_archive_immutability.sql
BEGIN;

INSERT INTO programs (slug, program_name, is_public) VALUES ('zzz_ar_prog', 'Archive Test', true);
INSERT INTO fields (name) VALUES ('zzz_ar_field');
INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES ('zzz_ar_obs', 'zzz_ar_prog', 9301, 'zzz_ar_field');

INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type, product_type, instrument, status, observation, spectrum_id, root, program_slug, archived_from_key, archived_at, collected_at) VALUES
  ('osn', 'data', 'data/products/nirspec/zzz_ar_obs/zzz_ar_obs_prism_clear_1_spec.fits',
   'sha256:' || repeat('b', 64), 1000, 'application/fits', 'nirspec_spec', 'nirspec', 'active', 'zzz_ar_obs', 'zzz_ar_obs_prism_clear_1', 'live', 'zzz_ar_prog', NULL, NULL, NULL),
  ('osn', 'data', 'archive/' || repeat('a', 64) || '/products/nirspec/zzz_ar_obs/zzz_ar_obs_prism_clear_1_spec.fits',
   'sha256:' || repeat('a', 64), 1000, 'application/fits', 'nirspec_spec', 'nirspec', 'archived', 'zzz_ar_obs', 'zzz_ar_obs_prism_clear_1', 'archive', 'zzz_ar_prog',
   'data/products/nirspec/zzz_ar_obs/zzz_ar_obs_prism_clear_1_spec.fits', now(), '2000-01-01');

DO $$
DECLARE
  k text := 'archive/' || repeat('a', 64) || '/products/nirspec/zzz_ar_obs/zzz_ar_obs_prism_clear_1_spec.fits';
  live_k text := 'data/products/nirspec/zzz_ar_obs/zzz_ar_obs_prism_clear_1_spec.fits';
  n int; ok boolean;
BEGIN
  -- 1. delete refused
  BEGIN
    DELETE FROM storage_objects WHERE storage_key = k; ok := true;
  EXCEPTION WHEN integrity_constraint_violation THEN ok := false; END;
  IF ok THEN RAISE EXCEPTION 'FAIL: archive row deleted'; END IF;

  -- 2. identity change refused
  BEGIN
    UPDATE storage_objects SET content_hash = 'sha256:' || repeat('c', 64) WHERE storage_key = k; ok := true;
  EXCEPTION WHEN integrity_constraint_violation THEN ok := false; END;
  IF ok THEN RAISE EXCEPTION 'FAIL: archive row content_hash rewritten'; END IF;
  BEGIN
    UPDATE storage_objects SET program_slug = 'other' WHERE storage_key = k; ok := true;
  EXCEPTION WHEN integrity_constraint_violation THEN ok := false; END;
  IF ok THEN RAISE EXCEPTION 'FAIL: archive row program_slug rewritten'; END IF;
  -- columns outside the original explicit list are frozen too (whole-row compare)
  BEGIN
    UPDATE storage_objects SET wcs_hash = 'sha256:' || repeat('f', 64) WHERE storage_key = k; ok := true;
  EXCEPTION WHEN integrity_constraint_violation THEN ok := false; END;
  IF ok THEN RAISE EXCEPTION 'FAIL: archive row wcs_hash rewritten'; END IF;
  BEGIN
    UPDATE storage_objects SET content_type = 'text/plain' WHERE storage_key = k; ok := true;
  EXCEPTION WHEN integrity_constraint_violation THEN ok := false; END;
  IF ok THEN RAISE EXCEPTION 'FAIL: archive row content_type rewritten'; END IF;
  -- collected_at supplied at insert is discarded while archived
  SELECT count(*) INTO n FROM storage_objects WHERE storage_key = k AND collected_at IS NOT NULL;
  IF n <> 0 THEN RAISE EXCEPTION 'FAIL: archived row kept a caller-supplied collected_at'; END IF;

  -- 3. status may not go anywhere but collected
  BEGIN
    UPDATE storage_objects SET status = 'active' WHERE storage_key = k; ok := true;
  EXCEPTION WHEN integrity_constraint_violation OR check_violation THEN ok := false; END;
  IF ok THEN RAISE EXCEPTION 'FAIL: archive row became active'; END IF;

  -- 4. archived -> collected allowed and stamped; nothing after that
  UPDATE storage_objects SET status = 'collected', collected_at = '2000-01-01' WHERE storage_key = k;
  SELECT count(*) INTO n FROM storage_objects WHERE storage_key = k AND status = 'collected' AND collected_at > now() - interval '1 minute';
  IF n <> 1 THEN RAISE EXCEPTION 'FAIL: archived -> collected did not stamp collected_at with the transition time'; END IF;
  BEGIN
    UPDATE storage_objects SET status = 'archived' WHERE storage_key = k; ok := true;
  EXCEPTION WHEN integrity_constraint_violation THEN ok := false; END;
  IF ok THEN RAISE EXCEPTION 'FAIL: collected row went back to archived'; END IF;

  -- 5. a live row cannot be flipped into an archive row in place
  BEGIN
    UPDATE storage_objects SET root = 'archive', status = 'archived', storage_key = 'archive/' || repeat('d', 64) || '/x_spec.fits' WHERE storage_key = live_k; ok := true;
  EXCEPTION WHEN integrity_constraint_violation OR check_violation THEN ok := false; END;
  IF ok THEN RAISE EXCEPTION 'FAIL: live row turned into an archive row'; END IF;

  -- 6. live rows behave as before
  UPDATE storage_objects SET content_hash = 'sha256:' || repeat('e', 64), updated_at = now() WHERE storage_key = live_k;
  DELETE FROM storage_objects WHERE storage_key = live_k;
  SELECT count(*) INTO n FROM storage_objects WHERE storage_key = live_k;
  IF n <> 0 THEN RAISE EXCEPTION 'FAIL: live row could not be deleted'; END IF;

  -- 7. CHECKs: an archive row needs an archive key and an archive status
  BEGIN
    INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type, product_type, status, root)
    VALUES ('osn', 'data', 'data/products/nirspec/zzz_ar_obs/bad_spec.fits', 'sha256:' || repeat('f', 64), 1, 'application/fits', 'nirspec_spec', 'archived', 'archive');
    ok := true;
  EXCEPTION WHEN check_violation THEN ok := false; END;
  IF ok THEN RAISE EXCEPTION 'FAIL: archive row accepted with a live key'; END IF;
  BEGIN
    INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type, product_type, status, root)
    VALUES ('osn', 'data', 'data/products/nirspec/zzz_ar_obs/bad2_spec.fits', 'sha256:' || repeat('f', 64), 1, 'application/fits', 'nirspec_spec', 'archived', 'live');
    ok := true;
  EXCEPTION WHEN check_violation THEN ok := false; END;
  IF ok THEN RAISE EXCEPTION 'FAIL: live row accepted with status archived'; END IF;

  RAISE NOTICE 'OK: archive registry rows are immutable (no delete, no rewrite, archived -> collected only); live rows unaffected.';
END $$;

ROLLBACK;
