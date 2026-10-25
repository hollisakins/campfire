-- =============================================================================
-- Catalog source views + the browsing hook (P6b-2, docs/design-data-releases-impl-plan.md §8 P6b-2).
-- =============================================================================
--   1. every cat_* view carries exactly the live table's columns, by name,
--      type and order, is security_invoker, readable by authenticated and
--      service_role, never by anon;
--   2. no VOLATILE function reads a cat_* view (writers stay on the live
--      tables), and every switched read-only catalog RPC reads no live
--      catalog table as a FROM/JOIN target any more;
--   3. dr_browse_tag() is NULL until set, transaction-local once set;
--   4. dr_request_context(): no header, no effect; a malformed or unknown tag
--      is PT404; a reader may select a published tag, not an unpublished one;
--      an admin may select any; the service role any existing tag.
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_cat_views.sql
\set ON_ERROR_STOP on

-- ------------------------------------------------------------------ 1 + 2
DO $$
DECLARE
  pairs text[][] := ARRAY[
    ['objects',                 'cat_objects'],
    ['targets',                 'cat_targets'],
    ['spectra',                 'cat_spectra'],
    ['spectrum_line_fits',      'cat_line_fits'],
    ['spectrum_lines',          'cat_spectrum_lines'],
    ['object_photometry',       'cat_photometry'],
    ['object_photometry_bands', 'cat_photometry_bands'],
    ['nircam_images',           'cat_nircam_images'],
    ['fields',                  'cat_fields'],
    ['mv_filter_options',       'cat_filter_options']
  ];
  switched text[] := ARRAY[
    'get_filtered_objects_paginated','get_filtered_spectra_paginated','get_filtered_object_ids',
    'get_adjacent_objects','get_objects_near','get_field_object_markers','get_csv_export_spectra',
    'get_csv_export_objects','object_scoped_aggregates','objects_matching_grating_filter',
    'objects_matching_observation_filter','objects_matching_line_filter','spectra_matching_line_filter',
    'object_line_snr','object_line_values','object_band_values','objects_matching_band_filter','get_nircam_fields',
    'get_nircam_field_summary','get_observation_manifest','get_database_overview',
    'get_observation_stats','get_observations_overview','get_programs_overview','cat_program_slugs'];
  live text; v text; bad text; i int; fn text; n int;
