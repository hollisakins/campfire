-- Guard: get_storage_budget() counts bytes AT REST -- every row that still
-- holds bytes (live pointers, retained archive copies, GC-pending tombstones:
-- superseded / revoked rows whose object is still in the bucket) at its stored
-- size -- and reports the P2 classes (live / archive / gc_pending / collected)
-- so the archive's growth is visible, revoking a field never makes the usage
-- drop, and a collected row (hash + size kept, bytes gone) never counts
-- against the cap.
--
-- Seed-independent: every assertion is a delta between the payload before and
-- after inserting fixture rows with known sizes.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_storage_budget_classes.sql
BEGIN;

-- The function is admin / service_role gated; call it as the service role.
SELECT set_config('request.jwt.claims', json_build_object('role', 'service_role')::text, true);

CREATE TEMP TABLE _before AS SELECT public.get_storage_budget() AS b;

INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, stored_size_bytes,
                             content_type, product_type, instrument, status, root, archived_at, collected_at) VALUES
  -- live, stored verbatim: 1000 at rest
  ('osn', 'data', 'zzz_bud/live_plain.fits', 'sha256:' || repeat('1', 64), 1000, NULL,
   'application/fits', 'nirspec_spec', 'nirspec', 'active', 'live', NULL, NULL),
  -- live, transport-compressed: 5000 logical, 2000 at rest
  ('osn', 'data', 'zzz_bud/live_gz.fits.gz', 'sha256:' || repeat('2', 64), 5000, 2000,
   'application/fits', 'nircam_mosaic', 'nircam', 'active', 'live', NULL, NULL),
  -- retained archive copy: 300 at rest
  ('osn', 'data', 'archive/' || repeat('3', 64) || '/products/nirspec/zzz_bud/a_spec.fits', 'sha256:' || repeat('3', 64), 300, NULL,
   'application/fits', 'nirspec_spec', 'nirspec', 'archived', 'archive', now(), NULL),
  -- collected archive row: 7000 logical, nothing at rest
  ('osn', 'data', 'archive/' || repeat('4', 64) || '/products/nircam/zzz_bud/m.fits.gz', 'sha256:' || repeat('4', 64), 7000, 4000,
   'application/fits', 'nircam_mosaic', 'nircam', 'collected', 'archive', now() - interval '400 days', now()),
  -- revoked live row: hidden, bytes retained (deploy revoke never deletes): 900 at rest
  ('osn', 'data', 'zzz_bud/revoked.fits', 'sha256:' || repeat('5', 64), 900, NULL,
   'application/fits', 'nirspec_spec', 'nirspec', 'revoked', 'live', NULL, NULL),
  -- superseded tombstone (GC-eligible later, object still in the bucket): 800 at rest
  ('osn', 'data', 'zzz_bud/superseded_old.fits', 'sha256:' || repeat('6', 64), 800, NULL,
   'application/fits', 'nirspec_spec', 'nirspec', 'superseded', 'live', NULL, NULL);

