-- =============================================================================
-- Browsing-mode equivalence (P6b-2, docs/design-data-releases-impl-plan.md §8 P6b-2).
-- =============================================================================
-- DR-seed is the seeded catalog frozen at the end of seeding (seed.sql §13),
-- so every switched read-only catalog RPC must return the same rows live and
-- with `campfire.dr_tag = 'DR-seed'` (what the X-Campfire-DR header sets
-- through dr_request_context), for an admin, the plain seed user and the
-- viewer. Each call runs twice in the same transaction, as the principal
-- (claims + SET LOCAL ROLE authenticated), and the two jsonb texts must be
-- identical. Known, accepted difference: counts of targets without a
-- published spectrum (get_database_overview.n_targets) -- a release holds the
-- targets of its spectra only. Before the comparisons: the observation
-- overview readers must not serve the live matview under a release (a live
-- edit visible in the matview must NOT show in DR mode); the sync streams
-- refuse the header; the three principals are made to differ (one program
-- with frozen rows goes private and the viewer loses its grant), so the
-- per-principal comparisons are not one comparison three times; and three
-- live-only tables must not leak into a release: a later deployment's
-- versions, an observation added after the freeze, the live layout key
-- (each edited live inside a savepoint, rolled back before the comparisons).
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_dr_browse.sql
\set ON_ERROR_STOP on
BEGIN;
-- the readers' path of the observation and program overviews; the seed does not refresh them
REFRESH MATERIALIZED VIEW public.mv_observations_overview;
REFRESH MATERIALIZED VIEW public.mv_programs_overview;

CREATE FUNCTION pg_temp.zzz_browse_as(p_uid uuid, p_tag text, p_sql text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE v text;
BEGIN
  PERFORM set_config('request.jwt.claims', json_build_object('sub', p_uid, 'role', 'authenticated')::text, true);
  PERFORM set_config('campfire.dr_tag', coalesce(p_tag, ''), true);
  SET LOCAL ROLE authenticated;
  EXECUTE p_sql INTO v;
  RESET ROLE;
  PERFORM set_config('campfire.dr_tag', '', true);
  PERFORM set_config('request.jwt.claims', '', true);
  RETURN coalesce(v, '<null>');
EXCEPTION WHEN OTHERS THEN
  RESET ROLE;
  PERFORM set_config('campfire.dr_tag', '', true);
  PERFORM set_config('request.jwt.claims', '', true);
  RETURN 'ERR: ' || SQLERRM;
END $$;
GRANT EXECUTE ON FUNCTION pg_temp.zzz_browse_as(uuid, text, text) TO authenticated;

-- compare live vs DR-seed for every principal; `p_strip` removes keys that
-- legitimately differ before comparing (a jsonb path list)
CREATE FUNCTION pg_temp.zzz_same(p_label text, p_sql text, p_strip text[] DEFAULT '{}', p_assert text DEFAULT NULL) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  who record; live text; frozen text; k text; lj jsonb; fj jsonb; ok boolean;
BEGIN
  FOR who IN SELECT u.id, u.email FROM auth.users u
             WHERE u.email IN ('admin@campfire.dev', 'user@campfire.dev', 'viewer@campfire.dev') ORDER BY u.email LOOP
    live := pg_temp.zzz_browse_as(who.id, NULL, p_sql);
    frozen := pg_temp.zzz_browse_as(who.id, 'DR-seed', p_sql);
    IF live LIKE 'ERR:%' OR frozen LIKE 'ERR:%' THEN
      RAISE EXCEPTION '% as %: live=% frozen=%', p_label, who.email, left(live, 300), left(frozen, 300);
    END IF;
    IF live = '<null>' AND frozen = '<null>' THEN
      RAISE EXCEPTION '% as %: both sides empty (the argument set exercises nothing)', p_label, who.email;
    END IF;
    -- the admin's live result must satisfy the case's own non-vacuity test
    IF p_assert IS NOT NULL AND who.email = 'admin@campfire.dev' THEN
      EXECUTE 'SELECT ' || p_assert INTO ok USING live::jsonb;
      IF NOT coalesce(ok, false) THEN
        RAISE EXCEPTION '% as admin: the result does not satisfy "%": %', p_label, p_assert, left(live, 400);
      END IF;
    END IF;
    IF array_length(p_strip, 1) > 0 AND live <> '<null>' AND frozen <> '<null>' THEN
      lj := live::jsonb; fj := frozen::jsonb;
      FOREACH k IN ARRAY p_strip LOOP
        IF jsonb_typeof(lj) = 'array' THEN
          lj := (SELECT jsonb_agg(e - k) FROM jsonb_array_elements(lj) e);
          fj := (SELECT jsonb_agg(e - k) FROM jsonb_array_elements(fj) e);
        ELSE
          lj := lj - k; fj := fj - k;
        END IF;
      END LOOP;
      live := lj::text; frozen := fj::text;
    END IF;
    IF live IS DISTINCT FROM frozen THEN
      RAISE EXCEPTION '% as % differs: live(%)=% ... frozen(%)=%', p_label, who.email,
        length(live), left(live, 400), length(frozen), left(frozen, 400);
    END IF;
  END LOOP;
  RAISE NOTICE 'OK %: identical live / DR-seed for admin, user, viewer', p_label;
END $$;

-- ---------------------------------------------------------------- readers
-- 1. the observation overviews never serve the live matview under a release:
--    bump one published spectrum's size live, refresh the matview, and the
--    reader's DR-mode total must still be the frozen one (savepoint: the
--    bump is undone before the comparisons)
SAVEPOINT live_edit;
DO $$
DECLARE
  usr uuid := (SELECT id FROM auth.users WHERE email = 'user@campfire.dev');
  obs text := (SELECT s.observation FROM dr_spectra s WHERE s.tag = 'DR-seed' GROUP BY s.observation ORDER BY count(*) DESC, s.observation LIMIT 1);
  frozen_total bigint := (SELECT sum(s.file_size) FROM dr_spectra s WHERE s.tag = 'DR-seed' AND s.observation = obs);
  got text; live_total bigint;
