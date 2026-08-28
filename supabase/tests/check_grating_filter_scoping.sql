-- Guard: the catalog grating filter must decide on the viewer-VISIBLE grating
-- set, not the stored objects.gratings aggregate (issue #488).
--
-- objects.gratings is computed at deploy time across ALL member spectra —
-- blind to publication status and spanning programs the viewer may not
-- access. Filtering on it alone made a PRISM-only row (as displayed) match an
-- M-grating filter through an unpublished or proprietary sibling spectrum,
-- and inversely excluded such rows from a "none of M" filter. This asserts
-- the canonical helper objects_matching_grating_filter() and the catalog RPCs
-- get_filtered_objects_paginated() / get_filtered_object_ids() scope the
-- filter to spectra the viewer can actually see. get_adjacent_objects() and
-- get_csv_export_objects() reuse the same helper + pre-filter pattern.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_grating_filter_scoping.sql
BEGIN;

DO $$
DECLARE
  v_obj_mixed INTEGER;   -- published PRISM in public program + published G395M in proprietary program
  v_obj_draft INTEGER;   -- public program only: published PRISM + draft G395M on one target
  v_json      JSONB;
  v_row       JSONB;
  v_count     BIGINT;
  v_ids       TEXT[];
BEGIN
  -- Fixtures ------------------------------------------------------------------
  INSERT INTO programs (slug, program_name, is_public) VALUES
    ('zzz_gf_pub',  'Grating Filter Test Public',  true),
    ('zzz_gf_prop', 'Grating Filter Test Private', false);

  INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
    ('gf_obs_pub',  'zzz_gf_pub',  9993, 'zzz_gf_field'),
    ('gf_obs_prop', 'zzz_gf_prop', 9994, 'zzz_gf_field');

  -- Stored aggregates deliberately span everything, mirroring the deploy-time
  -- builder (python/campfire/deploy/objects.py): publication-status-blind and
  -- across all member programs.
  INSERT INTO objects (object_id, field, ra, dec,
                       n_targets, n_spectra, programs, gratings, observations,
                       max_snr, max_exposure_time)
  VALUES
    ('TEST-GF-MIXED', 'zzz_gf_field', 150.0, 2.0,
     2, 2, ARRAY['zzz_gf_prop','zzz_gf_pub'], ARRAY['G395M','PRISM'],
     ARRAY['gf_obs_prop','gf_obs_pub'], 30.0, 2000.0),
    ('TEST-GF-DRAFT', 'zzz_gf_field', 150.2, 2.2,
     1, 2, ARRAY['zzz_gf_pub'], ARRAY['G395M','PRISM'],
     ARRAY['gf_obs_pub'], 20.0, 1500.0);

  SELECT id INTO v_obj_mixed FROM objects WHERE object_id = 'TEST-GF-MIXED';
  SELECT id INTO v_obj_draft FROM objects WHERE object_id = 'TEST-GF-DRAFT';

  INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id) VALUES
    ('test-gf-pub',   'zzz_gf_field', 150.0, 2.0, 'zzz_gf_pub',  'gf_obs_pub',  v_obj_mixed),
    ('test-gf-prop',  'zzz_gf_field', 150.0, 2.0, 'zzz_gf_prop', 'gf_obs_prop', v_obj_mixed),
    ('test-gf-draft', 'zzz_gf_field', 150.2, 2.2, 'zzz_gf_pub',  'gf_obs_pub',  v_obj_draft);

  INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status) VALUES
    ('test-gf-pub',   'PRISM', '/tmp/gf_pub_prism.fits',    10.0, 1000.0, 'published'),
    ('test-gf-prop',  'G395M', '/tmp/gf_prop_g395m.fits',   30.0, 2000.0, 'published'),
    ('test-gf-draft', 'PRISM', '/tmp/gf_draft_prism.fits',  15.0, 1200.0, 'published'),
    ('test-gf-draft', 'G395M', '/tmp/gf_draft_g395m.fits',  20.0, 1500.0, 'draft');

  -- 1) Helper semantics -------------------------------------------------------
  -- ('any' and 'none' share the same returned set; callers IN / NOT IN it.)
  -- Public-only viewer on the mixed object: the proprietary G395M is invisible.
  IF v_obj_mixed IN (SELECT public.objects_matching_grating_filter(ARRAY['G395M'], 'any', ARRAY['zzz_gf_pub'], false)) THEN
    RAISE EXCEPTION 'helper any: matched proprietary-only grating for public viewer';
  END IF;
  IF v_obj_mixed NOT IN (SELECT public.objects_matching_grating_filter(ARRAY['PRISM'], 'any', ARRAY['zzz_gf_pub'], false)) THEN
    RAISE EXCEPTION 'helper any: failed to match visible PRISM';
  END IF;
  IF v_obj_mixed IN (SELECT public.objects_matching_grating_filter(ARRAY['PRISM','G395M'], 'all', ARRAY['zzz_gf_pub'], false)) THEN
    RAISE EXCEPTION 'helper all: matched with G395M invisible to public viewer';
  END IF;
  -- Full-access viewer sees both member programs.
  IF v_obj_mixed NOT IN (SELECT public.objects_matching_grating_filter(ARRAY['G395M'], 'any', ARRAY['zzz_gf_pub','zzz_gf_prop'], false)) THEN
    RAISE EXCEPTION 'helper any: failed to match G395M for full-access viewer';
  END IF;
  IF v_obj_mixed NOT IN (SELECT public.objects_matching_grating_filter(ARRAY['PRISM','G395M'], 'all', ARRAY['zzz_gf_pub','zzz_gf_prop'], false)) THEN
    RAISE EXCEPTION 'helper all: failed for full-access viewer with both gratings visible';
  END IF;
  -- Duplicate selections must not break 'all' coverage counting.
  IF v_obj_mixed NOT IN (SELECT public.objects_matching_grating_filter(ARRAY['PRISM','G395M','PRISM'], 'all', ARRAY['zzz_gf_pub','zzz_gf_prop'], false)) THEN
    RAISE EXCEPTION 'helper all: duplicate grating selection broke coverage count';
  END IF;
  -- Draft object: G395M only visible when unpublished spectra are included.
  IF v_obj_draft IN (SELECT public.objects_matching_grating_filter(ARRAY['G395M'], 'any', ARRAY['zzz_gf_pub'], false)) THEN
    RAISE EXCEPTION 'helper any: matched draft-only grating without include_unpublished';
  END IF;
  IF v_obj_draft NOT IN (SELECT public.objects_matching_grating_filter(ARRAY['G395M'], 'any', ARRAY['zzz_gf_pub'], true)) THEN
    RAISE EXCEPTION 'helper any: failed to match draft grating with include_unpublished';
  END IF;
  -- The set must never contain NULL (NOT IN three-valued-logic safety).
  IF EXISTS (SELECT 1 FROM public.objects_matching_grating_filter(ARRAY['PRISM','G395M'], 'any', ARRAY['zzz_gf_pub','zzz_gf_prop'], true) f(id) WHERE f.id IS NULL) THEN
    RAISE EXCEPTION 'helper returned a NULL object id';
  END IF;

  -- 2) get_filtered_objects_paginated: the issue-#488 repro ------------------
  -- Public-only viewer filtering "M grating only" must NOT get the rows whose
  -- visible spectra are PRISM-only.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_gf_pub'],
      p_fields        => ARRAY['zzz_gf_field'],
      p_gratings      => ARRAY['G395M'],
      p_gratings_mode => 'any'
    );
  IF v_count <> 0 OR jsonb_array_length(v_json) <> 0 THEN
    RAISE EXCEPTION 'paginated any=G395M leaked PRISM-only rows to public viewer: count=%, rows=%', v_count, v_json;
  END IF;

  -- Inverse: "none of G395M" must return both objects for the public viewer.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_gf_pub'],
      p_fields        => ARRAY['zzz_gf_field'],
      p_gratings      => ARRAY['G395M'],
      p_gratings_mode => 'none'
    );
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'paginated none=G395M dropped visible PRISM-only rows: count=%', v_count;
  END IF;
  -- Filter/display consistency: a row returned by "none of G395M" must not
  -- itself display G395M. This guards the object_scoped_aggregates fast path,
  -- which must fall through to the viewer-scoped recompute whenever an object
  -- carries an unpublished member spectrum (the stored aggregates are
  -- publication-blind).
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(v_json) AS r(row)
    WHERE r.row -> 'gratings' @> '["G395M"]'::jsonb
  ) THEN
    RAISE EXCEPTION 'none=G395M returned a row whose displayed gratings contain G395M: %', v_json;
  END IF;
  -- The draft-sibling row must display published-only aggregates.
  SELECT r.row INTO v_row
    FROM jsonb_array_elements(v_json) AS r(row)
    WHERE r.row ->> 'object_id' = 'TEST-GF-DRAFT';
  IF v_row -> 'gratings' <> '["PRISM"]'::jsonb OR (v_row ->> 'n_spectra')::int <> 1 THEN
    RAISE EXCEPTION 'draft-sibling row displays unpublished aggregates: gratings=%, n_spectra=%',
      v_row -> 'gratings', v_row ->> 'n_spectra';
  END IF;

  -- Full-access viewer: any=G395M matches the mixed object only.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_gf_pub','zzz_gf_prop'],
      p_fields        => ARRAY['zzz_gf_field'],
      p_gratings      => ARRAY['G395M'],
      p_gratings_mode => 'any'
    );
  IF v_count <> 1 OR (v_json -> 0 ->> 'object_id') <> 'TEST-GF-MIXED' THEN
    RAISE EXCEPTION 'paginated any=G395M wrong rows for full-access viewer: count=%, rows=%', v_count, v_json;
  END IF;

  -- Admin draft-inclusive view: the draft G395M becomes visible.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_gf_pub'],
      p_fields        => ARRAY['zzz_gf_field'],
      p_gratings      => ARRAY['G395M'],
      p_gratings_mode => 'any',
      p_include_unpublished => true
    );
  IF v_count <> 1 OR (v_json -> 0 ->> 'object_id') <> 'TEST-GF-DRAFT' THEN
    RAISE EXCEPTION 'paginated any=G395M include_unpublished wrong rows: count=%, rows=%', v_count, v_json;
  END IF;

  -- 3) get_filtered_object_ids must agree with the paginated RPC -------------
  SELECT COALESCE(array_agg(f.object_id ORDER BY f.object_id), '{}') INTO v_ids
    FROM public.get_filtered_object_ids(
      p_program_slugs => ARRAY['zzz_gf_pub'],
      p_fields        => ARRAY['zzz_gf_field'],
      p_gratings      => ARRAY['G395M'],
      p_gratings_mode => 'any'
    ) f;
  IF v_ids <> '{}'::text[] THEN
    RAISE EXCEPTION 'object_ids any=G395M leaked PRISM-only rows: %', v_ids;
  END IF;
  SELECT COALESCE(array_agg(f.object_id ORDER BY f.object_id), '{}') INTO v_ids
    FROM public.get_filtered_object_ids(
      p_program_slugs => ARRAY['zzz_gf_pub'],
      p_fields        => ARRAY['zzz_gf_field'],
      p_gratings      => ARRAY['G395M'],
      p_gratings_mode => 'none'
    ) f;
  IF v_ids <> ARRAY['TEST-GF-DRAFT','TEST-GF-MIXED'] THEN
    RAISE EXCEPTION 'object_ids none=G395M wrong rows: %', v_ids;
  END IF;

  RAISE NOTICE 'OK: grating filter scoping holds (helper + paginated + object_ids RPCs).';
END $$;

ROLLBACK;
