-- Guard: the `lists` field of objects served to an API key must show only the
-- lists that key's user may see.
--
-- /api/v1/objects calls get_filtered_objects_paginated, and /api/v1/sync/objects
-- calls get_objects_for_sync, through the service-role client, which bypasses
-- RLS. Each object's `lists` field joins object_list_members -> object_lists, so
-- without an explicit viewer predicate every API key saw the name / slug / icon
-- / colour of every other user's PRIVATE lists on every object it could see.
-- Both RPCs take the user explicitly (p_viewer_id / p_user_id) and keep the
-- lists viewable_list_ids() would give that user: owned, public_read /
-- public_edit, or shared. Under a user JWT (the portal) RLS on object_lists
-- stays the gate, so the portal's display is unchanged, admins included, and a
-- forged p_viewer_id cannot widen it.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_api_list_scoping.sql
BEGIN;

DO $$
DECLARE
  c_owner  CONSTANT UUID := '00000000-0000-0000-0000-0000000000c1';  -- owns all three lists
  c_none   CONSTANT UUID := '00000000-0000-0000-0000-0000000000c2';  -- no lists, no grants, no shares
  c_grant  CONSTANT UUID := '00000000-0000-0000-0000-0000000000c3';  -- viewer share on the shared list
  c_admin  CONSTANT UUID := '00000000-0000-0000-0000-0000000000c4';
  c_pub    CONSTANT TEXT[] := ARRAY['zzz_ls_pub'];
  c_all    CONSTANT TEXT[] := ARRAY['zzz-ls-private', 'zzz-ls-public', 'zzz-ls-shared'];
  v_obj    INTEGER;
  v_got    TEXT[];
  r        RECORD;
