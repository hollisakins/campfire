SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

DROP FUNCTION
  "public"."get_csv_export_objects"(text[], text[], text[], text[], text, integer[], double precision, double precision, double precision, double precision, double precision,
  double precision,
  text,
  boolean,
  boolean,
  integer[],
  text,
  double precision,
  double precision,
  double precision,
  boolean,
  double precision,
  double precision,
  text, text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, text, integer);

CREATE OR REPLACE FUNCTION public.apply_object_reconciliation (
  p_field      text,
  p_inserts    jsonb                    DEFAULT '[]'::jsonb,
  p_revivals   jsonb                    DEFAULT '[]'::jsonb,
  p_updates    jsonb                    DEFAULT '[]'::jsonb,
  p_orphan_ids integer[]                DEFAULT '{}'::integer[],
  p_updated_at timestamp with time zone DEFAULT now()
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SET statement_timeout TO '300s'
  AS $function$
DECLARE
  v_insert_id_map JSONB := '{}'::jsonb;
  v_revived_ids   INTEGER[] := '{}';
  v_updated_ids   INTEGER[] := '{}';
  v_orphaned      INTEGER := 0;
  v_reactivated   INTEGER := 0;
  v_fk_count      INTEGER := 0;
BEGIN
  -- 1. Orphans FIRST: soft-delete active objects that lost all members, but
  --    EXCLUDE any whose object_id an insert is about to re-adopt. Ghost
  --    recovery: a stranded active ghost is classified as an orphan while the
  --    real new cluster is an insert holding the same IAU name; the upsert in
  --    step 2 adopts that row, so it must not be soft-deleted out from under it.
  UPDATE objects o SET
    is_active = false,
    updated_at = p_updated_at
  WHERE o.id = ANY(p_orphan_ids)
    AND o.is_active
    AND o.object_id NOT IN (
      SELECT x.object_id FROM jsonb_to_recordset(p_inserts) AS x(object_id TEXT)
    );
  GET DIAGNOSTICS v_orphaned = ROW_COUNT;

  -- 2. Inserts (idempotent). ON CONFLICT re-adopts a ghost / soft-deleted row
  --    holding this object_id. We deliberately do NOT touch version /
  --    redshift_* / last_inspected_* so inspection state — and the triggers
  --    scoped to those columns — stay untouched; a collision is only ever a
  --    freshly stranded ghost with no inspection history.
  WITH ins AS (
    INSERT INTO objects (
      object_id, field, ra, dec, n_targets, n_spectra,
      programs, gratings, observations, max_snr, max_exposure_time, updated_at
    )
    SELECT x.object_id, p_field, x.ra, x.dec, x.n_targets, x.n_spectra,
           x.programs, x.gratings, x.observations, x.max_snr,
           x.max_exposure_time, p_updated_at
    FROM jsonb_to_recordset(p_inserts) AS x(
      object_id TEXT, ra DOUBLE PRECISION, dec DOUBLE PRECISION,
      n_targets INTEGER, n_spectra INTEGER,
      programs TEXT[], gratings TEXT[], observations TEXT[],
      max_snr DOUBLE PRECISION, max_exposure_time DOUBLE PRECISION
    )
    ON CONFLICT (object_id) DO UPDATE SET
      field = EXCLUDED.field,
      ra = EXCLUDED.ra,
      dec = EXCLUDED.dec,
      n_targets = EXCLUDED.n_targets,
      n_spectra = EXCLUDED.n_spectra,
      programs = EXCLUDED.programs,
      gratings = EXCLUDED.gratings,
      observations = EXCLUDED.observations,
      max_snr = EXCLUDED.max_snr,
      max_exposure_time = EXCLUDED.max_exposure_time,
      is_active = true,
      updated_at = p_updated_at
    RETURNING id, object_id
  )
  SELECT coalesce(jsonb_object_agg(object_id, id), '{}'::jsonb)
  INTO v_insert_id_map
  FROM ins;

  -- 3. Revivals: reactivate inactive objects matched by position; refresh
  --    centroid + aggregates.
  WITH rev AS (
    UPDATE objects o SET
      is_active = true,
      object_id = r.object_id,
      ra = r.ra,
      dec = r.dec,
      n_targets = r.n_targets,
      n_spectra = r.n_spectra,
      programs = r.programs,
      gratings = r.gratings,
      observations = r.observations,
      max_snr = r.max_snr,
      max_exposure_time = r.max_exposure_time,
      updated_at = p_updated_at
    FROM jsonb_to_recordset(p_revivals) AS r(
      object_db_id INTEGER, object_id TEXT, ra DOUBLE PRECISION,
      dec DOUBLE PRECISION, n_targets INTEGER, n_spectra INTEGER,
      programs TEXT[], gratings TEXT[], observations TEXT[],
      max_snr DOUBLE PRECISION, max_exposure_time DOUBLE PRECISION
    )
    WHERE o.id = r.object_db_id
    RETURNING o.id
  )
  SELECT coalesce(array_agg(id), '{}') INTO v_revived_ids FROM rev;

  -- 4. Updates: refresh aggregates; set staleness only when provided;
  --    reactivate a membership-matched inactive object when reactivate=true.
  WITH upd AS (
    UPDATE objects o SET
      object_id = u.object_id,
      ra = u.ra,
      dec = u.dec,
      n_targets = u.n_targets,
      n_spectra = u.n_spectra,
      programs = u.programs,
      gratings = u.gratings,
      observations = u.observations,
      max_snr = u.max_snr,
      max_exposure_time = u.max_exposure_time,
      is_active = CASE WHEN u.reactivate THEN true ELSE o.is_active END,
      updated_at = p_updated_at
    FROM jsonb_to_recordset(p_updates) AS u(
      object_db_id INTEGER, object_id TEXT, ra DOUBLE PRECISION,
      dec DOUBLE PRECISION, n_targets INTEGER, n_spectra INTEGER,
      programs TEXT[], gratings TEXT[], observations TEXT[],
      max_snr DOUBLE PRECISION, max_exposure_time DOUBLE PRECISION,
      reactivate BOOLEAN
    )
    WHERE o.id = u.object_db_id
    RETURNING o.id AS id, u.reactivate AS reactivated
  )
  SELECT coalesce(array_agg(id), '{}'),
         coalesce(count(*) FILTER (WHERE reactivated), 0)
  INTO v_updated_ids, v_reactivated
  FROM upd;

  -- 5. Target FK assignment — the actual fix. Derive (target, object) pairs
  --    from each operation's member_target_db_ids (resolving insert object ids
  --    via the RETURNING map) and apply them in ONE typed, index-friendly
  --    UPDATE, inside this same transaction as the inserts above.
  WITH pairs AS (
    SELECT unnest(x.member_target_db_ids) AS target_id,
           (v_insert_id_map ->> x.object_id)::INTEGER AS object_id
    FROM jsonb_to_recordset(p_inserts)
      AS x(object_id TEXT, member_target_db_ids INTEGER[])
    UNION ALL
    SELECT unnest(r.member_target_db_ids), r.object_db_id
    FROM jsonb_to_recordset(p_revivals)
      AS r(object_db_id INTEGER, member_target_db_ids INTEGER[])
    UNION ALL
    SELECT unnest(u.member_target_db_ids), u.object_db_id
    FROM jsonb_to_recordset(p_updates)
      AS u(object_db_id INTEGER, member_target_db_ids INTEGER[])
  )
  UPDATE targets t SET
    object_id = p.object_id,
    updated_at = p_updated_at
  FROM pairs p
  WHERE t.id = p.target_id;
  GET DIAGNOSTICS v_fk_count = ROW_COUNT;

  -- 6. Refresh denormalized search_text for every object touched above. Targets
  --    are relinked (step 5), so member target_ids / programs / observations
  --    resolve. Scoped to the affected ids so it stays cheap per apply.
  PERFORM public.recompute_object_search_text(
    (SELECT coalesce(array_agg(value::int), '{}'::integer[])
       FROM jsonb_each_text(v_insert_id_map))
    || v_revived_ids
    || v_updated_ids
  );

  RETURN jsonb_build_object(
    'insert_id_map', v_insert_id_map,
    'revived_ids', to_jsonb(v_revived_ids),
    'updated_ids', to_jsonb(v_updated_ids),
    'inserted_count', (SELECT count(*) FROM jsonb_object_keys(v_insert_id_map)),
    'revived_count', coalesce(array_length(v_revived_ids, 1), 0),
    'updated_count', coalesce(array_length(v_updated_ids, 1), 0),
    'reactivated_count', v_reactivated,
    'orphaned_count', v_orphaned,
    'target_fks_set', v_fk_count
  );
END;
$function$;

CREATE OR REPLACE FUNCTION public.compute_object_redshift_auto (
  p_field text
)
  RETURNS integer
  LANGUAGE plpgsql
  SET statement_timeout TO '300s'
  AS $function$
DECLARE
  n INTEGER;
  -- (1+z)-scaled tolerance for a grating auto-fit to count as consistent
  -- with the PRISM anchor: |z_grating - z_prism| / (1 + z_prism) < z_delta.
  -- Also the re-review threshold: auto-fit shifts smaller than this are the
  -- same solution at different precision and do not badge inspected objects.
  z_delta CONSTANT DOUBLE PRECISION := 0.01;
BEGIN
  WITH computed AS (
    SELECT o.id,
           o.redshift_auto AS old_auto,
           o.redshift_quality AS quality,
           best.new_val
    FROM objects o
    LEFT JOIN LATERAL (
      WITH members AS (
        SELECT s.redshift_auto AS z,
               s.exposure_time,
               s.id,
               CASE
                 WHEN s.grating = 'PRISM' THEN 0
                 WHEN s.grating = 'G395M' THEN 1
                 WHEN s.grating = 'G395H' THEN 2
                 WHEN s.grating = 'G235M' THEN 3
                 WHEN s.grating = 'G235H' THEN 4
                 WHEN s.grating = 'G140M' THEN 5
                 WHEN s.grating = 'G140H' THEN 6
                 ELSE 7
               END AS priority
        FROM targets t
        JOIN spectra s ON s.target_id = t.target_id
        WHERE t.object_id = o.id
          AND s.redshift_auto IS NOT NULL
      ),
      prism AS (
        SELECT z FROM members WHERE priority = 0
        ORDER BY exposure_time DESC NULLS LAST, id ASC
        LIMIT 1
      ),
      -- Best member outright: PRISM if present, else best grating.
      base AS (
        SELECT z FROM members
        ORDER BY priority ASC, exposure_time DESC NULLS LAST, id ASC
        LIMIT 1
      ),
      -- PRISM-consistent refinement: best known grating whose auto-fit
      -- agrees with the PRISM anchor. Empty when no PRISM member exists.
      refined AS (
        SELECT m.z
        FROM members m, prism p
        WHERE m.priority BETWEEN 1 AND 6
          AND ABS(m.z - p.z) / (1.0 + p.z) < z_delta
        ORDER BY m.priority ASC, m.exposure_time DESC NULLS LAST, m.id ASC
        LIMIT 1
      )
      SELECT COALESCE((SELECT z FROM refined), (SELECT z FROM base)) AS new_val
    ) best ON TRUE
    WHERE o.field = p_field
  )
  UPDATE objects o
  SET redshift_auto = c.new_val,
      updated_at = NOW()
  FROM computed c
  WHERE o.id = c.id
    AND o.redshift_auto IS DISTINCT FROM c.new_val;

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$function$;

CREATE OR REPLACE FUNCTION public.dr_readiness()
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  SET statement_timeout TO '120s'
  AS $function$
