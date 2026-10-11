-- =============================================================================
-- Deploy lease harness (data releases P1, docs/design-data-releases-impl-plan.md §3).
-- =============================================================================
-- Proves the visible lease on deploy_scope_state:
--   1. begin on a fresh scope inserts the row, takes the lease, returns version 0.
--   2. a second begin while the lease is live RAISES deploy_scope_busy (CF409).
--   3. heartbeat succeeds only for the holder (actor + host); a different host
--      or actor gets false.
--   4. p_steal takes a live lease over (stolen=true, previous.live=true); the
--      displaced deploy's heartbeat now returns false.
--   5. release by the holder clears the lease without a version bump; a second
--      release is a no-op (false).
--   6. a lease whose heartbeat is older than deploy_lease_ttl() is stale and is
--      taken over WITHOUT p_steal (took_over_stale=true).
--   7. a successful claim_deploy_scope bumps the version AND clears the lease.
--   8. an authenticated admin calling with no p_actor is identified by
--      auth.uid(): begin + heartbeat agree.
--   9. a non-admin is denied all four RPCs; anon cannot execute them at all.
--
-- One transaction, ROLLBACK'd. A RAISE makes psql exit non-zero; success prints
-- the PASS marker. Seed-anchored uuids: admin@=1111…, user@=2222….
-- =============================================================================
\set ON_ERROR_STOP on
\set ADMIN '11111111-1111-1111-1111-111111111111'
\set USER '22222222-2222-2222-2222-222222222222'
BEGIN;

SET LOCAL ROLE service_role;
SELECT set_config('request.jwt.claims', json_build_object('role', 'service_role')::text, true);

DO $$
DECLARE
  r json;
  v_row deploy_scope_state%ROWTYPE;
  v_busy boolean := false;
  v_state text;
  v_msg text;
  v_detail text;
  a constant uuid := '11111111-1111-1111-1111-111111111111';
  b constant uuid := '22222222-2222-2222-2222-222222222222';
