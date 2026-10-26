-- Access matrix + privilege lints (P-1, docs/design-data-releases-infra-proposal.md §3.3).
--
-- Part A, lints over the whole schema (no fixtures):
--   1. no policy applies to PUBLIC (every CREATE POLICY carries an explicit TO);
--   2. anon holds no table / view / sequence privilege in public;
--   3. no postgres-owned function in public is executable by anon or by PUBLIC;
--   4. postgres' default privileges (schema-less and IN SCHEMA public) grant
--      nothing to anon and no PUBLIC execute on functions.
--   These four lines would have caught findings §0.1 and §0.2 of
--   docs/design-data-releases-impl-findings.md. Add a surface, keep them green.
--
-- Part B, a table-driven matrix over principals × surfaces on a fixture with one
-- public and one private program:
--   anon            → permission denied on every catalog table and on the RPCs;
--   authenticated, no grants → public rows only;
--   authenticated, private grant → both;
--   admin           → both.
--   (Share-link accounts are covered by check_share_link_scoping.sql.)
--
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_access_matrix.sql
-- Everything is inside one rolled-back transaction.

BEGIN;

-- ============================================================================
-- Part A: lints
-- ============================================================================
DO $$
DECLARE
  n int; bad text;
BEGIN
  SELECT count(*), string_agg(tablename || '.' || policyname, ', ' ORDER BY tablename, policyname)
    INTO n, bad
  FROM pg_policies WHERE schemaname = 'public' AND roles = '{public}'::name[];
  IF n > 0 THEN
    RAISE EXCEPTION 'LINT 1: % polic(ies) have no TO clause and apply to anon too: %', n, bad;
  END IF;

  SELECT count(*), string_agg(DISTINCT table_name, ', ')
    INTO n, bad
  FROM information_schema.role_table_grants
  WHERE grantee = 'anon' AND table_schema = 'public';
  IF n > 0 THEN
    RAISE EXCEPTION 'LINT 2: anon holds privileges on % relation grant(s) in public: %', n, bad;
  END IF;

  SELECT count(*), string_agg(p.proname, ', ' ORDER BY p.proname)
    INTO n, bad
  FROM pg_proc p
  JOIN pg_namespace ns ON ns.oid = p.pronamespace
  JOIN pg_roles r ON r.oid = p.proowner
  WHERE ns.nspname = 'public' AND r.rolname = 'postgres'
    -- dr_request_context is PostgREST's pre-request hook (P6b-2): it runs as
    -- the request role on EVERY Data API request, anonymous ones included, so
    -- anon must be able to execute it or every anon request fails. It reads
    -- nothing for the caller: with the X-Campfire-DR header it checks the tag
    -- against dr_visible_tags() and either sets a transaction-local setting
    -- or raises PT404; without the header it returns (check_cat_views.sql).
    AND p.proname <> 'dr_request_context'
    AND (has_function_privilege('anon', p.oid, 'EXECUTE')
         OR EXISTS (SELECT 1 FROM unnest(p.proacl) a WHERE a::text LIKE '=X/%'));
  IF n > 0 THEN
    RAISE EXCEPTION 'LINT 3: % function(s) executable by anon or PUBLIC (add REVOKE ALL ON FUNCTION ... FROM PUBLIC, anon): %', n, bad;
  END IF;

  SELECT count(*)
    INTO n
  FROM pg_default_acl d
  JOIN pg_roles r ON r.oid = d.defaclrole
  LEFT JOIN pg_namespace ns ON ns.oid = d.defaclnamespace
  WHERE r.rolname = 'postgres' AND coalesce(ns.nspname, 'public') = 'public'
    AND (d.defaclacl::text LIKE '%anon=%'
         OR (d.defaclobjtype = 'f' AND EXISTS (SELECT 1 FROM unnest(d.defaclacl) a WHERE a::text LIKE '=X/%')));
  IF n > 0 THEN
    RAISE EXCEPTION 'LINT 4: % default-privilege entr(ies) for postgres still grant anon or PUBLIC execute', n;
  END IF;

  RAISE NOTICE 'OK lints: no PUBLIC policies, no anon relation privileges, no anon/PUBLIC function execute, clean defaults.';
END $$;

-- ============================================================================
-- Part B: fixture
-- ============================================================================
INSERT INTO programs (slug, program_name, is_public) VALUES
  ('zzz_am_pub',  'Access Matrix Public Program',  true),
  ('zzz_am_priv', 'Access Matrix Private Program', false);

INSERT INTO fields (name) VALUES ('zzz_am_field');

INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
  ('zzz_am_obs_pub',  'zzz_am_pub',  9201, 'zzz_am_field'),
  ('zzz_am_obs_priv', 'zzz_am_priv', 9202, 'zzz_am_field');