BEGIN
  FOR i IN 1 .. array_length(pairs, 1) LOOP
    live := pairs[i][1]; v := pairs[i][2];
    SELECT string_agg(coalesce(l.attname, '?') || ':' || coalesce(format_type(l.atttypid, l.atttypmod), '?')
                      || ' vs ' || coalesce(w.attname, '?') || ':' || coalesce(format_type(w.atttypid, w.atttypmod), '?'), ', ')
      INTO bad
    FROM (SELECT attname, atttypid, atttypmod, row_number() OVER (ORDER BY attnum) AS pos FROM pg_attribute
          WHERE attrelid = ('public.' || live)::regclass AND attnum > 0 AND NOT attisdropped) l
    FULL JOIN (SELECT attname, atttypid, atttypmod, row_number() OVER (ORDER BY attnum) AS pos FROM pg_attribute
          WHERE attrelid = ('public.' || v)::regclass AND attnum > 0 AND NOT attisdropped) w ON w.pos = l.pos
    WHERE l.attname IS DISTINCT FROM w.attname OR l.atttypid IS DISTINCT FROM w.atttypid OR l.atttypmod IS DISTINCT FROM w.atttypmod;
    IF bad IS NOT NULL THEN
      RAISE EXCEPTION 'cat views: % does not mirror % column for column: %', v, live, bad;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = ('public.' || v)::regclass AND c.relkind = 'v'
                   AND c.reloptions @> ARRAY['security_invoker=true']) THEN
      RAISE EXCEPTION 'cat views: % is not a security_invoker view', v;
    END IF;
    IF has_table_privilege('anon', 'public.' || v, 'SELECT') THEN
      RAISE EXCEPTION 'cat views: anon can read %', v;
    END IF;
    IF NOT has_table_privilege('authenticated', 'public.' || v, 'SELECT')
       OR NOT has_table_privilege('service_role', 'public.' || v, 'SELECT') THEN
      RAISE EXCEPTION 'cat views: authenticated/service_role cannot read %', v;
    END IF;
    IF has_table_privilege('authenticated', 'public.' || v, 'INSERT') THEN
      RAISE EXCEPTION 'cat views: authenticated may write %', v;
    END IF;
  END LOOP;
  RAISE NOTICE 'OK 1: % cat_* views mirror their live tables (security_invoker, read-only, no anon)', array_length(pairs, 1);

  -- writers never read the browsing views
  SELECT string_agg(p.proname, ', ') INTO bad
  FROM pg_proc p
  WHERE p.pronamespace = 'public'::regnamespace AND p.provolatile = 'v'
    AND p.prosrc ~* '\mcat_(objects|targets|spectra|line_fits|spectrum_lines|photometry|photometry_bands|nircam_images|fields|filter_options)\M';
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'cat views: VOLATILE function(s) read a cat_* view: %', bad;
  END IF;
  -- the switched RPCs read no live catalog table as a FROM / JOIN / FROM ONLY
  -- / comma-join target, quoted or not (comment false positives accepted)
  FOREACH fn IN ARRAY switched LOOP
    SELECT count(*) INTO n FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace AND p.proname = fn;
    IF n = 0 THEN RAISE EXCEPTION 'cat views: switched function % does not exist', fn; END IF;
    IF EXISTS (SELECT 1 FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace AND p.proname = fn
               AND (p.prosrc ~* '\m(from|join)\s+(only\s+)?("?public"?\.)?"?(objects|spectra|targets|spectrum_line_fits|spectrum_lines|object_photometry|object_photometry_bands|nircam_images|fields|mv_filter_options|spectrum_line_fits_status)"?\M'
                    OR p.prosrc ~* ',\s*("?public"?\.)?"?(objects|spectra|targets|spectrum_line_fits|spectrum_lines|object_photometry|object_photometry_bands|nircam_images|fields|mv_filter_options|spectrum_line_fits_status)"?\s+\w')) THEN
      RAISE EXCEPTION 'cat views: % still reads a live catalog table directly', fn;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace AND p.proname = fn AND p.provolatile = 'v') THEN
      RAISE EXCEPTION 'cat views: % is VOLATILE', fn;
    END IF;
    -- a FROM-less LATERAL gated only by the tag test is pulled up into a join
    -- filter, so its correlated subqueries run for every outer row in both
    -- modes (the 20 s admin overview of 2026-10-16); gate per column with
    -- CASE WHEN public.dr_browse_tag() ... THEN (subquery) END instead
    IF EXISTS (SELECT 1 FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace AND p.proname = fn
               AND p.prosrc ~ '\n\s*WHERE\s+public\.dr_browse_tag\(\)\s+IS\s+(NOT\s+)?NULL\s*\)\s*\w+\s+ON\s+true') THEN
      RAISE EXCEPTION 'cat views: % gates a FROM-less lateral with a WHERE on the browse tag (pulled up into a join filter; use CASE per column)', fn;
    END IF;
  END LOOP;
  -- nothing outside the switched set and dr_* calls a switched RPC: a caller
  -- that reads live rows would otherwise mix them with frozen aggregates
  -- under the header. Allowed: the sync streams, which assert live first.
  SELECT string_agg(p.proname || ' -> ' || c.fn, ', ') INTO bad
  FROM pg_proc p
  CROSS JOIN LATERAL unnest(switched) AS c(fn)
  WHERE p.pronamespace = 'public'::regnamespace
    AND NOT (p.proname = ANY (switched)) AND p.proname NOT LIKE 'dr\_%'
    AND p.proname NOT IN ('get_objects_for_sync')
    AND substring(p.prosrc FROM position('$' IN p.prosrc)) ~ ('\m' || c.fn || '\s*\(')
    AND p.prosrc ~ ('\m' || c.fn || '\s*\(');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'cat views: function(s) outside the switched set call a switched RPC: %', bad;
  END IF;
  -- ...and every sync stream asserts live before anything else
  SELECT string_agg(p.proname, ', ') INTO bad
  FROM pg_proc p
  WHERE p.pronamespace = 'public'::regnamespace
    AND p.proname IN ('get_objects_for_sync','get_sync_deletions','get_spectra_for_sync','get_photometry_for_sync',
                      'get_line_fits_for_sync','get_lists_for_sync','get_storage_objects_for_sync')
    AND p.prosrc !~ '\mdr_assert_live\s*\(';
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'cat views: sync stream(s) without dr_assert_live(): %', bad;
  END IF;
  RAISE NOTICE 'OK 2: no writer reads a cat_* view; % switched RPCs read no live catalog table; no outside caller; sync streams assert live', array_length(switched, 1);