BEGIN
  -- Fixtures ------------------------------------------------------------------
  INSERT INTO programs (slug, program_name, is_public) VALUES
    ('zzz_ls_pub', 'List Scoping Test Public', true);
  INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
    ('ls_obs_pub', 'zzz_ls_pub', 9971, 'zzz_ls_field');

  INSERT INTO auth.users (id, email) VALUES
    (c_owner, 'zzz-ls-owner@test.invalid'),
    (c_none,  'zzz-ls-none@test.invalid'),
    (c_grant, 'zzz-ls-grantee@test.invalid'),
    (c_admin, 'zzz-ls-admin@test.invalid');
  INSERT INTO user_profiles (user_id, username, full_name, is_admin, can_comment, can_inspect) VALUES
    (c_owner, 'zzz-ls-owner',   'ZZZ LS Owner',   false, true, true),
    (c_none,  'zzz-ls-none',    'ZZZ LS None',    false, true, true),
    (c_grant, 'zzz-ls-grantee', 'ZZZ LS Grantee', false, true, true),
    (c_admin, 'zzz-ls-admin',   'ZZZ LS Admin',   true,  true, true);

  INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum) VALUES
    ('TEST-LS-1', 'zzz_ls_field', 151.00, 2.50, '{zzz_ls_pub}', '{ls_obs_pub}', true);
  SELECT id INTO v_obj FROM objects WHERE object_id = 'TEST-LS-1';
  INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id, has_published_spectrum) VALUES
    ('test-ls-1', 'zzz_ls_field', 151.00, 2.50, 'zzz_ls_pub', 'ls_obs_pub', v_obj, true);
  INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status) VALUES
    ('test-ls-1', 'PRISM', '/tmp/ls_1_prism.fits', 10.0, 1000.0, 'published');

  INSERT INTO object_lists (name, slug, visibility, created_by) VALUES
    ('ZZZ LS Private', 'zzz-ls-private', 'private',     c_owner),
    ('ZZZ LS Public',  'zzz-ls-public',  'public_read', c_owner),
    ('ZZZ LS Shared',  'zzz-ls-shared',  'private',     c_owner);
  INSERT INTO object_list_shares (list_id, user_id, role, granted_by)
  SELECT id, c_grant, 'viewer', c_owner FROM object_lists WHERE slug = 'zzz-ls-shared';
  INSERT INTO object_list_members (list_id, object_id, ra, dec)
  SELECT id, v_obj, 151.00, 2.50 FROM object_lists WHERE slug = ANY(c_all);

  -- 1) Service role, as the bearer routes call it ------------------------------
  PERFORM set_config('request.jwt.claims', json_build_object('role', 'service_role')::text, true);
  PERFORM set_config('role', 'service_role', true);

  FOR r IN SELECT * FROM (VALUES
    -- viewer, label, expected slugs
    (c_none,     'no-lists no-grants viewer', ARRAY['zzz-ls-public']),
    (c_grant,    'share grantee',             ARRAY['zzz-ls-public', 'zzz-ls-shared']),
    (c_owner,    'owner',                     c_all),
    -- Admins see only what viewable_list_ids() gives them here, as in sync.
    (c_admin,    'admin',                     ARRAY['zzz-ls-public']),
    -- An omitted viewer fails closed to public lists.
    (NULL::uuid, 'omitted viewer',            ARRAY['zzz-ls-public'])
  ) AS t(viewer, label, want)
  LOOP
    SELECT ARRAY(SELECT l ->> 'slug' FROM jsonb_array_elements(p.targets -> 0 -> 'lists') l ORDER BY 1)
      INTO v_got
      FROM public.get_filtered_objects_paginated(
        p_program_slugs => c_pub,
        p_fields        => ARRAY['zzz_ls_field'],
        p_viewer_id     => r.viewer
      ) p;
    IF v_got IS DISTINCT FROM r.want THEN
      RAISE EXCEPTION 'get_filtered_objects_paginated as service role, %: lists % (want %)', r.label, v_got, r.want;
    END IF;

    -- The sync stream's lists field is held to the same rule.
    SELECT ARRAY(SELECT jsonb_array_elements_text(o -> 'lists') ORDER BY 1)
      INTO v_got
      FROM public.get_objects_for_sync(p_program_slugs => c_pub, p_user_id => r.viewer) s,
           jsonb_array_elements(s.objects) o
     WHERE o ->> 'object_id' = 'TEST-LS-1';
    IF v_got IS DISTINCT FROM r.want THEN
      RAISE EXCEPTION 'get_objects_for_sync as service role, %: lists % (want %)', r.label, v_got, r.want;
    END IF;
  END LOOP;

  -- 2) Portal path (user JWT): RLS decides, p_viewer_id is ignored --------------
  PERFORM set_config('role', 'authenticated', true);

  FOR r IN SELECT * FROM (VALUES
    -- caller, forged p_viewer_id, label, expected slugs
    (c_none,  NULL::uuid, 'no-lists viewer',                     ARRAY['zzz-ls-public']),
    (c_none,  c_owner,    'no-lists viewer forging the owner id', ARRAY['zzz-ls-public']),
    (c_grant, NULL::uuid, 'share grantee',                       ARRAY['zzz-ls-public', 'zzz-ls-shared']),
    (c_owner, NULL::uuid, 'owner',                               c_all),
    -- select_lists + admin_manage_lists: the portal shows admins every list.
    (c_admin, NULL::uuid, 'admin',                               c_all)
  ) AS t(caller, viewer, label, want)
  LOOP
    PERFORM set_config('request.jwt.claims',
      json_build_object('sub', r.caller, 'role', 'authenticated')::text, true);
    SELECT ARRAY(SELECT l ->> 'slug' FROM jsonb_array_elements(p.targets -> 0 -> 'lists') l ORDER BY 1)
      INTO v_got
      FROM public.get_filtered_objects_paginated(
        p_program_slugs => c_pub,
        p_fields        => ARRAY['zzz_ls_field'],
        p_viewer_id     => r.viewer
      ) p;
    IF v_got IS DISTINCT FROM r.want THEN
      RAISE EXCEPTION 'get_filtered_objects_paginated under a user JWT, %: lists % (want %)', r.label, v_got, r.want;
    END IF;
  END LOOP;

  PERFORM set_config('request.jwt.claims', '', true);
  PERFORM set_config('role', 'none', true);

  RAISE NOTICE 'OK: API list scoping holds (objects + sync RPCs as service role; portal path unchanged under RLS).';
END $$;

ROLLBACK;
