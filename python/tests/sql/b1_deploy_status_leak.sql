-- =============================================================================
-- B1 (#217) deploy_status leak harness  —  the lifecycle-enforcement exit gate.
-- =============================================================================
-- Proves, against a freshly `supabase db reset` local DB, that:
--   1. A NON-ADMIN authenticated user (with full program access) gets ZERO
--      draft/revoked spectra/objects/targets through RLS, while a published
--      control row stays visible.
--   2. An ADMIN sees the unpublished rows (the `OR is_admin()` branch).
--   3. The service-role RPC predicate `p_include_unpublished` gates: false hides
--      the unpublished objects, true reveals them (this is the gate the /api/v1
--      REST routes rely on, where RLS is bypassed).
--
-- Everything runs in one transaction and is ROLLBACK'd — no persistence. The
-- fixture sets has_published_spectrum=false by hand because B1 ships no
-- population logic (that is B2); the value is always true in normal B1 operation.
--
-- Run:  docker exec -i supabase_db_campfire psql -U postgres -d postgres \
--          -v ON_ERROR_STOP=1 -f python/tests/sql/b1_deploy_status_leak.sql
-- A RAISE (leak) makes psql exit non-zero; success prints the PASS marker.
-- Seed-anchored: user@=2222…, admin@=1111…, victims = objects 1 & 2 in the
-- public program castellano3073, control = object 3 (published).
-- =============================================================================
\set ON_ERROR_STOP on
BEGIN;

-- ---- fixtures (run as the bootstrap superuser, bypasses RLS) ----------------
CREATE TEMP TABLE _vobj (oid int, st text);
INSERT INTO _vobj VALUES (1, 'draft'), (2, 'revoked');

CREATE TEMP TABLE _vtgt AS
  SELECT t.id, t.target_id, v.st
  FROM public.targets t JOIN _vobj v ON t.object_id = v.oid;

CREATE TEMP TABLE _vspec AS
  SELECT s.id, s.spectrum_id, vt.st
  FROM public.spectra s JOIN _vtgt vt ON s.target_id = vt.target_id;

CREATE TEMP TABLE _vobjid AS
  SELECT object_id FROM public.objects WHERE id IN (SELECT oid FROM _vobj);

DO $$ BEGIN
  IF (SELECT count(*) FROM _vspec) = 0 THEN
    RAISE EXCEPTION 'FIXTURE EMPTY: victim objects have no member spectra — seed shape changed';
  END IF;
  IF (SELECT count(*) FROM public.objects WHERE id = 3 AND has_published_spectrum) = 0 THEN
    RAISE EXCEPTION 'FIXTURE: control object 3 is not a published object — seed shape changed';
  END IF;
END $$;

-- Simulate B2 state: victims become unpublished.
UPDATE public.spectra s SET deploy_status = vs.st FROM _vspec vs WHERE s.id = vs.id;
UPDATE public.objects SET has_published_spectrum = false WHERE id IN (SELECT oid FROM _vobj);
UPDATE public.targets SET has_published_spectrum = false WHERE id IN (SELECT id FROM _vtgt);

-- The non-superuser roles need to read the victim-id scratch tables (these hold
-- only ids and are not RLS-protected); the base-table reads below stay RLS-gated.
GRANT SELECT ON _vobj, _vtgt, _vspec, _vobjid TO authenticated, service_role;

-- =========================================================================
-- 1. RLS as NON-ADMIN (user@, public-program access) -> zero leak
-- =========================================================================
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub', '22222222-2222-2222-2222-222222222222', 'role', 'authenticated')::text, true);

DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM public.spectra WHERE id IN (SELECT id FROM _vspec);
  IF n <> 0 THEN RAISE EXCEPTION 'RLS LEAK: non-admin sees % draft/revoked spectra (expected 0)', n; END IF;

  SELECT count(*) INTO n FROM public.objects WHERE id IN (SELECT oid FROM _vobj);
  IF n <> 0 THEN RAISE EXCEPTION 'RLS LEAK: non-admin sees % unpublished objects (expected 0)', n; END IF;

  SELECT count(*) INTO n FROM public.targets WHERE id IN (SELECT id FROM _vtgt);
  IF n <> 0 THEN RAISE EXCEPTION 'RLS LEAK: non-admin sees % unpublished targets (expected 0)', n; END IF;

  -- Regression guard: a published object in the SAME program stays visible.
  SELECT count(*) INTO n FROM public.objects WHERE id = 3;
  IF n <> 1 THEN RAISE EXCEPTION 'REGRESSION: non-admin cannot see published control object (got %, expected 1)', n; END IF;
END $$;

-- =========================================================================
-- 2. RLS as ADMIN (admin@) -> unpublished rows ARE visible
-- =========================================================================
SELECT set_config('request.jwt.claims',
  json_build_object('sub', '11111111-1111-1111-1111-111111111111', 'role', 'authenticated')::text, true);

DO $$
DECLARE n int; v int;
BEGIN
  SELECT count(*) INTO v FROM _vspec;
  SELECT count(*) INTO n FROM public.spectra WHERE id IN (SELECT id FROM _vspec);
  IF n <> v THEN RAISE EXCEPTION 'ADMIN BLOCKED: admin sees %/% unpublished spectra', n, v; END IF;

  SELECT count(*) INTO n FROM public.objects WHERE id IN (SELECT oid FROM _vobj);
  IF n <> 2 THEN RAISE EXCEPTION 'ADMIN BLOCKED: admin sees %/2 unpublished objects', n; END IF;
END $$;

-- =========================================================================
-- 3. RPC predicate as SERVICE_ROLE (RLS bypassed) -> p_include_unpublished gates
-- =========================================================================
RESET ROLE;
SET LOCAL ROLE service_role;
SELECT set_config('request.jwt.claims', '', true);

DO $$
DECLARE leaked int; shown int;
BEGIN
  SELECT count(*) INTO leaked
    FROM public.get_filtered_object_ids(ARRAY['castellano3073']::text[], p_include_unpublished := false) g
    WHERE g.object_id IN (SELECT object_id FROM _vobjid);
  IF leaked <> 0 THEN
    RAISE EXCEPTION 'RPC LEAK: get_filtered_object_ids(p_include_unpublished=false) returns % unpublished objects (expected 0)', leaked;
  END IF;

  SELECT count(*) INTO shown
    FROM public.get_filtered_object_ids(ARRAY['castellano3073']::text[], p_include_unpublished := true) g
    WHERE g.object_id IN (SELECT object_id FROM _vobjid);
  IF shown <> 2 THEN
    RAISE EXCEPTION 'RPC SWITCH BROKEN: get_filtered_object_ids(p_include_unpublished=true) returns %/2 unpublished objects', shown;
  END IF;
END $$;

RESET ROLE;
SELECT '✅ B1 DEPLOY_STATUS LEAK HARNESS: ALL ASSERTIONS PASSED' AS result;

ROLLBACK;
