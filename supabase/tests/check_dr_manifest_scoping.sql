-- Guard: the release manifest must stay CALLER-SCOPED (P6c-2).
--
-- Every file served under a data release (spectrum sidecars, downloads,
-- NIRCam images, page assets, the bearer file routes) is decided on a
-- dr_manifest row: the cookie paths read it under select_dr_manifest, the
-- service-role paths mirror its program rule in web/lib/server/dr-files.ts
-- (resolveReleaseMembersAs). If the policy drifts, a reader could obtain an
-- archive url for a program they do not hold, a link account could list a
-- release's files, or an unpublished release could leak through its files.
-- This asserts the policy's four rules directly on the table, before and
-- after publish:
--   program rows        -> holders of that program only (public programs: everyone)
--   field-level rows    -> every non-link reader of a visible tag (program_slug NULL)
--   page rows           -> same as field-level rows (component 'page')
--   link accounts       -> nothing, ever; anon -> no grant at all
--   unpublished tag     -> admins only
-- and that the pre-request hook's service-role rule (any release past its
-- freeze; never draft / freezing / failed) still holds, since the bearer gate
-- (web/lib/server/bearer-release.ts) is aligned with it.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_dr_manifest_scoping.sql
BEGIN;

INSERT INTO programs (slug, program_name, is_public) VALUES
  ('zzz_ms_pub',  'Manifest Scoping Public',  true),
  ('zzz_ms_prop', 'Manifest Scoping Private', false);
INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
  ('zzz_ms_obs_pub',  'zzz_ms_pub',  9701, 'zzz_ms_field'),
  ('zzz_ms_obs_prop', 'zzz_ms_prop', 9702, 'zzz_ms_field');

INSERT INTO auth.users (id, email) VALUES
  ('00000000-0000-0000-0000-0000000000e1', 'zzz-ms-admin@test.invalid'),
  ('00000000-0000-0000-0000-0000000000e2', 'zzz-ms-grantee@test.invalid'),
  ('00000000-0000-0000-0000-0000000000e3', 'zzz-ms-nogrant@test.invalid'),
  ('00000000-0000-0000-0000-0000000000e4', 'zzz-ms-link@test.invalid');
INSERT INTO user_profiles (user_id, username, full_name, is_admin, is_link_account, can_comment, can_inspect) VALUES
  ('00000000-0000-0000-0000-0000000000e1', 'zzz-ms-admin',   'ZZZ MS Admin',   true,  false, true, true),
  ('00000000-0000-0000-0000-0000000000e2', 'zzz-ms-grantee', 'ZZZ MS Grantee', false, false, true, false),
  ('00000000-0000-0000-0000-0000000000e3', 'zzz-ms-nogrant', 'ZZZ MS NoGrant', false, false, true, false),
  ('00000000-0000-0000-0000-0000000000e4', 'zzz-ms-link',    'ZZZ MS Link',    false, true,  false, false);
INSERT INTO user_program_access (user_id, program_slug) VALUES
  ('00000000-0000-0000-0000-0000000000e2', 'zzz_ms_prop');
INSERT INTO share_links (token, label, observation, link_user_id, link_password, include_drafts, allow_download, created_by)
VALUES ('zzz_ms_tok', 'ms link', 'zzz_ms_obs_pub', '00000000-0000-0000-0000-0000000000e4', 'x', false, true,
        '00000000-0000-0000-0000-0000000000e1');

-- One frozen release with four manifest rows: a public-program spectrum, a
-- private-program spectrum, a field-level mosaic (program_slug NULL) and a
-- page asset. All archived, so a file path would serve them.
INSERT INTO dr_releases (tag, state, components, created_by, freeze_started_at, frozen_at) VALUES
  ('DR-zzz-ms', 'frozen', '["nirspec","nircam/zzz_ms_field"]'::jsonb, '00000000-0000-0000-0000-0000000000e1', now(), now()),
  ('DR-zzz-ms-draft',    'draft',    '["nirspec"]'::jsonb, '00000000-0000-0000-0000-0000000000e1', NULL, NULL),
  ('DR-zzz-ms-freezing', 'freezing', '["nirspec"]'::jsonb, '00000000-0000-0000-0000-0000000000e1', now(), NULL),
  ('DR-zzz-ms-failed',   'failed',   '["nirspec"]'::jsonb, '00000000-0000-0000-0000-0000000000e1', now(), NULL),
  ('DR-zzz-ms-ready',    'ready',    '["nirspec"]'::jsonb, '00000000-0000-0000-0000-0000000000e1', now(), now());