BEGIN
  -- 1. fresh scope: row inserted, lease taken, version 0
  r := public.begin_deploy_scope('observation', 'lease_obs', a, 'host-a');
  IF (r->>'version')::int <> 0 OR (r->>'took_over_stale')::boolean OR (r->>'stolen')::boolean
     OR r->'previous' IS NULL OR (r->'previous')::text <> 'null' THEN
    RAISE EXCEPTION 'BEGIN1: expected version 0 / fresh lease, got %', r::text;
  END IF;
  SELECT * INTO v_row FROM deploy_scope_state WHERE scope_type = 'observation' AND scope_key = 'lease_obs';
  IF v_row.in_progress_since IS NULL OR v_row.in_progress_by <> a OR v_row.in_progress_host <> 'host-a'
     OR v_row.heartbeat_at IS NULL OR v_row.version <> 0 THEN
    RAISE EXCEPTION 'BEGIN1 ROW: lease fields not set: %', row_to_json(v_row)::text;
  END IF;

  -- 2. live lease → busy
  BEGIN
    PERFORM public.begin_deploy_scope('observation', 'lease_obs', b, 'host-b');
  EXCEPTION WHEN OTHERS THEN
    v_busy := true;
    GET STACKED DIAGNOSTICS v_state = RETURNED_SQLSTATE, v_msg = MESSAGE_TEXT, v_detail = PG_EXCEPTION_DETAIL;
  END;
  IF NOT v_busy THEN
    RAISE EXCEPTION 'BUSY: second begin on a live lease was allowed';
  END IF;
  IF v_state <> 'CF409' OR v_msg NOT LIKE 'deploy_scope_busy:%' THEN
    RAISE EXCEPTION 'BUSY: expected CF409 deploy_scope_busy, got % %', v_state, v_msg;
  END IF;
  IF (v_detail::json->>'in_progress_host') <> 'host-a' OR NOT (v_detail::json->>'live')::boolean THEN
    RAISE EXCEPTION 'BUSY DETAIL: expected the holder in DETAIL, got %', v_detail;
  END IF;
  -- the refused begin must not have touched the lease
  SELECT * INTO v_row FROM deploy_scope_state WHERE scope_type = 'observation' AND scope_key = 'lease_obs';
  IF v_row.in_progress_by <> a OR v_row.in_progress_host <> 'host-a' THEN
    RAISE EXCEPTION 'BUSY ROW: refused begin changed the lease: %', row_to_json(v_row)::text;
  END IF;

  -- 3. heartbeat: holder only
  IF NOT public.heartbeat_deploy_scope('observation', 'lease_obs', a, 'host-a') THEN
    RAISE EXCEPTION 'HB1: holder heartbeat returned false';
  END IF;
  IF public.heartbeat_deploy_scope('observation', 'lease_obs', a, 'host-b') THEN
    RAISE EXCEPTION 'HB2: same actor on another host could heartbeat';
  END IF;
  IF public.heartbeat_deploy_scope('observation', 'lease_obs', b, 'host-a') THEN
    RAISE EXCEPTION 'HB3: another actor could heartbeat';
  END IF;
  IF public.heartbeat_deploy_scope('observation', 'no_such_scope', a, 'host-a') THEN
    RAISE EXCEPTION 'HB4: heartbeat on a scope without a lease returned true';
  END IF;

  -- 4. steal
  r := public.begin_deploy_scope('observation', 'lease_obs', b, 'host-b', true);
  IF NOT (r->>'stolen')::boolean OR (r->>'took_over_stale')::boolean
     OR (r->'previous'->>'in_progress_host') <> 'host-a' OR NOT (r->'previous'->>'live')::boolean THEN
    RAISE EXCEPTION 'STEAL: expected stolen=true with previous host-a live, got %', r::text;
  END IF;
  IF public.heartbeat_deploy_scope('observation', 'lease_obs', a, 'host-a') THEN
    RAISE EXCEPTION 'STEAL HB: displaced deploy could still heartbeat';
  END IF;
  IF NOT public.heartbeat_deploy_scope('observation', 'lease_obs', b, 'host-b') THEN
    RAISE EXCEPTION 'STEAL HB2: new holder cannot heartbeat';
  END IF;

  -- 5. release: holder only, no version bump, idempotent
  IF public.release_deploy_scope('observation', 'lease_obs', a, 'host-a') THEN
    RAISE EXCEPTION 'REL1: non-holder release returned true';
  END IF;
  IF NOT public.release_deploy_scope('observation', 'lease_obs', b, 'host-b') THEN
    RAISE EXCEPTION 'REL2: holder release returned false';
  END IF;
  SELECT * INTO v_row FROM deploy_scope_state WHERE scope_type = 'observation' AND scope_key = 'lease_obs';
  IF v_row.in_progress_since IS NOT NULL OR v_row.in_progress_by IS NOT NULL
     OR v_row.in_progress_host IS NOT NULL OR v_row.heartbeat_at IS NOT NULL OR v_row.version <> 0 THEN
    RAISE EXCEPTION 'REL ROW: release left state behind or bumped the version: %', row_to_json(v_row)::text;
  END IF;
  IF public.release_deploy_scope('observation', 'lease_obs', b, 'host-b') THEN
    RAISE EXCEPTION 'REL3: second release returned true';
  END IF;

  -- 6. stale lease: taken over without p_steal
  PERFORM public.begin_deploy_scope('observation', 'lease_obs', a, 'host-a');
  UPDATE deploy_scope_state
     SET heartbeat_at = now() - public.deploy_lease_ttl() - interval '1 minute',
         in_progress_since = now() - interval '3 hours'
   WHERE scope_type = 'observation' AND scope_key = 'lease_obs';
  r := public.begin_deploy_scope('observation', 'lease_obs', b, 'host-b');
  IF NOT (r->>'took_over_stale')::boolean OR (r->>'stolen')::boolean
     OR (r->'previous'->>'in_progress_host') <> 'host-a' OR (r->'previous'->>'live')::boolean THEN
    RAISE EXCEPTION 'STALE: expected took_over_stale=true, got %', r::text;
  END IF;
  SELECT * INTO v_row FROM deploy_scope_state WHERE scope_type = 'observation' AND scope_key = 'lease_obs';
  IF v_row.in_progress_by <> b OR v_row.in_progress_host <> 'host-b'
     OR v_row.heartbeat_at < now() - interval '1 minute' THEN
    RAISE EXCEPTION 'STALE ROW: takeover did not refresh the lease: %', row_to_json(v_row)::text;
  END IF;
  -- a heartbeat exactly at the edge: ttl - 1 minute is still live
  UPDATE deploy_scope_state SET heartbeat_at = now() - public.deploy_lease_ttl() + interval '1 minute'
   WHERE scope_type = 'observation' AND scope_key = 'lease_obs';
  v_busy := false;
  BEGIN
    PERFORM public.begin_deploy_scope('observation', 'lease_obs', a, 'host-a');
  EXCEPTION WHEN SQLSTATE 'CF409' THEN
    v_busy := true;
  END;
  IF NOT v_busy THEN
    RAISE EXCEPTION 'EDGE: a heartbeat younger than the ttl was treated as stale';
  END IF;

  -- 7. claim clears the lease and bumps the version
  r := public.claim_deploy_scope('observation', 'lease_obs', 0, b);
  IF NOT (r->>'claimed')::boolean OR (r->>'version')::int <> 1 THEN
    RAISE EXCEPTION 'CLAIM: expected claimed v1, got %', r::text;
  END IF;
  SELECT * INTO v_row FROM deploy_scope_state WHERE scope_type = 'observation' AND scope_key = 'lease_obs';
  IF v_row.in_progress_since IS NOT NULL OR v_row.in_progress_by IS NOT NULL
     OR v_row.in_progress_host IS NOT NULL OR v_row.heartbeat_at IS NOT NULL
     OR v_row.version <> 1 OR v_row.last_actor <> b THEN
    RAISE EXCEPTION 'CLAIM ROW: claim did not clear the lease: %', row_to_json(v_row)::text;
  END IF;
  -- begin on the claimed scope reports the new version (the CAS value for finalize)
  r := public.begin_deploy_scope('observation', 'lease_obs', a, 'host-a');
  IF (r->>'version')::int <> 1 THEN
    RAISE EXCEPTION 'BEGIN AFTER CLAIM: expected version 1, got %', r::text;
  END IF;
  -- a CONFLICTING claim (stale expected version) leaves the lease alone
  r := public.claim_deploy_scope('observation', 'lease_obs', 0, b);
  IF (r->>'claimed')::boolean OR NOT (r->>'conflict')::boolean THEN
    RAISE EXCEPTION 'CLAIM CONFLICT: expected conflict, got %', r::text;
  END IF;
  SELECT * INTO v_row FROM deploy_scope_state WHERE scope_type = 'observation' AND scope_key = 'lease_obs';
  IF v_row.in_progress_by IS DISTINCT FROM a OR v_row.version <> 1 THEN
    RAISE EXCEPTION 'CLAIM CONFLICT ROW: conflicting claim changed the lease: %', row_to_json(v_row)::text;
  END IF;
  PERFORM public.release_deploy_scope('observation', 'lease_obs', a, 'host-a');

  -- invalid scope type
  v_busy := false;
  BEGIN
    PERFORM public.begin_deploy_scope('program', 'x', a, 'host-a');
  EXCEPTION WHEN OTHERS THEN
    v_busy := true;
  END;
  IF NOT v_busy THEN
    RAISE EXCEPTION 'SCOPE TYPE: invalid scope_type was accepted';
  END IF;