BEGIN
  UPDATE spectra SET file_size = file_size + 1000000
   WHERE id = (SELECT s.id FROM spectra s WHERE s.observation = obs AND s.deploy_status = 'published' ORDER BY s.id LIMIT 1);
  REFRESH MATERIALIZED VIEW public.mv_observations_overview;
  SELECT sum(s.file_size) INTO live_total FROM spectra s WHERE s.observation = obs AND s.deploy_status = 'published';
  IF live_total = frozen_total THEN RAISE EXCEPTION 'the live edit did not change the total for %', obs; END IF;
  got := pg_temp.zzz_browse_as(usr, 'DR-seed',
    format('SELECT (SELECT r.total_size_bytes::text FROM public.get_observation_stats((SELECT public.accessible_program_slugs()), false) r WHERE r.observation = %L)', obs));
  IF got IS DISTINCT FROM frozen_total::text THEN
    RAISE EXCEPTION 'get_observation_stats served live data under DR-seed for a reader: % (frozen %, live %)', got, frozen_total, live_total;
  END IF;
  got := pg_temp.zzz_browse_as(usr, 'DR-seed',
    format('SELECT (SELECT r.total_size_bytes::text FROM public.get_observations_overview((SELECT public.accessible_program_slugs()), false) r WHERE r.observation = %L)', obs));
  IF got IS DISTINCT FROM frozen_total::text THEN
    RAISE EXCEPTION 'get_observations_overview served live data under DR-seed for a reader: % (frozen %, live %)', got, frozen_total, live_total;
  END IF;
  -- and live mode still serves the edit (the matview path)
  got := pg_temp.zzz_browse_as(usr, NULL,
    format('SELECT (SELECT r.total_size_bytes::text FROM public.get_observation_stats((SELECT public.accessible_program_slugs()), false) r WHERE r.observation = %L)', obs));
  IF got IS DISTINCT FROM live_total::text THEN
    RAISE EXCEPTION 'get_observation_stats live mode did not serve the live total: % (live %)', got, live_total;
  END IF;
  RAISE NOTICE 'OK readers: observation overviews serve frozen numbers under DR-seed, live numbers live (observation %)', obs;
END $$;
ROLLBACK TO SAVEPOINT live_edit;

