SET lock_timeout = '5s';
SET statement_timeout = '60s';
-- Catalog Read Model phase 3c (#660): several Line S/N / Mag columns at once.
-- object_line_columns / object_band_columns return a per-object JSON map of
-- the asked lines / bands, each key with the window that admitted the row
-- (tested on the aggregates the filters test); the eight catalog RPCs take
-- p_line_columns / p_band_columns (JSON maps, display only), carry
-- line_columns / band_columns in their rows, and sort on 'line_snr:<line>' /
-- 'band_mag:<band>' / 'band_snr:<band>'.
SET local check_function_bodies = off;

DROP FUNCTION
  "public"."catalog_query_objects"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, text, double precision, text[], uuid, text, double precision, double
  precision, boolean, text, double precision, double precision, double precision, double precision, double precision, double precision, text, text, uuid);

DROP FUNCTION
  "public"."catalog_query_spectra"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, boolean, text, double precision, text[], text, double precision, double
  precision, boolean, text, double precision, double precision, double precision, double precision, uuid, double precision, double precision, text, text, uuid);

DROP FUNCTION
  "public"."get_adjacent_objects"(text, text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
  precision,
  double precision,
  text,
  boolean,
  boolean,
  integer[],
  text,
  double precision,
  double precision,
  double precision,
  text,
  text,
  boolean,
  double precision,
  double precision,
  text, text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, integer[]);

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
  text,
  text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, integer[], text, integer);

DROP FUNCTION
  "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
  precision,
  double precision,
  integer,
  integer,
  integer,
  integer[],
  text,
  text,
  boolean,
  boolean,
  boolean,
  text,
  text,
  uuid,
  double precision,
  double precision,
  double precision,
  boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, integer[], integer, integer);

DROP FUNCTION
  "public"."get_filtered_object_ids"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
  precision,
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
  text, text, uuid, text, text, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, integer[]);

DROP FUNCTION
  "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
  precision,
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
  text,
  text,
  uuid,
  text,
  text,
  integer,
  integer,
  boolean,
  text,
  double precision,
  double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], uuid, integer[], boolean);

DROP FUNCTION
  "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
  precision,
  double precision,
  integer,
  integer,
  integer,
  integer[],
  text,
  text,
  boolean,
  boolean,
  boolean,
  text,
  text,
  uuid,
  double precision,
  double precision,
  double precision,
  text,
  text,
  integer,
  integer,
  boolean,
  boolean,
  text,
  double precision,
  double precision, boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], integer[], boolean);

