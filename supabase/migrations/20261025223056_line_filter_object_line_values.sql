SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

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
        WHEN 'line_snr' THEN lv.snr
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
    FROM public.cat_objects o
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = o.id
    -- line values ('Line S/N' column + line_snr sort key): one caller-scoped scan per statement, hash-joined; NULLs when no line filter is active (#619)
    LEFT JOIN public.object_line_values(p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) lv ON lv.object_id = o.id
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
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.cat_objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
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
          JOIN public.cat_targets t ON t.id = c.target_id
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
    FROM public.cat_objects o
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
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.cat_objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
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
          JOIN public.cat_targets t ON t.id = c.target_id
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
    FROM public.cat_targets t
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
    lv.snr AS line_snr,
    bv.mag AS band_mag,
    bv.snr AS band_snr
  FROM page_objects po
  -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
  LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = po.id
  -- line values ('Line S/N' column + line_snr sort key): one caller-scoped scan per statement, hash-joined; NULLs when no line filter is active (#619)
  LEFT JOIN public.object_line_values(p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) lv ON lv.object_id = po.id
  LEFT JOIN member_targets mt ON mt.object_id = po.id
  LEFT JOIN visible_lists vl ON vl.object_id = po.id
  LEFT JOIN user_profiles up ON up.user_id = po.last_inspected_by
  LEFT JOIN LATERAL public.object_scoped_aggregates(po.id, v_filtered_program_slugs, p_include_unpublished) sa ON true
  LEFT JOIN LATERAL (
    SELECT op.photometry FROM public.cat_photometry op
    WHERE op.object_id = po.id ORDER BY op.updated_at DESC LIMIT 1
  ) phot ON true
  ORDER BY po.object_id ASC;
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
  FROM public.cat_objects o
  -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
  LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = o.id
  -- line values ('Line S/N' column + line_snr sort key): one caller-scoped scan per statement, hash-joined; NULLs when no line filter is active (#619)
  LEFT JOIN public.object_line_values(p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) lv ON lv.object_id = o.id
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
    AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.cat_objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
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
        JOIN public.cat_targets t ON t.id = c.target_id
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
    CASE WHEN p_sort_column = 'line_snr' AND p_sort_direction = 'asc' THEN lv.snr END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'line_snr' AND p_sort_direction = 'desc' THEN lv.snr END DESC NULLS LAST,
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
  p_after_tiebreak        text[]           DEFAULT NULL::text[],
  p_viewer_id             uuid             DEFAULT NULL::uuid
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
    FROM public.cat_objects o
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
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.cat_objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
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
          JOIN public.cat_targets t ON t.id = c.target_id
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
      -- the object's best S/N in the filtered line (sort key + 'Line S/N'
      -- column): the same aggregate the line filter tested, from the one
      -- object_line_values scan joined below; NULL without a line filter
      lv.snr AS line_snr,
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
    FROM public.cat_objects o
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = o.id
    -- line values ('Line S/N' column + line_snr sort key): one caller-scoped scan per statement, hash-joined; NULLs when no line filter is active (#619)
    LEFT JOIN public.object_line_values(p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) lv ON lv.object_id = o.id
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
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.cat_objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
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
          JOIN public.cat_targets t ON t.id = c.target_id
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
            ) ORDER BY t.target_id
          )
          FROM public.cat_targets t
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
          WHERE olm.object_id = fo.id
            -- Under a user JWT RLS on object_lists is the gate (select_lists:
            -- owner / public / shared, every list for admins). The service-role
            -- client (/api/v1/objects) bypasses RLS, so there the lists are
            -- those p_viewer_id can see: viewable_list_ids() for an explicit
            -- user, as in get_objects_for_sync. Role test as in
            -- object_band_values.
            AND (COALESCE((SELECT auth.role()), '') <> 'service_role'
                 OR ol.created_by = p_viewer_id
                 OR ol.visibility IN ('public_read', 'public_edit')
                 OR ol.id IN (SELECT list_id FROM object_list_shares
                              WHERE user_id = p_viewer_id))),
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

CREATE OR REPLACE FUNCTION public.object_line_snr (
  p_object_id           integer,
  p_line                text,
  p_include_stale       boolean,
  p_program_slugs       text[],
  p_include_unpublished boolean DEFAULT false
)
  RETURNS double precision
  LANGUAGE sql
  STABLE
  AS $function$
  SELECT v.snr
  FROM public.object_line_values(p_line, NULL, NULL, p_include_stale,
                                 p_program_slugs, p_include_unpublished) v
  WHERE v.object_id = p_object_id;
$function$;