-- 2. the sync streams refuse the header
DO $$
DECLARE usr uuid := (SELECT id FROM auth.users WHERE email = 'user@campfire.dev'); got text;
BEGIN
  got := pg_temp.zzz_browse_as(usr, 'DR-seed', 'SELECT (SELECT count(*) FROM public.get_objects_for_sync((SELECT public.accessible_program_slugs()), NULL, NULL, 10))::text');
  IF got NOT LIKE 'ERR:%live-only%' THEN RAISE EXCEPTION 'get_objects_for_sync under DR-seed should raise live-only, got %', left(got, 200); END IF;
  got := pg_temp.zzz_browse_as(usr, 'DR-seed', 'SELECT (SELECT count(*) FROM public.get_spectra_for_sync((SELECT public.accessible_program_slugs()), NULL, NULL, 10))::text');
  IF got NOT LIKE 'ERR:%live-only%' THEN RAISE EXCEPTION 'get_spectra_for_sync under DR-seed should raise live-only, got %', left(got, 200); END IF;
  got := pg_temp.zzz_browse_as(usr, NULL, 'SELECT (SELECT count(*) FROM public.get_objects_for_sync((SELECT public.accessible_program_slugs()), NULL, NULL, 10))::text');
  IF got LIKE 'ERR:%' THEN RAISE EXCEPTION 'get_objects_for_sync live should work, got %', left(got, 200); END IF;
  RAISE NOTICE 'OK sync: get_objects_for_sync / get_spectra_for_sync refuse X-Campfire-DR and work live (get_sync_deletions is service_role-only; check_cat_views asserts its guard)';
END $$;

-- 3. make the principals differ: one program with frozen spectra that both
--    user@ and viewer@ hold by grant goes private, and viewer@ loses the grant
DO $$
DECLARE
  usr uuid := (SELECT id FROM auth.users WHERE email = 'user@campfire.dev');
  vwr uuid := (SELECT id FROM auth.users WHERE email = 'viewer@campfire.dev');
  adm uuid := (SELECT id FROM auth.users WHERE email = 'admin@campfire.dev');
  prog text; a text; u text; v text; nu int; nv int;
BEGIN
  SELECT s.program_slug INTO prog
  FROM dr_spectra s
  WHERE s.tag = 'DR-seed'
    AND EXISTS (SELECT 1 FROM user_program_access g WHERE g.user_id = usr AND g.program_slug = s.program_slug)
    AND EXISTS (SELECT 1 FROM user_program_access g WHERE g.user_id = vwr AND g.program_slug = s.program_slug)
  GROUP BY s.program_slug ORDER BY count(*) DESC, s.program_slug LIMIT 1;
  IF prog IS NULL THEN RAISE EXCEPTION 'no frozen program is granted to both user@ and viewer@'; END IF;
  UPDATE programs SET is_public = false WHERE slug = prog;
  DELETE FROM user_program_access WHERE user_id = vwr AND program_slug = prog;
  a := pg_temp.zzz_browse_as(adm, NULL, 'SELECT (SELECT public.accessible_program_slugs())::text');
  u := pg_temp.zzz_browse_as(usr, NULL, 'SELECT (SELECT public.accessible_program_slugs())::text');
  v := pg_temp.zzz_browse_as(vwr, NULL, 'SELECT (SELECT public.accessible_program_slugs())::text');
  -- admin and user may legitimately coincide (the one private program is
  -- granted to user@); viewer@ must have lost exactly that program
  IF u = v OR NOT (prog = ANY (a::text[])) OR NOT (prog = ANY (u::text[])) OR prog = ANY (v::text[]) THEN
    RAISE EXCEPTION 'principals still collapse on %: admin %, user %, viewer %', prog, left(a, 80), left(u, 80), left(v, 80);
  END IF;
  nu := (pg_temp.zzz_browse_as(usr, 'DR-seed', 'SELECT (SELECT r.total_count::text FROM public.get_filtered_objects_paginated(p_program_slugs => (SELECT public.accessible_program_slugs()), p_page_size => 1) r)'))::int;
  nv := (pg_temp.zzz_browse_as(vwr, 'DR-seed', 'SELECT (SELECT r.total_count::text FROM public.get_filtered_objects_paginated(p_program_slugs => (SELECT public.accessible_program_slugs()), p_page_size => 1) r)'))::int;
  IF NOT (nv < nu) THEN RAISE EXCEPTION 'viewer should see fewer objects than user under DR-seed: viewer %, user %', nv, nu; END IF;
  nu := (pg_temp.zzz_browse_as(usr, 'DR-seed', 'SELECT (SELECT r.total_count::text FROM public.get_filtered_spectra_paginated(p_program_slugs => (SELECT public.accessible_program_slugs()), p_page_size => 1) r)'))::int;
  nv := (pg_temp.zzz_browse_as(vwr, 'DR-seed', 'SELECT (SELECT r.total_count::text FROM public.get_filtered_spectra_paginated(p_program_slugs => (SELECT public.accessible_program_slugs()), p_page_size => 1) r)'))::int;
  IF NOT (nv < nu) THEN RAISE EXCEPTION 'viewer should see fewer spectra than user under DR-seed: viewer %, user %', nv, nu; END IF;
  RAISE NOTICE 'OK principals: program % private, viewer@ without its grant; admin / user / viewer scopes differ; viewer sees fewer objects and spectra', prog;