CREATE OR REPLACE FUNCTION public.catalog_query_objects (
  p_tree                 jsonb,
  p_program_slugs        text[],
  p_include_unpublished  boolean          DEFAULT false,
  p_search               text             DEFAULT NULL::text,
  p_sort_column          text             DEFAULT 'object_id'::text,
  p_sort_direction       text             DEFAULT 'asc'::text,
  p_page                 integer          DEFAULT 1,
  p_page_size            integer          DEFAULT 50,
  p_include_count        boolean          DEFAULT true,
  p_after_sort_text      text             DEFAULT NULL::text,
  p_after_sort_num       double precision DEFAULT NULL::double precision,
  p_after_tiebreak       text[]           DEFAULT NULL::text[],
  p_viewer_id            uuid             DEFAULT NULL::uuid,
  p_line                 text             DEFAULT NULL::text,
  p_line_snr_min         double precision DEFAULT NULL::double precision,
  p_line_snr_max         double precision DEFAULT NULL::double precision,
  p_line_include_stale   boolean          DEFAULT false,
  p_band                 text             DEFAULT NULL::text,
  p_band_mag_min         double precision DEFAULT NULL::double precision,
  p_band_mag_max         double precision DEFAULT NULL::double precision,
  p_band_snr_min         double precision DEFAULT NULL::double precision,
  p_band_snr_max         double precision DEFAULT NULL::double precision,
  p_line_columns         jsonb            DEFAULT NULL::jsonb,
  p_band_columns         jsonb            DEFAULT NULL::jsonb,
  p_coord_ra             double precision DEFAULT NULL::double precision,
  p_coord_dec            double precision DEFAULT NULL::double precision,
  p_comment_search       text             DEFAULT NULL::text,
  p_comment_search_scope text             DEFAULT NULL::text,
  p_comment_user_id      uuid             DEFAULT NULL::uuid
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
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_ids integer[];
BEGIN
  IF p_sort_column = 'distance' AND (p_coord_ra IS NULL OR p_coord_dec IS NULL) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: sorting by distance needs p_coord_ra / p_coord_dec (the reference position)';
  END IF;
  v_ids := public.catalog_match_ids('objects', p_tree, p_program_slugs, p_include_unpublished, p_viewer_id);
  IF cardinality(v_ids) = 0 THEN
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size, false, NULL::TEXT, NULL::DOUBLE PRECISION, NULL::TEXT[];
    RETURN;
  END IF;
  RETURN QUERY SELECT * FROM public.get_filtered_objects_paginated(
    p_program_slugs := p_program_slugs,
    p_search := p_search,
    p_comment_search := p_comment_search,
    p_comment_search_scope := p_comment_search_scope,
    p_comment_user_id := p_comment_user_id,
    p_sort_column := p_sort_column,
    p_sort_direction := p_sort_direction,
    p_page := p_page,
    p_page_size := p_page_size,
    p_include_unpublished := p_include_unpublished,
    p_line := p_line,
    p_line_snr_min := p_line_snr_min,
    p_line_snr_max := p_line_snr_max,
    p_line_include_stale := p_line_include_stale,
    p_band := p_band,
    p_band_mag_min := p_band_mag_min,
    p_band_mag_max := p_band_mag_max,
    p_band_snr_min := p_band_snr_min,
    p_band_snr_max := p_band_snr_max,
    p_line_columns := p_line_columns,
    p_band_columns := p_band_columns,
    p_include_count := p_include_count,
    p_after_sort_text := p_after_sort_text,
    p_after_sort_num := p_after_sort_num,
    p_after_tiebreak := p_after_tiebreak,
    p_coord_ra := p_coord_ra,
    p_coord_dec := p_coord_dec,
    p_radius_degrees := CASE WHEN p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL THEN 180::double precision END,
    p_viewer_id := p_viewer_id,
    p_object_ids := v_ids,
    p_measurements_display_only := true);
END;
$function$;

REVOKE ALL
  ON FUNCTION
    "public"."catalog_query_objects"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, text, double precision, text[], uuid, text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, double precision, double precision, text, text, uuid)
  FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.catalog_query_spectra (
  p_tree                 jsonb,
  p_program_slugs        text[],
  p_include_unpublished  boolean          DEFAULT false,
  p_search               text             DEFAULT NULL::text,
  p_sort_column          text             DEFAULT 'target_id'::text,
  p_sort_direction       text             DEFAULT 'asc'::text,
  p_page                 integer          DEFAULT 1,
  p_page_size            integer          DEFAULT 50,
  p_include_thumbnails   boolean          DEFAULT false,
  p_include_count        boolean          DEFAULT true,
  p_after_sort_text      text             DEFAULT NULL::text,
  p_after_sort_num       double precision DEFAULT NULL::double precision,
  p_after_tiebreak       text[]           DEFAULT NULL::text[],
  p_line                 text             DEFAULT NULL::text,
  p_line_snr_min         double precision DEFAULT NULL::double precision,
  p_line_snr_max         double precision DEFAULT NULL::double precision,
  p_line_include_stale   boolean          DEFAULT false,
  p_band                 text             DEFAULT NULL::text,
  p_band_mag_min         double precision DEFAULT NULL::double precision,
  p_band_mag_max         double precision DEFAULT NULL::double precision,
  p_band_snr_min         double precision DEFAULT NULL::double precision,
  p_band_snr_max         double precision DEFAULT NULL::double precision,
  p_line_columns         jsonb            DEFAULT NULL::jsonb,
  p_band_columns         jsonb            DEFAULT NULL::jsonb,
  p_viewer_id            uuid             DEFAULT NULL::uuid,
  p_coord_ra             double precision DEFAULT NULL::double precision,
  p_coord_dec            double precision DEFAULT NULL::double precision,
  p_comment_search       text             DEFAULT NULL::text,
  p_comment_search_scope text             DEFAULT NULL::text,
  p_comment_user_id      uuid             DEFAULT NULL::uuid
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
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_ids integer[];
BEGIN
  IF p_sort_column = 'distance' AND (p_coord_ra IS NULL OR p_coord_dec IS NULL) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog_query: sorting by distance needs p_coord_ra / p_coord_dec (the reference position)';
  END IF;
  v_ids := public.catalog_match_ids('spectra', p_tree, p_program_slugs, p_include_unpublished, p_viewer_id);
  IF cardinality(v_ids) = 0 THEN
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size, false, NULL::TEXT, NULL::DOUBLE PRECISION, NULL::TEXT[];
    RETURN;
  END IF;
  RETURN QUERY SELECT * FROM public.get_filtered_spectra_paginated(
    p_program_slugs := p_program_slugs,
    p_search := p_search,
    p_comment_search := p_comment_search,
    p_comment_search_scope := p_comment_search_scope,
    p_comment_user_id := p_comment_user_id,
    p_sort_column := p_sort_column,
    p_sort_direction := p_sort_direction,
    p_page := p_page,
    p_page_size := p_page_size,
    p_include_thumbnails := p_include_thumbnails,
    p_include_unpublished := p_include_unpublished,
    p_line := p_line,
    p_line_snr_min := p_line_snr_min,
    p_line_snr_max := p_line_snr_max,
    p_line_include_stale := p_line_include_stale,
    p_band := p_band,
    p_band_mag_min := p_band_mag_min,
    p_band_mag_max := p_band_mag_max,
    p_band_snr_min := p_band_snr_min,
    p_band_snr_max := p_band_snr_max,
    p_line_columns := p_line_columns,
    p_band_columns := p_band_columns,
    p_include_count := p_include_count,
    p_after_sort_text := p_after_sort_text,
    p_after_sort_num := p_after_sort_num,
    p_after_tiebreak := p_after_tiebreak,
    p_coord_ra := p_coord_ra,
    p_coord_dec := p_coord_dec,
    p_radius_degrees := CASE WHEN p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL THEN 180::double precision END,
    p_spectrum_ids := v_ids,
    p_measurements_display_only := true);
END;
$function$;

REVOKE ALL
  ON FUNCTION
    "public"."catalog_query_spectra"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, boolean, text, double precision, text[], text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, uuid, double precision, double precision, text, text, uuid)
  FROM PUBLIC, "anon";

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
  p_band_snr_max          double precision DEFAULT NULL::double precision,
  p_line_columns          jsonb            DEFAULT NULL::jsonb,
  p_band_columns          jsonb            DEFAULT NULL::jsonb,
  p_object_ids            integer[]        DEFAULT NULL::integer[]
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
    OR (p_sort_column IN ('band_mag', 'band_snr') AND p_band IS NOT NULL)
    OR public.measurement_sort_key(p_sort_column, p_line_columns, p_band_columns) IS NOT NULL) THEN
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
        -- 'line_snr:<line>' / 'band_mag:<band>' / 'band_snr:<band>' (phase 3c)
        ELSE CASE
          WHEN p_sort_column LIKE 'line_snr:%' THEN (lc.line_columns -> split_part(p_sort_column, ':', 2) ->> 'snr')::double precision
          WHEN p_sort_column LIKE 'band_mag:%' THEN (bc.band_columns -> split_part(p_sort_column, ':', 2) ->> 'mag')::double precision
          WHEN p_sort_column LIKE 'band_snr:%' THEN (bc.band_columns -> split_part(p_sort_column, ':', 2) ->> 'snr')::double precision
        END
      END AS sort_num
    FROM public.cat_objects o
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = o.id
    -- line values ('Line S/N' column + line_snr sort key): one caller-scoped scan per statement, hash-joined; NULLs when no line filter is active (#619)
    LEFT JOIN public.object_line_values(p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) lv ON lv.object_id = o.id
    LEFT JOIN public.object_line_columns(p_line_columns, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) lc ON lc.object_id = o.id
    LEFT JOIN public.object_band_columns(p_band_columns, p_program_slugs, p_include_unpublished) bc ON bc.object_id = o.id
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
      AND (p_object_ids IS NULL OR o.id = ANY(p_object_ids))
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

REVOKE ALL
  ON FUNCTION
    "public"."get_adjacent_objects"(text, text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    text,
    text,
    boolean,
    double precision,
    double precision,
    text,
    text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[])
  FROM PUBLIC, "anon";

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
  p_line_columns          jsonb            DEFAULT NULL::jsonb,
  p_band_columns          jsonb            DEFAULT NULL::jsonb,
  p_object_ids            integer[]        DEFAULT NULL::integer[],
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
    band_snr            double precision,
    line_columns        jsonb,
    band_columns        jsonb
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
      AND (p_object_ids IS NULL OR o.id = ANY(p_object_ids))
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
    bv.snr AS band_snr,
    lc.line_columns,
    bc.band_columns
  FROM page_objects po
  -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
  LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = po.id
  -- line values ('Line S/N' column + line_snr sort key): one caller-scoped scan per statement, hash-joined; NULLs when no line filter is active (#619)
  LEFT JOIN public.object_line_values(p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) lv ON lv.object_id = po.id
  LEFT JOIN public.object_line_columns(p_line_columns, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) lc ON lc.object_id = po.id
  LEFT JOIN public.object_band_columns(p_band_columns, p_program_slugs, p_include_unpublished) bc ON bc.object_id = po.id
  LEFT JOIN member_targets mt ON mt.object_id = po.id
  LEFT JOIN visible_lists vl ON vl.object_id = po.id
  LEFT JOIN user_profiles up ON up.user_id = po.last_inspected_by
  LEFT JOIN LATERAL public.object_scoped_aggregates(po.id, v_filtered_program_slugs, p_include_unpublished) sa ON true
  LEFT JOIN LATERAL (
    SELECT public.bands_jsonb_of(op.id, public.dr_browse_tag()) AS photometry FROM public.cat_catalog_matches op
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
    text,
    text,
    uuid,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], text, integer)
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
  p_line_columns          jsonb            DEFAULT NULL::jsonb,
  p_band_columns          jsonb            DEFAULT NULL::jsonb,
  p_spectrum_ids          integer[]        DEFAULT NULL::integer[],
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
    band_snr          double precision,
    line_columns      jsonb,
    band_columns      jsonb
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
        (SELECT __l.snr FROM public.cat_lines __l WHERE __l.spectrum_id = s.id AND __l.line = p_line)
      END AS line_snr,
      bv.mag AS band_mag,
      bv.snr AS band_snr,
      CASE WHEN jsonb_typeof(p_line_columns) = 'object' THEN
        (SELECT jsonb_object_agg(__l.line, jsonb_build_object('snr', __l.snr, 'flux', __l.flux))
         FROM jsonb_each(p_line_columns) __spec(line, w)
         JOIN public.cat_lines __l ON __l.spectrum_id = s.id AND __l.line = __spec.line
         -- the stale gate the object-level columns apply (the fit's stored flag)
         JOIN public.cat_line_fits __f ON __f.spectrum_id = s.id
         WHERE (COALESCE(p_line_include_stale, false) OR NOT __f.stale_redshift)
           AND ((__spec.w ->> 'snr_min') IS NULL OR __l.snr >= (__spec.w ->> 'snr_min')::double precision)
           AND ((__spec.w ->> 'snr_max') IS NULL OR __l.snr <= (__spec.w ->> 'snr_max')::double precision))
      END AS line_columns,
      bc.band_columns
    FROM public.cat_targets t
    JOIN public.cat_spectra s ON s.target_id = t.target_id
    LEFT JOIN public.cat_objects o ON o.id = t.object_id
    LEFT JOIN visible_lists vl ON vl.object_id = t.object_id
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = t.object_id
    LEFT JOIN public.object_band_columns(p_band_columns, p_program_slugs, p_include_unpublished) bc ON bc.object_id = t.object_id
    WHERE t.program_slug = ANY(v_filtered_program_slugs)
      AND (p_after_id IS NULL OR s.id > p_after_id)
      AND (p_spectrum_ids IS NULL OR s.id = ANY(p_spectrum_ids))
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
      AND (p_search IS NULL OR s.id IN (SELECT __s.id FROM public.cat_spectra __s WHERE __s.search_text ILIKE '%' || p_search || '%'))
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
    df.distance, df.dq_flags, df.lists, df.line_snr, df.band_mag, df.band_snr,
    df.line_columns, df.band_columns
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  LEFT JOIN user_profiles up ON up.user_id = df.last_inspected_by
  ORDER BY df.id ASC
  LIMIT v_page_size;
END;
$function$;

REVOKE ALL
  ON FUNCTION
    "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], integer, integer)
  FROM PUBLIC, "anon";

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
  p_band_snr_max          double precision DEFAULT NULL::double precision,
  p_line_columns          jsonb            DEFAULT NULL::jsonb,
  p_band_columns          jsonb            DEFAULT NULL::jsonb,
  p_object_ids            integer[]        DEFAULT NULL::integer[]
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
    OR (p_sort_column IN ('band_mag', 'band_snr') AND p_band IS NOT NULL)
    OR public.measurement_sort_key(p_sort_column, p_line_columns, p_band_columns) IS NOT NULL) THEN
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
  LEFT JOIN public.object_line_columns(p_line_columns, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) lc ON lc.object_id = o.id
  LEFT JOIN public.object_band_columns(p_band_columns, p_program_slugs, p_include_unpublished) bc ON bc.object_id = o.id
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
    AND (p_object_ids IS NULL OR o.id = ANY(p_object_ids))
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
    -- 'line_snr:<line>' / 'band_mag:<band>' / 'band_snr:<band>' (phase 3c)
    CASE WHEN p_sort_column LIKE 'line_snr:%' AND p_sort_direction = 'asc' THEN (lc.line_columns -> split_part(p_sort_column, ':', 2) ->> 'snr')::double precision END ASC NULLS LAST,
    CASE WHEN p_sort_column LIKE 'line_snr:%' AND p_sort_direction = 'desc' THEN (lc.line_columns -> split_part(p_sort_column, ':', 2) ->> 'snr')::double precision END DESC NULLS LAST,
    CASE WHEN p_sort_column LIKE 'band_mag:%' AND p_sort_direction = 'asc' THEN (bc.band_columns -> split_part(p_sort_column, ':', 2) ->> 'mag')::double precision END ASC NULLS LAST,
    CASE WHEN p_sort_column LIKE 'band_mag:%' AND p_sort_direction = 'desc' THEN (bc.band_columns -> split_part(p_sort_column, ':', 2) ->> 'mag')::double precision END DESC NULLS LAST,
    CASE WHEN p_sort_column LIKE 'band_snr:%' AND p_sort_direction = 'asc' THEN (bc.band_columns -> split_part(p_sort_column, ':', 2) ->> 'snr')::double precision END ASC NULLS LAST,
    CASE WHEN p_sort_column LIKE 'band_snr:%' AND p_sort_direction = 'desc' THEN (bc.band_columns -> split_part(p_sort_column, ':', 2) ->> 'snr')::double precision END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN o.max_exposure_time END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN o.max_exposure_time END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'asc' THEN o.photo_z END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'desc' THEN o.photo_z END DESC NULLS LAST,
    o.object_id ASC;
END;
$function$;

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_object_ids"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
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
    text,
    text,
    uuid,
    text, text, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[])
  FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.get_filtered_objects_paginated (
  p_program_slugs             text[],
  p_filter_programs           text[]           DEFAULT NULL::text[],
  p_fields                    text[]           DEFAULT NULL::text[],
  p_gratings                  text[]           DEFAULT NULL::text[],
  p_gratings_mode             text             DEFAULT 'any'::text,
  p_observations              text[]           DEFAULT NULL::text[],
  p_redshift_quality          integer[]        DEFAULT NULL::integer[],
  p_redshift_min              double precision DEFAULT NULL::double precision,
  p_redshift_max              double precision DEFAULT NULL::double precision,
  p_max_snr_min               double precision DEFAULT NULL::double precision,
  p_max_snr_max               double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min     double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max     double precision DEFAULT NULL::double precision,
  p_search                    text             DEFAULT NULL::text,
  p_inspected_only            boolean          DEFAULT NULL::boolean,
  p_needs_review              boolean          DEFAULT NULL::boolean,
  p_list_ids                  integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode             text             DEFAULT 'any'::text,
  p_coord_ra                  double precision DEFAULT NULL::double precision,
  p_coord_dec                 double precision DEFAULT NULL::double precision,
  p_radius_degrees            double precision DEFAULT NULL::double precision,
  p_has_photometry            boolean          DEFAULT NULL::boolean,
  p_photo_z_min               double precision DEFAULT NULL::double precision,
  p_photo_z_max               double precision DEFAULT NULL::double precision,
  p_comment_search            text             DEFAULT NULL::text,
  p_comment_search_scope      text             DEFAULT NULL::text,
  p_comment_user_id           uuid             DEFAULT NULL::uuid,
  p_sort_column               text             DEFAULT 'object_id'::text,
  p_sort_direction            text             DEFAULT 'asc'::text,
  p_page                      integer          DEFAULT 1,
  p_page_size                 integer          DEFAULT 50,
  p_include_unpublished       boolean          DEFAULT false,
  p_line                      text             DEFAULT NULL::text,
  p_line_snr_min              double precision DEFAULT NULL::double precision,
  p_line_snr_max              double precision DEFAULT NULL::double precision,
  p_line_include_stale        boolean          DEFAULT false,
  p_band                      text             DEFAULT NULL::text,
  p_band_mag_min              double precision DEFAULT NULL::double precision,
  p_band_mag_max              double precision DEFAULT NULL::double precision,
  p_band_snr_min              double precision DEFAULT NULL::double precision,
  p_band_snr_max              double precision DEFAULT NULL::double precision,
  p_include_count             boolean          DEFAULT true,
  p_after_sort_text           text             DEFAULT NULL::text,
  p_after_sort_num            double precision DEFAULT NULL::double precision,
  p_after_tiebreak            text[]           DEFAULT NULL::text[],
  p_viewer_id                 uuid             DEFAULT NULL::uuid,
  p_object_ids                integer[]        DEFAULT NULL::integer[],
  p_line_columns              jsonb            DEFAULT NULL::jsonb,
  p_band_columns              jsonb            DEFAULT NULL::jsonb,
  p_measurements_display_only boolean          DEFAULT false
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
    OR (p_sort_column IN ('band_mag', 'band_snr') AND p_band IS NOT NULL)
    OR public.measurement_sort_key(p_sort_column, p_line_columns, p_band_columns) IS NOT NULL) THEN
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
      AND (p_line IS NULL OR p_measurements_display_only OR o.id = ANY(v_line_object_ids))
      AND (p_band IS NULL OR p_measurements_display_only OR o.id = ANY(v_band_object_ids))
      AND (p_object_ids IS NULL OR o.id = ANY(p_object_ids))
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
        NOT v_coord_search_active OR p_measurements_display_only
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
      -- the several-columns maps (phase 3c): one scan each, hash-joined, NULL without a request
      lc.line_columns,
      bc.band_columns,
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
    LEFT JOIN public.object_line_columns(p_line_columns, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) lc ON lc.object_id = o.id
    LEFT JOIN public.object_band_columns(p_band_columns, p_program_slugs, p_include_unpublished) bc ON bc.object_id = o.id
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
      AND (p_line IS NULL OR p_measurements_display_only OR o.id = ANY(v_line_object_ids))
      AND (p_band IS NULL OR p_measurements_display_only OR o.id = ANY(v_band_object_ids))
      AND (p_object_ids IS NULL OR o.id = ANY(p_object_ids))
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
        NOT v_coord_search_active OR p_measurements_display_only
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
        -- 'line_snr:<line>' / 'band_mag:<band>' / 'band_snr:<band>' (phase 3c)
        ELSE CASE
          WHEN p_sort_column LIKE 'line_snr:%' THEN (c.line_columns -> split_part(p_sort_column, ':', 2) ->> 'snr')::double precision
          WHEN p_sort_column LIKE 'band_mag:%' THEN (c.band_columns -> split_part(p_sort_column, ':', 2) ->> 'mag')::double precision
          WHEN p_sort_column LIKE 'band_snr:%' THEN (c.band_columns -> split_part(p_sort_column, ':', 2) ->> 'snr')::double precision
        END
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
        'line_columns', fo.line_columns,
        'band_columns', fo.band_columns,
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

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
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
    text,
    text,
    uuid,
    text,
    text,
    integer,
    integer,
    boolean,
    text,
    double precision,
    double precision,
    boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], uuid, integer[], jsonb, jsonb, boolean)
  FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.get_filtered_spectra_paginated (
  p_program_slugs             text[],
  p_filter_programs           text[]           DEFAULT NULL::text[],
  p_fields                    text[]           DEFAULT NULL::text[],
  p_gratings                  text[]           DEFAULT NULL::text[],
  p_gratings_mode             text             DEFAULT 'any'::text,
  p_observations              text[]           DEFAULT NULL::text[],
  p_redshift_quality          integer[]        DEFAULT NULL::integer[],
  p_redshift_min              double precision DEFAULT NULL::double precision,
  p_redshift_max              double precision DEFAULT NULL::double precision,
  p_max_snr_min               double precision DEFAULT NULL::double precision,
  p_max_snr_max               double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min     double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max     double precision DEFAULT NULL::double precision,
  p_dq_flags_include_any      integer          DEFAULT NULL::integer,
  p_dq_flags_include_all      integer          DEFAULT NULL::integer,
  p_dq_flags_exclude          integer          DEFAULT NULL::integer,
  p_list_ids                  integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode             text             DEFAULT 'any'::text,
  p_search                    text             DEFAULT NULL::text,
  p_inspected_only            boolean          DEFAULT NULL::boolean,
  p_needs_review              boolean          DEFAULT NULL::boolean,
  p_has_photometry            boolean          DEFAULT NULL::boolean,
  p_comment_search            text             DEFAULT NULL::text,
  p_comment_search_scope      text             DEFAULT NULL::text,
  p_comment_user_id           uuid             DEFAULT NULL::uuid,
  p_coord_ra                  double precision DEFAULT NULL::double precision,
  p_coord_dec                 double precision DEFAULT NULL::double precision,
  p_radius_degrees            double precision DEFAULT NULL::double precision,
  p_sort_column               text             DEFAULT 'target_id'::text,
  p_sort_direction            text             DEFAULT 'asc'::text,
  p_page                      integer          DEFAULT 1,
  p_page_size                 integer          DEFAULT 50,
  p_include_thumbnails        boolean          DEFAULT false,
  p_include_unpublished       boolean          DEFAULT false,
  p_line                      text             DEFAULT NULL::text,
  p_line_snr_min              double precision DEFAULT NULL::double precision,
  p_line_snr_max              double precision DEFAULT NULL::double precision,
  p_line_include_stale        boolean          DEFAULT false,
  p_band                      text             DEFAULT NULL::text,
  p_band_mag_min              double precision DEFAULT NULL::double precision,
  p_band_mag_max              double precision DEFAULT NULL::double precision,
  p_band_snr_min              double precision DEFAULT NULL::double precision,
  p_band_snr_max              double precision DEFAULT NULL::double precision,
  p_include_count             boolean          DEFAULT true,
  p_after_sort_text           text             DEFAULT NULL::text,
  p_after_sort_num            double precision DEFAULT NULL::double precision,
  p_after_tiebreak            text[]           DEFAULT NULL::text[],
  p_spectrum_ids              integer[]        DEFAULT NULL::integer[],
  p_line_columns              jsonb            DEFAULT NULL::jsonb,
  p_band_columns              jsonb            DEFAULT NULL::jsonb,
  p_measurements_display_only boolean          DEFAULT false
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
    OR (p_sort_column IN ('band_mag', 'band_snr') AND p_band IS NOT NULL)
    OR public.measurement_sort_key(p_sort_column, p_line_columns, p_band_columns) IS NOT NULL) THEN
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
        (SELECT __l.snr FROM public.cat_lines __l WHERE __l.spectrum_id = s.id AND __l.line = p_line)
      END AS line_snr,
      -- the parent object's magnitude and S/N in the filtered band (sort keys
      -- + 'Mag' / 'Band S/N' columns). Object-level: photometry belongs to the
      -- sky position, so every grating of one object reports the same pair.
      bv.mag AS band_mag,
      bv.snr AS band_snr,
      -- the several-columns maps (phase 3c): this spectrum's lines, its object's bands
      CASE WHEN jsonb_typeof(p_line_columns) = 'object' THEN
        (SELECT jsonb_object_agg(__l.line, jsonb_build_object('snr', __l.snr, 'flux', __l.flux))
         FROM jsonb_each(p_line_columns) __spec(line, w)
         JOIN public.cat_lines __l ON __l.spectrum_id = s.id AND __l.line = __spec.line
         -- the stale gate the object-level columns apply (the fit's stored flag)
         JOIN public.cat_line_fits __f ON __f.spectrum_id = s.id
         WHERE (COALESCE(p_line_include_stale, false) OR NOT __f.stale_redshift)
           AND ((__spec.w ->> 'snr_min') IS NULL OR __l.snr >= (__spec.w ->> 'snr_min')::double precision)
           AND ((__spec.w ->> 'snr_max') IS NULL OR __l.snr <= (__spec.w ->> 'snr_max')::double precision))
      END AS line_columns,
      bc.band_columns,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
            POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM public.cat_targets t
    JOIN public.cat_spectra s ON s.target_id = t.target_id
    LEFT JOIN public.cat_objects o ON o.id = t.object_id
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = t.object_id
    LEFT JOIN public.object_band_columns(p_band_columns, p_program_slugs, p_include_unpublished) bc ON bc.object_id = t.object_id
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
      AND (p_line IS NULL OR p_measurements_display_only OR s.id = ANY(v_line_spectrum_ids))
      AND (p_band IS NULL OR p_measurements_display_only OR t.object_id = ANY(v_band_object_ids))
      AND (p_spectrum_ids IS NULL OR s.id = ANY(p_spectrum_ids))
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
      AND (p_search IS NULL OR s.id IN (SELECT __s.id FROM public.cat_spectra __s WHERE __s.search_text ILIKE '%' || p_search || '%'))
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
        NOT v_coord_search_active OR p_measurements_display_only
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
    WHERE NOT v_coord_search_active OR p_measurements_display_only OR fs.distance <= p_radius_degrees
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
        -- 'line_snr:<line>' / 'band_mag:<band>' / 'band_snr:<band>' (phase 3c)
        ELSE CASE
          WHEN p_sort_column LIKE 'line_snr:%' THEN (df.line_columns -> split_part(p_sort_column, ':', 2) ->> 'snr')::double precision
          WHEN p_sort_column LIKE 'band_mag:%' THEN (df.band_columns -> split_part(p_sort_column, ':', 2) ->> 'mag')::double precision
          WHEN p_sort_column LIKE 'band_snr:%' THEN (df.band_columns -> split_part(p_sort_column, ':', 2) ->> 'snr')::double precision
        END
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
      'line_columns', r.line_columns,
      'band_columns', r.band_columns,
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
  LEFT JOIN public.cat_spectra sth ON p_include_thumbnails AND sth.id = r.spectrum_pk
  -- Drop the has_more overflow row.
  WHERE r.row_num <= p_page_size;
END;
$function$;

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    text,
    text,
    integer,
    integer,
    boolean,
    boolean,
    text,
    double precision,
    double precision,
    boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], integer[], jsonb, jsonb, boolean)
  FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.measurement_sort_key (
  p_sort_column  text,
  p_line_columns jsonb,
  p_band_columns jsonb
)
  RETURNS text
  LANGUAGE sql
  IMMUTABLE
  PARALLEL SAFE
  AS $function$
  SELECT CASE
    WHEN p_sort_column LIKE 'line_snr:%' AND jsonb_typeof(p_line_columns) = 'object' AND p_line_columns ? split_part(p_sort_column, ':', 2) THEN split_part(p_sort_column, ':', 2)
    WHEN (p_sort_column LIKE 'band_mag:%' OR p_sort_column LIKE 'band_snr:%') AND jsonb_typeof(p_band_columns) = 'object' AND p_band_columns ? split_part(p_sort_column, ':', 2) THEN split_part(p_sort_column, ':', 2)
  END;