DECLARE
  v_is_admin boolean;
  v_spectra jsonb;
  v_objects jsonb;
  v_spectra_status jsonb;
  v_nircam_drafts bigint;
  v_deployments jsonb;
  v_leases jsonb;
  v_redrafted jsonb;
  v_dangling jsonb;
  v_etag jsonb;
  v_lines jsonb;
  v_has_sh_spectra boolean;
  v_has_sh_storage boolean;
  v_sh_spectra bigint;
  v_sh_storage bigint;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  -- 1. published spectra: one pass, grouped on the three keys we need.
  WITH po AS MATERIALIZED (
    SELECT s.cfpipe_version, s.observation, s.program_slug, count(*) AS n
    FROM spectra s
    WHERE s.deploy_status = 'published'
    GROUP BY 1, 2, 3
  ),
  by_version AS (
    SELECT COALESCE(cfpipe_version, 'unknown') AS v,
           sum(n) AS spectra,
           count(DISTINCT observation) AS observations
    FROM po
    GROUP BY 1
  )
  SELECT jsonb_build_object(
    'spectra_by_version',
      (SELECT COALESCE(jsonb_object_agg(v, jsonb_build_object('spectra', spectra, 'observations', observations)), '{}'::jsonb)
       FROM by_version),
    'published',
      (SELECT jsonb_build_object(
         'spectra', COALESCE(sum(n), 0),
         'observations', count(DISTINCT observation),
         'programs', count(DISTINCT program_slug))
       FROM po))
  INTO v_spectra;

  -- 2. draft / revoked spectra (partial index on deploy_status <> 'published').
  SELECT jsonb_build_object(
    'draft', count(*) FILTER (WHERE deploy_status = 'draft'),
    'revoked', count(*) FILTER (WHERE deploy_status = 'revoked'))
  INTO v_spectra_status
  FROM spectra
  WHERE deploy_status <> 'published';

  -- 3. objects: published / needs_review / implicit pins in one pass over active rows.
  SELECT jsonb_build_object(
    'published', count(*) FILTER (WHERE has_published_spectrum),
    'needs_review', count(*) FILTER (WHERE needs_review),
    'implicit_pins', count(*) FILTER (WHERE inspected_used_auto))
  INTO v_objects
  FROM objects
  WHERE is_active;

  -- 4. NIRCam drafts and deployments by status (small tables).
  SELECT count(*) INTO v_nircam_drafts FROM nircam_images WHERE deploy_status = 'draft';

  SELECT jsonb_build_object(
    'draft', count(*) FILTER (WHERE status = 'draft'),
    'revoked', count(*) FILTER (WHERE status = 'revoked'))
  INTO v_deployments
  FROM deployments;

  -- 5. live deploy leases (with the stale flag the takeover rule uses).
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'scope_type', d.scope_type,
      'scope_key', d.scope_key,
      'version', d.version,
      'in_progress_by', d.in_progress_by,
      'in_progress_by_username', up.username,
      'in_progress_host', d.in_progress_host,
      'in_progress_since', d.in_progress_since,
      'heartbeat_at', d.heartbeat_at,
      'stale', COALESCE(d.heartbeat_at, d.in_progress_since) < now() - deploy_lease_ttl())
    ORDER BY d.in_progress_since), '[]'::jsonb)
  INTO v_leases
  FROM deploy_scope_state d
  LEFT JOIN user_profiles up ON up.user_id = d.in_progress_by
  WHERE d.in_progress_since IS NOT NULL;

  -- 6. re-drafted NIRCam fields (see the header comment for the definition).
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'field', f.field,
      'draft_images', f.n_draft,
      'published_images', f.n_published,
      'published_deployments', f.n_published_deployments)
    ORDER BY f.field), '[]'::jsonb)
  INTO v_redrafted
  FROM (
    SELECT ni.field,
           count(*) FILTER (WHERE ni.deploy_status = 'draft') AS n_draft,
           count(*) FILTER (WHERE ni.deploy_status = 'published') AS n_published,
           (SELECT count(*) FROM deployments dp
             WHERE dp.field = ni.field AND dp.status = 'published') AS n_published_deployments
    FROM nircam_images ni
    GROUP BY ni.field
  ) f
  WHERE f.n_draft > 0 AND (f.n_published > 0 OR f.n_published_deployments > 0);

  -- 7. dangling registry rows: active nirspec_spec rows whose spectrum has no
  --    spectra row any more (deploy remove deletes spectra, not registry rows).
  SELECT jsonb_build_object(
    'count', COALESCE(sum(n), 0),
    'by_observation', COALESCE(jsonb_object_agg(COALESCE(observation, 'unknown'), n), '{}'::jsonb))
  INTO v_dangling
  FROM (
    SELECT so.observation, count(*) AS n
    FROM storage_objects so
    WHERE so.status = 'active'
      AND so.product_type = 'nirspec_spec'
      AND NOT EXISTS (SELECT 1 FROM spectra s WHERE s.spectrum_id = so.spectrum_id)
    GROUP BY so.observation
  ) x;

  -- 8. provisional etag: hashes by product_type.
  SELECT COALESCE(jsonb_object_agg(product_type, n), '{}'::jsonb)
  INTO v_etag
  FROM (
    SELECT so.product_type, count(*) AS n
    FROM storage_objects so
    WHERE so.status = 'active'
      AND so.content_hash LIKE 'etag:%'
      AND (so.backend = 'r2' OR so.product_type IN ('nirspec_spec', 'nircam_mosaic'))
    GROUP BY so.product_type
  ) x;

  -- 9. line-fit staleness from the ledger view.
  SELECT jsonb_build_object(
    'total', count(*),
    'stale_redshift', count(*) FILTER (WHERE stale_redshift),
    'stale_spectrum', count(*) FILTER (WHERE stale_spectrum))
  INTO v_lines
  FROM spectrum_line_fits_status;

  -- 10. science hash coverage, only once the columns exist (P0b, PR #586).
  SELECT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = 'spectra' AND column_name = 'science_hash'),
         EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = 'storage_objects' AND column_name = 'science_hash')
  INTO v_has_sh_spectra, v_has_sh_storage;
  IF v_has_sh_spectra THEN
    EXECUTE 'SELECT count(*) FROM public.spectra WHERE deploy_status = ''published'' AND science_hash IS NULL'
      INTO v_sh_spectra;
  END IF;
  IF v_has_sh_storage THEN
    EXECUTE 'SELECT count(*) FROM public.storage_objects WHERE status = ''active'' AND product_type = ''nirspec_spec'' AND science_hash IS NULL'
      INTO v_sh_storage;
  END IF;

  RETURN jsonb_build_object(
    'generated_at', now(),
    'spectra_by_version', v_spectra->'spectra_by_version',
    'published', (v_spectra->'published') || jsonb_build_object('objects', v_objects->'published'),
    'drafts', jsonb_build_object(
      'spectra', v_spectra_status->'draft',
      'nircam_images', v_nircam_drafts,
      'deployments', v_deployments->'draft'),
    'revoked', jsonb_build_object(
      'spectra', v_spectra_status->'revoked',
      'deployments', v_deployments->'revoked'),
    'leases', v_leases,
    'redrafted_nircam_fields', v_redrafted,
    'dangling_registry_rows', v_dangling,
    'etag_hash_rows', v_etag,
    'line_fits', v_lines,
    'needs_review', v_objects->'needs_review',
    'implicit_pins', v_objects->'implicit_pins',
    'science_hash_missing', jsonb_build_object(
      'spectra', to_jsonb(v_sh_spectra),
      'storage_objects', to_jsonb(v_sh_storage)));
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_adjacent_objects (
  p_current_object_id     text,
  p_program_slugs         text[],
  p_filter_programs       text[]           DEFAULT NULL::text[],
  p_fields                text[]           DEFAULT NULL::text[],
  p_gratings              text[]           DEFAULT NULL::text[],
  p_gratings_mode         text             DEFAULT 'any'::text,
  p_observations          text[]           DEFAULT NULL::text[],
  p_redshift_quality      integer[]        DEFAULT NULL::integer[],
  p_redshift_min          double precision DEFAULT NULL::double precision,
  p_redshift_max          double precision DEFAULT NULL::double precision,
  p_max_snr_min           double precision DEFAULT NULL::double precision,
  p_max_snr_max           double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max double precision DEFAULT NULL::double precision,
  p_search                text             DEFAULT NULL::text,
  p_inspected_only        boolean          DEFAULT NULL::boolean,
  p_needs_review          boolean          DEFAULT NULL::boolean,
  p_list_ids              integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode         text             DEFAULT 'any'::text,
  p_coord_ra              double precision DEFAULT NULL::double precision,
  p_coord_dec             double precision DEFAULT NULL::double precision,
  p_radius_degrees        double precision DEFAULT NULL::double precision,
  p_sort_column           text             DEFAULT 'object_id'::text,
  p_sort_direction        text             DEFAULT 'asc'::text,
  p_has_photometry        boolean          DEFAULT NULL::boolean,
  p_photo_z_min           double precision DEFAULT NULL::double precision,
  p_photo_z_max           double precision DEFAULT NULL::double precision,
  p_comment_search        text             DEFAULT NULL::text,
  p_comment_search_scope  text             DEFAULT NULL::text,
  p_comment_user_id       uuid             DEFAULT NULL::uuid,
  p_include_unpublished   boolean          DEFAULT false,
  p_line                  text             DEFAULT NULL::text,
  p_line_snr_min          double precision DEFAULT NULL::double precision,
  p_line_snr_max          double precision DEFAULT NULL::double precision,
  p_line_include_stale    boolean          DEFAULT false,
  p_band                  text             DEFAULT NULL::text,
  p_band_mag_min          double precision DEFAULT NULL::double precision,
  p_band_mag_max          double precision DEFAULT NULL::double precision,
  p_band_snr_min          double precision DEFAULT NULL::double precision,
  p_band_snr_max          double precision DEFAULT NULL::double precision
)
  RETURNS TABLE (
    prev_object_id text,
    next_object_id text,
    current_index  bigint,
    total_count    bigint
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  AS $function$
DECLARE
  v_line_object_ids INTEGER[];
  v_band_object_ids INTEGER[];
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_grating_object_ids INTEGER[];
  v_observation_filter_active BOOLEAN;
  v_observation_object_ids INTEGER[];
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_sort_is_text BOOLEAN;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN v_list_ids_mode := 'any'; END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  -- Same sortable set as get_filtered_objects_paginated (photo_z included).
  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)
    OR (p_sort_column = 'line_snr' AND p_line IS NOT NULL)
    OR (p_sort_column IN ('band_mag', 'band_snr') AND p_band IS NOT NULL)) THEN
    p_sort_column := 'object_id';
  END IF;
  IF v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
    p_sort_direction := 'asc';
  END IF;
  v_sort_is_text := p_sort_column IN ('object_id', 'field');

  -- Emission-line filter: the viewer-visible object set with a measurement of
  -- p_line in the S/N bounds, materialized once per call (see
  -- objects_matching_line_filter for the invariants).
  IF p_line IS NOT NULL THEN
    v_line_object_ids := ARRAY(SELECT public.objects_matching_line_filter(
      p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false),
      p_program_slugs, p_include_unpublished));
  END IF;

  -- Photometry band filter: the objects whose photometry in p_band lies in the
  -- magnitude / S/N window, materialized once per call the same way (see
  -- objects_matching_band_filter for the invariants). Object-level even in the
  -- spectra RPCs — photometry belongs to the sky position, not the spectrum —
  -- so a spectrum matches iff its target's parent object does.
  IF p_band IS NOT NULL THEN
    v_band_object_ids := ARRAY(SELECT public.objects_matching_band_filter(
      p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max,
      p_program_slugs, p_include_unpublished));
  END IF;

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs))
    INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN QUERY SELECT NULL::TEXT, NULL::TEXT, 0::BIGINT, 0::BIGINT;
    RETURN;
  END IF;

  -- Issue #488: materialize the viewer-visible grating match set ONCE per call
  -- (statements below consume it via hashed = ANY; an IN-subplan would be
  -- rebuilt per statement — twice in the count+page RPC). See
  -- objects_matching_grating_filter().
  IF v_grating_filter_active THEN
    v_grating_object_ids := ARRAY(SELECT public.objects_matching_grating_filter(p_gratings, v_gratings_mode, p_program_slugs, p_include_unpublished));
  END IF;

  -- Issue #491: same once-per-call materialization for the viewer-visible
  -- observation match set. Scoped to the full accessible p_program_slugs (not
  -- the p_filter_programs-narrowed set) to stay consistent with what rows
  -- display — see objects_matching_observation_filter().
  -- COALESCE: array_length('{}',1) is NULL, and a NULL flag would make the
  -- NOT-flag predicate below reject every row instead of treating an empty
  -- selection as no filter (the pre-#491 predicate's explicit behavior).
  v_observation_filter_active := (p_observations IS NOT NULL AND COALESCE(array_length(p_observations, 1), 0) > 0);
  IF v_observation_filter_active THEN
    v_observation_object_ids := ARRAY(SELECT public.objects_matching_observation_filter(p_observations, p_program_slugs, p_include_unpublished));
  END IF;

  RETURN QUERY
  WITH filtered AS (
    -- Narrow projection on purpose: this is what gets sorted.
    SELECT
      o.object_id,
      CASE p_sort_column
        WHEN 'object_id' THEN o.object_id WHEN 'field' THEN o.field ELSE NULL
      END AS sort_text,
      CASE p_sort_column
        WHEN 'ra' THEN o.ra WHEN 'dec' THEN o.dec
        WHEN 'redshift' THEN o.redshift::DOUBLE PRECISION
        WHEN 'redshift_quality' THEN o.redshift_quality::DOUBLE PRECISION
        WHEN 'n_targets' THEN o.n_targets::DOUBLE PRECISION
        WHEN 'n_spectra' THEN o.n_spectra::DOUBLE PRECISION
        WHEN 'max_snr' THEN o.max_snr WHEN 'max_exposure_time' THEN o.max_exposure_time
        WHEN 'photo_z' THEN o.photo_z
        WHEN 'line_snr' THEN public.object_line_snr(o.id, p_line, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished)
        WHEN 'band_mag' THEN bv.mag
        WHEN 'band_snr' THEN bv.snr
        WHEN 'distance' THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS sort_num
    FROM objects o
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = o.id
    WHERE
      o.programs && v_filtered_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        -- Issue #488: the o.gratings array tests are index-backed pre-filters
        -- only (deploy-time aggregate over ALL member spectra, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_grating_object_ids — see
        -- objects_matching_grating_filter() for the invariants.
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'none' AND (NOT o.gratings && p_gratings
            OR NOT (o.id = ANY(v_grating_object_ids))))
      )
      AND (
        NOT v_observation_filter_active
        -- Issue #491: the o.observations && test is an index-backed pre-filter
        -- only (deploy-time aggregate over ALL member targets, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_observation_object_ids — see
        -- objects_matching_observation_filter() for the invariants.
        OR (o.observations && p_observations
            AND o.id = ANY(v_observation_object_ids))
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_line IS NULL OR o.id = ANY(v_line_object_ids))
      AND (p_band IS NULL OR o.id = ANY(v_band_object_ids))
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
      AND (p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0))
      AND (p_needs_review IS NULL
        -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
        OR o.needs_review = p_needs_review)
      AND (
        NOT v_coord_search_active
        OR (
          -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
          -- every RA, so the RA bound is dropped there (the box is only the index
          -- pre-filter; the Haversine cut below decides membership either way).
          (ABS(p_coord_dec) + p_radius_degrees >= 90
           OR o.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                      AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
          AND 2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          ))) <= p_radius_degrees
        )
      )
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND o.id IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = o.id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND o.id NOT IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
      AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin; see get_filtered_objects_paginated for rationale.
        OR o.id IN (
          SELECT c.object_id FROM comments c
          WHERE c.object_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
          UNION
          SELECT t.object_id FROM comments c
          JOIN targets t ON t.id = c.target_id
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
  ),
  ranked AS (
    SELECT
      f.object_id,
      LAG(f.object_id) OVER w AS prev_id,
      LEAD(f.object_id) OVER w AS next_id,
      ROW_NUMBER() OVER w AS rn,
      -- Same window as the others (LAG/LEAD/ROW_NUMBER ignore the frame), so
      -- all four run in ONE WindowAgg; a separate COUNT(*) OVER () added a
      -- second pass with its own tuplestore, which spilled at prod's 3.5 MB
      -- work_mem.
      COUNT(*) OVER w AS total
    FROM filtered f
    WINDOW w AS (ORDER BY
      CASE WHEN v_sort_is_text AND p_sort_direction = 'asc' THEN f.sort_text END ASC NULLS LAST,
      CASE WHEN v_sort_is_text AND p_sort_direction = 'desc' THEN f.sort_text END DESC NULLS LAST,
      CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'asc' THEN f.sort_num END ASC NULLS LAST,
      CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'desc' THEN f.sort_num END DESC NULLS LAST,
      f.object_id ASC
      ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
  )
  SELECT r.prev_id, r.next_id, r.rn::BIGINT, r.total::BIGINT
  FROM ranked r
  WHERE r.object_id = p_current_object_id;

  IF NOT FOUND THEN
    RETURN QUERY SELECT NULL::TEXT, NULL::TEXT, 0::BIGINT, 0::BIGINT;
  END IF;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_csv_export_objects (
  p_program_slugs         text[],
  p_filter_programs       text[]           DEFAULT NULL::text[],
  p_fields                text[]           DEFAULT NULL::text[],
  p_gratings              text[]           DEFAULT NULL::text[],
  p_gratings_mode         text             DEFAULT 'any'::text,
  p_redshift_quality      integer[]        DEFAULT NULL::integer[],
  p_redshift_min          double precision DEFAULT NULL::double precision,
  p_redshift_max          double precision DEFAULT NULL::double precision,
  p_max_snr_min           double precision DEFAULT NULL::double precision,
  p_max_snr_max           double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max double precision DEFAULT NULL::double precision,
  p_search                text             DEFAULT NULL::text,
  p_inspected_only        boolean          DEFAULT NULL::boolean,
  p_needs_review          boolean          DEFAULT NULL::boolean,
  p_list_ids              integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode         text             DEFAULT 'any'::text,
  p_coord_ra              double precision DEFAULT NULL::double precision,
  p_coord_dec             double precision DEFAULT NULL::double precision,
  p_radius_degrees        double precision DEFAULT NULL::double precision,
  p_has_photometry        boolean          DEFAULT NULL::boolean,
  p_photo_z_min           double precision DEFAULT NULL::double precision,
  p_photo_z_max           double precision DEFAULT NULL::double precision,
  p_comment_search        text             DEFAULT NULL::text,
  p_comment_search_scope  text             DEFAULT NULL::text,
  p_comment_user_id       uuid             DEFAULT NULL::uuid,
  p_include_unpublished   boolean          DEFAULT false,
  p_line                  text             DEFAULT NULL::text,
  p_line_snr_min          double precision DEFAULT NULL::double precision,
  p_line_snr_max          double precision DEFAULT NULL::double precision,
  p_line_include_stale    boolean          DEFAULT false,
  p_band                  text             DEFAULT NULL::text,
  p_band_mag_min          double precision DEFAULT NULL::double precision,
  p_band_mag_max          double precision DEFAULT NULL::double precision,
  p_band_snr_min          double precision DEFAULT NULL::double precision,
  p_band_snr_max          double precision DEFAULT NULL::double precision,
  p_after_object_id       text             DEFAULT NULL::text,
  p_page_size             integer          DEFAULT 5000
)
  RETURNS TABLE (
    object_id           text,
    field               text,
    ra                  double precision,
    "dec"               double precision,
    redshift            numeric,
    redshift_quality    integer,
    redshift_inspected  numeric,
    redshift_auto       double precision,
    last_inspected_at   timestamp with time zone,
    last_inspected_by   text,
    last_data_change_at timestamp with time zone,
    staleness_reason    text,
    needs_review        boolean,
    version             integer,
    n_targets           integer,
    n_spectra           integer,
    programs            text,
    gratings            text,
    max_snr             double precision,
    max_exposure_time   double precision,
    member_target_ids   text,
    distance            double precision,
    lists               text,
    has_photometry      boolean,
    photo_z             double precision,
    photo_z_err_lo      double precision,
    photo_z_err_hi      double precision,
    photometry          jsonb,
    line_snr            double precision,
    band_mag            double precision,
    band_snr            double precision
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET jit TO 'off'
  SET statement_timeout TO '120s'
  AS $function$
DECLARE
  v_line_object_ids INTEGER[];
  v_band_object_ids INTEGER[];
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_grating_object_ids INTEGER[];
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_page_size INTEGER;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN v_list_ids_mode := 'any'; END IF;
  v_page_size := LEAST(GREATEST(COALESCE(p_page_size, 5000), 1), 10000);

  -- Emission-line filter: the viewer-visible object set with a measurement of
  -- p_line in the S/N bounds, materialized once per call (see
  -- objects_matching_line_filter for the invariants).
  IF p_line IS NOT NULL THEN
    v_line_object_ids := ARRAY(SELECT public.objects_matching_line_filter(
      p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false),
      p_program_slugs, p_include_unpublished));
  END IF;

  -- Photometry band filter: the objects whose photometry in p_band lies in the
  -- magnitude / S/N window, materialized once per call the same way (see
  -- objects_matching_band_filter for the invariants). Object-level even in the
  -- spectra RPCs — photometry belongs to the sky position, not the spectrum —
  -- so a spectrum matches iff its target's parent object does.
  IF p_band IS NOT NULL THEN
    v_band_object_ids := ARRAY(SELECT public.objects_matching_band_filter(
      p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max,
      p_program_slugs, p_include_unpublished));
  END IF;

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;


  -- Issue #488: materialize the viewer-visible grating match set ONCE per call
  -- (statements below consume it via hashed = ANY; an IN-subplan would be
  -- rebuilt per statement — twice in the count+page RPC). See
  -- objects_matching_grating_filter().
  --
  -- Deliberately scoped to p_program_slugs, NOT v_filtered_program_slugs,
  -- matching get_filtered_objects_paginated's filter scoping so this export
  -- returns exactly the row set the catalog table shows for identical filter
  -- args. Narrowing the filter to v_filtered_program_slugs would make exports
  -- silently drop rows the table displays. The asymmetry a reader may notice
  -- — this RPC's exported aggregate columns (sa.*) ARE narrowed by
  -- p_filter_programs while the table's are not — predates the grating
  -- filter and is a display-scoping question, not a filter one.
  IF v_grating_filter_active THEN
    v_grating_object_ids := ARRAY(SELECT public.objects_matching_grating_filter(p_gratings, v_gratings_mode, p_program_slugs, p_include_unpublished));
  END IF;

  RETURN QUERY
  -- Issue #490: page-first evaluation. The previous shape ran the per-row
  -- laterals (object_scoped_aggregates, photometry) for every row that passed
  -- the cheap filters — before the LIMIT — and the member_targets /
  -- visible_lists CTEs aggregated over the whole catalog, so per-page cost
  -- scaled with catalog size instead of page size. Select the page of ids
  -- first (cheap objects-only filters + keyset + LIMIT), then join the
  -- expensive work against just that page. MATERIALIZED fences the page so
  -- the planner can't push the laterals back under the LIMIT.
  WITH page_objects AS MATERIALIZED (
    SELECT o.id, o.object_id, o.field, o.ra, o.dec,
      o.redshift, o.redshift_quality,
      o.redshift_inspected, o.redshift_auto,
      o.last_inspected_at, o.last_inspected_by,
      o.last_data_change_at, o.staleness_reason, o.needs_review, o.version,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) * POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      o.has_photometry, o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi
    FROM objects o
    WHERE o.programs && v_filtered_program_slugs
      AND (p_after_object_id IS NULL OR o.object_id > p_after_object_id)
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        -- Issue #488: the o.gratings array tests are index-backed pre-filters
        -- only (deploy-time aggregate over ALL member spectra, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_grating_object_ids — see
        -- objects_matching_grating_filter() for the invariants.
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'none' AND (NOT o.gratings && p_gratings
            OR NOT (o.id = ANY(v_grating_object_ids))))
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_line IS NULL OR o.id = ANY(v_line_object_ids))
      AND (p_band IS NULL OR o.id = ANY(v_band_object_ids))
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND o.redshift_quality > 0) OR (p_inspected_only = FALSE AND o.redshift_quality = 0))
      AND (p_needs_review IS NULL
        -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
        OR o.needs_review = p_needs_review)
      AND (NOT v_coord_search_active OR (
        -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
        -- every RA, so the RA bound is dropped there (the box is only the index
        -- pre-filter; the Haversine cut below decides membership either way).
        (ABS(p_coord_dec) + p_radius_degrees >= 90
         OR o.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                    AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
        AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        -- Exact haversine cut lives inside the page selection (it used to be a
        -- post-CTE distance_filtered pass) so the LIMIT counts only surviving
        -- rows and the keyset cursor stays correct.
        AND 2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) * POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)))) <= p_radius_degrees
      ))
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND o.id IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = o.id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND o.id NOT IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
      AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin; see get_filtered_objects_paginated for rationale.
        OR o.id IN (
          SELECT c.object_id FROM comments c
          WHERE c.object_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
          UNION
          SELECT t.object_id FROM comments c
          JOIN targets t ON t.id = c.target_id
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
    ORDER BY o.object_id ASC
    LIMIT v_page_size
  ),
  -- Both aggregation CTEs are restricted to the page's ids — previously they
  -- aggregated targets / list memberships for the entire catalog on every page.
  member_targets AS (
    SELECT t.object_id, string_agg(t.target_id, ';' ORDER BY t.target_id) AS member_target_ids
    FROM targets t
    WHERE t.object_id IN (SELECT po.id FROM page_objects po)
      AND t.program_slug = ANY(v_filtered_program_slugs)
    GROUP BY t.object_id
  ),
  visible_lists AS (
    SELECT olm.object_id, string_agg(ol.slug, ';' ORDER BY ol.slug) AS lists
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE olm.object_id IN (SELECT po.id FROM page_objects po)
      AND (ol.created_by = auth.uid() OR ol.visibility IN ('public_read', 'public_edit')
           OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = auth.uid()))
    GROUP BY olm.object_id
  )
  SELECT po.object_id, po.field, po.ra, po.dec,
    po.redshift, po.redshift_quality,
    po.redshift_inspected, po.redshift_auto,
    po.last_inspected_at, up.full_name AS last_inspected_by,
    po.last_data_change_at, po.staleness_reason, po.needs_review, po.version,
    -- Aggregates scoped to accessible (+ filtered) programs so mixed-program
    -- objects don't export proprietary member metadata. See
    -- object_scoped_aggregates().
    sa.n_targets, sa.n_spectra,
    array_to_string(sa.programs, ';') AS programs,
    array_to_string(sa.gratings, ';') AS gratings,
    sa.max_snr, sa.max_exposure_time,
    mt.member_target_ids, po.distance, vl.lists,
    po.has_photometry, po.photo_z, po.photo_z_err_lo, po.photo_z_err_hi,
    phot.photometry,
    CASE WHEN p_line IS NOT NULL THEN public.object_line_snr(po.id, p_line, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) END AS line_snr,
    bv.mag AS band_mag,
    bv.snr AS band_snr
  FROM page_objects po
  -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
  LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = po.id
  LEFT JOIN member_targets mt ON mt.object_id = po.id
  LEFT JOIN visible_lists vl ON vl.object_id = po.id
  LEFT JOIN user_profiles up ON up.user_id = po.last_inspected_by
  LEFT JOIN LATERAL public.object_scoped_aggregates(po.id, v_filtered_program_slugs, p_include_unpublished) sa ON true
  LEFT JOIN LATERAL (
    SELECT op.photometry FROM object_photometry op
    WHERE op.object_id = po.id ORDER BY op.updated_at DESC LIMIT 1
  ) phot ON true
  ORDER BY po.object_id ASC;
