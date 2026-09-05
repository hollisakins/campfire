-- =============================================================================
-- storage_objects scope leak harness (epic #210) — the exit gate for opening the
-- registry from admin-only to program-scoped reads.
-- =============================================================================
-- Proves, against a freshly `supabase db reset` local DB, that a NON-ADMIN
-- authenticated user (full public-program access) gets through RLS:
--   1. ZERO storage_objects whose parent spectrum is draft/revoked (publish gate).
--   2. ZERO storage_objects in a program they cannot access (program gate).
--   3. The still-published control row stays visible (no over-blocking).
-- And that an ADMIN sees the unpublished rows, and the service-role RPCs
-- (get_storage_objects_for_sync / filter_accessible_storage_keys) gate the same
-- way via p_include_unpublished — the predicate the /api/v1 routes rely on.
--
-- Runs in one ROLLBACK'd transaction. Seed-anchored: user@=2222…, admin@=1111…,
-- all seed finals are published nirspec_spec rows in public programs.
--
-- Run:  docker exec -i supabase_db_campfire psql -U postgres -d postgres \
--          -v ON_ERROR_STOP=1 -f python/tests/sql/storage_objects_scope_leak.sql
-- =============================================================================
\set ON_ERROR_STOP on
BEGIN;

-- ---- fixtures (run as the bootstrap superuser, bypasses RLS) ----------------
-- Three published finals: a control (stays published) and two victims we flip to
-- draft/revoked. A fourth control gets re-pointed into a private program.
-- One published final per DISTINCT target (so flipping one victim's target/
-- spectrum can never disturb another pick — control and victims are independent).
CREATE TEMP TABLE _pick AS
  SELECT storage_key, spectrum_id, target_id,
         row_number() OVER (ORDER BY storage_key) AS rn
  FROM (
    SELECT DISTINCT ON (s.target_id) so.storage_key, so.spectrum_id, s.target_id
    FROM storage_objects so
    JOIN spectra s ON s.spectrum_id = so.spectrum_id
    JOIN targets t ON t.target_id = s.target_id
    JOIN programs p ON p.slug = t.program_slug
    WHERE so.product_type = 'nirspec_spec'
      AND so.status = 'active'
      AND s.deploy_status = 'published'
      AND p.is_public
    ORDER BY s.target_id, so.storage_key
  ) d
  ORDER BY storage_key
  LIMIT 4;

DO $$ BEGIN
  IF (SELECT count(*) FROM _pick) < 4 THEN
    RAISE EXCEPTION 'FIXTURE: need >=4 published public-program finals in seed (got %)',
      (SELECT count(*) FROM _pick);
  END IF;
END $$;

CREATE TEMP TABLE _ctrl AS SELECT storage_key, spectrum_id FROM _pick WHERE rn = 1;
CREATE TEMP TABLE _vic_draft AS SELECT storage_key, spectrum_id, target_id FROM _pick WHERE rn = 2;
CREATE TEMP TABLE _vic_revoked AS SELECT storage_key, spectrum_id, target_id FROM _pick WHERE rn = 3;
CREATE TEMP TABLE _vic_xprog AS SELECT storage_key, spectrum_id, target_id FROM _pick WHERE rn = 4;

-- Publish-gate victims: flip their spectra (B1 status).
UPDATE spectra SET deploy_status = 'draft'   WHERE spectrum_id IN (SELECT spectrum_id FROM _vic_draft);
UPDATE spectra SET deploy_status = 'revoked' WHERE spectrum_id IN (SELECT spectrum_id FROM _vic_revoked);

-- Program-gate victim: move its target into a private program the user can't access.
INSERT INTO programs (slug, program_name, is_public)
  VALUES ('leak_priv_prog', 'Leak Private Program', false)
  ON CONFLICT (slug) DO UPDATE SET is_public = false;
UPDATE targets SET program_slug = 'leak_priv_prog'
  WHERE target_id IN (SELECT target_id FROM _vic_xprog);

GRANT SELECT ON _ctrl, _vic_draft, _vic_revoked, _vic_xprog TO authenticated, service_role;

-- =========================================================================
-- 1. RLS as NON-ADMIN (user@, public-program access) -> zero leak
-- =========================================================================
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub', '22222222-2222-2222-2222-222222222222', 'role', 'authenticated')::text, true);

DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM storage_objects WHERE storage_key IN (SELECT storage_key FROM _vic_draft);
  IF n <> 0 THEN RAISE EXCEPTION 'RLS LEAK: non-admin sees a DRAFT-spectrum storage object (expected 0)'; END IF;

  SELECT count(*) INTO n FROM storage_objects WHERE storage_key IN (SELECT storage_key FROM _vic_revoked);
  IF n <> 0 THEN RAISE EXCEPTION 'RLS LEAK: non-admin sees a REVOKED-spectrum storage object (expected 0)'; END IF;

  SELECT count(*) INTO n FROM storage_objects WHERE storage_key IN (SELECT storage_key FROM _vic_xprog);
  IF n <> 0 THEN RAISE EXCEPTION 'RLS LEAK: non-admin sees an OUT-OF-PROGRAM storage object (expected 0)'; END IF;

  -- No over-blocking: the published, in-program control stays visible.
  SELECT count(*) INTO n FROM storage_objects WHERE storage_key IN (SELECT storage_key FROM _ctrl);
  IF n <> 1 THEN RAISE EXCEPTION 'REGRESSION: non-admin cannot see published control storage object (got %)', n; END IF;
END $$;

-- =========================================================================
-- 2. RLS as ADMIN (admin@) -> unpublished rows ARE visible
-- =========================================================================
SELECT set_config('request.jwt.claims',
  json_build_object('sub', '11111111-1111-1111-1111-111111111111', 'role', 'authenticated')::text, true);

DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM storage_objects
    WHERE storage_key IN (SELECT storage_key FROM _vic_draft
                          UNION ALL SELECT storage_key FROM _vic_revoked
                          UNION ALL SELECT storage_key FROM _vic_xprog);
  IF n <> 3 THEN RAISE EXCEPTION 'ADMIN BLOCKED: admin sees %/3 unpublished/out-of-program storage objects', n; END IF;
END $$;

-- =========================================================================
-- 3. RPC predicates as SERVICE_ROLE (RLS bypassed) -> p_include_unpublished gates
-- =========================================================================
RESET ROLE;
SET LOCAL ROLE service_role;
SELECT set_config('request.jwt.claims', '', true);

DO $$
DECLARE pubs text[];
        leaked int; shown int;
        keys text[];
BEGIN
  pubs := ARRAY(SELECT slug FROM programs WHERE is_public);
  keys := ARRAY(SELECT storage_key FROM _ctrl
                UNION ALL SELECT storage_key FROM _vic_draft
                UNION ALL SELECT storage_key FROM _vic_revoked
                UNION ALL SELECT storage_key FROM _vic_xprog);

  -- filter_accessible_storage_keys: only the control is authorized for a member.
  SELECT count(*) INTO shown
    FROM public.filter_accessible_storage_keys(keys, pubs, false)
    WHERE storage_key IN (SELECT storage_key FROM _ctrl);
  IF shown <> 1 THEN RAISE EXCEPTION 'PRESIGN GATE: control key not authorized for member (got %)', shown; END IF;

  SELECT count(*) INTO leaked
    FROM public.filter_accessible_storage_keys(keys, pubs, false)
    WHERE storage_key IN (SELECT storage_key FROM _vic_draft
                          UNION ALL SELECT storage_key FROM _vic_revoked
                          UNION ALL SELECT storage_key FROM _vic_xprog);
  IF leaked <> 0 THEN RAISE EXCEPTION 'PRESIGN LEAK: % unpublished/out-of-program keys authorized (expected 0)', leaked; END IF;

  -- Admin override authorizes everything.
  SELECT count(*) INTO shown FROM public.filter_accessible_storage_keys(keys, pubs, true);
  IF shown <> 4 THEN RAISE EXCEPTION 'PRESIGN ADMIN: include_unpublished=true authorized %/4 keys', shown; END IF;

  -- get_storage_objects_for_sync: victim keys absent for a member, present for admin.
  SELECT count(*) INTO leaked FROM (
    SELECT jsonb_array_elements(objects)->>'storage_key' AS k
    FROM public.get_storage_objects_for_sync(p_program_slugs => pubs, p_limit => 100000, p_include_counts => false, p_include_unpublished => false)
  ) q WHERE k IN (SELECT storage_key FROM _vic_draft
                  UNION ALL SELECT storage_key FROM _vic_revoked
                  UNION ALL SELECT storage_key FROM _vic_xprog);
  IF leaked <> 0 THEN RAISE EXCEPTION 'SYNC LEAK: % unpublished/out-of-program rows in member sync (expected 0)', leaked; END IF;

  SELECT count(*) INTO shown FROM (
    SELECT jsonb_array_elements(objects)->>'storage_key' AS k
    FROM public.get_storage_objects_for_sync(p_program_slugs => pubs, p_limit => 100000, p_include_counts => false, p_include_unpublished => true)
  ) q WHERE k IN (SELECT storage_key FROM _vic_draft
                  UNION ALL SELECT storage_key FROM _vic_revoked
                  UNION ALL SELECT storage_key FROM _vic_xprog);
  IF shown <> 3 THEN RAISE EXCEPTION 'SYNC ADMIN: admin sync returns %/3 unpublished rows', shown; END IF;
END $$;

RESET ROLE;
SELECT '✅ STORAGE_OBJECTS SCOPE LEAK HARNESS: ALL ASSERTIONS PASSED' AS result;

ROLLBACK;