$function$;

REVOKE ALL ON FUNCTION "public"."measurement_sort_key"(text, jsonb, jsonb) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.object_band_columns (
  p_bands               jsonb,
  p_program_slugs       text[],
  p_include_unpublished boolean DEFAULT false
)
  RETURNS TABLE (
    object_id    integer,
    band_columns jsonb
  )
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  ROWS 20000
  SET search_path TO 'public'
  AS $function$
  SELECT per_band.object_id,
         jsonb_object_agg(per_band.band, jsonb_build_object('mag', per_band.mag, 'snr', per_band.snr))
  FROM (
    SELECT b.object_id, b.band, min(b.mag) AS mag, max(b.snr) AS snr
    FROM jsonb_each(CASE WHEN jsonb_typeof(p_bands) = 'object' THEN p_bands ELSE '{}'::jsonb END) spec(band, w)
    JOIN public.cat_photometry b ON b.band = spec.band
    JOIN public.cat_objects o ON o.id = b.object_id
    WHERE b.object_id IS NOT NULL
      AND o.programs && p_program_slugs
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (COALESCE((SELECT auth.role()), '') = 'service_role' OR (
        o.programs && (SELECT public.accessible_program_slugs())
        AND (o.has_published_spectrum OR (SELECT public.is_admin())
             OR (SELECT public.link_sees_drafts()))
        AND (o.is_active OR (SELECT public.is_admin()))
        AND ((SELECT NOT public.is_link_account())
             OR o.observations && ARRAY[(SELECT public.link_observation())]::text[])
        AND (public.dr_browse_tag() IS NULL
             OR ((SELECT NOT public.is_link_account())
                 AND public.dr_browse_tag() = ANY ((SELECT public.dr_visible_tags())::text[])))
      ))
    GROUP BY b.object_id, b.band, spec.w
    -- the key's own window, on the aggregates the filter tested (no bound is
    -- pushed into WHERE on purpose: see object_band_values)
    HAVING ((spec.w ->> 'mag_min') IS NULL OR min(b.mag) >= (spec.w ->> 'mag_min')::double precision)
       AND ((spec.w ->> 'mag_max') IS NULL OR min(b.mag) <= (spec.w ->> 'mag_max')::double precision)
       AND ((spec.w ->> 'snr_min') IS NULL OR max(b.snr) >= (spec.w ->> 'snr_min')::double precision)
       AND ((spec.w ->> 'snr_max') IS NULL OR max(b.snr) <= (spec.w ->> 'snr_max')::double precision)
  ) per_band
  GROUP BY per_band.object_id;
