-- Guard: the catalog observations filter must decide on the viewer-VISIBLE
-- observation set, not the stored objects.observations aggregate (issue #491
-- — same root cause as #488, on the observations axis).
--
-- objects.observations is computed at deploy time across ALL member targets —
-- blind to publication status and spanning programs the viewer may not
-- access. Filtering on it alone matched objects whose only member in the
-- selected observation is invisible to the viewer (proprietary program, or
-- draft-only member), while the displayed row — scoped via
-- object_scoped_aggregates / the member_targets publication gate — hides that
-- observation. This asserts the canonical helper
-- objects_matching_observation_filter() and the catalog RPCs
-- get_filtered_objects_paginated() / get_filtered_object_ids() /
-- get_adjacent_objects() scope the filter to members the viewer can actually
-- see. get_csv_export_objects() takes no observations parameter.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_observation_filter_scoping.sql
BEGIN;

DO $$
DECLARE
  v_obj_mixed INTEGER;   -- members in a public and a proprietary observation
  v_obj_draft INTEGER;   -- public program only: one published member + one draft-only member in a second observation
  v_json      JSONB;
  v_row       JSONB;
  v_count     BIGINT;
  v_total     BIGINT;
  v_ids       TEXT[];
BEGIN
  -- Fixtures ------------------------------------------------------------------
  INSERT INTO programs (slug, program_name, is_public) VALUES
    ('zzz_of_pub',  'Observation Filter Test Public',  true),
    ('zzz_of_prop', 'Observation Filter Test Private', false);

  INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
    ('of_obs_pub',  'zzz_of_pub',  9995, 'zzz_of_field'),
    ('of_obs_pub2', 'zzz_of_pub',  9996, 'zzz_of_field'),
    ('of_obs_prop', 'zzz_of_prop', 9997, 'zzz_of_field');

  -- Stored aggregates deliberately span everything, mirroring the deploy-time
  -- builder (python/campfire/deploy/objects.py): publication-status-blind and
  -- across all member programs.
  INSERT INTO objects (object_id, field, ra, dec,
                       n_targets, n_spectra, programs, gratings, observations,
                       max_snr, max_exposure_time)
  VALUES
    ('TEST-OF-MIXED', 'zzz_of_field', 150.0, 2.0,
     2, 2, ARRAY['zzz_of_prop','zzz_of_pub'], ARRAY['PRISM'],
     ARRAY['of_obs_prop','of_obs_pub'], 30.0, 2000.0),
    ('TEST-OF-DRAFT', 'zzz_of_field', 150.2, 2.2,
     2, 2, ARRAY['zzz_of_pub'], ARRAY['PRISM'],
     ARRAY['of_obs_pub','of_obs_pub2'], 20.0, 1500.0);

  SELECT id INTO v_obj_mixed FROM objects WHERE object_id = 'TEST-OF-MIXED';
  SELECT id INTO v_obj_draft FROM objects WHERE object_id = 'TEST-OF-DRAFT';

  -- has_published_spectrum is the deploy-maintained member gate the helper
  -- tests (no trigger keeps it — set it as reconcile would).
  INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id, has_published_spectrum) VALUES
    ('test-of-pub',      'zzz_of_field', 150.0, 2.0, 'zzz_of_pub',  'of_obs_pub',  v_obj_mixed, true),
    ('test-of-prop',     'zzz_of_field', 150.0, 2.0, 'zzz_of_prop', 'of_obs_prop', v_obj_mixed, true),
    ('test-of-draftpub', 'zzz_of_field', 150.2, 2.2, 'zzz_of_pub',  'of_obs_pub',  v_obj_draft, true),
    ('test-of-draft',    'zzz_of_field', 150.2, 2.2, 'zzz_of_pub',  'of_obs_pub2', v_obj_draft, false);

  INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status) VALUES
    ('test-of-pub',      'PRISM', '/tmp/of_pub_prism.fits',      10.0, 1000.0, 'published'),
    ('test-of-prop',     'PRISM', '/tmp/of_prop_prism.fits',     30.0, 2000.0, 'published'),
    ('test-of-draftpub', 'PRISM', '/tmp/of_draftpub_prism.fits', 15.0, 1200.0, 'published'),
    ('test-of-draft',    'PRISM', '/tmp/of_draft_prism.fits',    20.0, 1500.0, 'draft');

  -- 1) Helper semantics -------------------------------------------------------
  -- Public-only viewer on the mixed object: the proprietary observation's
  -- member is invisible.
  IF v_obj_mixed IN (SELECT public.objects_matching_observation_filter(ARRAY['of_obs_prop'], ARRAY['zzz_of_pub'], false)) THEN
    RAISE EXCEPTION 'helper: matched proprietary-only observation for public viewer';
  END IF;
  IF v_obj_mixed NOT IN (SELECT public.objects_matching_observation_filter(ARRAY['of_obs_pub'], ARRAY['zzz_of_pub'], false)) THEN
    RAISE EXCEPTION 'helper: failed to match visible public observation';
  END IF;
  -- Full-access viewer sees both member programs.
  IF v_obj_mixed NOT IN (SELECT public.objects_matching_observation_filter(ARRAY['of_obs_prop'], ARRAY['zzz_of_pub','zzz_of_prop'], false)) THEN
    RAISE EXCEPTION 'helper: failed to match proprietary observation for full-access viewer';
  END IF;
  -- Draft-only member: its observation only visible when unpublished included.
  IF v_obj_draft IN (SELECT public.objects_matching_observation_filter(ARRAY['of_obs_pub2'], ARRAY['zzz_of_pub'], false)) THEN
    RAISE EXCEPTION 'helper: matched draft-only member observation without include_unpublished';
  END IF;
  IF v_obj_draft NOT IN (SELECT public.objects_matching_observation_filter(ARRAY['of_obs_pub2'], ARRAY['zzz_of_pub'], true)) THEN
    RAISE EXCEPTION 'helper: failed to match draft-member observation with include_unpublished';
  END IF;
  -- The set must never contain NULL (NOT-IN three-valued-logic safety at any
  -- future call site).
  IF EXISTS (SELECT 1 FROM public.objects_matching_observation_filter(ARRAY['of_obs_pub','of_obs_pub2','of_obs_prop'], ARRAY['zzz_of_pub','zzz_of_prop'], true) f(id) WHERE f.id IS NULL) THEN
    RAISE EXCEPTION 'helper returned a NULL object id';
  END IF;

  -- 2) get_filtered_objects_paginated: the issue-#491 repro ------------------
  -- Public-only viewer filtering on the proprietary observation must get
  -- nothing — previously the stored-array && test matched TEST-OF-MIXED.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_of_pub'],
      p_fields        => ARRAY['zzz_of_field'],
      p_observations  => ARRAY['of_obs_prop']
    );
  IF v_count <> 0 OR jsonb_array_length(v_json) <> 0 THEN
    RAISE EXCEPTION 'paginated obs=of_obs_prop leaked rows to public viewer: count=%, rows=%', v_count, v_json;
  END IF;

  -- Draft-only member observation must not match without include_unpublished.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_of_pub'],
      p_fields        => ARRAY['zzz_of_field'],
      p_observations  => ARRAY['of_obs_pub2']
    );
  IF v_count <> 0 OR jsonb_array_length(v_json) <> 0 THEN
    RAISE EXCEPTION 'paginated obs=of_obs_pub2 matched via draft-only member: count=%, rows=%', v_count, v_json;
  END IF;

  -- Filter/display consistency: rows matched via the visible public
  -- observation must not surface the hidden members. (The row JSON carries
  -- member observations via member_targets — there is no top-level
  -- observations key.)
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_of_pub'],
      p_fields        => ARRAY['zzz_of_field'],
      p_observations  => ARRAY['of_obs_pub']
    );
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'paginated obs=of_obs_pub dropped visible rows: count=%', v_count;
  END IF;
  IF EXISTS (
    SELECT 1
    FROM jsonb_array_elements(v_json) AS r(row),
         jsonb_array_elements(r.row -> 'member_targets') AS m(member)
    WHERE m.member ->> 'observation' IN ('of_obs_prop', 'of_obs_pub2')
  ) THEN
    RAISE EXCEPTION 'obs=of_obs_pub returned a row displaying a hidden member observation: %', v_json;
  END IF;

  -- Full-access viewer: the proprietary observation matches the mixed object.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_of_pub','zzz_of_prop'],
      p_fields        => ARRAY['zzz_of_field'],
      p_observations  => ARRAY['of_obs_prop']
    );
  IF v_count <> 1 OR (v_json -> 0 ->> 'object_id') <> 'TEST-OF-MIXED' THEN
    RAISE EXCEPTION 'paginated obs=of_obs_prop wrong rows for full-access viewer: count=%, rows=%', v_count, v_json;
  END IF;

  -- Admin draft-inclusive view: the draft member's observation becomes
  -- visible.
  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_of_pub'],
      p_fields        => ARRAY['zzz_of_field'],
      p_observations  => ARRAY['of_obs_pub2'],
      p_include_unpublished => true
    );
  IF v_count <> 1 OR (v_json -> 0 ->> 'object_id') <> 'TEST-OF-DRAFT' THEN
    RAISE EXCEPTION 'paginated obs=of_obs_pub2 include_unpublished wrong rows: count=%, rows=%', v_count, v_json;
  END IF;

  -- 3) get_filtered_object_ids must agree with the paginated RPC -------------
  SELECT COALESCE(array_agg(f.object_id ORDER BY f.object_id), '{}') INTO v_ids
    FROM public.get_filtered_object_ids(
      p_program_slugs => ARRAY['zzz_of_pub'],
      p_fields        => ARRAY['zzz_of_field'],
      p_observations  => ARRAY['of_obs_prop']
    ) f;
  IF v_ids <> '{}'::text[] THEN
    RAISE EXCEPTION 'object_ids obs=of_obs_prop leaked rows: %', v_ids;
  END IF;
  SELECT COALESCE(array_agg(f.object_id ORDER BY f.object_id), '{}') INTO v_ids
    FROM public.get_filtered_object_ids(
      p_program_slugs => ARRAY['zzz_of_pub'],
      p_fields        => ARRAY['zzz_of_field'],
      p_observations  => ARRAY['of_obs_pub']
    ) f;
  IF v_ids <> ARRAY['TEST-OF-DRAFT','TEST-OF-MIXED'] THEN
    RAISE EXCEPTION 'object_ids obs=of_obs_pub wrong rows: %', v_ids;
  END IF;

  -- 4) get_adjacent_objects reuses the same helper + pre-filter --------------
  SELECT total_count INTO v_total
    FROM public.get_adjacent_objects(
      p_current_object_id => 'TEST-OF-MIXED',
      p_program_slugs     => ARRAY['zzz_of_pub'],
      p_fields            => ARRAY['zzz_of_field'],
      p_observations      => ARRAY['of_obs_prop']
    );
  IF v_total <> 0 THEN
    RAISE EXCEPTION 'adjacent obs=of_obs_prop counted invisible rows for public viewer: total=%', v_total;
  END IF;
  SELECT total_count INTO v_total
    FROM public.get_adjacent_objects(
      p_current_object_id => 'TEST-OF-MIXED',
      p_program_slugs     => ARRAY['zzz_of_pub','zzz_of_prop'],
      p_fields            => ARRAY['zzz_of_field'],
      p_observations      => ARRAY['of_obs_prop']
    );
  IF v_total <> 1 THEN
    RAISE EXCEPTION 'adjacent obs=of_obs_prop wrong count for full-access viewer: total=%', v_total;
  END IF;

  -- 5) Same guarantees under real RLS, as a normal authenticated viewer ------
  -- The RPCs are SECURITY INVOKER; zzz_of_pub is public, so it is accessible
  -- with no JWT claims set.
  PERFORM set_config('role', 'authenticated', true);

  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_of_pub'],
      p_fields        => ARRAY['zzz_of_field'],
      p_observations  => ARRAY['of_obs_prop']
    );
  IF v_count <> 0 OR jsonb_array_length(v_json) <> 0 THEN
    RAISE EXCEPTION 'authenticated: obs=of_obs_prop leaked rows: count=%, rows=%', v_count, v_json;
  END IF;

  SELECT targets, total_count INTO v_json, v_count
    FROM public.get_filtered_objects_paginated(
      p_program_slugs => ARRAY['zzz_of_pub'],
      p_fields        => ARRAY['zzz_of_field'],
      p_observations  => ARRAY['of_obs_pub']
    );
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'authenticated: obs=of_obs_pub dropped visible rows: count=%', v_count;
  END IF;
  IF EXISTS (
    SELECT 1
    FROM jsonb_array_elements(v_json) AS r(row),
         jsonb_array_elements(r.row -> 'member_targets') AS m(member)
    WHERE m.member ->> 'observation' IN ('of_obs_prop', 'of_obs_pub2')
  ) THEN
    RAISE EXCEPTION 'authenticated: obs=of_obs_pub returned a row displaying a hidden member observation: %', v_json;
  END IF;

  PERFORM set_config('role', 'none', true);

  RAISE NOTICE 'OK: observation filter scoping holds (helper + paginated + object_ids + adjacent RPCs, owner + authenticated).';
END $$;

ROLLBACK;