DO $$
DECLARE
  b0 json := (SELECT b FROM _before);
  b1 json := public.get_storage_budget();
  d_total bigint := (b1->>'total_bytes')::bigint - (b0->>'total_bytes')::bigint;
  d_registry bigint := (b1->>'registry_bytes')::bigint - (b0->>'registry_bytes')::bigint;
  d_live bigint := (b1->'by_class'->'live'->>'bytes')::bigint - (b0->'by_class'->'live'->>'bytes')::bigint;
  d_live_n bigint := (b1->'by_class'->'live'->>'count')::bigint - (b0->'by_class'->'live'->>'count')::bigint;
  d_arch bigint := (b1->'by_class'->'archive'->>'bytes')::bigint - (b0->'by_class'->'archive'->>'bytes')::bigint;
  d_arch_n bigint := (b1->'by_class'->'archive'->>'count')::bigint - (b0->'by_class'->'archive'->>'count')::bigint;
  d_gc bigint := (b1->'by_class'->'gc_pending'->>'bytes')::bigint - (b0->'by_class'->'gc_pending'->>'bytes')::bigint;
  d_gc_n bigint := (b1->'by_class'->'gc_pending'->>'count')::bigint - (b0->'by_class'->'gc_pending'->>'count')::bigint;
  d_coll_n bigint := (b1->'by_class'->'collected'->>'count')::bigint - (b0->'by_class'->'collected'->>'count')::bigint;
  d_coll_logical bigint := (b1->'by_class'->'collected'->>'logical_bytes')::bigint - (b0->'by_class'->'collected'->>'logical_bytes')::bigint;
  d_osn bigint := COALESCE((b1->'by_backend'->>'osn')::bigint, 0) - COALESCE((b0->'by_backend'->>'osn')::bigint, 0);
  d_mosaic bigint := COALESCE((b1->'by_product_type'->>'nircam_mosaic')::bigint, 0) - COALESCE((b0->'by_product_type'->>'nircam_mosaic')::bigint, 0);
BEGIN
  -- at rest: 1000 + 2000 (stored, not 5000) + 300 (archive) + 900 (revoked) + 800 (superseded);
  -- only the collected row adds nothing
  IF d_registry <> 5000 THEN RAISE EXCEPTION 'registry_bytes delta: expected 5000, got %', d_registry; END IF;
  IF d_total <> 5000 THEN RAISE EXCEPTION 'total_bytes delta: expected 5000, got %', d_total; END IF;
  IF d_live <> 3000 OR d_live_n <> 2 THEN RAISE EXCEPTION 'by_class.live delta: expected 3000 bytes / 2 rows, got % / %', d_live, d_live_n; END IF;
  IF d_arch <> 300 OR d_arch_n <> 1 THEN RAISE EXCEPTION 'by_class.archive delta: expected 300 bytes / 1 row, got % / %', d_arch, d_arch_n; END IF;
  IF d_gc <> 1700 OR d_gc_n <> 2 THEN RAISE EXCEPTION 'by_class.gc_pending delta: expected 1700 bytes / 2 rows (revoked + superseded), got % / %', d_gc, d_gc_n; END IF;
  IF d_coll_n <> 1 OR d_coll_logical <> 7000 THEN RAISE EXCEPTION 'by_class.collected delta: expected 1 row / 7000 logical, got % / %', d_coll_n, d_coll_logical; END IF;
  IF (b1->'by_class'->'collected'->>'bytes')::bigint <> 0 THEN RAISE EXCEPTION 'collected rows must report 0 bytes at rest'; END IF;
  -- the breakdowns use the same at-rest scope
  IF d_osn <> 5000 THEN RAISE EXCEPTION 'by_backend.osn delta: expected 5000, got %', d_osn; END IF;
  IF d_mosaic <> 2000 THEN RAISE EXCEPTION 'by_product_type.nircam_mosaic delta: expected 2000 (stored size, live only), got %', d_mosaic; END IF;
  -- classes add up to the registry total
  IF (b1->'by_class'->'live'->>'bytes')::bigint + (b1->'by_class'->'archive'->>'bytes')::bigint
     + (b1->'by_class'->'gc_pending'->>'bytes')::bigint <> (b1->>'registry_bytes')::bigint THEN
    RAISE EXCEPTION 'by_class live + archive + gc_pending must equal registry_bytes';
  END IF;
  RAISE NOTICE 'OK: storage budget counts bytes at rest by class; only collected rows cost nothing.';
END $$;

-- A non-admin user is still refused.
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims', json_build_object('sub', '00000000-0000-0000-0000-00000000bb02', 'role', 'authenticated')::text, true);
DO $$
BEGIN
  BEGIN
    PERFORM public.get_storage_budget();
    RAISE EXCEPTION 'get_storage_budget did not refuse a non-admin';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM NOT LIKE 'Access denied%' THEN RAISE; END IF;
  END;
END $$;
RESET ROLE;

ROLLBACK;