END $$;
-- the programs overview's live path is the matview, so it must see the flip
-- (under a release the row comes from the live programs table)
REFRESH MATERIALIZED VIEW public.mv_programs_overview;

-- 4. provenance under a release comes from the frozen spectra, never from a
--    later live deployment (deployments is live-only)
SAVEPOINT provenance;
DO $$
DECLARE
  usr uuid := (SELECT id FROM auth.users WHERE email = 'user@campfire.dev');
  adm uuid := (SELECT id FROM auth.users WHERE email = 'admin@campfire.dev');
  obs text := (SELECT s.observation FROM dr_spectra s WHERE s.tag = 'DR-seed' GROUP BY s.observation ORDER BY count(*) DESC, s.observation LIMIT 1);
  exp_cf text := (SELECT s.cfpipe_version FROM dr_spectra s WHERE s.tag = 'DR-seed' AND s.observation = obs AND s.cfpipe_version IS NOT NULL
                  GROUP BY s.cfpipe_version ORDER BY count(*) DESC, s.cfpipe_version LIMIT 1);
  q_stats text; q_ov text; got text;
BEGIN
  INSERT INTO deployments (observation, deployed_by, deployed_at, cfpipe_version, jwst_version, crds_context,
                           n_targets, n_spectra, n_new_targets, force_overwrite, source_ids_filter, supabase_only, status, published_at)
  VALUES (obs, adm, now(), 'zzz-cfpipe-9.9.9', 'zzz-jwst-9', 'jwst_9999.pmap', 0, 0, 0, false, NULL, true, 'published', now());
  q_stats := format('SELECT (SELECT r.cfpipe_version FROM public.get_observation_stats((SELECT public.accessible_program_slugs()), false) r WHERE r.observation = %L)', obs);
  q_ov := format('SELECT (SELECT r.cfpipe_version FROM public.get_observations_overview((SELECT public.accessible_program_slugs()), false) r WHERE r.observation = %L)', obs);
  -- live (the admin's live path) reports the new deployment
  got := pg_temp.zzz_browse_as(adm, NULL, q_stats);
  IF got IS DISTINCT FROM 'zzz-cfpipe-9.9.9' THEN RAISE EXCEPTION 'live get_observation_stats should report the new deployment, got %', got; END IF;
  -- the release reports its own spectra's version, for a reader and an admin
  got := pg_temp.zzz_browse_as(usr, 'DR-seed', q_stats);
  IF got IS DISTINCT FROM exp_cf OR got = 'zzz-cfpipe-9.9.9' THEN
    RAISE EXCEPTION 'get_observation_stats under DR-seed labels frozen spectra with a later deployment: % (frozen spectra: %)', got, exp_cf;
  END IF;
  got := pg_temp.zzz_browse_as(adm, 'DR-seed', q_stats);
  IF got IS DISTINCT FROM exp_cf OR got = 'zzz-cfpipe-9.9.9' THEN
    RAISE EXCEPTION 'get_observation_stats under DR-seed (admin) labels frozen spectra with a later deployment: %', got;
  END IF;
  got := pg_temp.zzz_browse_as(usr, 'DR-seed', q_ov);
  IF got IS DISTINCT FROM exp_cf OR got = 'zzz-cfpipe-9.9.9' THEN
    RAISE EXCEPTION 'get_observations_overview under DR-seed labels frozen spectra with a later deployment: %', got;
  END IF;
  -- no frozen deployments record: deployed_at is NULL under the release
  got := pg_temp.zzz_browse_as(usr, 'DR-seed', format('SELECT (SELECT r.deployed_at::text FROM public.get_observation_stats((SELECT public.accessible_program_slugs()), false) r WHERE r.observation = %L)', obs));
  IF got IS NOT NULL AND got <> '<null>' THEN RAISE EXCEPTION 'deployed_at under DR-seed should be NULL, got %', got; END IF;
  RAISE NOTICE 'OK provenance: a later live deployment of % does not relabel the release (frozen %)', obs, exp_cf;
END $$;
ROLLBACK TO SAVEPOINT provenance;

-- 5. an observation created after the freeze (or one without frozen
--    spectra) is not listed under the release
SAVEPOINT newobs;
DO $$
DECLARE
  usr uuid := (SELECT id FROM auth.users WHERE email = 'user@campfire.dev');
  adm uuid := (SELECT id FROM auth.users WHERE email = 'admin@campfire.dev');
  prog text := (SELECT s.program_slug FROM dr_spectra s WHERE s.tag = 'DR-seed' GROUP BY s.program_slug ORDER BY count(*) DESC, s.program_slug LIMIT 1);
  fld text := (SELECT t.field FROM dr_targets t WHERE t.tag = 'DR-seed' LIMIT 1);
  q text := 'SELECT (SELECT count(*) FROM public.get_observations_overview((SELECT public.accessible_program_slugs()), false) r WHERE r.observation = ''zzz_dr_newobs'')::text';
  got text;
BEGIN
  INSERT INTO observations (name, program_slug, jwst_program_id, field, file_globs, gratings)
  VALUES ('zzz_dr_newobs', prog, 9999, fld, ARRAY['zzz*'], '{}');
  got := pg_temp.zzz_browse_as(adm, NULL, q);
  IF got <> '1' THEN RAISE EXCEPTION 'live overview should list the new observation, got %', got; END IF;
  got := pg_temp.zzz_browse_as(usr, 'DR-seed', q);
  IF got <> '0' THEN RAISE EXCEPTION 'get_observations_overview under DR-seed lists an observation the release has no spectra of (reader): %', got; END IF;
  got := pg_temp.zzz_browse_as(adm, 'DR-seed', q);
  IF got <> '0' THEN RAISE EXCEPTION 'get_observations_overview under DR-seed lists an observation the release has no spectra of (admin): %', got; END IF;
  RAISE NOTICE 'OK observations: a post-freeze observation is listed live, not under DR-seed';
END $$;
ROLLBACK TO SAVEPOINT newobs;

-- 6. the NIRCam field readers never hand out the live layout key under a
--    release (the manifest's archived copy, or NULL before P6b-3 lists it)
SAVEPOINT layout;
DO $$
DECLARE
  usr uuid := (SELECT id FROM auth.users WHERE email = 'user@campfire.dev');
  adm uuid := (SELECT id FROM auth.users WHERE email = 'admin@campfire.dev');
  fld text := (SELECT ni.field FROM dr_nircam_images ni WHERE ni.tag = 'DR-seed' LIMIT 1);
  k text := 'data/products/nircam/zzz/layout/zzz_layout_live.png';
  q1 text; q2 text; got text;
BEGIN
  IF EXISTS (SELECT 1 FROM storage_objects so WHERE so.product_type = 'nircam_layout' AND so.field = fld AND so.status = 'active') THEN
    RAISE EXCEPTION 'the seed carries an active layout for %; adapt this case', fld;
  END IF;
  INSERT INTO storage_objects (backend, bucket, storage_key, content_hash, size_bytes, content_type, product_type, instrument, status, field)
  VALUES ('osn', 'data', k, 'sha256:zzz-layout', 10, 'image/png', 'nircam_layout', 'nircam', 'active', fld);
  q1 := format('SELECT (SELECT r.layout_key FROM public.get_nircam_fields() r WHERE r.field = %L)', fld);
  q2 := format('SELECT (SELECT r.layout_key FROM public.get_nircam_field_summary(%L) r)', fld);
  got := pg_temp.zzz_browse_as(adm, NULL, q1);
  IF got IS DISTINCT FROM k THEN RAISE EXCEPTION 'live get_nircam_fields should hand out the new layout key, got %', got; END IF;
  got := pg_temp.zzz_browse_as(adm, NULL, q2);
  IF got IS DISTINCT FROM k THEN RAISE EXCEPTION 'live get_nircam_field_summary should hand out the new layout key, got %', got; END IF;
  got := pg_temp.zzz_browse_as(usr, 'DR-seed', q1);
  IF got = k THEN RAISE EXCEPTION 'get_nircam_fields under DR-seed hands out the LIVE layout key'; END IF;
  got := pg_temp.zzz_browse_as(adm, 'DR-seed', q1);
  IF got = k THEN RAISE EXCEPTION 'get_nircam_fields under DR-seed (admin) hands out the LIVE layout key'; END IF;
  got := pg_temp.zzz_browse_as(usr, 'DR-seed', q2);
  IF got = k THEN RAISE EXCEPTION 'get_nircam_field_summary under DR-seed hands out the LIVE layout key'; END IF;
  RAISE NOTICE 'OK layout: the live layout key of % is served live and never under DR-seed (manifest: %)', fld, coalesce(got, 'none archived yet');
END $$;
ROLLBACK TO SAVEPOINT layout;

-- ---------------------------------------------------------- equivalence
DO $$
DECLARE
  aps text := '(SELECT public.accessible_program_slugs())';
  grat text := (SELECT gratings[1] FROM mv_filter_options);
  line text := (SELECT l.line FROM spectrum_lines l GROUP BY l.line ORDER BY count(*) DESC, l.line LIMIT 1);
  band text := (SELECT b.band FROM object_photometry_bands b GROUP BY b.band ORDER BY count(*) DESC, b.band LIMIT 1);
  obj record := (SELECT o FROM objects o WHERE o.has_published_spectrum AND o.is_active ORDER BY o.object_id OFFSET 3 LIMIT 1);
  -- an object with a measured line, so the line-S/N case returns a value
  fit_oid int; fit_line text;
  -- spectra.search_text is target_id + the file stem, not the object name
  srch_spec text := (SELECT left(s.target_id, 8) FROM spectra s WHERE s.deploy_status = 'published' ORDER BY s.id LIMIT 1);
  fld text := (SELECT o.field FROM objects o WHERE o.has_published_spectrum AND o.is_active GROUP BY o.field ORDER BY count(*) DESC, o.field LIMIT 1);
  obs text := (SELECT t.observation FROM targets t WHERE t.has_published_spectrum GROUP BY t.observation ORDER BY count(*) DESC, t.observation LIMIT 1);
  q text;
BEGIN
  SELECT t.object_id, l.line INTO fit_oid, fit_line
  FROM spectrum_lines l
  JOIN spectra s ON s.id = l.spectrum_id
  JOIN targets t ON t.target_id = s.target_id
  WHERE t.object_id IS NOT NULL AND l.snr IS NOT NULL AND s.deploy_status = 'published'
  ORDER BY l.snr DESC, l.spectrum_id, l.line LIMIT 1;
  IF grat IS NULL OR line IS NULL OR band IS NULL OR obj IS NULL OR fld IS NULL OR obs IS NULL OR fit_oid IS NULL THEN
    RAISE EXCEPTION 'seed lacks a grating/line/band/object/field/observation to exercise (% % % % % %)', grat, line, band, obj.object_id, fld, obs;
  END IF;
  RAISE NOTICE 'arguments: grating % line % band % object % field % observation %', grat, line, band, obj.object_id, fld, obs;

  -- objects list
  q := format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_filtered_objects_paginated(p_program_slugs => %s, p_page_size => 50%%s) r)::text', aps);
  PERFORM pg_temp.zzz_same('objects default page', format(q, ''));
  PERFORM pg_temp.zzz_same('objects grating filter', format(q, format(', p_gratings => ARRAY[%L], p_gratings_mode => ''any''', grat)));
  PERFORM pg_temp.zzz_same('objects line filter', format(q, format(', p_line => %L, p_line_snr_min => 3, p_line_include_stale => true', line)));
  PERFORM pg_temp.zzz_same('objects band filter', format(q, format(', p_band => %L, p_band_mag_max => 40', band)));
  PERFORM pg_temp.zzz_same('objects band sort', format(q, format(', p_band => %L, p_sort_column => ''band_mag'', p_sort_direction => ''asc''', band)));
  PERFORM pg_temp.zzz_same('objects text search', format(q, format(', p_search => %L', left(obj.object_id, 6))),
                           '{}', '($1->0->>''total_count'')::int > 0');
  PERFORM pg_temp.zzz_same('objects sorted by redshift', format(q, ', p_sort_column => ''redshift'', p_sort_direction => ''desc'''));
  PERFORM pg_temp.zzz_same('objects page 2', format(q, ', p_page => 2'));
  PERFORM pg_temp.zzz_same('objects field + quality', format(q, format(', p_fields => ARRAY[%L], p_redshift_quality => ARRAY[3,4]', fld)));
  PERFORM pg_temp.zzz_same('objects coord search', format(q, format(', p_coord_ra => %s, p_coord_dec => %s, p_radius_degrees => 0.05', obj.ra, obj.dec)));
  PERFORM pg_temp.zzz_same('objects has_photometry', format(q, ', p_has_photometry => true, p_photo_z_min => 0'));
  PERFORM pg_temp.zzz_same('objects observation filter', format(q, format(', p_observations => ARRAY[%L]', obs)));

  -- spectra list
  q := format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_filtered_spectra_paginated(p_program_slugs => %s, p_page_size => 50, p_include_thumbnails => true%%s) r)::text', aps);
  PERFORM pg_temp.zzz_same('spectra default page', format(q, ''));
  PERFORM pg_temp.zzz_same('spectra grating filter', format(q, format(', p_gratings => ARRAY[%L]', grat)));
  PERFORM pg_temp.zzz_same('spectra line filter', format(q, format(', p_line => %L, p_line_snr_min => 3, p_line_include_stale => true', line)));
  PERFORM pg_temp.zzz_same('spectra band filter', format(q, format(', p_band => %L, p_band_snr_min => 1', band)));
  PERFORM pg_temp.zzz_same('spectra text search', format(q, format(', p_search => %L', srch_spec)),
                           '{}', '($1->0->>''total_count'')::int > 0');
  PERFORM pg_temp.zzz_same('spectra sorted by snr', format(q, ', p_sort_column => ''signal_to_noise'', p_sort_direction => ''desc'''));
  PERFORM pg_temp.zzz_same('spectra page 2', format(q, ', p_page => 2'));
  PERFORM pg_temp.zzz_same('spectra dq filter', format(q, ', p_dq_flags_exclude => 1'));

  -- ids, adjacent, near, markers, aggregates
  PERFORM pg_temp.zzz_same('object ids', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_filtered_object_ids(p_program_slugs => %s) r)::text', aps));
  PERFORM pg_temp.zzz_same('adjacent objects', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_adjacent_objects(p_current_object_id => %L, p_program_slugs => %s) r)::text', obj.object_id, aps));
  PERFORM pg_temp.zzz_same('objects near', format('SELECT (SELECT jsonb_agg(to_jsonb(r) ORDER BY to_jsonb(r)::text) FROM public.get_objects_near(%s, %s, 0.05, %s, 20, NULL, false) r)::text', obj.ra, obj.dec, aps));
  PERFORM pg_temp.zzz_same('field markers', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_field_object_markers(%L, false, NULL, 500) r)::text', fld));
  PERFORM pg_temp.zzz_same('scoped aggregates', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.object_scoped_aggregates(%s, %s, false) r)::text', obj.id, aps));

  -- filter helpers
  PERFORM pg_temp.zzz_same('grating helper', format('SELECT (SELECT jsonb_agg(x ORDER BY x) FROM public.objects_matching_grating_filter(ARRAY[%L], ''any'', %s, false) x)::text', grat, aps));
  PERFORM pg_temp.zzz_same('observation helper', format('SELECT (SELECT jsonb_agg(x ORDER BY x) FROM public.objects_matching_observation_filter(ARRAY[%L], %s, false) x)::text', obs, aps));
  PERFORM pg_temp.zzz_same('line helper (objects)', format('SELECT (SELECT jsonb_agg(x ORDER BY x) FROM public.objects_matching_line_filter(%L, 3, NULL, true, %s, false) x)::text', line, aps));
  PERFORM pg_temp.zzz_same('line helper (spectra)', format('SELECT (SELECT jsonb_agg(x ORDER BY x) FROM public.spectra_matching_line_filter(%L, 3, NULL, true) x)::text', line));
  PERFORM pg_temp.zzz_same('line snr', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.object_line_snr(%s, %L, true, %s, false) r)::text', fit_oid, fit_line, aps),
                           '{}', 'jsonb_typeof($1->0) = ''number''');
  PERFORM pg_temp.zzz_same('band values', format('SELECT (SELECT jsonb_agg(to_jsonb(r) ORDER BY to_jsonb(r)::text) FROM public.object_band_values(%L, NULL, 40, NULL, NULL, %s, false) r)::text', band, aps));
  PERFORM pg_temp.zzz_same('band helper', format('SELECT (SELECT jsonb_agg(x ORDER BY x) FROM public.objects_matching_band_filter(%L, NULL, 40, NULL, NULL, %s, false) x)::text', band, aps));

  -- NIRCam: admins see draft mosaics live (RLS hides them from readers) and
  -- a release holds published mosaics only, so the seed's draft images are
  -- dropped for the comparison (rolled back with the transaction)
  DELETE FROM nircam_images WHERE deploy_status <> 'published';
  PERFORM pg_temp.zzz_same('nircam fields', 'SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_nircam_fields() r)::text');
  PERFORM pg_temp.zzz_same('nircam field summary', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_nircam_field_summary(%L) r)::text',
                                                          (SELECT ni.field FROM nircam_images ni WHERE ni.deploy_status = 'published' LIMIT 1)));
  PERFORM pg_temp.zzz_same('csv objects', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_csv_export_objects(p_program_slugs => %s, p_page_size => 50) r)::text', aps));
  PERFORM pg_temp.zzz_same('csv spectra', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_csv_export_spectra(p_program_slugs => %s, p_page_size => 50) r)::text', aps));
  PERFORM pg_temp.zzz_same('observation manifest', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_observation_manifest(%L, %s, false) r)::text', obs, aps));
  PERFORM pg_temp.zzz_same('database overview', 'SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_database_overview(false) r)::text', ARRAY['n_targets']);
  -- live: readers get the matview (refreshed above), admins the live path;
  -- under DR-seed everyone takes the cat_* path. The provenance columns are
  -- by design not comparable: live they describe the newest deployment,
  -- under a release the release's own spectra (no frozen deployments
  -- record: deployed_at, deployer and patch counts are NULL / 0); case 4
  -- above pins the release-side rule. Counts, pointings and gratings must
  -- agree.
  PERFORM pg_temp.zzz_same('observation stats', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_observation_stats(%s, false) r)::text', aps),
                           ARRAY['crds_context','cfpipe_version','jwst_version','reduced_at','deployed_at','deployed_by_username','deployed_by_full_name','n_patches_since_full','last_patch_at']);
  -- (live lists every observation of the programs, with zero counts for
  -- those without published spectra; a release lists only the observations
  -- it holds spectra of, case 5 above, so the comparison keeps the rows
  -- with spectra on both sides)
  PERFORM pg_temp.zzz_same('observations overview', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_observations_overview(%s, false) r WHERE r.spectrum_count > 0)::text', aps),
                           ARRAY['crds_context','cfpipe_version','jwst_version','reduced_at','deployed_at','deployed_by_username','deployed_by_full_name','n_patches_since_full','last_patch_at']);
  -- programs overview (P6c-2): live is the matview (every program; every
  -- target counted, with or without a published spectrum), under a release
  -- the programs with frozen targets. Compared on the caller's programs with
  -- published spectra on both sides. By design not comparable: target_count,
  -- fields and observations (live: every target of the program, the
  -- n_targets difference above; release: the targets of its spectra),
  -- n_observations (live: the program's config rows; release: the
  -- observations it holds) and last_reduced_at (deployment vs frozen
  -- spectrum). Identity, gratings and PIDs must agree.
  PERFORM pg_temp.zzz_same('programs overview', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_programs_overview() r WHERE r.slug IN (SELECT unnest(%s)) AND COALESCE(array_length(r.gratings, 1), 0) > 0)::text', aps),
                           ARRAY['target_count','fields','observations','n_observations','last_reduced_at']);
  PERFORM pg_temp.zzz_same('program slugs', 'SELECT to_jsonb(public.cat_program_slugs())::text');