CREATE OR REPLACE FUNCTION public.object_line_values (
  p_line                text,
  p_snr_min             double precision,
  p_snr_max             double precision,
  p_include_stale       boolean,
  p_program_slugs       text[],
  p_include_unpublished boolean          DEFAULT false
)
  RETURNS TABLE (
    object_id integer,
    snr       double precision
  )
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  ROWS 2000
  SET search_path TO 'public'
  AS $function$
  SELECT t.object_id, max(l.snr)
  FROM public.cat_spectrum_lines l
  -- unique-key lookups, pinned with LIMIT 1 (see the plan-shape note above)
  CROSS JOIN LATERAL (
    SELECT f.program_slug, f.object_version, f.z_quality, f.z_used
    FROM public.cat_line_fits f WHERE f.spectrum_id = l.spectrum_id LIMIT 1
  ) f
  CROSS JOIN LATERAL (
    SELECT s.target_id, s.program_slug, s.observation, s.deploy_status
    FROM public.cat_spectra s WHERE s.id = l.spectrum_id LIMIT 1
  ) s
  CROSS JOIN LATERAL (
    SELECT t.object_id
    FROM public.cat_targets t WHERE t.target_id = s.target_id LIMIT 1
  ) t
  CROSS JOIN LATERAL (
    SELECT o.version, o.redshift_quality, o.redshift, o.programs,
           o.has_published_spectrum, o.observations
    FROM public.cat_objects o WHERE o.id = t.object_id LIMIT 1
  ) o
  WHERE p_line IS NOT NULL
    AND l.line = p_line
    AND l.snr IS NOT NULL
    -- Gate 1: the caller's access-checked scope (what the RPCs apply themselves).
    AND f.program_slug = ANY(p_program_slugs)
    AND (p_include_unpublished OR s.deploy_status = 'published')
    AND (p_include_stale OR NOT public.line_fit_stale_redshift(
           o.version, o.redshift_quality, (o.redshift)::double precision,
           f.object_version, f.z_quality, f.z_used))
    -- Gate 2a: verbatim select_spectra_by_access (policies.sql) for every
    -- caller except the service role (the JWT role claim, the same test the
    -- deploy-side RPCs use; NOT auth.uid() IS NULL, which an `authenticated`
    -- request without a subject would also satisfy).
    AND (COALESCE((SELECT auth.role()), '') = 'service_role' OR (
      s.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
      AND ((SELECT NOT public.is_link_account())
           OR s.observation = (SELECT public.link_observation()))
      AND (s.deploy_status = 'published' OR (SELECT public.is_admin())
           OR (s.deploy_status = 'draft' AND (SELECT public.link_sees_drafts())))
    ))
    -- Gate 2b: verbatim select_objects_by_access (policies.sql), same callers.
    -- A separate conjunct, not `svc OR (s... AND o...)`: a predicate naming
    -- both s and o would be a join clause between them again.
    AND (COALESCE((SELECT auth.role()), '') = 'service_role' OR (
      o.programs && (SELECT public.accessible_program_slugs())
      AND (o.has_published_spectrum OR (SELECT public.is_admin())
           OR (SELECT public.link_sees_drafts()))
      AND ((SELECT NOT public.is_link_account())
           OR o.observations && ARRAY[(SELECT public.link_observation())]::text[])
      -- (P6b-2) the frozen policies' extra conjuncts when a release is
      -- browsed: no link accounts, and the tag visible to the caller
      AND (public.dr_browse_tag() IS NULL
           OR ((SELECT NOT public.is_link_account())
               AND public.dr_browse_tag() = ANY ((SELECT public.dr_visible_tags())::text[])))
    ))
  GROUP BY t.object_id
  HAVING (p_snr_min IS NULL OR max(l.snr) >= p_snr_min)
     AND (p_snr_max IS NULL OR max(l.snr) <= p_snr_max);
$function$;

REVOKE ALL ON FUNCTION "public"."object_line_values"(text, double precision, double precision, boolean, text[], boolean) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.objects_matching_line_filter (
  p_line                text,
  p_snr_min             double precision,
  p_snr_max             double precision,
  p_include_stale       boolean,
  p_program_slugs       text[],
  p_include_unpublished boolean          DEFAULT false
)
  RETURNS SETOF integer
  LANGUAGE sql
  STABLE
  AS $function$
  SELECT v.object_id
  FROM public.object_line_values(p_line, p_snr_min, p_snr_max, p_include_stale,
                                 p_program_slugs, p_include_unpublished) v;
$function$;

GRANT EXECUTE ON FUNCTION "public"."object_line_values"(text, double precision, double precision, boolean, text[], boolean) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."object_line_values"(text, double precision, double precision, boolean, text[], boolean) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."object_line_values"(text, double precision, double precision, boolean, text[], boolean) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."object_line_values"(text, double precision, double precision, boolean, text[], boolean) TO "service_role";