$function$;

REVOKE ALL ON FUNCTION "public"."object_band_columns"(jsonb, text[], boolean) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.object_line_columns (
  p_lines               jsonb,
  p_include_stale       boolean,
  p_program_slugs       text[],
  p_include_unpublished boolean DEFAULT false
)
  RETURNS TABLE (
    object_id    integer,
    line_columns jsonb
  )
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  ROWS 2000
  SET search_path TO 'public'
  AS $function$
  SELECT per_line.object_id,
         jsonb_object_agg(per_line.line, jsonb_build_object('snr', per_line.snr, 'flux', per_line.flux))
  FROM (
    SELECT l.object_id, l.line,
           max(l.snr) AS snr,
           -- the flux of the best-S/N fit of the line
           (array_agg(l.flux ORDER BY l.snr DESC NULLS LAST))[1] AS flux
    FROM jsonb_each(CASE WHEN jsonb_typeof(p_lines) = 'object' THEN p_lines ELSE '{}'::jsonb END) spec(line, w)
    JOIN public.cat_lines l ON l.line = spec.line
    CROSS JOIN LATERAL (
      SELECT f.program_slug, f.stale_redshift
      FROM public.cat_line_fits f WHERE f.spectrum_id = l.spectrum_id LIMIT 1
    ) f
    CROSS JOIN LATERAL (
      SELECT s.program_slug, s.observation, s.deploy_status
      FROM public.cat_spectra s WHERE s.id = l.spectrum_id LIMIT 1
    ) s
    CROSS JOIN LATERAL (
      SELECT o.programs, o.has_published_spectrum, o.observations, o.is_active
      FROM public.cat_objects o WHERE o.id = l.object_id LIMIT 1
    ) o
    WHERE l.snr IS NOT NULL
      AND l.object_id IS NOT NULL
      AND f.program_slug = ANY(p_program_slugs)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_include_stale OR NOT f.stale_redshift)
      AND (COALESCE((SELECT auth.role()), '') = 'service_role' OR (
        s.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
        AND ((SELECT NOT public.is_link_account())
             OR s.observation = (SELECT public.link_observation()))
        AND (s.deploy_status = 'published' OR (SELECT public.is_admin())
             OR (s.deploy_status = 'draft' AND (SELECT public.link_sees_drafts())))
      ))
      AND (COALESCE((SELECT auth.role()), '') = 'service_role' OR (
        o.programs && (SELECT public.accessible_program_slugs())
        AND (o.has_published_spectrum OR (SELECT public.is_admin())
             OR (SELECT public.link_sees_drafts()))
        AND (o.is_active OR (SELECT public.is_admin()))
        AND ((SELECT NOT public.is_link_account())
             OR o.observations && ARRAY[(SELECT public.link_observation())]::text[])
        AND (public.dr_browse_tag() IS NULL
             OR ((SELECT NOT public.is_link_account())
                 AND public.dr_browse_tag() = ANY ((SELECT public.dr_visible_tags())::text[])))
      ))
    GROUP BY l.object_id, l.line, spec.w
    -- the key's own window, on the aggregate the filter tested
    HAVING ((spec.w ->> 'snr_min') IS NULL OR max(l.snr) >= (spec.w ->> 'snr_min')::double precision)
       AND ((spec.w ->> 'snr_max') IS NULL OR max(l.snr) <= (spec.w ->> 'snr_max')::double precision)
  ) per_line
  GROUP BY per_line.object_id;