INSERT INTO auth.users (id, email) VALUES
  ('00000000-0000-0000-0000-0000000000c1', 'zzz-am-admin@test.invalid'),
  ('00000000-0000-0000-0000-0000000000c2', 'zzz-am-grantee@test.invalid'),
  ('00000000-0000-0000-0000-0000000000c3', 'zzz-am-nogrant@test.invalid');

INSERT INTO user_profiles (user_id, username, full_name, is_admin, can_comment, can_inspect) VALUES
  ('00000000-0000-0000-0000-0000000000c1', 'zzz-am-admin',   'ZZZ AM Admin',   true,  true, true),
  ('00000000-0000-0000-0000-0000000000c2', 'zzz-am-grantee', 'ZZZ AM Grantee', false, true, false),
  ('00000000-0000-0000-0000-0000000000c3', 'zzz-am-nogrant', 'ZZZ AM NoGrant', false, true, false);

INSERT INTO user_program_access (user_id, program_slug) VALUES
  ('00000000-0000-0000-0000-0000000000c2', 'zzz_am_priv');

INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum) VALUES
  ('ZZZ-AM-PUB',  'zzz_am_field', 160.0, 3.0, ARRAY['zzz_am_pub'],  ARRAY['zzz_am_obs_pub'],  true),
  ('ZZZ-AM-PRIV', 'zzz_am_field', 160.1, 3.1, ARRAY['zzz_am_priv'], ARRAY['zzz_am_obs_priv'], true);

INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id, has_published_spectrum)
SELECT v.tid, 'zzz_am_field', v.ra, 3.0, v.prog, v.obs, o.id, true
FROM (VALUES
  ('zzz-am-t-pub',  160.0, 'zzz_am_pub',  'zzz_am_obs_pub',  'ZZZ-AM-PUB'),
  ('zzz-am-t-priv', 160.1, 'zzz_am_priv', 'zzz_am_obs_priv', 'ZZZ-AM-PRIV')
) AS v(tid, ra, prog, obs, oid)
JOIN objects o ON o.object_id = v.oid;

INSERT INTO spectra (target_id, grating, fits_path, deploy_status) VALUES
  ('zzz-am-t-pub',  'PRISM', '/tmp/zzz_am_pub_spec.fits',  'published'),
  ('zzz-am-t-priv', 'PRISM', '/tmp/zzz_am_priv_spec.fits', 'published');

INSERT INTO catalog_matches (object_id, field, ra, dec, catalog_name, photometry)
SELECT o.id, 'zzz_am_field', o.ra, o.dec, 'zzz_am_cat', '{}'::jsonb FROM objects o
WHERE o.object_id IN ('ZZZ-AM-PUB', 'ZZZ-AM-PRIV');

INSERT INTO comments (user_id, content, object_id)
SELECT '00000000-0000-0000-0000-0000000000c2', 'zzz am note', o.id FROM objects o WHERE o.object_id = 'ZZZ-AM-PRIV';

INSERT INTO flag_audit_log (field_name, object_id, user_id)
SELECT 'redshift_quality', o.id, '00000000-0000-0000-0000-0000000000c2' FROM objects o WHERE o.object_id = 'ZZZ-AM-PRIV';

-- ============================================================================
-- Part B: the matrix. One helper counts fixture rows on a surface as a principal.
-- ============================================================================
CREATE FUNCTION pg_temp.zzz_am_count(p_role text, p_uid uuid, p_sql text) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  IF p_uid IS NULL THEN
    PERFORM set_config('request.jwt.claims', json_build_object('role', p_role)::text, true);
  ELSE
    PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', p_role)::text, true);
  END IF;
  EXECUTE format('SET LOCAL ROLE %I', p_role);
  EXECUTE p_sql INTO n;
  RESET ROLE;
  RETURN n;
EXCEPTION WHEN insufficient_privilege THEN
  RESET ROLE;
  RETURN -1;   -- "permission denied" is a legitimate, asserted outcome for anon
END $$;
-- P-1: functions default to no PUBLIC execute, so the helper must be granted
-- explicitly before it is called under SET LOCAL ROLE authenticated.
GRANT EXECUTE ON FUNCTION pg_temp.zzz_am_count(text, uuid, text) TO authenticated;