END $$;

-- ------------------------------------------------------------------ 3
-- transaction-local: set in one transaction, gone in the next
BEGIN;
DO $$ BEGIN
  IF public.dr_browse_tag() IS NOT NULL THEN RAISE EXCEPTION 'dr_browse_tag not NULL at start: %', public.dr_browse_tag(); END IF;
  PERFORM set_config('campfire.dr_tag', 'DR-seed', true);
  IF public.dr_browse_tag() <> 'DR-seed' THEN RAISE EXCEPTION 'dr_browse_tag did not pick up the setting'; END IF;
END $$;
COMMIT;
DO $$ BEGIN
  IF public.dr_browse_tag() IS NOT NULL THEN
    RAISE EXCEPTION 'dr_browse_tag leaked across transactions: %', public.dr_browse_tag();
  END IF;
  RAISE NOTICE 'OK 3: dr_browse_tag() is transaction-local';
END $$;

-- ------------------------------------------------------------------ 4
BEGIN;
INSERT INTO auth.users (id, email) VALUES
  ('00000000-0000-0000-0000-0000000000c1', 'zzz-cat-admin@test.invalid'),
  ('00000000-0000-0000-0000-0000000000c2', 'zzz-cat-user@test.invalid');
INSERT INTO user_profiles (user_id, username, full_name, is_admin, is_link_account, can_comment, can_inspect) VALUES
  ('00000000-0000-0000-0000-0000000000c1', 'zzz-cat-admin', 'ZZZ Cat Admin', true,  false, true, true),
  ('00000000-0000-0000-0000-0000000000c2', 'zzz-cat-user',  'ZZZ Cat User',  false, false, true, false);
INSERT INTO auth.users (id, email) VALUES ('00000000-0000-0000-0000-0000000000c3', 'zzz-cat-link@test.invalid');
INSERT INTO user_profiles (user_id, username, full_name, is_admin, is_link_account, can_comment, can_inspect) VALUES
  ('00000000-0000-0000-0000-0000000000c3', 'zzz-cat-link', 'ZZZ Cat Link', false, true, false, false);
INSERT INTO share_links (token, label, observation, link_user_id, link_password, include_drafts, allow_download, created_by)
VALUES ('zzz_cat_tok', 'cat link', (SELECT t.observation FROM targets t WHERE t.has_published_spectrum LIMIT 1),
        '00000000-0000-0000-0000-0000000000c3', 'x', false, true, '00000000-0000-0000-0000-0000000000c1');
INSERT INTO dr_releases (tag, state, components, created_by, freeze_started_at) VALUES
  ('DR-zzz-unpub',    'frozen',   '["nirspec"]'::jsonb, '00000000-0000-0000-0000-0000000000c1', now()),
  ('DR-zzz-draft',    'draft',    '["nirspec"]'::jsonb, '00000000-0000-0000-0000-0000000000c1', NULL),
  ('DR-zzz-freezing', 'freezing', '["nirspec"]'::jsonb, '00000000-0000-0000-0000-0000000000c1', now()),
  ('DR-zzz-failed',   'failed',   '["nirspec"]'::jsonb, '00000000-0000-0000-0000-0000000000c1', now());