INSERT INTO dr_manifest (tag, storage_key, component, program_slug, field, product_type, spectrum_id, backend, bucket,
                         content_hash, size_bytes, byte_state, archive_key) VALUES
  ('DR-zzz-ms', 'data/products/nirspec/zzz_ms_obs_pub/pub_1_spec.fits',  'nirspec', 'zzz_ms_pub',  'zzz_ms_field', 'nirspec_spec', 'pub_1_spec',  'osn', 'data',
   'sha256:' || repeat('a1', 32), 100, 'stamped', 'archive/' || repeat('a1', 32) || '/products/nirspec/zzz_ms_obs_pub/pub_1_spec.fits'),
  ('DR-zzz-ms', 'data/products/nirspec/zzz_ms_obs_prop/prop_1_spec.fits', 'nirspec', 'zzz_ms_prop', 'zzz_ms_field', 'nirspec_spec', 'prop_1_spec', 'osn', 'data',
   'sha256:' || repeat('a2', 32), 100, 'stamped', 'archive/' || repeat('a2', 32) || '/products/nirspec/zzz_ms_obs_prop/prop_1_spec.fits'),
  ('DR-zzz-ms', 'data/products/nircam/zzz_ms_field/f444w/zzz_ms_field_f444w_sci.fits', 'nircam/zzz_ms_field', NULL, 'zzz_ms_field', 'nircam_mosaic', NULL, 'osn', 'data',
   'sha256:' || repeat('a3', 32), 100, 'copied', 'archive/' || repeat('a3', 32) || '/products/nircam/zzz_ms_field/f444w/zzz_ms_field_f444w_sci.fits'),
  ('DR-zzz-ms', 'dr/DR-zzz-ms/page/a4a4a4a4a4a4a4a4/nz.png', 'page', NULL, NULL, 'dr_page_asset', NULL, 'osn', 'data',
   'sha256:' || repeat('a4', 32), 100, 'copied', 'dr/DR-zzz-ms/page/a4a4a4a4a4a4a4a4/nz.png');

