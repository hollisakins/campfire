-- =============================================================================
-- B2 (#218) intermediate-product lifecycle harness.
-- =============================================================================
-- Builds on the B1 leak gate. Drives the seed's published control object (#3,
-- public program castellano3073) through a full revoke -> recover cycle using
-- the B2 lifecycle RPCs, and proves end to end that:
--   A. get_lifecycle_status().enabled is true (the capability marker the deploy
--      CLI gates `--in-prep` on — confirms B1 is applied to this DB).
--   B. set_spectra_deploy_status('revoked') flips spectra.deploy_status AND
--      recomputes targets/objects.has_published_spectrum to false AND writes a
--      deploy_events audit row — all in one call.
--   C. A NON-ADMIN then sees ZERO of the revoked object/spectra through RLS, and
--      ZERO rows of the admin-only deploy_events / spectrum_exposures tables.
--   D. set_spectra_deploy_status('published') recovers: has_published_spectrum
--      back to true, an admin can read spectrum_exposures, a 'recover' audit row.
--   E. The non-admin sees the recovered object again (regression guard).
--   F. service_role can log_deploy_event (the deploy 'upload' path).
--   G. A non-admin is DENIED the lifecycle RPC (the admin/service_role gate).
--
-- One transaction, ROLLBACK'd. A RAISE (any failed assertion) makes psql exit
-- non-zero; success prints the PASS marker. Seed-anchored: admin@=1111…,
-- user@=2222…, control object id=3 in castellano3073.
-- =============================================================================
\set ON_ERROR_STOP on
\set ADMIN '11111111-1111-1111-1111-111111111111'
\set USER  '22222222-2222-2222-2222-222222222222'
BEGIN;

-- ---- fixtures (bootstrap superuser, bypasses RLS) ---------------------------
CREATE TEMP TABLE _o3tgt AS
  SELECT t.id, t.target_id FROM public.targets t WHERE t.object_id = 3;
CREATE TEMP TABLE _o3spec AS
  SELECT s.id FROM public.spectra s JOIN _o3tgt t ON s.target_id = t.target_id;

DO $$ BEGIN
  IF (SELECT count(*) FROM _o3spec) = 0 THEN
    RAISE EXCEPTION 'FIXTURE EMPTY: object 3 has no member spectra — seed shape changed';
  END IF;
  IF (SELECT count(*) FROM public.objects WHERE id = 3 AND has_published_spectrum) = 0 THEN
    RAISE EXCEPTION 'FIXTURE: object 3 is not published at start — seed shape changed';
  END IF;
END $$;

-- A spectrum_exposures row for an object-3 spectrum, so the admin-only RLS on
-- that table is exercised against real data (it is otherwise empty here).
INSERT INTO public.spectrum_exposures (spectrum_id, exposure_ref, stage)
  SELECT (SELECT id FROM _o3spec ORDER BY id LIMIT 1), 'harness_root_1_nrs1_999', 'cal';

GRANT SELECT ON _o3tgt, _o3spec TO authenticated, service_role;

-- =========================================================================
-- A. capability marker enabled (B1 applied), checked as ADMIN
-- =========================================================================
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub', :'ADMIN', 'role', 'authenticated')::text, true);

DO $$
DECLARE st json;
BEGIN
  st := public.get_lifecycle_status();
  IF (st->>'enabled')::boolean IS NOT TRUE THEN
    RAISE EXCEPTION 'CAPABILITY: get_lifecycle_status().enabled is not true: %', st::text;
  END IF;
END $$;

-- =========================================================================
-- B. revoke object 3 via the lifecycle RPC (admin) -> status + recompute + audit
-- =========================================================================
DO $$
DECLARE ids int[]; flag boolean;
BEGIN
  SELECT array_agg(id) INTO ids FROM _o3spec;
  PERFORM public.set_spectra_deploy_status(
    p_spectrum_db_ids := ids, p_to := 'revoked', p_action := 'revoke',
    p_actor := '11111111-1111-1111-1111-111111111111'::uuid);

  IF EXISTS (SELECT 1 FROM public.spectra WHERE id = ANY(ids) AND deploy_status <> 'revoked') THEN
    RAISE EXCEPTION 'STATUS: a member spectrum was not set revoked';
  END IF;
  SELECT has_published_spectrum INTO flag FROM public.objects WHERE id = 3;
  IF flag IS NOT FALSE THEN
    RAISE EXCEPTION 'RECOMPUTE: object 3 has_published_spectrum=% after revoke (expected false)', flag;
  END IF;
  IF EXISTS (SELECT 1 FROM public.targets WHERE object_id = 3 AND has_published_spectrum) THEN
    RAISE EXCEPTION 'RECOMPUTE: a target of object 3 still has_published_spectrum after revoke';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.deploy_events WHERE action = 'revoke' AND status_to = 'revoked') THEN
    RAISE EXCEPTION 'AUDIT: no deploy_events row for the revoke';
  END IF;