END;
$function$;

REVOKE ALL
  ON FUNCTION
    "public"."get_csv_export_objects"(text[], text[], text[], text[], text, integer[], double precision, double precision, double precision, double precision, double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text, text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, text, integer)
  FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.get_csv_export_spectra (
  p_program_slugs         text[],
  p_filter_programs       text[]           DEFAULT NULL::text[],
  p_fields                text[]           DEFAULT NULL::text[],
  p_gratings              text[]           DEFAULT NULL::text[],
  p_gratings_mode         text             DEFAULT 'any'::text,
  p_observations          text[]           DEFAULT NULL::text[],
  p_redshift_quality      integer[]        DEFAULT NULL::integer[],
  p_redshift_min          double precision DEFAULT NULL::double precision,
  p_redshift_max          double precision DEFAULT NULL::double precision,
  p_max_snr_min           double precision DEFAULT NULL::double precision,
  p_max_snr_max           double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max double precision DEFAULT NULL::double precision,
  p_dq_flags_include_any  integer          DEFAULT NULL::integer,
  p_dq_flags_include_all  integer          DEFAULT NULL::integer,
  p_dq_flags_exclude      integer          DEFAULT NULL::integer,
  p_list_ids              integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode         text             DEFAULT 'any'::text,
  p_search                text             DEFAULT NULL::text,
  p_inspected_only        boolean          DEFAULT NULL::boolean,
  p_needs_review          boolean          DEFAULT NULL::boolean,
  p_has_photometry        boolean          DEFAULT NULL::boolean,
  p_comment_search        text             DEFAULT NULL::text,
  p_comment_search_scope  text             DEFAULT NULL::text,
  p_comment_user_id       uuid             DEFAULT NULL::uuid,
  p_coord_ra              double precision DEFAULT NULL::double precision,
  p_coord_dec             double precision DEFAULT NULL::double precision,
  p_radius_degrees        double precision DEFAULT NULL::double precision,
  p_include_unpublished   boolean          DEFAULT false,
  p_line                  text             DEFAULT NULL::text,
  p_line_snr_min          double precision DEFAULT NULL::double precision,
  p_line_snr_max          double precision DEFAULT NULL::double precision,
  p_line_include_stale    boolean          DEFAULT false,
  p_band                  text             DEFAULT NULL::text,
  p_band_mag_min          double precision DEFAULT NULL::double precision,
  p_band_mag_max          double precision DEFAULT NULL::double precision,
  p_band_snr_min          double precision DEFAULT NULL::double precision,
  p_band_snr_max          double precision DEFAULT NULL::double precision,
  p_after_id              integer          DEFAULT NULL::integer,
  p_page_size             integer          DEFAULT 5000
)
  RETURNS TABLE (
    id                integer,
    spectrum_id       text,
    target_id         text,
    grating           text,
    field             text,
    observation       text,
    ra                double precision,
    "dec"             double precision,
    redshift          numeric,
    redshift_quality  integer,
    redshift_auto     double precision,
    signal_to_noise   double precision,
    exposure_time     double precision,
    fits_path         text,
    program_slug      text,
    program_name      text,
    last_inspected_at timestamp with time zone,
    last_inspected_by text,
    distance          double precision,
    dq_flags          integer,
    lists             text,
    line_snr          double precision,
    band_mag          double precision,
    band_snr          double precision
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET statement_timeout TO '120s'
  AS $function$
DECLARE
  v_line_spectrum_ids INTEGER[];
  v_band_object_ids INTEGER[];
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_page_size INTEGER;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (p_comment_search IS NOT NULL AND p_comment_search != '' AND p_comment_search_scope IN ('just_me', 'everyone'));
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN v_list_ids_mode := 'any'; END IF;
  v_page_size := LEAST(GREATEST(COALESCE(p_page_size, 5000), 1), 10000);
  -- Emission-line filter: the spectra with a measurement of p_line in the S/N
  -- bounds, materialized once per call (see spectra_matching_line_filter).
  IF p_line IS NOT NULL THEN
    v_line_spectrum_ids := ARRAY(SELECT public.spectra_matching_line_filter(
      p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false)));
  END IF;

  -- Photometry band filter: the objects whose photometry in p_band lies in the
  -- magnitude / S/N window, materialized once per call the same way (see
  -- objects_matching_band_filter for the invariants). Object-level even in the
  -- spectra RPCs — photometry belongs to the sky position, not the spectrum —
  -- so a spectrum matches iff its target's parent object does.
  IF p_band IS NOT NULL THEN
    v_band_object_ids := ARRAY(SELECT public.objects_matching_band_filter(
      p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max,
      p_program_slugs, p_include_unpublished));
  END IF;

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;

  RETURN QUERY
  WITH visible_lists AS (
    SELECT olm.object_id, string_agg(ol.slug, ';' ORDER BY ol.slug) AS lists
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE ol.created_by = auth.uid() OR ol.visibility IN ('public_read', 'public_edit')
       OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = auth.uid())
    GROUP BY olm.object_id
  ),
  filtered_spectra AS (
    SELECT s.id, s.spectrum_id, t.target_id, s.grating, t.field, t.ra, t.dec,
      o.redshift, o.redshift_quality,
      s.redshift_auto,
      s.signal_to_noise, s.exposure_time, s.fits_path, t.program_slug, t.observation,
      o.last_inspected_at, o.last_inspected_by,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) * POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      COALESCE(s.dq_flags, 0) AS dq_flags,
      vl.lists,
      CASE WHEN p_line IS NOT NULL THEN
        (SELECT __l.snr FROM public.spectrum_lines __l WHERE __l.spectrum_id = s.id AND __l.line = p_line)
      END AS line_snr,
      bv.mag AS band_mag,
      bv.snr AS band_snr
    FROM targets t
    JOIN spectra s ON s.target_id = t.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    LEFT JOIN visible_lists vl ON vl.object_id = t.object_id
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = t.object_id
    WHERE t.program_slug = ANY(v_filtered_program_slugs)
      AND (p_after_id IS NULL OR s.id > p_after_id)
      AND (o.id IS NULL OR o.is_active = true)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      -- B1: hide unpublished spectra (fail-closed; admin opt-in only).
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min) AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min) AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min) AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
      AND (p_line IS NULL OR s.id = ANY(v_line_spectrum_ids))
      AND (p_band IS NULL OR t.object_id = ANY(v_band_object_ids))
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND t.object_id IN (
            SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = t.object_id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND (t.object_id IS NULL OR t.object_id NOT IN (
            SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        )))
      )
      AND (p_search IS NULL OR s.id IN (SELECT __s.id FROM public.spectra __s WHERE __s.search_text ILIKE '%' || p_search || '%'))
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND o.redshift_quality > 0) OR (p_inspected_only = FALSE AND COALESCE(o.redshift_quality, 0) = 0))
      AND (p_needs_review IS NULL
        -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
        OR o.needs_review = p_needs_review)
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (NOT v_comment_search_active OR t.id IN (
        SELECT c.target_id FROM comments c WHERE c.target_id IS NOT NULL AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))))
      AND (NOT v_coord_search_active OR (
        -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
        -- every RA, so the RA bound is dropped there (the box is only the index
        -- pre-filter; the Haversine cut below decides membership either way).
        (ABS(p_coord_dec) + p_radius_degrees >= 90
         OR t.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                    AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)))
  ),
  distance_filtered AS (SELECT fs.* FROM filtered_spectra fs WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees)
  SELECT df.id, df.spectrum_id, df.target_id, df.grating, df.field, df.observation,
    df.ra, df.dec, df.redshift, df.redshift_quality, df.redshift_auto,
    df.signal_to_noise, df.exposure_time, df.fits_path, df.program_slug,
    pr.program_name, df.last_inspected_at, up.full_name AS last_inspected_by,
    df.distance, df.dq_flags, df.lists, df.line_snr, df.band_mag, df.band_snr
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  LEFT JOIN user_profiles up ON up.user_id = df.last_inspected_by
  ORDER BY df.id ASC
  LIMIT v_page_size;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_filtered_object_ids (
  p_program_slugs         text[],
  p_filter_programs       text[]           DEFAULT NULL::text[],
  p_fields                text[]           DEFAULT NULL::text[],
  p_gratings              text[]           DEFAULT NULL::text[],
  p_gratings_mode         text             DEFAULT 'any'::text,
  p_observations          text[]           DEFAULT NULL::text[],
  p_redshift_quality      integer[]        DEFAULT NULL::integer[],
  p_redshift_min          double precision DEFAULT NULL::double precision,
  p_redshift_max          double precision DEFAULT NULL::double precision,
  p_max_snr_min           double precision DEFAULT NULL::double precision,
  p_max_snr_max           double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max double precision DEFAULT NULL::double precision,
  p_search                text             DEFAULT NULL::text,
  p_inspected_only        boolean          DEFAULT NULL::boolean,
  p_needs_review          boolean          DEFAULT NULL::boolean,
  p_list_ids              integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode         text             DEFAULT 'any'::text,
  p_coord_ra              double precision DEFAULT NULL::double precision,
  p_coord_dec             double precision DEFAULT NULL::double precision,
  p_radius_degrees        double precision DEFAULT NULL::double precision,
  p_has_photometry        boolean          DEFAULT NULL::boolean,
  p_photo_z_min           double precision DEFAULT NULL::double precision,
  p_photo_z_max           double precision DEFAULT NULL::double precision,
  p_comment_search        text             DEFAULT NULL::text,
  p_comment_search_scope  text             DEFAULT NULL::text,
  p_comment_user_id       uuid             DEFAULT NULL::uuid,
  p_sort_column           text             DEFAULT 'object_id'::text,
  p_sort_direction        text             DEFAULT 'asc'::text,
  p_include_unpublished   boolean          DEFAULT false,
  p_line                  text             DEFAULT NULL::text,
  p_line_snr_min          double precision DEFAULT NULL::double precision,
  p_line_snr_max          double precision DEFAULT NULL::double precision,
  p_line_include_stale    boolean          DEFAULT false,
  p_band                  text             DEFAULT NULL::text,
  p_band_mag_min          double precision DEFAULT NULL::double precision,
  p_band_mag_max          double precision DEFAULT NULL::double precision,
  p_band_snr_min          double precision DEFAULT NULL::double precision,
  p_band_snr_max          double precision DEFAULT NULL::double precision
)
  RETURNS TABLE (
    object_id text
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  AS $function$
DECLARE
  v_line_object_ids INTEGER[];
  v_band_object_ids INTEGER[];
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_grating_object_ids INTEGER[];
  v_observation_filter_active BOOLEAN;
  v_observation_object_ids INTEGER[];
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN
    v_gratings_mode := 'any';
  END IF;
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN
    v_list_ids_mode := 'any';
  END IF;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)
    OR (p_sort_column = 'line_snr' AND p_line IS NOT NULL)
    OR (p_sort_column IN ('band_mag', 'band_snr') AND p_band IS NOT NULL)) THEN
    p_sort_column := 'object_id';
  END IF;

  -- Intersect user-accessible programs with filter selection
  -- Emission-line filter: the viewer-visible object set with a measurement of
  -- p_line in the S/N bounds, materialized once per call (see
  -- objects_matching_line_filter for the invariants).
  IF p_line IS NOT NULL THEN
    v_line_object_ids := ARRAY(SELECT public.objects_matching_line_filter(
      p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false),
      p_program_slugs, p_include_unpublished));
  END IF;

  -- Photometry band filter: the objects whose photometry in p_band lies in the
  -- magnitude / S/N window, materialized once per call the same way (see
  -- objects_matching_band_filter for the invariants). Object-level even in the
  -- spectra RPCs — photometry belongs to the sky position, not the spectrum —
  -- so a spectrum matches iff its target's parent object does.
  IF p_band IS NOT NULL THEN
    v_band_object_ids := ARRAY(SELECT public.objects_matching_band_filter(
      p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max,
      p_program_slugs, p_include_unpublished));
  END IF;

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_slugs)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;

  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN;
  END IF;

  -- Issue #488: materialize the viewer-visible grating match set ONCE per call
  -- (statements below consume it via hashed = ANY; an IN-subplan would be
  -- rebuilt per statement — twice in the count+page RPC). See
  -- objects_matching_grating_filter().
  IF v_grating_filter_active THEN
    v_grating_object_ids := ARRAY(SELECT public.objects_matching_grating_filter(p_gratings, v_gratings_mode, p_program_slugs, p_include_unpublished));
  END IF;

  -- Issue #491: same once-per-call materialization for the viewer-visible
  -- observation match set. Scoped to the full accessible p_program_slugs (not
  -- the p_filter_programs-narrowed set) to stay consistent with what rows
  -- display — see objects_matching_observation_filter().
  -- COALESCE: array_length('{}',1) is NULL, and a NULL flag would make the
  -- NOT-flag predicate below reject every row instead of treating an empty
  -- selection as no filter (the pre-#491 predicate's explicit behavior).
  v_observation_filter_active := (p_observations IS NOT NULL AND COALESCE(array_length(p_observations, 1), 0) > 0);
  IF v_observation_filter_active THEN
    v_observation_object_ids := ARRAY(SELECT public.objects_matching_observation_filter(p_observations, p_program_slugs, p_include_unpublished));
  END IF;

  RETURN QUERY
  SELECT o.object_id
  FROM objects o
  -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
  LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = o.id
  WHERE
    o.programs && v_filtered_program_slugs
    AND o.is_active = true
    AND (p_include_unpublished OR o.has_published_spectrum)
    AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
    AND (
      NOT v_grating_filter_active
      -- Issue #488: the o.gratings array tests are index-backed pre-filters
      -- only (deploy-time aggregate over ALL member spectra, unpublished and
      -- inaccessible programs included); the viewer-visible decision is the
      -- hashed = ANY over the once-per-call v_grating_object_ids — see
      -- objects_matching_grating_filter() for the invariants.
      OR (v_gratings_mode = 'any' AND o.gratings && p_gratings
          AND o.id = ANY(v_grating_object_ids))
      OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings
          AND o.id = ANY(v_grating_object_ids))
      OR (v_gratings_mode = 'none' AND (NOT o.gratings && p_gratings
          OR NOT (o.id = ANY(v_grating_object_ids))))
    )
    AND (
      NOT v_observation_filter_active
      -- Issue #491: the o.observations && test is an index-backed pre-filter
      -- only (deploy-time aggregate over ALL member targets, unpublished and
      -- inaccessible programs included); the viewer-visible decision is the
      -- hashed = ANY over the once-per-call v_observation_object_ids — see
      -- objects_matching_observation_filter() for the invariants.
      OR (o.observations && p_observations
          AND o.id = ANY(v_observation_object_ids))
    )
    AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
    AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
    AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
    AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
    AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
    AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
    AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
    AND (p_line IS NULL OR o.id = ANY(v_line_object_ids))
    AND (p_band IS NULL OR o.id = ANY(v_band_object_ids))
    AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
    AND (
      p_inspected_only IS NULL
      OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
      OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
    )
    AND (
      p_needs_review IS NULL
        -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
        OR o.needs_review = p_needs_review
    )
    AND (
      NOT v_coord_search_active
      OR (
        -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
        -- every RA, so the RA bound is dropped there (the box is only the index
        -- pre-filter; the Haversine cut below decides membership either way).
        (ABS(p_coord_dec) + p_radius_degrees >= 90
         OR o.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                    AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
        AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        AND 2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
        ))) <= p_radius_degrees
      )
    )
    AND (
      NOT v_list_filter_active
      OR (v_list_ids_mode = 'any' AND o.id IN (
          SELECT olm.object_id FROM object_list_members olm
          WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
      OR (v_list_ids_mode = 'all' AND (
          SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
          WHERE olm.object_id = o.id AND olm.list_id = ANY(p_list_ids)
      ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
      OR (v_list_ids_mode = 'none' AND o.id NOT IN (
          SELECT olm.object_id FROM object_list_members olm
          WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
    )
    AND (
      NOT v_comment_search_active
      -- Uncorrelated semijoin; see get_filtered_objects_paginated for rationale.
      OR o.id IN (
        SELECT c.object_id FROM comments c
        WHERE c.object_id IS NOT NULL
          AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (
            p_comment_search_scope = 'everyone'
            OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
          )
        UNION
        SELECT t.object_id FROM comments c
        JOIN targets t ON t.id = c.target_id
        WHERE c.target_id IS NOT NULL
          AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (
            p_comment_search_scope = 'everyone'
            OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
          )
      )
    )
  ORDER BY
    CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN
      2 * DEGREES(ASIN(SQRT(
        POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
        COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
        POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
      ))) END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN
      2 * DEGREES(ASIN(SQRT(
        POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
        COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
        POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
      ))) END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN o.object_id END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN o.object_id END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN o.field END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN o.field END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN o.ra END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN o.ra END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN o.dec END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN o.dec END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN o.redshift END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN o.redshift END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN o.redshift_quality END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN o.redshift_quality END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'asc' THEN o.n_targets END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'desc' THEN o.n_targets END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'asc' THEN o.n_spectra END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'desc' THEN o.n_spectra END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN o.max_snr END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN o.max_snr END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'line_snr' AND p_sort_direction = 'asc' THEN public.object_line_snr(o.id, p_line, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'line_snr' AND p_sort_direction = 'desc' THEN public.object_line_snr(o.id, p_line, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'band_mag' AND p_sort_direction = 'asc' THEN bv.mag END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'band_mag' AND p_sort_direction = 'desc' THEN bv.mag END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'band_snr' AND p_sort_direction = 'asc' THEN bv.snr END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'band_snr' AND p_sort_direction = 'desc' THEN bv.snr END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN o.max_exposure_time END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN o.max_exposure_time END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'asc' THEN o.photo_z END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'desc' THEN o.photo_z END DESC NULLS LAST,
    o.object_id ASC;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_filtered_objects_paginated (
  p_program_slugs         text[],
  p_filter_programs       text[]           DEFAULT NULL::text[],
  p_fields                text[]           DEFAULT NULL::text[],
  p_gratings              text[]           DEFAULT NULL::text[],
  p_gratings_mode         text             DEFAULT 'any'::text,
  p_observations          text[]           DEFAULT NULL::text[],
  p_redshift_quality      integer[]        DEFAULT NULL::integer[],
  p_redshift_min          double precision DEFAULT NULL::double precision,
  p_redshift_max          double precision DEFAULT NULL::double precision,
  p_max_snr_min           double precision DEFAULT NULL::double precision,
  p_max_snr_max           double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max double precision DEFAULT NULL::double precision,
  p_search                text             DEFAULT NULL::text,
  p_inspected_only        boolean          DEFAULT NULL::boolean,
  p_needs_review          boolean          DEFAULT NULL::boolean,
  p_list_ids              integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode         text             DEFAULT 'any'::text,
  p_coord_ra              double precision DEFAULT NULL::double precision,
  p_coord_dec             double precision DEFAULT NULL::double precision,
  p_radius_degrees        double precision DEFAULT NULL::double precision,
  p_has_photometry        boolean          DEFAULT NULL::boolean,
  p_photo_z_min           double precision DEFAULT NULL::double precision,
  p_photo_z_max           double precision DEFAULT NULL::double precision,
  p_comment_search        text             DEFAULT NULL::text,
  p_comment_search_scope  text             DEFAULT NULL::text,
  p_comment_user_id       uuid             DEFAULT NULL::uuid,
  p_sort_column           text             DEFAULT 'object_id'::text,
  p_sort_direction        text             DEFAULT 'asc'::text,
  p_page                  integer          DEFAULT 1,
  p_page_size             integer          DEFAULT 50,
  p_include_unpublished   boolean          DEFAULT false,
  p_line                  text             DEFAULT NULL::text,
  p_line_snr_min          double precision DEFAULT NULL::double precision,
  p_line_snr_max          double precision DEFAULT NULL::double precision,
  p_line_include_stale    boolean          DEFAULT false,
  p_band                  text             DEFAULT NULL::text,
  p_band_mag_min          double precision DEFAULT NULL::double precision,
  p_band_mag_max          double precision DEFAULT NULL::double precision,
  p_band_snr_min          double precision DEFAULT NULL::double precision,
  p_band_snr_max          double precision DEFAULT NULL::double precision,
  p_include_count         boolean          DEFAULT true,
  p_after_sort_text       text             DEFAULT NULL::text,
  p_after_sort_num        double precision DEFAULT NULL::double precision,
  p_after_tiebreak        text[]           DEFAULT NULL::text[]
)
  RETURNS TABLE (
    targets        jsonb,
    total_count    bigint,
    page           integer,
    page_size      integer,
    has_more       boolean,
    next_sort_text text,
    next_sort_num  double precision,
    next_tiebreak  text[]
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  AS $function$
DECLARE
  v_line_object_ids INTEGER[];
  v_band_object_ids INTEGER[];
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_grating_object_ids INTEGER[];
  v_observation_filter_active BOOLEAN;
  v_observation_object_ids INTEGER[];
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_offset INTEGER;
  v_total_count BIGINT;
  v_sort_is_text BOOLEAN;
  v_keyset_active BOOLEAN;
BEGIN
  p_page := COALESCE(p_page, 1);
  p_page_size := COALESCE(p_page_size, 50);
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN
    v_gratings_mode := 'any';
  END IF;
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN
    v_list_ids_mode := 'any';
  END IF;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)
    OR (p_sort_column = 'line_snr' AND p_line IS NOT NULL)
    OR (p_sort_column IN ('band_mag', 'band_snr') AND p_band IS NOT NULL)) THEN
    p_sort_column := 'object_id';
  END IF;

  IF v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  -- T2-F: cursor slot of the resolved sort column (see the spectra RPC).
  v_sort_is_text := p_sort_column IN ('object_id', 'field');
  v_keyset_active := (p_after_tiebreak IS NOT NULL AND array_length(p_after_tiebreak, 1) = 1);

  -- A cursor page always starts at the cursor, never at an offset.
  v_offset := CASE WHEN v_keyset_active THEN 0 ELSE (p_page - 1) * p_page_size END;

  -- Intersect user-accessible programs with filter selection
  -- Emission-line filter: the viewer-visible object set with a measurement of
  -- p_line in the S/N bounds, materialized once per call (see
  -- objects_matching_line_filter for the invariants).
  IF p_line IS NOT NULL THEN
    v_line_object_ids := ARRAY(SELECT public.objects_matching_line_filter(
      p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false),
      p_program_slugs, p_include_unpublished));
  END IF;

  -- Photometry band filter: the objects whose photometry in p_band lies in the
  -- magnitude / S/N window, materialized once per call the same way (see
  -- objects_matching_band_filter for the invariants). Object-level even in the
  -- spectra RPCs — photometry belongs to the sky position, not the spectrum —
  -- so a spectrum matches iff its target's parent object does.
  IF p_band IS NOT NULL THEN
    v_band_object_ids := ARRAY(SELECT public.objects_matching_band_filter(
      p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max,
      p_program_slugs, p_include_unpublished));
  END IF;

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_slugs)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;

  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size, false, NULL::TEXT, NULL::DOUBLE PRECISION, NULL::TEXT[];
    RETURN;
  END IF;


  -- Issue #488: materialize the viewer-visible grating match set ONCE per call
  -- (statements below consume it via hashed = ANY; an IN-subplan would be
  -- rebuilt per statement — twice in the count+page RPC). See
  -- objects_matching_grating_filter().
  IF v_grating_filter_active THEN
    v_grating_object_ids := ARRAY(SELECT public.objects_matching_grating_filter(p_gratings, v_gratings_mode, p_program_slugs, p_include_unpublished));
  END IF;

  -- Issue #491: same once-per-call materialization for the viewer-visible
  -- observation match set. Scoped to the full accessible p_program_slugs (not
  -- the p_filter_programs-narrowed set) to stay consistent with what rows
  -- display — see objects_matching_observation_filter().
  -- COALESCE: array_length('{}',1) is NULL, and a NULL flag would make the
  -- NOT-flag predicate below reject every row instead of treating an empty
  -- selection as no filter (the pre-#491 predicate's explicit behavior).
  v_observation_filter_active := (p_observations IS NOT NULL AND COALESCE(array_length(p_observations, 1), 0) > 0);
  IF v_observation_filter_active THEN
    v_observation_object_ids := ARRAY(SELECT public.objects_matching_observation_filter(p_observations, p_program_slugs, p_include_unpublished));
  END IF;

  -- Step 1: count — a second full pass over the filter, so only when the
  -- caller doesn't already know the total for this filter set (#501).
  IF p_include_count THEN
    SELECT COUNT(*) INTO v_total_count
    FROM objects o
    WHERE
      -- Access control: object must have at least one accessible program
      o.programs && v_filtered_program_slugs
      AND o.is_active = true
      -- B1: hide objects with no published spectrum (fail-closed).
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        -- Issue #488: the o.gratings array tests are index-backed pre-filters
        -- only (deploy-time aggregate over ALL member spectra, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_grating_object_ids — see
        -- objects_matching_grating_filter() for the invariants.
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'none' AND (NOT o.gratings && p_gratings
            OR NOT (o.id = ANY(v_grating_object_ids))))
      )
      AND (
        NOT v_observation_filter_active
        -- Issue #491: the o.observations && test is an index-backed pre-filter
        -- only (deploy-time aggregate over ALL member targets, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_observation_object_ids — see
        -- objects_matching_observation_filter() for the invariants.
        OR (o.observations && p_observations
            AND o.id = ANY(v_observation_object_ids))
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_line IS NULL OR o.id = ANY(v_line_object_ids))
      AND (p_band IS NULL OR o.id = ANY(v_band_object_ids))
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      AND (
        p_needs_review IS NULL
          -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
          OR o.needs_review = p_needs_review
      )
      AND (
        NOT v_coord_search_active
        OR (
          -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
          -- every RA, so the RA bound is dropped there (the box is only the index
          -- pre-filter; the Haversine cut below decides membership either way).
          (ABS(p_coord_dec) + p_radius_degrees >= 90
           OR o.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                      AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
          AND 2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          ))) <= p_radius_degrees
        )
      )
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND o.id IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = o.id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND o.id NOT IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
      AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin: collect the object_ids that have a matching
        -- comment ONCE (object-level comments directly + target-level comments
        -- mapped through their parent object), then probe o.id IN (...). The old
        -- correlated EXISTS-inside-OR re-ran a per-object targets subquery for
        -- every (object x matching-comment) pair -> 271k subplan executions /
        -- ~870ms here, multi-second on broad terms or cold cache.
        OR o.id IN (
          SELECT c.object_id FROM comments c
          WHERE c.object_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
          UNION
          SELECT t.object_id FROM comments c
          JOIN targets t ON t.id = c.target_id
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      );

  ELSE
    v_total_count := -1;
  END IF;

  -- Step 2: fetch page
  RETURN QUERY
  WITH candidates AS (
    SELECT
      o.id,
      o.object_id,
      o.field,
      o.ra,
      o.dec,
      o.n_targets,
      o.n_spectra,
      o.programs,
      o.gratings,
      o.max_snr,
      o.max_exposure_time,
      o.redshift,
      o.redshift_quality,
      o.redshift_inspected,
      o.redshift_auto,
      o.inspected_used_auto,
      o.last_inspected_at,
      o.last_inspected_by,
      o.last_data_change_at,
      o.staleness_reason,
      o.needs_review,
      o.version,
      o.is_active,
      o.photo_z,
      o.has_photometry,
      o.created_at,
      -- the object's best S/N in the filtered line (sort key + 'Line S/N' column)
      CASE WHEN p_line IS NOT NULL THEN public.object_line_snr(o.id, p_line, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) END AS line_snr,
      -- the object's magnitude and S/N in the filtered band (sort keys + the
      -- 'Mag' / 'Band S/N' columns); the same aggregates the band filter tested
      bv.mag AS band_mag,
      bv.snr AS band_snr,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM objects o
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = o.id
    WHERE
      o.programs && v_filtered_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        -- Issue #488: the o.gratings array tests are index-backed pre-filters
        -- only (deploy-time aggregate over ALL member spectra, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_grating_object_ids — see
        -- objects_matching_grating_filter() for the invariants.
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'none' AND (NOT o.gratings && p_gratings
            OR NOT (o.id = ANY(v_grating_object_ids))))
      )
      AND (
        NOT v_observation_filter_active
        -- Issue #491: the o.observations && test is an index-backed pre-filter
        -- only (deploy-time aggregate over ALL member targets, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_observation_object_ids — see
        -- objects_matching_observation_filter() for the invariants.
        OR (o.observations && p_observations
            AND o.id = ANY(v_observation_object_ids))
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_line IS NULL OR o.id = ANY(v_line_object_ids))
      AND (p_band IS NULL OR o.id = ANY(v_band_object_ids))
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      AND (
        p_needs_review IS NULL
          -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
          OR o.needs_review = p_needs_review
      )
      AND (
        NOT v_coord_search_active
        OR (
          -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
          -- every RA, so the RA bound is dropped there (the box is only the index
          -- pre-filter; the Haversine cut below decides membership either way).
          (ABS(p_coord_dec) + p_radius_degrees >= 90
           OR o.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                      AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
          AND 2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          ))) <= p_radius_degrees
        )
      )
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND o.id IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = o.id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND o.id NOT IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
      AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin; see the count query above for the rationale.
        OR o.id IN (
          SELECT c.object_id FROM comments c
          WHERE c.object_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
          UNION
          SELECT t.object_id FROM comments c
          JOIN targets t ON t.id = c.target_id
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
  ),
  -- T2-F: sort key in one of two typed slots; the ORDER BY, the keyset
  -- predicate and the returned next-cursor all read these (see the spectra
  -- RPC). The CASEs fold to one column at plan time (force_custom_plan).
  keyed AS (
    SELECT c.*,
      CASE p_sort_column
        WHEN 'object_id' THEN c.object_id
        WHEN 'field' THEN c.field
      END AS sort_text,
      CASE p_sort_column
        WHEN 'distance' THEN c.distance::double precision
        WHEN 'ra' THEN c.ra::double precision
        WHEN 'dec' THEN c."dec"::double precision
        WHEN 'redshift' THEN c.redshift::double precision
        WHEN 'redshift_quality' THEN c.redshift_quality::double precision
        WHEN 'n_targets' THEN c.n_targets::double precision
        WHEN 'n_spectra' THEN c.n_spectra::double precision
        WHEN 'max_snr' THEN c.max_snr::double precision
        WHEN 'max_exposure_time' THEN c.max_exposure_time::double precision
        WHEN 'photo_z' THEN c.photo_z::double precision
        WHEN 'line_snr' THEN c.line_snr
        WHEN 'band_mag' THEN c.band_mag
        WHEN 'band_snr' THEN c.band_snr
      END AS sort_num
    FROM candidates c
  ),
  -- One row past the page so has_more is exact; with_members drops it and the
  -- cursor is taken from row p_page_size.
  filtered_objects AS (
    SELECT *, ROW_NUMBER() OVER () AS rn
    FROM (
      SELECT * FROM keyed k
      WHERE
        NOT v_keyset_active
        -- Keyset predicate — same shape as the spectra RPC, tiebreak object_id.
        OR (
          CASE WHEN v_sort_is_text THEN
            (p_after_sort_text IS NOT NULL AND (
                 (p_sort_direction = 'asc'  AND k.sort_text > p_after_sort_text)
              OR (p_sort_direction = 'desc' AND k.sort_text < p_after_sort_text)
              OR k.sort_text IS NULL))
            OR (k.sort_text IS NOT DISTINCT FROM p_after_sort_text AND k.object_id > p_after_tiebreak[1])
          ELSE
            (p_after_sort_num IS NOT NULL AND (
                 (p_sort_direction = 'asc'  AND k.sort_num > p_after_sort_num)
              OR (p_sort_direction = 'desc' AND k.sort_num < p_after_sort_num)
              OR k.sort_num IS NULL))
            OR (k.sort_num IS NOT DISTINCT FROM p_after_sort_num AND k.object_id > p_after_tiebreak[1])
          END
        )
      ORDER BY
        CASE WHEN p_sort_direction = 'asc'  THEN k.sort_text END ASC  NULLS LAST,
        CASE WHEN p_sort_direction = 'desc' THEN k.sort_text END DESC NULLS LAST,
        CASE WHEN p_sort_direction = 'asc'  THEN k.sort_num  END ASC  NULLS LAST,
        CASE WHEN p_sort_direction = 'desc' THEN k.sort_num  END DESC NULLS LAST,
        k.object_id ASC
      LIMIT p_page_size + 1 OFFSET v_offset
    ) sorted_page
  ),
  cursor_row AS (
    SELECT fo.sort_text, fo.sort_num, fo.object_id
    FROM filtered_objects fo
    WHERE fo.rn = p_page_size
      AND EXISTS (SELECT 1 FROM filtered_objects x WHERE x.rn > p_page_size)
  ),
  with_members AS (
    SELECT
      fo.rn,
      jsonb_build_object(
        'id', fo.id,
        'object_id', fo.object_id,
        'field', fo.field,
        'ra', fo.ra,
        'dec', fo.dec,
        -- Aggregates scoped to the viewer's accessible programs so mixed-program
        -- objects don't leak proprietary member metadata. Deliberately NOT
        -- narrowed by p_filter_programs: a program filter selects which objects
        -- appear (overlap test above), but each row still shows the object's
        -- full accessible programs/observations. Filter and sort above run on
        -- the global o.* columns; the substitution happens only on the
        -- paginated result set.
        'n_targets', sa.n_targets,
        'n_spectra', sa.n_spectra,
        'programs', sa.programs,
        'gratings', sa.gratings,
        'max_snr', sa.max_snr,
        'max_exposure_time', sa.max_exposure_time,
        'line_snr', fo.line_snr,
        'band_mag', fo.band_mag,
        'band_snr', fo.band_snr,
        'redshift', fo.redshift,
        'redshift_quality', fo.redshift_quality,
        'redshift_inspected', fo.redshift_inspected,
        'redshift_auto', fo.redshift_auto,
        'inspected_used_auto', fo.inspected_used_auto,
        'last_inspected_at', fo.last_inspected_at,
        'last_inspected_by', fo.last_inspected_by,
        'last_data_change_at', fo.last_data_change_at,
        'staleness_reason', fo.staleness_reason,
        'needs_review', fo.needs_review,
        'version', fo.version,
        'is_active', fo.is_active,
        'photo_z', fo.photo_z,
        'has_photometry', fo.has_photometry,
        'created_at', fo.created_at,
        'distance', fo.distance,
        -- Phase D: member_targets becomes provenance only (target_id, program,
        -- observation). Inspection state lives on the object now; redshift_auto
        -- on targets is retained for transitional UI display until Phase E.
        'member_targets', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'target_id', t.target_id,
              'program_slug', t.program_slug,
              'observation', t.observation,
              'redshift_auto', t.redshift_auto
            )
          )
          FROM targets t
          WHERE t.object_id = fo.id
            AND t.program_slug = ANY(p_program_slugs)
            -- Same publication gate as object_scoped_aggregates: the RPC is
            -- reached via the service-role client (/api/v1/objects), so RLS
            -- won't hide draft-only members here.
            AND (p_include_unpublished OR t.has_published_spectrum)
          ),
          '[]'::jsonb
        ),
        'lists', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'id', ol.id,
              'name', ol.name,
              'slug', ol.slug,
              'icon', ol.icon,
              'color', ol.color
            ) ORDER BY ol.name
          )
          FROM object_list_members olm
          JOIN object_lists ol ON ol.id = olm.list_id
          WHERE olm.object_id = fo.id),
          '[]'::jsonb
        )
      ) AS obj_json
    FROM filtered_objects fo
    LEFT JOIN LATERAL public.object_scoped_aggregates(fo.id, p_program_slugs, p_include_unpublished) sa ON true
    -- Drop the has_more overflow row before the per-row aggregates run.
    WHERE fo.rn <= p_page_size
  )
  SELECT
    COALESCE(jsonb_agg(wm.obj_json ORDER BY wm.rn), '[]'::jsonb),
    v_total_count,
    p_page,
    p_page_size,
    EXISTS (SELECT 1 FROM filtered_objects x WHERE x.rn > p_page_size),
    (SELECT cr.sort_text FROM cursor_row cr),
    (SELECT cr.sort_num FROM cursor_row cr),
    (SELECT ARRAY[cr.object_id] FROM cursor_row cr)
  FROM with_members wm;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_filtered_spectra_paginated (
  p_program_slugs         text[],
  p_filter_programs       text[]           DEFAULT NULL::text[],
  p_fields                text[]           DEFAULT NULL::text[],
  p_gratings              text[]           DEFAULT NULL::text[],
  p_gratings_mode         text             DEFAULT 'any'::text,
  p_observations          text[]           DEFAULT NULL::text[],
  p_redshift_quality      integer[]        DEFAULT NULL::integer[],
  p_redshift_min          double precision DEFAULT NULL::double precision,
  p_redshift_max          double precision DEFAULT NULL::double precision,
  p_max_snr_min           double precision DEFAULT NULL::double precision,
  p_max_snr_max           double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max double precision DEFAULT NULL::double precision,
  p_dq_flags_include_any  integer          DEFAULT NULL::integer,
  p_dq_flags_include_all  integer          DEFAULT NULL::integer,
  p_dq_flags_exclude      integer          DEFAULT NULL::integer,
  p_list_ids              integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode         text             DEFAULT 'any'::text,
  p_search                text             DEFAULT NULL::text,
  p_inspected_only        boolean          DEFAULT NULL::boolean,
  p_needs_review          boolean          DEFAULT NULL::boolean,
  p_has_photometry        boolean          DEFAULT NULL::boolean,
  p_comment_search        text             DEFAULT NULL::text,
  p_comment_search_scope  text             DEFAULT NULL::text,
  p_comment_user_id       uuid             DEFAULT NULL::uuid,
  p_coord_ra              double precision DEFAULT NULL::double precision,
  p_coord_dec             double precision DEFAULT NULL::double precision,
  p_radius_degrees        double precision DEFAULT NULL::double precision,
  p_sort_column           text             DEFAULT 'target_id'::text,
  p_sort_direction        text             DEFAULT 'asc'::text,
  p_page                  integer          DEFAULT 1,
  p_page_size             integer          DEFAULT 50,
  p_include_thumbnails    boolean          DEFAULT false,
  p_include_unpublished   boolean          DEFAULT false,
  p_line                  text             DEFAULT NULL::text,
  p_line_snr_min          double precision DEFAULT NULL::double precision,
  p_line_snr_max          double precision DEFAULT NULL::double precision,
  p_line_include_stale    boolean          DEFAULT false,
  p_band                  text             DEFAULT NULL::text,
  p_band_mag_min          double precision DEFAULT NULL::double precision,
  p_band_mag_max          double precision DEFAULT NULL::double precision,
  p_band_snr_min          double precision DEFAULT NULL::double precision,
  p_band_snr_max          double precision DEFAULT NULL::double precision,
  p_include_count         boolean          DEFAULT true,
  p_after_sort_text       text             DEFAULT NULL::text,
  p_after_sort_num        double precision DEFAULT NULL::double precision,
  p_after_tiebreak        text[]           DEFAULT NULL::text[]
)
  RETURNS TABLE (
    targets        jsonb,
    total_count    bigint,
    page           integer,
    page_size      integer,
    has_more       boolean,
    next_sort_text text,
    next_sort_num  double precision,
    next_tiebreak  text[]
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  AS $function$
DECLARE
  v_line_spectrum_ids INTEGER[];
  v_band_object_ids INTEGER[];
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_offset INTEGER;
  v_sort_is_text BOOLEAN;
  v_keyset_active BOOLEAN;
BEGIN
  p_page := COALESCE(p_page, 1);
  p_page_size := COALESCE(p_page_size, 50);
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);

  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );

  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);

  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN
    v_gratings_mode := 'any';
  END IF;

  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN
    v_list_ids_mode := 'any';
  END IF;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN (
    'target_id', 'spectrum_id', 'field', 'observation', 'program_slug', 'ra', 'dec', 'redshift',
    'redshift_quality', 'redshift_auto', 'signal_to_noise', 'exposure_time', 'grating'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)
    OR (p_sort_column = 'line_snr' AND p_line IS NOT NULL)
    OR (p_sort_column IN ('band_mag', 'band_snr') AND p_band IS NOT NULL)) THEN
    p_sort_column := 'spectrum_id';
  END IF;

  IF v_coord_search_active AND p_sort_column IN ('target_id', 'spectrum_id') AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  -- T2-F: which cursor slot the resolved sort column lives in. Every other
  -- whitelisted column is numeric (double precision, or integer cast to it).
  v_sort_is_text := p_sort_column IN ('target_id', 'spectrum_id', 'field', 'observation', 'program_slug', 'grating');
  v_keyset_active := (p_after_tiebreak IS NOT NULL AND array_length(p_after_tiebreak, 1) = 3);

  -- A cursor page always starts at the cursor, never at an offset.
  v_offset := CASE WHEN v_keyset_active THEN 0 ELSE (p_page - 1) * p_page_size END;

  -- Emission-line filter: the spectra with a measurement of p_line in the S/N
  -- bounds, materialized once per call (see spectra_matching_line_filter).
  IF p_line IS NOT NULL THEN
    v_line_spectrum_ids := ARRAY(SELECT public.spectra_matching_line_filter(
      p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false)));
  END IF;

  -- Photometry band filter: the objects whose photometry in p_band lies in the
  -- magnitude / S/N window, materialized once per call the same way (see
  -- objects_matching_band_filter for the invariants). Object-level even in the
  -- spectra RPCs — photometry belongs to the sky position, not the spectrum —
  -- so a spectrum matches iff its target's parent object does.
  IF p_band IS NOT NULL THEN
    v_band_object_ids := ARRAY(SELECT public.objects_matching_band_filter(
      p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max,
      p_program_slugs, p_include_unpublished));
  END IF;

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_slugs)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;

  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size, false, NULL::TEXT, NULL::DOUBLE PRECISION, NULL::TEXT[];
    RETURN;
  END IF;

  -- filtered_spectra / distance_filtered are NOT MATERIALIZED (T2-F follow-up,
  -- #511): with the count skipped (every cursor page, every web page after the
  -- first) the count subquery folds away and the page is one scan with a
  -- top-N sort, instead of the ~80 k-row filtered set being materialized to
  -- temp (~32 MB) on every page as it was when the CTE was shared with the
  -- count. With the count requested (page 1) the filter runs twice — a scan
  -- each for the count and the page — which costs about what the spill did.
  -- The CTE carries NO thumbnail columns: the two SVGs are ~1.5 kB per row
  -- (103 MB temp spill per render before T1-5 / #501); they are joined onto
  -- the <= p_page_size page rows at the end, only when p_include_thumbnails.
  RETURN QUERY
  WITH filtered_spectra AS NOT MATERIALIZED (
    SELECT
      t.id AS tgt_db_id,
      t.target_id,
      t.program_slug,
      t.field,
      t.observation,
      t.ra,
      t.dec,
      -- Phase D: redshift / redshift_quality / inspected flags now live on the
      -- parent object. LEFT JOIN so spectra whose target has no object FK
      -- (shouldn't happen post-reconcile, but safe) still appear.
      o.redshift,
      o.redshift_quality,
      o.redshift_inspected,
      o.last_inspected_at,
      o.last_inspected_by,
      o.is_active AS object_is_active,
      o.has_photometry AS object_has_photometry,
      o.object_id AS parent_object_id,
      t.max_snr,
      t.max_exposure_time,
      t.created_at,
      t.updated_at,
      s.id AS spectrum_pk,
      s.spectrum_id,
      s.grating,
      s.fits_path,
      s.signal_to_noise,
      s.exposure_time,
      s.redshift_auto,
      COALESCE(s.dq_flags, 0) AS dq_flags,
      s.file_hash,
      s.file_size,
      -- the spectrum's S/N in the filtered line (sort key + 'Line S/N' column)
      CASE WHEN p_line IS NOT NULL THEN
        (SELECT __l.snr FROM public.spectrum_lines __l WHERE __l.spectrum_id = s.id AND __l.line = p_line)
      END AS line_snr,
      -- the parent object's magnitude and S/N in the filtered band (sort keys
      -- + 'Mag' / 'Band S/N' columns). Object-level: photometry belongs to the
      -- sky position, so every grating of one object reports the same pair.
      bv.mag AS band_mag,
      bv.snr AS band_snr,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
            POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM targets t
    JOIN spectra s ON s.target_id = t.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = t.object_id
    WHERE
      t.program_slug = ANY(v_filtered_program_slugs)
      -- Hide spectra whose parent object was soft-deleted.
      AND (o.id IS NULL OR o.is_active = true)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      -- B1: hide unpublished spectra (fail-closed; admin opt-in only).
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
      AND (p_line IS NULL OR s.id = ANY(v_line_spectrum_ids))
      AND (p_band IS NULL OR t.object_id = ANY(v_band_object_ids))
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND t.object_id IN (
            SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = t.object_id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND (t.object_id IS NULL OR t.object_id NOT IN (
            SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        )))
      )
      AND (p_search IS NULL OR s.id IN (SELECT __s.id FROM public.spectra __s WHERE __s.search_text ILIKE '%' || p_search || '%'))
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND COALESCE(o.redshift_quality, 0) = 0)
      )
      AND (
        p_needs_review IS NULL
          -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
          OR o.needs_review = p_needs_review
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin: build the set of matching target_ids ONCE
        -- (trgm/seq scan over the tiny comments table) instead of re-probing
        -- comments per outer row. Correlated EXISTS-inside-OR can't be pulled
        -- up and re-executes per spectrum -> timeouts on broad access. See the
        -- objects path below for the object-level analogue.
        OR t.id IN (
          SELECT c.target_id FROM comments c
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
      AND (
        NOT v_coord_search_active
        OR (
          -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
          -- every RA, so the RA bound is dropped there (the box is only the index
          -- pre-filter; the Haversine cut below decides membership either way).
          (ABS(p_coord_dec) + p_radius_degrees >= 90
           OR t.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                      AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
          AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        )
      )
  ),
  distance_filtered AS NOT MATERIALIZED (
    SELECT fs.*
    FROM filtered_spectra fs
    WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees
  ),
  -- T2-F: one row = one candidate plus its sort key, materialized in exactly
  -- one of two typed slots. The ORDER BY, the keyset predicate and the
  -- returned next-cursor all read these two columns, so they cannot drift
  -- apart. p_sort_column is a constant under force_custom_plan, so each CASE
  -- folds to the single referenced column at plan time.
  keyed AS (
    SELECT df.*,
      CASE p_sort_column
        WHEN 'target_id' THEN df.target_id
        WHEN 'spectrum_id' THEN df.spectrum_id
        WHEN 'field' THEN df.field
        WHEN 'observation' THEN df.observation
        WHEN 'program_slug' THEN df.program_slug
        WHEN 'grating' THEN df.grating
      END AS sort_text,
      CASE p_sort_column
        WHEN 'distance' THEN df.distance::double precision
        WHEN 'ra' THEN df.ra::double precision
        WHEN 'dec' THEN df."dec"::double precision
        WHEN 'redshift' THEN df.redshift::double precision
        WHEN 'redshift_quality' THEN df.redshift_quality::double precision
        WHEN 'redshift_auto' THEN df.redshift_auto::double precision
        WHEN 'signal_to_noise' THEN df.signal_to_noise::double precision
        WHEN 'exposure_time' THEN df.exposure_time::double precision
        WHEN 'line_snr' THEN df.line_snr
        WHEN 'band_mag' THEN df.band_mag
        WHEN 'band_snr' THEN df.band_snr
      END AS sort_num
    FROM distance_filtered df
  ),
  -- One row past the page (p_page_size + 1) so has_more is exact; the final
  -- SELECT drops it and the cursor is taken from row p_page_size.
  page_rows AS (
    SELECT *, ROW_NUMBER() OVER () as row_num
    FROM (
      SELECT * FROM keyed k
      WHERE
        NOT v_keyset_active
        -- Keyset: rows strictly after the cursor in (sort key <dir> NULLS
        -- LAST, target_id, grating, spectrum_id) order. With a non-NULL cursor
        -- value that is every row whose key sorts after it plus the whole NULL
        -- tail; with a NULL cursor value (the walk is inside the tail) only
        -- NULL-keyed rows past the tiebreak. Equal keys fall through to the
        -- row-value tiebreak comparison.
        OR (
          CASE WHEN v_sort_is_text THEN
            (p_after_sort_text IS NOT NULL AND (
                 (p_sort_direction = 'asc'  AND k.sort_text > p_after_sort_text)
              OR (p_sort_direction = 'desc' AND k.sort_text < p_after_sort_text)
              OR k.sort_text IS NULL))
            OR (k.sort_text IS NOT DISTINCT FROM p_after_sort_text
                AND (k.target_id, k.grating, k.spectrum_id) > (p_after_tiebreak[1], p_after_tiebreak[2], p_after_tiebreak[3]))
          ELSE
            (p_after_sort_num IS NOT NULL AND (
                 (p_sort_direction = 'asc'  AND k.sort_num > p_after_sort_num)
              OR (p_sort_direction = 'desc' AND k.sort_num < p_after_sort_num)
              OR k.sort_num IS NULL))
            OR (k.sort_num IS NOT DISTINCT FROM p_after_sort_num
                AND (k.target_id, k.grating, k.spectrum_id) > (p_after_tiebreak[1], p_after_tiebreak[2], p_after_tiebreak[3]))
          END
        )
      -- Exactly one of the four key terms is live for a given call (the other
      -- three are constant NULL); the tail makes the order total. spectrum_id
      -- was added to the tail in T2-F — (target_id, grating) alone is not
      -- unique (one grating can pair with several filters), which a keyset
      -- cursor cannot tolerate.
      ORDER BY
        CASE WHEN p_sort_direction = 'asc'  THEN k.sort_text END ASC  NULLS LAST,
        CASE WHEN p_sort_direction = 'desc' THEN k.sort_text END DESC NULLS LAST,
        CASE WHEN p_sort_direction = 'asc'  THEN k.sort_num  END ASC  NULLS LAST,
        CASE WHEN p_sort_direction = 'desc' THEN k.sort_num  END DESC NULLS LAST,
        k.target_id ASC, k.grating ASC, k.spectrum_id ASC
      LIMIT p_page_size + 1 OFFSET v_offset
    ) sorted_page
  ),
  -- The row the next cursor is built from: the page's last row, and only when
  -- an overflow row proved there is a next page.
  cursor_row AS (
    SELECT pr.sort_text, pr.sort_num, pr.target_id, pr.grating, pr.spectrum_id
    FROM page_rows pr
    WHERE pr.row_num = p_page_size
      AND EXISTS (SELECT 1 FROM page_rows x WHERE x.row_num > p_page_size)
  )
  SELECT
    COALESCE(jsonb_agg(jsonb_build_object(
      'id', r.tgt_db_id,
      'target_id', r.target_id,
      'parent_object_id', r.parent_object_id,
      'program_slug', r.program_slug,
      'program_name', pr.program_name,
      'field', r.field,
      'observation', r.observation,
      'ra', r.ra,
      'dec', r.dec,
      -- Phase D: redshift fields are object-level reads
      'redshift', r.redshift,
      'redshift_inspected', r.redshift_inspected,
      'redshift_quality', r.redshift_quality,
      'last_inspected_at', r.last_inspected_at,
      'last_inspected_by', r.last_inspected_by,
      'max_snr', r.max_snr,
      'max_exposure_time', r.max_exposure_time,
      'line_snr', r.line_snr,
      'band_mag', r.band_mag,
      'band_snr', r.band_snr,
      'created_at', r.created_at,
      'updated_at', r.updated_at,
      'distance', CASE WHEN v_coord_search_active THEN r.distance ELSE NULL END,
      'spectra', jsonb_build_array(jsonb_build_object(
        'id', r.spectrum_pk,
        'spectrum_id', r.spectrum_id,
        'target_id', r.target_id,
        'grating', r.grating,
        'fits_path', r.fits_path,
        'signal_to_noise', r.signal_to_noise,
        'exposure_time', r.exposure_time,
        -- Phase D: per-spectrum auto-z and DQ
        'redshift_auto', r.redshift_auto,
        'dq_flags', r.dq_flags,
        'file_hash', r.file_hash,
        'file_size', r.file_size,
        'thumbnail_svg_fnu', sth.thumbnail_svg_fnu,
        'thumbnail_svg_flambda', sth.thumbnail_svg_flambda
      ))
    ) ORDER BY r.row_num), '[]'::jsonb),
    CASE WHEN p_include_count THEN (SELECT COUNT(*) FROM distance_filtered) ELSE -1::BIGINT END,
    p_page,
    p_page_size,
    EXISTS (SELECT 1 FROM page_rows x WHERE x.row_num > p_page_size),
    (SELECT cr.sort_text FROM cursor_row cr),
    (SELECT cr.sort_num FROM cursor_row cr),
    (SELECT ARRAY[cr.target_id, cr.grating, cr.spectrum_id] FROM cursor_row cr)
  FROM page_rows r
  LEFT JOIN programs pr ON pr.slug = r.program_slug
  -- Thumbnails only for the page, only when asked (constant-false join
  -- condition otherwise, which the planner drops).
  LEFT JOIN spectra sth ON p_include_thumbnails AND sth.id = r.spectrum_pk
  -- Drop the has_more overflow row.
  WHERE r.row_num <= p_page_size;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_objects_for_sync (
  p_program_slugs        text[],
  p_user_id              uuid                     DEFAULT NULL::uuid,
  p_updated_since        timestamp with time zone DEFAULT NULL::timestamp WITH time zone,
  p_limit                integer                  DEFAULT 1000,
  p_include_counts       boolean                  DEFAULT true,
  p_include_unpublished  boolean                  DEFAULT false,
  p_after_object_id      text                     DEFAULT NULL::text,
  p_filter_program_slugs text[]                   DEFAULT NULL::text[]
)
  RETURNS TABLE (
    objects                jsonb,
    total_count            bigint,
    total_accessible_count bigint,
    deleted_ids            integer[]
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET statement_timeout TO '120s'
  AS $function$
BEGIN
  RETURN QUERY
  -- matched is MATERIALIZED so the three aggregate CTEs below each see the
  -- same ~p_limit-row set without re-evaluating the WHERE/ORDER/LIMIT.
  WITH matched AS MATERIALIZED (
    SELECT o.id, o.object_id, o.field, o.ra, o.dec,
           o.n_targets, o.n_spectra, o.programs, o.gratings,
           o.max_snr, o.max_exposure_time,
           o.redshift, o.redshift_quality,
           o.redshift_inspected, o.redshift_auto,
           o.inspected_used_auto,
           o.last_inspected_at, o.last_inspected_by,
           o.last_data_change_at, o.staleness_reason, o.needs_review,
           o.version, o.is_active,
           o.has_photometry, o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi,
           o.created_at, o.updated_at
    FROM objects o
    WHERE o.programs && p_program_slugs
      -- Phase D: hide soft-deleted objects from sync. Reactivation rewrites
      -- updated_at, so re-activated rows get re-synced naturally on next pull.
      AND o.is_active = true
      -- B1: drop objects with no published spectrum (fail-closed).
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
      -- Keyset (#103): seek past the previous page's last object_id. object_id
      -- is UNIQUE, so a strict > needs no (sort_col, id) tiebreaker. Any future
      -- change to this ORDER BY must keep the ordering column UNIQUE (or switch
      -- to a row-value cursor) or keyset will skip/duplicate rows.
      AND (p_after_object_id IS NULL OR o.object_id > p_after_object_id)
      AND (p_filter_program_slugs IS NULL
           OR o.programs && p_filter_program_slugs
           OR o.id IN (
             SELECT olm.object_id
             FROM object_list_members olm
             JOIN object_lists ol ON ol.id = olm.list_id
             WHERE ol.visibility NOT IN ('public_read', 'public_edit')
               AND (ol.created_by = p_user_id
                    OR ol.id IN (SELECT list_id FROM object_list_shares
                                 WHERE user_id = p_user_id))))
    ORDER BY o.object_id
    LIMIT p_limit
  ),
  -- One pass over the page's members (#569 follow-up). Every target of every
  -- page object, flagged with the two scopes the aggregates below need; the
  -- spectra join rides on it. Replaces a per-row LATERAL
  -- object_scoped_aggregates() call (~24 buffer probes per object, ~1.6 s per
  -- 5000-row page on production) plus two separate targets x spectra scans.
  page_members AS MATERIALIZED (
    SELECT t.object_id, t.target_id, t.program_slug,
           -- member_target_ids / spectra payload scope: accessible programs.
           t.program_slug = ANY(p_program_slugs) AS in_programs,
           -- object_scoped_aggregates' recompute scope (its CTE m): accessible
           -- programs AND, B1, a target that contributes a published spectrum.
           (t.program_slug = ANY(p_program_slugs)
            AND (p_include_unpublished OR t.has_published_spectrum)) AS in_scope
    FROM targets t
    WHERE t.object_id IN (SELECT id FROM matched)
  ),
  member_spectra AS MATERIALIZED (
    SELECT pm.object_id, pm.in_programs, pm.in_scope,
           s.id, s.target_id, s.grating, s.signal_to_noise, s.exposure_time,
           s.redshift_auto, s.dq_flags, s.deploy_status,
           (p_include_unpublished OR s.deploy_status = 'published') AS visible
    FROM page_members pm
    JOIN spectra s ON s.target_id = pm.target_id
  ),
  member_targets_agg AS (
    SELECT pm.object_id,
           jsonb_agg(pm.target_id ORDER BY pm.target_id) AS target_ids
    FROM page_members pm
    WHERE pm.in_programs
    GROUP BY pm.object_id
  ),
  -- Phase D: per-spectrum payload (per design doc) so the Python client
  -- can render redshift_auto and dq_flags per grating without a second
  -- round-trip.
  spectra_agg AS (
    SELECT ms.object_id,
           jsonb_agg(jsonb_build_object(
             'id', ms.id,
             'target_id', ms.target_id,
             'grating', ms.grating,
             'signal_to_noise', ms.signal_to_noise,
             'exposure_time', ms.exposure_time,
             'redshift_auto', ms.redshift_auto,
             'dq_flags', ms.dq_flags
           ) ORDER BY ms.target_id, ms.grating) AS spectra
    FROM member_spectra ms
    WHERE ms.in_programs AND ms.visible
    GROUP BY ms.object_id
  ),
  -- Set-based object_scoped_aggregates() for the page: the same fast path and
  -- recompute, per object. Keep in step with that function.
  scope_targets AS (
    SELECT pm.object_id,
           array_agg(DISTINCT pm.program_slug ORDER BY pm.program_slug) AS programs,
           COUNT(*)::integer AS n_targets
    FROM page_members pm
    WHERE pm.in_scope
    GROUP BY pm.object_id
  ),
  scope_spectra AS (
    SELECT ms.object_id,
           -- Fast-path probes, over ALL member spectra (any program): no
           -- non-published spectrum, and a visible count matching stored
           -- n_spectra. This RPC runs RLS-free, so the count probe is
           -- trivially true here, as it is for the helper's RLS-free callers.
           bool_or(ms.deploy_status <> 'published') AS has_unpublished,
           COUNT(*)::integer AS n_all,
           -- Recompute (the helper's CTE sp): visible spectra of in-scope targets.
           array_agg(DISTINCT ms.grating ORDER BY ms.grating)
             FILTER (WHERE ms.in_scope AND ms.visible AND ms.grating IS NOT NULL) AS gratings,
           (COUNT(*) FILTER (WHERE ms.in_scope AND ms.visible))::integer AS n_spectra,
           MAX(ms.signal_to_noise) FILTER (WHERE ms.in_scope AND ms.visible) AS max_snr,
           MAX(ms.exposure_time) FILTER (WHERE ms.in_scope AND ms.visible) AS max_exposure_time
    FROM member_spectra ms
    GROUP BY ms.object_id
  ),
  scoped_aggs AS (
    SELECT m.id AS object_id,
           -- The helper's `stored` branch condition; a NULL (e.g. a NULL
           -- stored n_spectra) falls through to the recompute, as there.
           COALESCE(NOT p_include_unpublished
                    AND m.programs <@ p_program_slugs
                    AND NOT COALESCE(ss.has_unpublished, false)
                    AND m.n_spectra = COALESCE(ss.n_all, 0), false) AS use_stored,
           st.programs AS r_programs, st.n_targets AS r_n_targets,
           ss.gratings AS r_gratings, ss.n_spectra AS r_n_spectra,
           ss.max_snr AS r_max_snr, ss.max_exposure_time AS r_max_exposure_time
    FROM matched m
    LEFT JOIN scope_targets st ON st.object_id = m.id
    LEFT JOIN scope_spectra ss ON ss.object_id = m.id
  ),
  lists_agg AS (
    SELECT olm.object_id,
           jsonb_agg(ol.slug ORDER BY ol.slug) AS list_slugs
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE olm.object_id IN (SELECT id FROM matched)
      AND (ol.created_by = p_user_id
           OR ol.visibility IN ('public_read', 'public_edit')
           OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = p_user_id))
    GROUP BY olm.object_id
  ),
  -- Count CTEs are gated on p_include_counts; when FALSE the planner
  -- collapses them to One-Time Filter: false and skips the scan.
  total AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
  ),
  -- Tombstones: on the first incremental page (p_updated_since set, no
  -- cursor), the ids of in-scope objects that changed since the cursor and are
  -- no longer visible to this caller -- soft-deleted (is_active = false;
  -- apply_object_reconciliation stamps updated_at) or, for non-admins, left without a
  -- published spectrum (recompute_has_published_spectrum stamps updated_at on
  -- the flip). The client deletes them locally, so an incremental sync no
  -- longer leaves ghosts that force the next pull into a full resync. Only the
  -- integer ids travel: nothing about an unpublished object is disclosed, and
  -- an id the client never mirrored deletes nothing.
  deleted AS (
    SELECT COALESCE(array_agg(o.id ORDER BY o.id), '{}'::INTEGER[]) AS ids
    FROM objects o
    WHERE p_updated_since IS NOT NULL
      AND p_after_object_id IS NULL
      AND o.programs && p_program_slugs
      AND o.updated_at > p_updated_since
      AND NOT (o.is_active AND (p_include_unpublished OR o.has_published_spectrum))
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'field', m.field,
        'ra', m.ra,
        'dec', m.dec,
        -- Aggregates scoped to the caller's accessible programs so a sync that
        -- pulls a mixed-program object doesn't leak proprietary member metadata
        -- into the Python catalog. Same semantics as object_scoped_aggregates(),
        -- computed per page in scoped_aggs.
        'n_targets', CASE WHEN sa.use_stored THEN m.n_targets ELSE COALESCE(sa.r_n_targets, 0) END,
        'n_spectra', CASE WHEN sa.use_stored THEN m.n_spectra ELSE COALESCE(sa.r_n_spectra, 0) END,
        'programs', CASE WHEN sa.use_stored THEN m.programs ELSE COALESCE(sa.r_programs, '{}') END,
        'gratings', CASE WHEN sa.use_stored THEN m.gratings ELSE COALESCE(sa.r_gratings, '{}') END,
        'max_snr', CASE WHEN sa.use_stored THEN m.max_snr ELSE sa.r_max_snr END,
        'max_exposure_time', CASE WHEN sa.use_stored THEN m.max_exposure_time ELSE sa.r_max_exposure_time END,
        'redshift', m.redshift,
        'redshift_quality', m.redshift_quality,
        'redshift_inspected', m.redshift_inspected,
        'redshift_auto', m.redshift_auto,
        'inspected_used_auto', m.inspected_used_auto,
        'last_inspected_at', m.last_inspected_at,
        'last_inspected_by', m.last_inspected_by,
        'last_data_change_at', m.last_data_change_at,
        'staleness_reason', m.staleness_reason,
        'needs_review', m.needs_review,
        'version', m.version,
        'is_active', m.is_active,
        'has_photometry', m.has_photometry,
        'photo_z', m.photo_z,
        'photo_z_err_lo', m.photo_z_err_lo,
        'photo_z_err_hi', m.photo_z_err_hi,
        'created_at', m.created_at,
        'updated_at', m.updated_at,
        'member_target_ids', COALESCE(mt.target_ids, '[]'::jsonb),
        'spectra',           COALESCE(sp.spectra,    '[]'::jsonb),
        'lists',             COALESCE(la.list_slugs, '[]'::jsonb)
      )
      -- Keyset (#103): the client uses the LAST element's object_id as the next
      -- page's cursor, so the page array MUST be in object_id order. matched is
      -- ORDER BY object_id, but the LEFT JOINs below can reorder it, so pin the
      -- aggregate order explicitly.
      ORDER BY m.object_id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT,
    (SELECT d.ids FROM deleted d)
  FROM matched m
  LEFT JOIN member_targets_agg mt ON mt.object_id = m.id
  LEFT JOIN spectra_agg         sp ON sp.object_id = m.id
  LEFT JOIN lists_agg           la ON la.object_id = m.id
  JOIN scoped_aggs              sa ON sa.object_id = m.id;
END;
$function$;

GRANT EXECUTE
  ON FUNCTION
    "public"."get_csv_export_objects"(text[], text[], text[], text[], text, integer[], double precision, double precision, double precision, double precision, double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text, text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, text, integer)
  TO "authenticated";

REVOKE ALL
  ON FUNCTION
    "public"."get_csv_export_objects"(text[], text[], text[], text[], text, integer[], double precision, double precision, double precision, double precision, double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text, text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, text, integer)
  FROM "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_csv_export_objects"(text[], text[], text[], text[], text, integer[], double precision, double precision, double precision, double precision, double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text, text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, text, integer)
  TO "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_csv_export_objects"(text[], text[], text[], text[], text, integer[], double precision, double precision, double precision, double precision, double precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    boolean,
    double precision,
    double precision,
    text, text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, text, integer)
  TO "service_role";