$function$;

REVOKE ALL ON FUNCTION "public"."object_line_columns"(jsonb, boolean, text[], boolean) FROM PUBLIC, "anon";

COMMENT ON FUNCTION
  "public"."catalog_query_objects"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, text, double precision, text[], uuid, text, double precision, double
  precision,
  boolean,
  text, double precision, double precision, double precision, double precision, jsonb, jsonb, double precision, double precision, text, text, uuid) IS 'The objects list for a predicate tree (Catalog Read Model phase 3): catalog_match_ids() then get_filtered_objects_paginated(p_object_ids := ...), so the row shape, sort, paging and cursor are the legacy RPC''s. p_line / p_band only drive the Line S/N and Mag columns and sorts, p_coord_ra / p_coord_dec only the distance column and sort (the legacy RPC runs with p_measurements_display_only, so none of them filters). The explicit scope goes through to the tree for a service-role caller.';

COMMENT ON FUNCTION
  "public"."catalog_query_spectra"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, boolean, text, double precision, text[], text, double precision, double
  precision,
  boolean,
  text,
  double precision, double precision, double precision, double precision, jsonb, jsonb, uuid, double precision, double precision, text, text, uuid) IS 'The spectra list for a predicate tree rooted at spectra (Catalog Read Model phase 3): catalog_match_ids() then get_filtered_spectra_paginated(p_spectrum_ids := ...).';