END $$;

-- =========================================================================
-- C. NON-ADMIN sees zero of the revoked object + zero admin-only-table rows
-- =========================================================================
SELECT set_config('request.jwt.claims',
  json_build_object('sub', :'USER', 'role', 'authenticated')::text, true);

DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM public.objects WHERE id = 3;
  IF n <> 0 THEN RAISE EXCEPTION 'LEAK: non-admin sees revoked object 3 (got %, expected 0)', n; END IF;

  SELECT count(*) INTO n FROM public.spectra WHERE id IN (SELECT id FROM _o3spec);
  IF n <> 0 THEN RAISE EXCEPTION 'LEAK: non-admin sees % revoked spectra of object 3', n; END IF;

  -- Admin-only audit log: rows exist (the revoke above), non-admin sees none.
  SELECT count(*) INTO n FROM public.deploy_events;
  IF n <> 0 THEN RAISE EXCEPTION 'LEAK: non-admin reads deploy_events (% rows)', n; END IF;

  -- Admin-only intermediates: a row exists (fixture), non-admin sees none.
  SELECT count(*) INTO n FROM public.spectrum_exposures;
  IF n <> 0 THEN RAISE EXCEPTION 'LEAK: non-admin reads spectrum_exposures (% rows)', n; END IF;
END $$;

-- =========================================================================
-- D. recover via the RPC (admin) -> visible again + admin reads intermediates
-- =========================================================================
SELECT set_config('request.jwt.claims',
  json_build_object('sub', :'ADMIN', 'role', 'authenticated')::text, true);

DO $$
DECLARE ids int[]; flag boolean; n int;
BEGIN
  SELECT array_agg(id) INTO ids FROM _o3spec;
  PERFORM public.set_spectra_deploy_status(
    p_spectrum_db_ids := ids, p_to := 'published', p_action := 'recover',
    p_actor := '11111111-1111-1111-1111-111111111111'::uuid);

  SELECT has_published_spectrum INTO flag FROM public.objects WHERE id = 3;
  IF flag IS NOT TRUE THEN
    RAISE EXCEPTION 'RECOMPUTE: object 3 has_published_spectrum=% after recover (expected true)', flag;
  END IF;
  SELECT count(*) INTO n FROM public.spectrum_exposures;
  IF n < 1 THEN RAISE EXCEPTION 'ADMIN BLOCKED: admin cannot read spectrum_exposures (got %)', n; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.deploy_events WHERE action = 'recover' AND status_to = 'published') THEN
    RAISE EXCEPTION 'AUDIT: no deploy_events row for the recover';
  END IF;
END $$;

-- =========================================================================
-- E. NON-ADMIN sees the recovered object again (regression guard)
-- =========================================================================
SELECT set_config('request.jwt.claims',
  json_build_object('sub', :'USER', 'role', 'authenticated')::text, true);

DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM public.objects WHERE id = 3;
  IF n <> 1 THEN RAISE EXCEPTION 'REGRESSION: non-admin cannot see recovered object 3 (got %, expected 1)', n; END IF;
END $$;

-- =========================================================================
-- F. service_role can write an 'upload' audit event (the deploy path)
-- =========================================================================
RESET ROLE;
SET LOCAL ROLE service_role;
SELECT set_config('request.jwt.claims', json_build_object('role', 'service_role')::text, true);

DO $$
DECLARE ev uuid;
BEGIN
  ev := public.log_deploy_event(p_action := 'upload', p_observation := 'harness_obs', p_affected_count := 3);
  IF ev IS NULL THEN RAISE EXCEPTION 'service_role could not log_deploy_event'; END IF;
END $$;

-- =========================================================================
-- G. a NON-ADMIN authenticated caller is DENIED the lifecycle RPC
-- =========================================================================
RESET ROLE;
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub', :'USER', 'role', 'authenticated')::text, true);

DO $$
DECLARE denied boolean := false;
BEGIN
  BEGIN
    PERFORM public.set_spectra_deploy_status(p_spectrum_db_ids := ARRAY[1], p_to := 'revoked');
  EXCEPTION WHEN OTHERS THEN
    denied := true;  -- expected: 'Access denied: Admin privileges required'
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'GATE: non-admin was allowed to call set_spectra_deploy_status';
  END IF;
END $$;

RESET ROLE;
SELECT '✅ B2 LIFECYCLE HARNESS: ALL ASSERTIONS PASSED' AS result;

ROLLBACK;