-- run SQL as a principal (claims + SET LOCAL ROLE); errors come back as text
CREATE FUNCTION pg_temp.zzz_ms_as(p_role text, p_uid uuid, p_sql text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE v text;
BEGIN
  IF p_uid IS NULL THEN
    PERFORM set_config('request.jwt.claims', json_build_object('role', p_role)::text, true);
  ELSE
    PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', p_role)::text, true);
  END IF;
  EXECUTE format('SET LOCAL ROLE %I', p_role);
  EXECUTE p_sql INTO v;
  RESET ROLE;
  PERFORM set_config('request.jwt.claims', '', true);
  RETURN v;
EXCEPTION WHEN OTHERS THEN
  RESET ROLE;
  PERFORM set_config('request.jwt.claims', '', true);
  RETURN 'ERR: ' || SQLERRM;
END $$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_ms_as(text, uuid, text) TO authenticated, anon;

-- the pre-request hook as a principal with a header; the sqlstate or the tag it set
CREATE FUNCTION pg_temp.zzz_ms_hook(p_role text, p_uid uuid, p_header text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE v text;
BEGIN
  PERFORM set_config('campfire.dr_tag', '', true);
  IF p_uid IS NULL THEN
    PERFORM set_config('request.jwt.claims', json_build_object('role', p_role)::text, true);
  ELSE
    PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', p_role)::text, true);
  END IF;
  PERFORM set_config('request.headers', json_build_object('x-campfire-dr', p_header)::text, true);
  EXECUTE format('SET LOCAL ROLE %I', p_role);
  BEGIN
    PERFORM public.dr_request_context();
    v := 'SET:' || coalesce(public.dr_browse_tag(), '');
  EXCEPTION WHEN OTHERS THEN
    v := SQLSTATE;
  END;
  RESET ROLE;
  PERFORM set_config('request.jwt.claims', '', true);
  PERFORM set_config('request.headers', '', true);
  PERFORM set_config('campfire.dr_tag', '', true);
  RETURN v;
END $$;

DO $$
DECLARE
  admin   uuid := '00000000-0000-0000-0000-0000000000e1';
  grantee uuid := '00000000-0000-0000-0000-0000000000e2';
  nogrant uuid := '00000000-0000-0000-0000-0000000000e3';
  link    uuid := '00000000-0000-0000-0000-0000000000e4';
  principals uuid[] := ARRAY[NULL, nogrant, grantee, link, admin];
  names text[] := ARRAY['anon', 'nogrant', 'grantee', 'link', 'admin'];
  -- what each principal may see of the four rows, as a sorted list of components
  -- (frozen = unpublished: admins only; published: the policy's rules)
  frozen_expected text[] := ARRAY['ERR', '', '', '', 'nircam/zzz_ms_field,nirspec,nirspec,page'];
  pub_expected text[] := ARRAY[
    'ERR',                                           -- anon: no grant on the table at all
    'nircam/zzz_ms_field,nirspec,page',              -- nogrant: public program + field + page
    'nircam/zzz_ms_field,nirspec,nirspec,page',      -- grantee: + the private program's row
    '',                                              -- link: nothing
    'nircam/zzz_ms_field,nirspec,nirspec,page'];     -- admin: everything
  q constant text := $q$SELECT coalesce(string_agg(component, ',' ORDER BY component), '') FROM dr_manifest WHERE tag = 'DR-zzz-ms'$q$;
  got text; role text; j int;
BEGIN
  FOR j IN 1 .. 5 LOOP
    role := CASE WHEN j = 1 THEN 'anon' ELSE 'authenticated' END;
    got := pg_temp.zzz_ms_as(role, principals[j], q);
    got := CASE WHEN got LIKE 'ERR:%' THEN 'ERR' ELSE got END;
    IF got IS DISTINCT FROM frozen_expected[j] THEN
      RAISE EXCEPTION 'manifest (frozen) as %: got [%] expected [%]', names[j], got, frozen_expected[j];
    END IF;
  END LOOP;

  UPDATE dr_releases SET state = 'published', published_at = now() WHERE tag = 'DR-zzz-ms';
  FOR j IN 1 .. 5 LOOP
    role := CASE WHEN j = 1 THEN 'anon' ELSE 'authenticated' END;
    got := pg_temp.zzz_ms_as(role, principals[j], q);
    got := CASE WHEN got LIKE 'ERR:%' THEN 'ERR' ELSE got END;
    IF got IS DISTINCT FROM pub_expected[j] THEN
      RAISE EXCEPTION 'manifest (published) as %: got [%] expected [%]', names[j], got, pub_expected[j];
    END IF;
  END LOOP;

  -- the private program's row is the ONLY difference between nogrant and grantee
  got := pg_temp.zzz_ms_as('authenticated', nogrant,
    $q$SELECT coalesce(string_agg(program_slug, ','), '') FROM dr_manifest WHERE tag = 'DR-zzz-ms' AND program_slug = 'zzz_ms_prop'$q$);
  IF got <> '' THEN RAISE EXCEPTION 'nogrant sees the private program row: %', got; END IF;
  -- a reader can never read an archive key of a program they do not hold
  got := pg_temp.zzz_ms_as('authenticated', nogrant,
    $q$SELECT coalesce(string_agg(archive_key, ','), '') FROM dr_manifest WHERE tag = 'DR-zzz-ms' AND archive_key LIKE '%prop_1_spec%'$q$);
  IF got <> '' THEN RAISE EXCEPTION 'nogrant obtained a private archive key: %', got; END IF;
  -- withdrawn: readers lose the files again
  UPDATE dr_releases SET state = 'withdrawn', withdrawn_at = now() WHERE tag = 'DR-zzz-ms';
  got := pg_temp.zzz_ms_as('authenticated', grantee, q);
  IF got <> '' THEN RAISE EXCEPTION 'grantee still sees a withdrawn release''s manifest: %', got; END IF;
  got := pg_temp.zzz_ms_as('authenticated', admin, q);
  IF got <> 'nircam/zzz_ms_field,nirspec,nirspec,page' THEN RAISE EXCEPTION 'admin lost the withdrawn manifest: %', got; END IF;
  UPDATE dr_releases SET state = 'published', withdrawn_at = NULL WHERE tag = 'DR-zzz-ms';
  RAISE NOTICE 'OK 1: select_dr_manifest (program rows per program; field and page rows for every non-link reader; link nothing; anon no grant; unpublished and withdrawn admin-only)';
END $$;

-- The bearer gate (web/lib/server/bearer-release.ts) admits admins to any
-- release past its freeze and readers to published ones, exactly what the
-- hook admits the service role / the user to; pin the hook side here.
DO $$
DECLARE
  grantee uuid := '00000000-0000-0000-0000-0000000000e2';
  link    uuid := '00000000-0000-0000-0000-0000000000e4';
  got text;
BEGIN
  FOR got IN SELECT unnest(ARRAY['DR-zzz-ms-draft', 'DR-zzz-ms-freezing', 'DR-zzz-ms-failed']) LOOP
    IF pg_temp.zzz_ms_hook('service_role', NULL, got) <> 'PT404' THEN
      RAISE EXCEPTION 'service role should be refused % (not past its freeze)', got;
    END IF;
  END LOOP;
  IF pg_temp.zzz_ms_hook('service_role', NULL, 'DR-zzz-ms-ready') <> 'SET:DR-zzz-ms-ready' THEN
    RAISE EXCEPTION 'service role should browse a ready release';
  END IF;
  IF pg_temp.zzz_ms_hook('service_role', NULL, 'DR-zzz-ms') <> 'SET:DR-zzz-ms' THEN
    RAISE EXCEPTION 'service role should browse a published release';
  END IF;
  IF pg_temp.zzz_ms_hook('authenticated', grantee, 'DR-zzz-ms') <> 'SET:DR-zzz-ms' THEN
    RAISE EXCEPTION 'a reader should browse a published release';
  END IF;
  IF pg_temp.zzz_ms_hook('authenticated', grantee, 'DR-zzz-ms-ready') <> 'PT404' THEN
    RAISE EXCEPTION 'a reader should be refused an unpublished (ready) release';
  END IF;
  IF pg_temp.zzz_ms_hook('authenticated', link, 'DR-zzz-ms') <> 'PT404' THEN
    RAISE EXCEPTION 'a link account should be refused a published release';
  END IF;
  RAISE NOTICE 'OK 2: pre-request hook states (service role: past the freeze only; reader: published; link: never)';
END $$;

ROLLBACK;