DO $$
DECLARE
  admin   uuid := '00000000-0000-0000-0000-0000000000c1';
  grantee uuid := '00000000-0000-0000-0000-0000000000c2';
  nogrant uuid := '00000000-0000-0000-0000-0000000000c3';
  -- surface → (sql over the fixture rows only)
  surfaces text[][] := ARRAY[
    ['targets',            $q$SELECT count(*) FROM targets WHERE target_id LIKE 'zzz-am-%'$q$],
    ['objects',            $q$SELECT count(*) FROM objects WHERE object_id LIKE 'ZZZ-AM-%'$q$],
    ['spectra',            $q$SELECT count(*) FROM spectra WHERE fits_path LIKE '/tmp/zzz_am_%'$q$],
    ['catalog_matches',  $q$SELECT count(*) FROM catalog_matches WHERE catalog_name = 'zzz_am_cat'$q$],
    ['comments',           $q$SELECT count(*) FROM comments WHERE content = 'zzz am note'$q$],
    ['flag_audit_log',     $q$SELECT count(*) FROM flag_audit_log WHERE user_id = '00000000-0000-0000-0000-0000000000c2'$q$],
    ['programs',           $q$SELECT count(*) FROM programs WHERE slug LIKE 'zzz_am_%'$q$]
  ];
  -- expected counts: [anon, nogrant, grantee, admin]; -1 = permission denied
  expected int[][] := ARRAY[
    [-1, 1, 2, 2],   -- targets
    [-1, 1, 2, 2],   -- objects
    [-1, 1, 2, 2],   -- spectra
    [-1, 1, 2, 2],   -- catalog_matches
    [-1, 0, 1, 1],   -- comments (on the private object)
    [-1, 0, 1, 1],   -- flag_audit_log (on the private object)
    [-1, 1, 2, 2]    -- programs (private row visible only with a grant / admin)
  ];
  i int; got int; who text;
BEGIN
  FOR i IN 1 .. array_length(surfaces, 1) LOOP
    got := pg_temp.zzz_am_count('anon', NULL, surfaces[i][2]);
    IF got IS DISTINCT FROM expected[i][1] THEN
      RAISE EXCEPTION 'MATRIX %: anon got % (expected %)', surfaces[i][1], got, expected[i][1];
    END IF;
    got := pg_temp.zzz_am_count('authenticated', nogrant, surfaces[i][2]);
    IF got IS DISTINCT FROM expected[i][2] THEN
      RAISE EXCEPTION 'MATRIX %: user without grants got % (expected %)', surfaces[i][1], got, expected[i][2];
    END IF;
    got := pg_temp.zzz_am_count('authenticated', grantee, surfaces[i][2]);
    IF got IS DISTINCT FROM expected[i][3] THEN
      RAISE EXCEPTION 'MATRIX %: grantee got % (expected %)', surfaces[i][1], got, expected[i][3];
    END IF;
    got := pg_temp.zzz_am_count('authenticated', admin, surfaces[i][2]);
    IF got IS DISTINCT FROM expected[i][4] THEN
      RAISE EXCEPTION 'MATRIX %: admin got % (expected %)', surfaces[i][1], got, expected[i][4];
    END IF;
  END LOOP;

  -- RPC surfaces: anon must not be able to call the helpers or the roster leak.
  IF pg_temp.zzz_am_count('anon', NULL, 'SELECT cardinality(public.accessible_program_slugs())') <> -1 THEN
    RAISE EXCEPTION 'MATRIX rpc: anon can execute accessible_program_slugs()';
  END IF;
  IF pg_temp.zzz_am_count('anon', NULL, 'SELECT count(*) FROM public.get_program_stats()') <> -1 THEN
    RAISE EXCEPTION 'MATRIX rpc: anon can execute get_program_stats() (private roster leak)';
  END IF;
  IF pg_temp.zzz_am_count('anon', NULL, 'SELECT public.revoke_all_user_refresh_tokens(''00000000-0000-0000-0000-0000000000c2'')') <> -1 THEN
    RAISE EXCEPTION 'MATRIX rpc: anon can execute revoke_all_user_refresh_tokens()';
  END IF;
  -- ...while an authenticated user still can resolve their programs.
  IF pg_temp.zzz_am_count('authenticated', nogrant, $q$SELECT count(*) FROM unnest(public.accessible_program_slugs()) s WHERE s = 'zzz_am_pub'$q$) <> 1
     OR pg_temp.zzz_am_count('authenticated', nogrant, $q$SELECT count(*) FROM unnest(public.accessible_program_slugs()) s WHERE s = 'zzz_am_priv'$q$) <> 0
     OR pg_temp.zzz_am_count('authenticated', grantee, $q$SELECT count(*) FROM unnest(public.accessible_program_slugs()) s WHERE s = 'zzz_am_priv'$q$) <> 1 THEN
    RAISE EXCEPTION 'MATRIX rpc: accessible_program_slugs() wrong for an authenticated principal';
  END IF;

  RAISE NOTICE 'OK: access matrix holds for anon / no-grant / grantee / admin over % surfaces + RPCs.', array_length(surfaces, 1);
END $$;

ROLLBACK;