GRANT EXECUTE
  ON FUNCTION
    "public"."catalog_query_objects"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, text, double precision, text[], uuid, text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, double precision, double precision, text, text, uuid)
  TO "authenticated";

REVOKE ALL
  ON FUNCTION
    "public"."catalog_query_objects"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, text, double precision, text[], uuid, text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, double precision, double precision, text, text, uuid)
  FROM "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."catalog_query_objects"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, text, double precision, text[], uuid, text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, double precision, double precision, text, text, uuid)
  TO "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."catalog_query_objects"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, text, double precision, text[], uuid, text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, double precision, double precision, text, text, uuid)
  TO "service_role";

GRANT EXECUTE
  ON FUNCTION
    "public"."catalog_query_spectra"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, boolean, text, double precision, text[], text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, uuid, double precision, double precision, text, text, uuid)
  TO "authenticated";

REVOKE ALL
  ON FUNCTION
    "public"."catalog_query_spectra"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, boolean, text, double precision, text[], text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, uuid, double precision, double precision, text, text, uuid)
  FROM "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."catalog_query_spectra"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, boolean, text, double precision, text[], text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, uuid, double precision, double precision, text, text, uuid)
  TO "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."catalog_query_spectra"(jsonb, text[], boolean, text, text, text, integer, integer, boolean, boolean, text, double precision, text[], text, double precision, double
    precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, uuid, double precision, double precision, text, text, uuid)
  TO "service_role";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_adjacent_objects"(text, text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    text,
    text,
    boolean,
    double precision,
    double precision,
    text,
    text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[])
  TO "authenticated";