END $$;

-- an unpublished tag is invisible to a reader through the views too: the
-- hook refuses it (check_cat_views), and even a setting forced in place
-- returns nothing under the frozen tables' RLS
DO $$
DECLARE usr uuid := (SELECT id FROM auth.users WHERE email = 'user@campfire.dev'); got text;
BEGIN
  INSERT INTO dr_releases (tag, state, components, created_by, freeze_started_at)
  VALUES ('DR-zzz-browse', 'freezing', '["nirspec"]'::jsonb, (SELECT id FROM auth.users WHERE email = 'admin@campfire.dev'), now());
  PERFORM public.dr_freeze_job('DR-zzz-browse');
  IF (SELECT state FROM dr_releases WHERE tag = 'DR-zzz-browse') <> 'frozen' THEN
    RAISE EXCEPTION 'probe freeze failed: %', (SELECT error FROM dr_releases WHERE tag = 'DR-zzz-browse');
  END IF;
  got := pg_temp.zzz_browse_as(usr, 'DR-zzz-browse', 'SELECT count(*)::text FROM public.cat_objects');
  IF got <> '0' THEN RAISE EXCEPTION 'reader sees % frozen objects of an unpublished tag through cat_objects', got; END IF;
  got := pg_temp.zzz_browse_as((SELECT id FROM auth.users WHERE email = 'admin@campfire.dev'), 'DR-zzz-browse', 'SELECT count(*)::text FROM public.cat_objects');
  IF got::int = 0 THEN RAISE EXCEPTION 'admin sees nothing of an unpublished tag through cat_objects'; END IF;
  RAISE NOTICE 'OK: an unpublished tag is empty for a reader through cat_*, populated for an admin';
END $$;

ROLLBACK;
