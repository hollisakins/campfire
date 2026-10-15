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
-- targets of its spectra only.
-- Run: psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_dr_browse.sql
\set ON_ERROR_STOP on
BEGIN;

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
CREATE FUNCTION pg_temp.zzz_same(p_label text, p_sql text, p_strip text[] DEFAULT '{}', p_allow_empty boolean DEFAULT false) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  who record; live text; frozen text; k text; lj jsonb; fj jsonb;
BEGIN
  FOR who IN SELECT u.id, u.email FROM auth.users u
             WHERE u.email IN ('admin@campfire.dev', 'user@campfire.dev', 'viewer@campfire.dev') ORDER BY u.email LOOP
    live := pg_temp.zzz_browse_as(who.id, NULL, p_sql);
    frozen := pg_temp.zzz_browse_as(who.id, 'DR-seed', p_sql);
    IF live LIKE 'ERR:%' OR frozen LIKE 'ERR:%' THEN
      RAISE EXCEPTION '% as %: live=% frozen=%', p_label, who.email, left(live, 300), left(frozen, 300);
    END IF;
    IF live = '<null>' AND frozen = '<null>' AND NOT p_allow_empty THEN
      RAISE EXCEPTION '% as %: both sides empty (the argument set exercises nothing)', p_label, who.email;
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

DO $$
DECLARE
  aps text := '(SELECT public.accessible_program_slugs())';
  grat text := (SELECT gratings[1] FROM mv_filter_options);
  line text := (SELECT l.line FROM spectrum_lines l GROUP BY l.line ORDER BY count(*) DESC, l.line LIMIT 1);
  band text := (SELECT b.band FROM object_photometry_bands b GROUP BY b.band ORDER BY count(*) DESC, b.band LIMIT 1);
  obj record := (SELECT o FROM objects o WHERE o.has_published_spectrum AND o.is_active ORDER BY o.object_id OFFSET 3 LIMIT 1);
  fld text := (SELECT o.field FROM objects o WHERE o.has_published_spectrum AND o.is_active GROUP BY o.field ORDER BY count(*) DESC, o.field LIMIT 1);
  obs text := (SELECT t.observation FROM targets t WHERE t.has_published_spectrum GROUP BY t.observation ORDER BY count(*) DESC, t.observation LIMIT 1);
  q text;
BEGIN
  IF grat IS NULL OR line IS NULL OR band IS NULL OR obj IS NULL OR fld IS NULL OR obs IS NULL THEN
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
  PERFORM pg_temp.zzz_same('objects text search', format(q, format(', p_search => %L', left(obj.object_id, 6))));
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
  PERFORM pg_temp.zzz_same('spectra text search', format(q, format(', p_search => %L', left(obj.object_id, 6))));
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
  PERFORM pg_temp.zzz_same('line snr', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.object_line_snr(%s, %L, true, %s, false) r)::text', obj.id, line, aps));
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
  -- readers get these from mv_observations_overview (live-only by
  -- construction, and not refreshed by the seed, hence empty for them); the
  -- live path that reads cat_spectra / cat_targets is the admin's
  PERFORM pg_temp.zzz_same('observation stats', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_observation_stats(%s, false) r)::text', aps), '{}', true);
  PERFORM pg_temp.zzz_same('observations overview', format('SELECT (SELECT jsonb_agg(to_jsonb(r)) FROM public.get_observations_overview(%s, false) r)::text', aps), '{}', true);
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