REVOKE ALL
  ON FUNCTION
    "public"."get_adjacent_objects"(text, text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    text,
    text,
    boolean,
    double precision,
    double precision,
    text,
    text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[])
  FROM "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_adjacent_objects"(text, text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    text,
    text,
    boolean,
    double precision,
    double precision,
    text,
    text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[])
  TO "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_adjacent_objects"(text, text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    text,
    boolean,
    boolean,
    integer[],
    text,
    double precision,
    double precision,
    double precision,
    text,
    text,
    boolean,
    double precision,
    double precision,
    text,
    text, uuid, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[])
  TO "service_role";

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
    text,
    text,
    uuid,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], text, integer)
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
    text,
    text,
    uuid,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], text, integer)
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
    text,
    text,
    uuid,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], text, integer)
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
    text,
    text,
    uuid,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], text, integer)
  TO "service_role";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], integer, integer)
  TO "authenticated";

REVOKE ALL
  ON FUNCTION
    "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], integer, integer)
  FROM "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], integer, integer)
  TO "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_csv_export_spectra"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    boolean,
    text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[], integer, integer)
  TO "service_role";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_object_ids"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
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
    text,
    text,
    uuid,
    text, text, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[])
  TO "authenticated";

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_object_ids"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
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
    text,
    text,
    uuid,
    text, text, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[])
  FROM "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_object_ids"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
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
    text,
    text,
    uuid,
    text, text, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[])
  TO "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_object_ids"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision, double
    precision,
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
    text,
    text,
    uuid,
    text, text, boolean, text, double precision, double precision, boolean, text, double precision, double precision, double precision, double precision, jsonb, jsonb, integer[])
  TO "service_role";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
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
    text,
    text,
    uuid,
    text,
    text,
    integer,
    integer,
    boolean,
    text,
    double precision,
    double precision,
    boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], uuid, integer[], jsonb, jsonb, boolean)
  TO "authenticated";

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
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
    text,
    text,
    uuid,
    text,
    text,
    integer,
    integer,
    boolean,
    text,
    double precision,
    double precision,
    boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], uuid, integer[], jsonb, jsonb, boolean)
  FROM "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
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
    text,
    text,
    uuid,
    text,
    text,
    integer,
    integer,
    boolean,
    text,
    double precision,
    double precision,
    boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], uuid, integer[], jsonb, jsonb, boolean)
  TO "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_objects_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
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
    text,
    text,
    uuid,
    text,
    text,
    integer,
    integer,
    boolean,
    text,
    double precision,
    double precision,
    boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], uuid, integer[], jsonb, jsonb, boolean)
  TO "service_role";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    text,
    text,
    integer,
    integer,
    boolean,
    boolean,
    text,
    double precision,
    double precision,
    boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], integer[], jsonb, jsonb, boolean)
  TO "authenticated";