END $$;

-- 8. authenticated admin, no explicit actor: identified by auth.uid()
RESET ROLE;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub', :'ADMIN', 'role', 'authenticated')::text, true);

DO $$
DECLARE r json; v_by uuid;
BEGIN
  r := public.begin_deploy_scope('field', 'lease_field', NULL, 'laptop');
  SELECT in_progress_by INTO v_by FROM deploy_scope_state WHERE scope_type = 'field' AND scope_key = 'lease_field';
  IF v_by IS DISTINCT FROM '11111111-1111-1111-1111-111111111111'::uuid THEN
    RAISE EXCEPTION 'ADMIN BEGIN: in_progress_by should default to auth.uid(), got %', v_by;
  END IF;
  IF NOT public.heartbeat_deploy_scope('field', 'lease_field') THEN
    RAISE EXCEPTION 'ADMIN HB: default-actor heartbeat should match the lease';
  END IF;
  IF NOT public.release_deploy_scope('field', 'lease_field') THEN
    RAISE EXCEPTION 'ADMIN REL: default-actor release should clear the lease';
  END IF;
END $$;

-- 9. non-admin denied; anon has no execute privilege
RESET ROLE;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub', :'USER', 'role', 'authenticated')::text, true);

DO $$
DECLARE denied int := 0;
BEGIN
  BEGIN PERFORM public.begin_deploy_scope('observation', 'lease_obs'); EXCEPTION WHEN OTHERS THEN denied := denied + 1; END;
  BEGIN PERFORM public.heartbeat_deploy_scope('observation', 'lease_obs'); EXCEPTION WHEN OTHERS THEN denied := denied + 1; END;
  BEGIN PERFORM public.release_deploy_scope('observation', 'lease_obs'); EXCEPTION WHEN OTHERS THEN denied := denied + 1; END;
  BEGIN PERFORM public.claim_deploy_scope('observation', 'lease_obs', 1); EXCEPTION WHEN OTHERS THEN denied := denied + 1; END;
  IF denied <> 4 THEN
    RAISE EXCEPTION 'GATE: non-admin was allowed % of 4 lease RPCs', 4 - denied;
  END IF;
END $$;

RESET ROLE;
SET LOCAL ROLE anon;
SELECT set_config('request.jwt.claims', json_build_object('role', 'anon')::text, true);

DO $$
DECLARE denied int := 0;
BEGIN
  BEGIN PERFORM public.begin_deploy_scope('observation', 'lease_obs'); EXCEPTION WHEN insufficient_privilege THEN denied := denied + 1; END;
  BEGIN PERFORM public.heartbeat_deploy_scope('observation', 'lease_obs'); EXCEPTION WHEN insufficient_privilege THEN denied := denied + 1; END;
  BEGIN PERFORM public.release_deploy_scope('observation', 'lease_obs'); EXCEPTION WHEN insufficient_privilege THEN denied := denied + 1; END;
  BEGIN PERFORM public.deploy_lease_ttl(); EXCEPTION WHEN insufficient_privilege THEN denied := denied + 1; END;
  IF denied <> 4 THEN
    RAISE EXCEPTION 'ANON: anon could execute % of 4 lease functions (REVOKE missing)', 4 - denied;
  END IF;
END $$;

RESET ROLE;
SELECT '✅ DEPLOY LEASE HARNESS: ALL ASSERTIONS PASSED' AS result;

ROLLBACK;