-- run the hook as a principal with a header, inside a savepoint so an
-- expected failure does not end the transaction; returns the sqlstate or the
-- tag it set
CREATE FUNCTION pg_temp.zzz_hook(p_role text, p_uid uuid, p_header text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE v text;
BEGIN
  PERFORM set_config('campfire.dr_tag', '', true);
  IF p_uid IS NULL THEN
    PERFORM set_config('request.jwt.claims', json_build_object('role', p_role)::text, true);
  ELSE
    PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', p_role)::text, true);
  END IF;
  PERFORM set_config('request.headers', CASE WHEN p_header IS NULL THEN '{}' ELSE json_build_object('x-campfire-dr', p_header)::text END, true);
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
DECLARE admin uuid := '00000000-0000-0000-0000-0000000000c1'; usr uuid := '00000000-0000-0000-0000-0000000000c2'; got text;
BEGIN
  got := pg_temp.zzz_hook('authenticated', usr, NULL);
  IF got <> 'SET:' THEN RAISE EXCEPTION 'no header should be a no-op, got %', got; END IF;
  got := pg_temp.zzz_hook('authenticated', usr, '');
  IF got <> 'SET:' THEN RAISE EXCEPTION 'empty header should be a no-op, got %', got; END IF;
  got := pg_temp.zzz_hook('authenticated', usr, 'DR-seed');
  IF got <> 'SET:DR-seed' THEN RAISE EXCEPTION 'reader + published tag should select it, got %', got; END IF;
  got := pg_temp.zzz_hook('authenticated', usr, 'DR-zzz-unpub');
  IF got <> 'PT404' THEN RAISE EXCEPTION 'reader + unpublished tag should be PT404, got %', got; END IF;
  got := pg_temp.zzz_hook('authenticated', usr, 'DR-nope');
  IF got <> 'PT404' THEN RAISE EXCEPTION 'unknown tag should be PT404, got %', got; END IF;
  got := pg_temp.zzz_hook('authenticated', usr, 'bad tag; drop');
  IF got <> 'PT404' THEN RAISE EXCEPTION 'malformed tag should be PT404, got %', got; END IF;
  got := pg_temp.zzz_hook('authenticated', admin, 'DR-zzz-unpub');
  IF got <> 'SET:DR-zzz-unpub' THEN RAISE EXCEPTION 'admin + unpublished tag should select it, got %', got; END IF;
  got := pg_temp.zzz_hook('anon', NULL, 'DR-seed');
  IF got <> 'SET:DR-seed' THEN RAISE EXCEPTION 'anon + published tag should select it (tables stay closed by grants), got %', got; END IF;
  got := pg_temp.zzz_hook('anon', NULL, 'DR-zzz-unpub');
  IF got <> 'PT404' THEN RAISE EXCEPTION 'anon + unpublished tag should be PT404, got %', got; END IF;
  got := pg_temp.zzz_hook('service_role', NULL, 'DR-zzz-unpub');
  IF got <> 'SET:DR-zzz-unpub' THEN RAISE EXCEPTION 'service role + any existing tag should select it, got %', got; END IF;
  got := pg_temp.zzz_hook('service_role', NULL, 'DR-nope');
  IF got <> 'PT404' THEN RAISE EXCEPTION 'service role + unknown tag should be PT404, got %', got; END IF;
  -- the service role is limited to releases past their freeze
  got := pg_temp.zzz_hook('service_role', NULL, 'DR-zzz-draft');
  IF got <> 'PT404' THEN RAISE EXCEPTION 'service role + draft tag should be PT404, got %', got; END IF;
  got := pg_temp.zzz_hook('service_role', NULL, 'DR-zzz-freezing');
  IF got <> 'PT404' THEN RAISE EXCEPTION 'service role + freezing tag should be PT404, got %', got; END IF;
  got := pg_temp.zzz_hook('service_role', NULL, 'DR-zzz-failed');
  IF got <> 'PT404' THEN RAISE EXCEPTION 'service role + failed tag should be PT404, got %', got; END IF;
  -- link accounts never browse a release, published or not
  got := pg_temp.zzz_hook('authenticated', '00000000-0000-0000-0000-0000000000c3', 'DR-seed');
  IF got <> 'PT404' THEN RAISE EXCEPTION 'link account + published tag should be PT404, got %', got; END IF;
  got := pg_temp.zzz_hook('authenticated', '00000000-0000-0000-0000-0000000000c3', 'DR-zzz-unpub');
  IF got <> 'PT404' THEN RAISE EXCEPTION 'link account + unpublished tag should be PT404, got %', got; END IF;
  got := pg_temp.zzz_hook('authenticated', '00000000-0000-0000-0000-0000000000c3', NULL);
  IF got <> 'SET:' THEN RAISE EXCEPTION 'link account without header should be a no-op, got %', got; END IF;
  RAISE NOTICE 'OK 4: dr_request_context (no-op without header; PT404 on malformed/unknown/invisible; reader published only; link refused; admin every state; service role post-freeze states)';
END $$;
ROLLBACK;

-- ------------------------------------------------------------------ 5
-- plan shape (the 2026-10-16 production regression): every cat_* view must
-- flatten into an Append of plain scans — no Subquery Scan, which a join
-- cannot parameterize — and the arm not selected must be gated off (never
-- executed), live and under a tag alike.
BEGIN;
DO $$
DECLARE
  pairs text[][] := ARRAY[
    ['objects','dr_objects','cat_objects'], ['targets','dr_targets','cat_targets'],
    ['spectra','dr_spectra','cat_spectra'], ['spectrum_line_fits','dr_line_fits','cat_line_fits'],
    ['spectrum_lines','dr_spectrum_lines','cat_spectrum_lines'], ['object_photometry','dr_photometry','cat_photometry'],
    ['object_photometry_bands','dr_photometry_bands','cat_photometry_bands'], ['nircam_images','dr_nircam_images','cat_nircam_images'],
    ['fields','dr_fields','cat_fields'], ['mv_filter_options','dr_filter_options','cat_filter_options']];
  i int; live text; frz text; v text; line text; plan text; mode text;
BEGIN
  FOREACH mode IN ARRAY ARRAY['live', 'dr'] LOOP
    PERFORM set_config('campfire.dr_tag', CASE WHEN mode = 'dr' THEN 'DR-seed' ELSE '' END, true);
    FOR i IN 1 .. array_length(pairs, 1) LOOP
      live := pairs[i][1]; frz := pairs[i][2]; v := pairs[i][3];
      plan := '';
      FOR line IN EXECUTE format('EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) SELECT count(*) FROM public.%I', v) LOOP
        plan := plan || line || E'\n';
      END LOOP;
      IF plan ~ 'Subquery Scan' THEN
        RAISE EXCEPTION 'cat views: % is not flattened (% mode): a join through it cannot use an index%', v, mode, E'\n' || plan;
      END IF;
      -- the unselected arm must be gated off, not merely empty
      IF mode = 'live' AND plan !~ ('on ' || frz || E'[^\\n]*\\(never executed\\)') THEN
        RAISE EXCEPTION 'cat views: % scans % live%', v, frz, E'\n' || plan;
      END IF;
      IF mode = 'dr' AND plan !~ ('on ' || live || E'[^\\n]*\\(never executed\\)') THEN
        RAISE EXCEPTION 'cat views: % scans % under a release%', v, live, E'\n' || plan;
      END IF;
    END LOOP;
  END LOOP;
  PERFORM set_config('campfire.dr_tag', '', true);
  RAISE NOTICE 'OK 5: % cat_* views flatten to plain scans; the unselected arm is never executed, live and under a tag', array_length(pairs, 1);
END $$;
ROLLBACK;