REVOKE ALL
  ON FUNCTION
    "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    text,
    text,
    integer,
    integer,
    boolean,
    boolean,
    text,
    double precision,
    double precision,
    boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], integer[], jsonb, jsonb, boolean)
  FROM "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    text,
    text,
    integer,
    integer,
    boolean,
    boolean,
    text,
    double precision,
    double precision,
    boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], integer[], jsonb, jsonb, boolean)
  TO "postgres";

GRANT EXECUTE
  ON FUNCTION
    "public"."get_filtered_spectra_paginated"(text[], text[], text[], text[], text, text[], integer[], double precision, double precision, double precision, double precision,
    double precision,
    double precision,
    integer,
    integer,
    integer,
    integer[],
    text,
    text,
    boolean,
    boolean,
    boolean,
    text,
    text,
    uuid,
    double precision,
    double precision,
    double precision,
    text,
    text,
    integer,
    integer,
    boolean,
    boolean,
    text,
    double precision,
    double precision,
    boolean, text, double precision, double precision, double precision, double precision, boolean, text, double precision, text[], integer[], jsonb, jsonb, boolean)
  TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."measurement_sort_key"(text, jsonb, jsonb) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."measurement_sort_key"(text, jsonb, jsonb) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."measurement_sort_key"(text, jsonb, jsonb) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."measurement_sort_key"(text, jsonb, jsonb) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."object_band_columns"(jsonb, text[], boolean) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."object_band_columns"(jsonb, text[], boolean) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."object_band_columns"(jsonb, text[], boolean) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."object_band_columns"(jsonb, text[], boolean) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."object_line_columns"(jsonb, boolean, text[], boolean) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."object_line_columns"(jsonb, boolean, text[], boolean) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."object_line_columns"(jsonb, boolean, text[], boolean) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."object_line_columns"(jsonb, boolean, text[], boolean) TO "service_role";
