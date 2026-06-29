-- =============================================================================
-- B4 (#220) multi-reducer concurrency harness.
-- =============================================================================
-- Proves the optimistic compare-and-set in claim_deploy_scope detects a
-- concurrent deploy of the same scope (the "not silently clobbered" gate):
--   1. First deploy of a new scope (expected version 0) -> claimed v1.
--   2. Reducer A (read v1) commits -> v2.
--   3. Reducer B (also read v1, stale) commits -> CONFLICT (current is now 2).
--   4. B re-reads (v2) and retries -> claimed v3.
--   5. A non-admin is DENIED the RPC (admin/service_role gate).
--
-- One transaction, ROLLBACK'd. A RAISE makes psql exit non-zero; success prints
-- the PASS marker. Seed-anchored only for the non-admin uuid (user@=2222…).
-- =============================================================================
\set ON_ERROR_STOP on
\set USER '22222222-2222-2222-2222-222222222222'
BEGIN;

SET LOCAL ROLE service_role;
SELECT set_config('request.jwt.claims', json_build_object('role', 'service_role')::text, true);

DO $$
DECLARE r json;
BEGIN
  -- 1. new scope, expected 0 -> v1
  r := public.claim_deploy_scope('observation', 'harness_obs', 0);
  IF NOT (r->>'claimed')::boolean OR (r->>'version')::int <> 1 THEN
    RAISE EXCEPTION 'CLAIM1: expected claimed v1, got %', r::text;
  END IF;

  -- 2. reducer A read v1, commits -> v2
  r := public.claim_deploy_scope('observation', 'harness_obs', 1);
  IF NOT (r->>'claimed')::boolean OR (r->>'version')::int <> 2 THEN
    RAISE EXCEPTION 'CLAIM A: expected claimed v2, got %', r::text;
  END IF;

  -- 3. reducer B read v1 too (stale) -> CONFLICT (current is 2)
  r := public.claim_deploy_scope('observation', 'harness_obs', 1);
  IF (r->>'claimed')::boolean OR NOT (r->>'conflict')::boolean OR (r->>'current')::int <> 2 THEN
    RAISE EXCEPTION 'CLAIM B: expected conflict (current 2), got %', r::text;
  END IF;

  -- 4. B re-reads (2) and retries -> v3
  r := public.claim_deploy_scope('observation', 'harness_obs', 2);
  IF NOT (r->>'claimed')::boolean OR (r->>'version')::int <> 3 THEN
    RAISE EXCEPTION 'CLAIM B2: expected claimed v3, got %', r::text;
  END IF;

  -- get_deploy_scope_version reflects the latest
  IF public.get_deploy_scope_version('observation', 'harness_obs') <> 3 THEN
    RAISE EXCEPTION 'VERSION: get_deploy_scope_version should be 3';
  END IF;
END $$;

-- 5. non-admin is denied
RESET ROLE;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub', :'USER', 'role', 'authenticated')::text, true);

DO $$
DECLARE denied boolean := false;
BEGIN
  BEGIN
    PERFORM public.claim_deploy_scope('observation', 'harness_obs', 3);
  EXCEPTION WHEN OTHERS THEN
    denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'GATE: non-admin was allowed to claim_deploy_scope';
  END IF;
END $$;

RESET ROLE;
SELECT '✅ B4 MULTI-REDUCER HARNESS: ALL ASSERTIONS PASSED' AS result;

ROLLBACK;
