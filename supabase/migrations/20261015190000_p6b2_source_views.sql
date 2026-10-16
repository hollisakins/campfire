SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.dr_assert_live()
  RETURNS void
  LANGUAGE plpgsql
  STABLE
  AS $function$
BEGIN
  IF public.dr_browse_tag() IS NOT NULL THEN
    RAISE EXCEPTION USING ERRCODE = 'PT400', MESSAGE = 'sync streams are live-only (X-Campfire-DR not allowed)';
  END IF;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_assert_live"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_browse_tag()
  RETURNS text
  LANGUAGE sql
  STABLE
  AS $function$
  SELECT NULLIF(current_setting('campfire.dr_tag', true), '');
$function$;

REVOKE ALL ON FUNCTION "public"."dr_browse_tag"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.dr_request_context()
  RETURNS void
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  v_tag text;
  v_ok boolean;
BEGIN
  v_tag := NULLIF(current_setting('request.headers', true), '')::json ->> 'x-campfire-dr';
  IF v_tag IS NULL OR v_tag = '' THEN
    RETURN;
  END IF;
  IF v_tag !~ '^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$' THEN
    RAISE EXCEPTION USING ERRCODE = 'PT404', MESSAGE = 'unknown data release';
  END IF;
  IF COALESCE(NULLIF(current_setting('request.jwt.claims', true), '')::json ->> 'role', '') = 'service_role' THEN
    -- the service role (the /api/v1 routes, scoped by p_program_slugs) may
    -- browse any release that has frozen rows: not a draft, a running freeze
    -- or a failed one
    SELECT EXISTS (SELECT 1 FROM dr_releases r WHERE r.tag = v_tag
                     AND r.state NOT IN ('draft', 'freezing', 'failed')) INTO v_ok;
  ELSIF (SELECT public.is_link_account()) THEN
    -- share links never browse releases (the frozen policies exclude them;
    -- refusing here beats an empty catalog answered with 200s)
    v_ok := false;
  ELSE
    v_ok := v_tag = ANY (public.dr_visible_tags());
  END IF;
  IF NOT v_ok THEN
    RAISE EXCEPTION USING ERRCODE = 'PT404', MESSAGE = 'unknown data release';
  END IF;
  PERFORM set_config('campfire.dr_tag', v_tag, true);
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
    FROM public.cat_objects o
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
    SELECT op.photometry FROM public.cat_photometry op
    WHERE op.object_id = po.id ORDER BY op.updated_at DESC LIMIT 1
  ) phot ON true
  ORDER BY po.object_id ASC;
END;
$function$;

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
        (SELECT __l.snr FROM public.cat_spectrum_lines __l WHERE __l.spectrum_id = s.id AND __l.line = p_line)
      END AS line_snr,
      bv.mag AS band_mag,
      bv.snr AS band_snr
    FROM public.cat_targets t
    JOIN public.cat_spectra s ON s.target_id = t.target_id
    LEFT JOIN public.cat_objects o ON o.id = t.object_id
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
    df.distance, df.dq_flags, df.lists, df.line_snr, df.band_mag, df.band_snr
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  LEFT JOIN user_profiles up ON up.user_id = df.last_inspected_by
  ORDER BY df.id ASC
  LIMIT v_page_size;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_database_overview (
  p_include_unpublished boolean DEFAULT false
)
  RETURNS TABLE (
    n_programs            bigint,
    n_observations        bigint,
    n_pointings           bigint,
    n_targets             bigint,
    n_spectra             bigint,
    total_size_bytes      bigint,
    latest_deployed_at    timestamp with time zone,
    latest_cfpipe_version text
  )
  LANGUAGE sql
  STABLE
  AS $function$
  WITH latest AS (
    SELECT d.deployed_at, d.cfpipe_version
    FROM public.deployments d
    WHERE d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
  )
  SELECT
    (SELECT COUNT(*)::bigint FROM public.programs) AS n_programs,
    (SELECT COUNT(*)::bigint FROM public.observations) AS n_observations,
    (SELECT COALESCE(SUM(jsonb_array_length(pointings)), 0)::bigint
       FROM public.observations
       WHERE pointings IS NOT NULL) AS n_pointings,
    (SELECT COUNT(*)::bigint FROM public.cat_targets) AS n_targets,
    -- B1: gate spectra count/size on publish status (no alias param available).
    (SELECT COUNT(*)::bigint FROM public.cat_spectra
       WHERE p_include_unpublished OR deploy_status = 'published') AS n_spectra,
    (SELECT COALESCE(SUM(file_size), 0)::bigint FROM public.cat_spectra
       WHERE p_include_unpublished OR deploy_status = 'published') AS total_size_bytes,
    (SELECT deployed_at FROM latest) AS latest_deployed_at,
    (SELECT cfpipe_version FROM latest) AS latest_cfpipe_version;
$function$;

CREATE OR REPLACE FUNCTION public.get_field_object_markers (
  p_field               text,
  p_include_unpublished boolean DEFAULT false,
  p_cursor              text    DEFAULT NULL::text,
  p_page_size           integer DEFAULT 5000
)
  RETURNS TABLE (
    object_id         text,
    ra                double precision,
    "dec"             double precision,
    redshift          double precision,
    redshift_quality  integer,
    field             text,
    n_targets         integer,
    n_spectra         integer,
    programs          text[],
    member_target_ids text[]
  )
  LANGUAGE sql
  STABLE
  AS $function$
  -- programs, n_targets, n_spectra and member_target_ids are scoped to the
  -- caller's accessible programs so mixed-program objects don't leak proprietary
  -- member metadata on the map. redshift / redshift_quality stay visible (object
  -- science, per the access policy). Object row visibility is enforced by RLS
  -- (programs && accessible); this function is SECURITY INVOKER and additionally
  -- gates the member aggregates by an explicit accessible_program_slugs()
  -- filter. The logic mirrors object_scoped_aggregates(), per page row.
  WITH acc AS (
    SELECT public.accessible_program_slugs() AS slugs
  ),
  page AS (
    SELECT o.id, o.object_id, o.ra, o.dec, o.redshift, o.redshift_quality, o.field
    FROM public.cat_objects o
    CROSS JOIN acc
    WHERE o.field = p_field
      AND o.is_active
      AND (p_cursor IS NULL OR o.object_id > p_cursor)
      -- Same membership rule as the old inner JOIN: an object appears only if
      -- it has >= 1 visible member (accessible program; B1: contributing a
      -- published spectrum unless the admin opts in). Applied BEFORE the
      -- LIMIT so a page is never short for a reason other than exhaustion.
      AND EXISTS (
        SELECT 1 FROM public.cat_targets t
        WHERE t.object_id = o.id
          AND t.program_slug = ANY(acc.slugs)
          AND (p_include_unpublished OR t.has_published_spectrum)
      )
    ORDER BY o.object_id
    LIMIT GREATEST(1, LEAST(COALESCE(p_page_size, 5000), 50000))
  )
  -- Per-row LATERAL aggregation rather than set-based CTEs joined back to the
  -- page (perf, #515 follow-up to T1-6). The RLS predicate `programs &&
  -- accessible_program_slugs()` reaches the planner as an InitPlan, so it is
  -- estimated at the default ~1 % selectivity while it really matches nearly
  -- every row; the page CTE was estimated at 9 rows (actual 5000) and the
  -- planner joined the aggregates with nested loops over materialized
  -- subqueries, discarding 12.5 M rows per join — 5.9 s per page under RLS,
  -- over the 8 s statement timeout on later pages. A LATERAL aggregate per
  -- page row is the plan an accurate estimate would produce anyway (one index
  -- probe per object) and does not depend on the estimate: 0.24 s for the
  -- same page. Same filters, same output; only the join shape changed.
  SELECT
    p.object_id,
    p.ra,
    p.dec,
    p.redshift::double precision,
    p.redshift_quality,
    p.field,
    COALESCE(mt.n_targets, 0)                       AS n_targets,
    COALESCE(sp.n_spectra, 0)                       AS n_spectra,
    COALESCE(mt.programs, ARRAY[]::TEXT[])          AS programs,
    COALESCE(mt.member_target_ids, ARRAY[]::TEXT[]) AS member_target_ids
  FROM page p
  CROSS JOIN acc
  LEFT JOIN LATERAL (
    SELECT array_agg(t.target_id ORDER BY t.target_id)                AS member_target_ids,
           array_agg(DISTINCT t.program_slug ORDER BY t.program_slug) AS programs,
           COUNT(*)::int                                              AS n_targets
    FROM public.cat_targets t
    WHERE t.object_id = p.id
      AND t.program_slug = ANY(acc.slugs)
      AND (p_include_unpublished OR t.has_published_spectrum)
  ) mt ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::int AS n_spectra
    FROM public.cat_spectra s
    JOIN public.cat_targets t ON t.target_id = s.target_id
    WHERE t.object_id = p.id
      AND t.program_slug = ANY(acc.slugs)
      AND (p_include_unpublished OR s.deploy_status = 'published')
  ) sp ON true
  ORDER BY p.object_id;
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
    FROM public.cat_objects o
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
        (SELECT __l.snr FROM public.cat_spectrum_lines __l WHERE __l.spectrum_id = s.id AND __l.line = p_line)
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
    FROM public.cat_targets t
    JOIN public.cat_spectra s ON s.target_id = t.target_id
    LEFT JOIN public.cat_objects o ON o.id = t.object_id
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
  LEFT JOIN public.cat_spectra sth ON p_include_thumbnails AND sth.id = r.spectrum_pk
  -- Drop the has_more overflow row.
  WHERE r.row_num <= p_page_size;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_line_fits_for_sync (
  p_program_slugs       text[],
  p_updated_since       timestamp with time zone DEFAULT NULL::timestamp WITH time zone,
  p_limit               integer                  DEFAULT 1000,
  p_include_unpublished boolean                  DEFAULT false,
  p_include_counts      boolean                  DEFAULT true,
  p_after_id            integer                  DEFAULT NULL::integer
)
  RETURNS TABLE (
    line_fit_records jsonb,
    total_count      bigint
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET statement_timeout TO '120s'
  AS $function$
BEGIN
  PERFORM public.dr_assert_live();  -- live-only (P6b-2)
  RETURN QUERY
  WITH matched AS (
    SELECT f.spectrum_id, f.target_id, f.grating, f.program_slug, f.observation,
           s.spectrum_id AS spectrum_name, t.field, o.object_id AS current_object_id,
           f.z_used, f.z_source, f.z_quality, f.object_id, f.object_version,
           f.z_fit, f.z_fit_err, f.dv, f.dv_err, f.sigma_v, f.sigma_v_err, f.kin_source,
           f.n_lines, f.n_detected, f.n_broad, f.chi2, f.dof, f.lines,
           f.fit_version, f.cfpipe_version, f.f_lsf, f.spectrum_hash, f.fitted_at,
           f.created_at, f.updated_at,
           -- The record's sync timestamp: the latest of the fit row and the two
           -- parent rows its staleness flags derive from, so the client's
           -- incremental cursor (max local updated_at) advances past a
           -- re-inspection or redeploy instead of re-sending the row forever.
           GREATEST(f.updated_at, s.updated_at, o.updated_at) AS effective_updated_at,
           -- staleness vs the live inspection state: the same two functions
           -- the spectrum_line_fits_status view calls, so the mirrored record
           -- and the ledger cannot disagree.
           (o.id IS NOT NULL AND public.line_fit_stale_redshift(
              o.version, o.redshift_quality, (o.redshift)::double precision,
              f.object_version, f.z_quality, f.z_used)) AS stale_redshift,
           public.line_fit_stale_spectrum(
              s.file_hash, s.science_hash,
              f.spectrum_hash, f.spectrum_science_hash) AS stale_spectrum
    FROM spectrum_line_fits f
    JOIN spectra s ON s.id = f.spectrum_id
    LEFT JOIN targets t ON t.target_id = f.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE f.program_slug = ANY(p_program_slugs)
      -- Hide fits whose parent object was soft-deleted (same guard as
      -- get_spectra_for_sync), so the line catalog never carries a row the
      -- spectra / objects streams hide.
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      -- Incremental: stale_redshift / stale_spectrum are derived from the
      -- parent rows, so a re-inspected object or a redeployed spectrum must
      -- re-send the fit even though the fit row itself did not change.
      AND (p_updated_since IS NULL
           OR f.updated_at > p_updated_since
           OR s.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
      AND (p_after_id IS NULL OR f.spectrum_id > p_after_id)
    ORDER BY f.spectrum_id
    LIMIT p_limit
  ),
  total AS (
    SELECT COUNT(*) AS cnt
    FROM spectrum_line_fits f
    JOIN spectra s ON s.id = f.spectrum_id
    LEFT JOIN targets t ON t.target_id = f.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE p_include_counts
      AND f.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_updated_since IS NULL
           OR f.updated_at > p_updated_since
           OR s.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'spectrum_id', m.spectrum_id,
        'spectrum_name', m.spectrum_name,
        'target_id', m.target_id,
        'grating', m.grating,
        'program_slug', m.program_slug,
        'observation', m.observation,
        'field', m.field,
        'current_object_id', m.current_object_id,
        'z_used', m.z_used,
        'z_source', m.z_source,
        'z_quality', m.z_quality,
        'object_id', m.object_id,
        'object_version', m.object_version,
        'z_fit', m.z_fit,
        'z_fit_err', m.z_fit_err,
        'dv', m.dv,
        'dv_err', m.dv_err,
        'sigma_v', m.sigma_v,
        'sigma_v_err', m.sigma_v_err,
        'kin_source', m.kin_source,
        'n_lines', m.n_lines,
        'n_detected', m.n_detected,
        'n_broad', m.n_broad,
        'chi2', m.chi2,
        'dof', m.dof,
        'lines', m.lines,
        'fit_version', m.fit_version,
        'cfpipe_version', m.cfpipe_version,
        'f_lsf', m.f_lsf,
        'spectrum_hash', m.spectrum_hash,
        'fitted_at', m.fitted_at,
        'stale_redshift', m.stale_redshift,
        'stale_spectrum', m.stale_spectrum,
        'created_at', m.created_at,
        'fit_updated_at', m.updated_at,
        'updated_at', m.effective_updated_at
      ) ORDER BY m.spectrum_id
    ), '[]'::jsonb) AS line_fit_records,
    (SELECT cnt FROM total) AS total_count
  FROM matched m;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_lists_for_sync (
  p_user_id             uuid    DEFAULT NULL::uuid,
  p_include_unpublished boolean DEFAULT false
)
  RETURNS jsonb
  LANGUAGE plpgsql
  STABLE
  AS $function$
BEGIN
  PERFORM public.dr_assert_live();  -- live-only (P6b-2)
  RETURN COALESCE(
    (SELECT jsonb_agg(jsonb_build_object(
      'id', ol.id,
      'slug', ol.slug,
      'name', ol.name,
      'description', ol.description,
      'visibility', ol.visibility,
      'is_system', ol.is_system,
      'created_by', ol.created_by,
      'created_at', ol.created_at,
      'updated_at', ol.updated_at,
      -- B1: count only members whose object has a published spectrum (unlinked
      -- coordinate-keyed members, object_id IS NULL, still count). Fail-closed.
      'member_count', (
        SELECT COUNT(*) FROM object_list_members olm
        LEFT JOIN objects o ON o.id = olm.object_id
        WHERE olm.list_id = ol.id
          AND (p_include_unpublished OR olm.object_id IS NULL OR o.has_published_spectrum)
      )
    ) ORDER BY ol.is_system DESC, ol.name)
    FROM object_lists ol
    WHERE ol.created_by = p_user_id
       OR ol.visibility IN ('public_read', 'public_edit')
       OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = p_user_id)),
    '[]'::jsonb
  );
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_nircam_field_summary (
  p_field text
)
  RETURNS TABLE (
    field                 text,
    display_name          text,
    center_ra             double precision,
    center_dec            double precision,
    coverage_area_arcmin2 double precision,
    coverage_area_deg2    double precision,
    filters               text[],
    tiles                 text[],
    pixel_scales          text[],
    extensions            text[],
    epochs                text[],
    n_files               bigint,
    total_bytes           bigint,
    last_updated          timestamp without time zone,
    layout_key            text
  )
  LANGUAGE sql
  STABLE
  SET search_path TO 'public'
  AS $function$
  SELECT
    ni.field,
    COALESCE(f.display_name, upper(ni.field)) AS display_name,
    f.center_ra,
    f.center_dec,
    f.coverage_area_arcmin2,
    f.coverage_area_deg2,
    array_agg(DISTINCT ni.filter ORDER BY ni.filter)           AS filters,
    array_agg(DISTINCT ni.tile ORDER BY ni.tile)               AS tiles,
    array_agg(DISTINCT ni.pixel_scale ORDER BY ni.pixel_scale) AS pixel_scales,
    array_agg(DISTINCT ni.extension ORDER BY ni.extension)     AS extensions,
    array_agg(DISTINCT ni.epoch ORDER BY ni.epoch)             AS epochs,
    COUNT(*)::bigint                       AS n_files,
    COALESCE(SUM(ni.file_size), 0)::bigint AS total_bytes,
    MAX(ni.created_at)                     AS last_updated,
    CASE WHEN public.dr_browse_tag() IS NULL THEN
      (SELECT so.storage_key FROM storage_objects so
         WHERE so.product_type = 'nircam_layout'
           AND so.field = ni.field
           AND so.status = 'active'
         LIMIT 1)
    ELSE
      (SELECT COALESCE(m.archive_key, m.storage_key) FROM public.dr_manifest m
         WHERE m.tag = public.dr_browse_tag()
           AND m.component = 'nircam/' || ni.field
           AND m.product_type = 'nircam_layout'
         ORDER BY m.storage_key LIMIT 1)
    END                                    AS layout_key
  FROM public.cat_nircam_images ni
  LEFT JOIN public.cat_fields f ON f.name = ni.field
  WHERE ni.field = p_field
  GROUP BY ni.field, f.display_name, f.center_ra, f.center_dec,
           f.coverage_area_arcmin2, f.coverage_area_deg2;
$function$;

CREATE OR REPLACE FUNCTION public.get_nircam_fields()
  RETURNS TABLE (
    field                 text,
    display_name          text,
    center_ra             double precision,
    center_dec            double precision,
    coverage_area_arcmin2 double precision,
    coverage_area_deg2    double precision,
    n_filters             integer,
    n_tiles               integer,
    n_files               bigint,
    total_bytes           bigint,
    last_updated          timestamp without time zone,
    layout_key            text
  )
  LANGUAGE sql
  STABLE
  SET search_path TO 'public'
  AS $function$
  SELECT
    ni.field,
    COALESCE(f.display_name, upper(ni.field)) AS display_name,
    f.center_ra,
    f.center_dec,
    f.coverage_area_arcmin2,
    f.coverage_area_deg2,
    COUNT(DISTINCT ni.filter)::integer AS n_filters,
    COUNT(DISTINCT ni.tile)::integer   AS n_tiles,
    COUNT(*)::bigint                    AS n_files,
    COALESCE(SUM(ni.file_size), 0)::bigint AS total_bytes,
    MAX(ni.created_at)                  AS last_updated,
    -- the layout plot: live from the registry; under a browsed release
    -- (P6b-2) from the release's manifest (archived copy when stamped; NULL
    -- until the freeze lists field products, P6b-3), never the live key
    CASE WHEN public.dr_browse_tag() IS NULL THEN
      (SELECT so.storage_key FROM storage_objects so
         WHERE so.product_type = 'nircam_layout'
           AND so.field = ni.field
           AND so.status = 'active'
         LIMIT 1)
    ELSE
      (SELECT COALESCE(m.archive_key, m.storage_key) FROM public.dr_manifest m
         WHERE m.tag = public.dr_browse_tag()
           AND m.component = 'nircam/' || ni.field
           AND m.product_type = 'nircam_layout'
         ORDER BY m.storage_key LIMIT 1)
    END                                 AS layout_key
  FROM public.cat_nircam_images ni
  LEFT JOIN public.cat_fields f ON f.name = ni.field
  GROUP BY ni.field, f.display_name, f.center_ra, f.center_dec,
           f.coverage_area_arcmin2, f.coverage_area_deg2
  ORDER BY ni.field;
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
  PERFORM public.dr_assert_live();  -- live-only (P6b-2)
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

CREATE OR REPLACE FUNCTION public.get_objects_near (
  p_ra                  double precision,
  p_dec                 double precision,
  p_radius_degrees      double precision,
  p_program_slugs       text[],
  p_limit               integer          DEFAULT 10,
  p_exclude_object_id   text             DEFAULT NULL::text,
  p_include_unpublished boolean          DEFAULT false
)
  RETURNS TABLE (
    id               integer,
    object_id        text,
    field            text,
    ra               double precision,
    "dec"            double precision,
    redshift         double precision,
    redshift_quality integer,
    gratings         text[],
    n_spectra        integer,
    distance         double precision
  )
  LANGUAGE sql
  STABLE
  AS $function$
  WITH near AS (
    SELECT o.id, o.object_id, o.field, o.ra, o.dec,
           o.redshift::DOUBLE PRECISION AS redshift, o.redshift_quality,
           2 * DEGREES(ASIN(SQRT(
             POWER(SIN(RADIANS(o.dec - p_dec) / 2), 2) +
             COS(RADIANS(p_dec)) * COS(RADIANS(o.dec)) *
             POWER(SIN(RADIANS(o.ra - p_ra) / 2), 2)
           ))) AS distance
    FROM public.cat_objects o
    WHERE o.programs && p_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND o.dec BETWEEN p_dec - p_radius_degrees AND p_dec + p_radius_degrees
      AND o.ra BETWEEN p_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_dec)), 1e-6)
                   AND p_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_dec)), 1e-6)
      AND 2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_dec) / 2), 2) +
            COS(RADIANS(p_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_ra) / 2), 2)
          ))) <= p_radius_degrees
      AND (p_exclude_object_id IS NULL OR o.object_id <> p_exclude_object_id)
    ORDER BY distance ASC, o.object_id ASC
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 10), 1), 100)
  )
  SELECT n.id, n.object_id, n.field, n.ra, n.dec, n.redshift, n.redshift_quality,
         COALESCE(sa.gratings, '{}'::TEXT[]) AS gratings,
         COALESCE(sa.n_spectra, 0) AS n_spectra,
         n.distance
  FROM near n
  LEFT JOIN LATERAL public.object_scoped_aggregates(n.id, p_program_slugs, p_include_unpublished) sa ON true
  ORDER BY n.distance ASC, n.object_id ASC;
$function$;

CREATE OR REPLACE FUNCTION public.get_observation_manifest (
  p_obs_name            text,
  p_program_slugs       text[],
  p_include_unpublished boolean DEFAULT false
)
  RETURNS TABLE (
    spectra_id      integer,
    spectrum_id     text,
    target_id       text,
    grating         text,
    fits_path       text,
    file_hash       text,
    file_size       bigint,
    signal_to_noise double precision,
    cfpipe_version  text
  )
  LANGUAGE plpgsql
  STABLE
  AS $function$
BEGIN
  RETURN QUERY
  SELECT s.id, s.spectrum_id, s.target_id, s.grating, s.fits_path, s.file_hash, s.file_size,
         s.signal_to_noise, s.cfpipe_version
  FROM public.cat_spectra s
  JOIN public.cat_targets t ON t.target_id = s.target_id
  WHERE t.observation = p_obs_name AND t.program_slug = ANY(p_program_slugs)
    -- B1: fail-closed; admin sync passes p_include_unpublished => true.
    AND (p_include_unpublished OR s.deploy_status = 'published')
  ORDER BY s.spectrum_id;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_observation_stats (
  p_program_slugs       text[],
  p_include_unpublished boolean DEFAULT false
)
  RETURNS TABLE (
    observation           text,
    program_slug          text,
    program_name          text,
    field                 text,
    target_count          bigint,
    spectrum_count        bigint,
    total_size_bytes      bigint,
    pointings             jsonb,
    crds_context          text,
    cfpipe_version        text,
    jwst_version          text,
    reduced_at            timestamp with time zone,
    deployed_at           timestamp with time zone,
    deployed_by_username  text,
    deployed_by_full_name text,
    n_patches_since_full  integer,
    last_patch_at         timestamp with time zone
  )
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_slugs text[] := public.scoped_program_slugs(p_program_slugs);
  v_link boolean := public.is_link_account();
  v_unpublished boolean := p_include_unpublished
    AND (auth.role() = 'service_role'
         OR COALESCE(current_setting('request.jwt.claims', true), '') = ''
         OR public.is_admin());
  -- Callers whose RLS view included draft data before these readers went
  -- SECURITY DEFINER (admins; share links minted with include_drafts) keep
  -- the live aggregate; the published-only snapshot would drop their
  -- draft-only observations entirely.
  -- (P6b-2) a browsed release never reads the matview: the live path
  -- reads cat_spectra / cat_targets, which are the frozen rows then
  v_live boolean := v_unpublished OR public.is_admin() OR public.link_sees_drafts()
    OR public.dr_browse_tag() IS NOT NULL;
  -- What the live path may count, derived from the caller's authorization,
  -- never from the raw p_include_unpublished argument: a drafts link sees
  -- published + draft (policies: select_spectra_by_access /
  -- authenticated_select_deployments); an admin sees published only unless
  -- opted in. Counts and deployment provenance use the same set, so a draft
  -- re-reduction's version stamps never sit next to published-only counts
  -- (the matview's invariant, views.sql).
  v_statuses text[] := CASE
    WHEN v_unpublished THEN ARRAY['draft', 'published', 'revoked']
    WHEN public.link_sees_drafts() THEN ARRAY['published', 'draft']
    ELSE ARRAY['published'] END;
BEGIN
  IF NOT v_live THEN
    RETURN QUERY
    SELECT mv.observation, mv.program_slug, mv.program_name, mv.field,
      mv.target_count, mv.spectrum_count, mv.total_size_bytes,
      mv.pointings,
      mv.crds_context, mv.cfpipe_version, mv.jwst_version,
      mv.reduced_at, mv.deployed_at,
      -- Deployer identity stays hidden from share links, as user_profiles
      -- RLS hid it before the reader went SECURITY DEFINER.
      CASE WHEN v_link THEN NULL ELSE mv.deployed_by_username END,
      CASE WHEN v_link THEN NULL ELSE mv.deployed_by_full_name END,
      mv.n_patches_since_full, mv.last_patch_at
    FROM public.mv_observations_overview mv
    WHERE mv.program_slug = ANY(v_slugs)
      -- Share links see only the observation they were minted for (mirrors
      -- accessible_observations_select; the matview has no RLS to do it).
      AND (NOT v_link OR mv.observation = (SELECT public.link_observation()))
      -- The live aggregate only yields observations that have targets.
      AND mv.target_count > 0
    ORDER BY mv.observation;
    RETURN;
  END IF;

  -- Live aggregate (unpublished-inclusive). Aggregate stats first, then LEFT
  -- JOIN observations once for the JSONB payload; provenance from the most
  -- recent FULL deployment, patches counted since it.
  RETURN QUERY
  WITH stats AS (
    SELECT t.observation, t.program_slug, p.program_name, t.field,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes
    FROM public.cat_targets t
    JOIN programs p ON p.slug = t.program_slug
    LEFT JOIN public.cat_spectra s ON s.target_id = t.target_id
      -- Only the statuses this caller is authorized for.
      AND s.deploy_status = ANY(v_statuses)
    WHERE t.program_slug = ANY(v_slugs)
      AND (NOT v_link OR t.observation = (SELECT public.link_observation()))
      -- Targets count on the same terms as their spectra (the snapshot's
      -- has_published_spectrum gate, generalised): a target with no spectrum
      -- in the authorized statuses is not counted unless the caller opted
      -- into unpublished data, where every target appears as before.
      AND (v_unpublished OR EXISTS (
        SELECT 1 FROM public.cat_spectra sx
        WHERE sx.target_id = t.target_id AND sx.deploy_status = ANY(v_statuses)))
    GROUP BY t.observation, t.program_slug, p.program_name, t.field
  )
  SELECT s.observation, s.program_slug, s.program_name, s.field,
    s.target_count, s.spectrum_count, s.total_size_bytes,
    o.pointings,
    COALESCE(full_dep.crds_context, frozen_dep.crds_context),
    COALESCE(full_dep.cfpipe_version, frozen_dep.cfpipe_version),
    COALESCE(full_dep.jwst_version, frozen_dep.jwst_version),
    COALESCE(full_dep.reduced_at, frozen_dep.reduced_at), full_dep.deployed_at,
    CASE WHEN v_link THEN NULL ELSE full_dep.deployed_by_username END,
    CASE WHEN v_link THEN NULL ELSE full_dep.deployed_by_full_name END,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM stats s
  LEFT JOIN observations o ON o.name = s.observation
  -- Provenance: live, the newest full deployment. Under a browsed release
  -- (P6b-2) deployments is live-only (a later deploy would label frozen
  -- spectra with its versions), so the versions come from the release's own
  -- spectra of the observation: the most common non-null value of each
  -- field, the latest reduced_at; there is no frozen deployments record, so
  -- deployed_at, the deployer and the patch counts are NULL / 0 then.
  LEFT JOIN LATERAL (
    SELECT d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
    WHERE public.dr_browse_tag() IS NULL
      AND d.observation = s.observation AND d.source_ids_filter IS NULL
      AND d.status = ANY(v_statuses)
    ORDER BY d.deployed_at DESC
    LIMIT 1
  ) full_dep ON true
  LEFT JOIN LATERAL (
    SELECT
      (SELECT x.crds_context FROM public.cat_spectra x
         WHERE x.observation = s.observation AND x.deploy_status = ANY(v_statuses) AND x.crds_context IS NOT NULL
         GROUP BY x.crds_context ORDER BY count(*) DESC, x.crds_context LIMIT 1) AS crds_context,
      (SELECT x.cfpipe_version FROM public.cat_spectra x
         WHERE x.observation = s.observation AND x.deploy_status = ANY(v_statuses) AND x.cfpipe_version IS NOT NULL
         GROUP BY x.cfpipe_version ORDER BY count(*) DESC, x.cfpipe_version LIMIT 1) AS cfpipe_version,
      (SELECT x.jwst_version FROM public.cat_spectra x
         WHERE x.observation = s.observation AND x.deploy_status = ANY(v_statuses) AND x.jwst_version IS NOT NULL
         GROUP BY x.jwst_version ORDER BY count(*) DESC, x.jwst_version LIMIT 1) AS jwst_version,
      (SELECT max(x.reduced_at) FROM public.cat_spectra x
         WHERE x.observation = s.observation AND x.deploy_status = ANY(v_statuses)) AS reduced_at
    WHERE public.dr_browse_tag() IS NOT NULL
  ) frozen_dep ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE public.dr_browse_tag() IS NULL
      AND d.observation = s.observation
      AND d.source_ids_filter IS NOT NULL
      AND d.status = ANY(v_statuses)
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
  ) patches ON true
  ORDER BY s.observation;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_observations_overview (
  p_program_slugs       text[],
  p_include_unpublished boolean DEFAULT false
)
  RETURNS TABLE (
    observation           text,
    program_slug          text,
    program_name          text,
    field                 text,
    cycle                 integer,
    gratings              text[],
    pointing_count        integer,
    pointings             jsonb,
    target_count          bigint,
    spectrum_count        bigint,
    total_size_bytes      bigint,
    crds_context          text,
    cfpipe_version        text,
    jwst_version          text,
    reduced_at            timestamp with time zone,
    deployed_at           timestamp with time zone,
    deployed_by_username  text,
    deployed_by_full_name text,
    n_patches_since_full  integer,
    last_patch_at         timestamp with time zone
  )
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_slugs text[] := public.scoped_program_slugs(p_program_slugs);
  v_link boolean := public.is_link_account();
  v_unpublished boolean := p_include_unpublished
    AND (auth.role() = 'service_role'
         OR COALESCE(current_setting('request.jwt.claims', true), '') = ''
         OR public.is_admin());
  -- Callers whose RLS view included draft data before these readers went
  -- SECURITY DEFINER (admins; share links minted with include_drafts) keep
  -- the live aggregate; the published-only snapshot would drop their
  -- draft-only observations entirely.
  -- (P6b-2) a browsed release never reads the matview: the live path
  -- reads cat_spectra / cat_targets, which are the frozen rows then
  v_live boolean := v_unpublished OR public.is_admin() OR public.link_sees_drafts()
    OR public.dr_browse_tag() IS NOT NULL;
  -- What the live path may count, derived from the caller's authorization,
  -- never from the raw p_include_unpublished argument: a drafts link sees
  -- published + draft (policies: select_spectra_by_access /
  -- authenticated_select_deployments); an admin sees published only unless
  -- opted in. Counts and deployment provenance use the same set, so a draft
  -- re-reduction's version stamps never sit next to published-only counts
  -- (the matview's invariant, views.sql).
  v_statuses text[] := CASE
    WHEN v_unpublished THEN ARRAY['draft', 'published', 'revoked']
    WHEN public.link_sees_drafts() THEN ARRAY['published', 'draft']
    ELSE ARRAY['published'] END;
BEGIN
  IF NOT v_live THEN
    RETURN QUERY
    SELECT mv.observation, mv.program_slug, mv.program_name, mv.field,
      mv.cycle, mv.gratings, mv.pointing_count, mv.pointings,
      mv.target_count, mv.spectrum_count, mv.total_size_bytes,
      mv.crds_context, mv.cfpipe_version, mv.jwst_version,
      mv.reduced_at, mv.deployed_at,
      -- Deployer identity stays hidden from share links, as user_profiles
      -- RLS hid it before the reader went SECURITY DEFINER.
      CASE WHEN v_link THEN NULL ELSE mv.deployed_by_username END,
      CASE WHEN v_link THEN NULL ELSE mv.deployed_by_full_name END,
      mv.n_patches_since_full, mv.last_patch_at
    FROM public.mv_observations_overview mv
    WHERE mv.program_slug = ANY(v_slugs)
      -- Share links see only the observation they were minted for (mirrors
      -- accessible_observations_select; the matview has no RLS to do it).
      AND (NOT v_link OR mv.observation = (SELECT public.link_observation()))
    ORDER BY mv.program_slug, mv.observation;
    RETURN;
  END IF;

  RETURN QUERY
  WITH stats AS (
    SELECT t.observation, t.program_slug,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes,
      ARRAY_AGG(DISTINCT s.grating ORDER BY s.grating)
        FILTER (WHERE s.grating IS NOT NULL) AS gratings
    FROM public.cat_targets t
    LEFT JOIN public.cat_spectra s ON s.target_id = t.target_id
      -- Only the statuses this caller is authorized for.
      AND s.deploy_status = ANY(v_statuses)
    WHERE t.program_slug = ANY(v_slugs)
      AND (NOT v_link OR t.observation = (SELECT public.link_observation()))
      -- Targets count on the same terms as their spectra (see
      -- get_observation_stats).
      AND (v_unpublished OR EXISTS (
        SELECT 1 FROM public.cat_spectra sx
        WHERE sx.target_id = t.target_id AND sx.deploy_status = ANY(v_statuses)))
    GROUP BY t.observation, t.program_slug
  )
  SELECT
    o.name AS observation,
    o.program_slug,
    p.program_name,
    o.field,
    p.cycle,
    CASE
      WHEN COALESCE(array_length(s.gratings, 1), 0) > 0 THEN s.gratings
      ELSE COALESCE(o.gratings, ARRAY[]::text[])
    END AS gratings,
    COALESCE(jsonb_array_length(o.pointings), 0) AS pointing_count,
    o.pointings,
    COALESCE(s.target_count, 0)::bigint AS target_count,
    COALESCE(s.spectrum_count, 0)::bigint AS spectrum_count,
    COALESCE(s.total_size_bytes, 0)::bigint AS total_size_bytes,
    COALESCE(full_dep.crds_context, frozen_dep.crds_context),
    COALESCE(full_dep.cfpipe_version, frozen_dep.cfpipe_version),
    COALESCE(full_dep.jwst_version, frozen_dep.jwst_version),
    COALESCE(full_dep.reduced_at, frozen_dep.reduced_at), full_dep.deployed_at,
    CASE WHEN v_link THEN NULL ELSE full_dep.deployed_by_username END,
    CASE WHEN v_link THEN NULL ELSE full_dep.deployed_by_full_name END,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM public.observations o
  JOIN public.programs p ON p.slug = o.program_slug
  LEFT JOIN stats s ON s.observation = o.name AND s.program_slug = o.program_slug
  -- Provenance as in get_observation_stats: the newest full deployment live;
  -- under a browsed release (P6b-2) the release's own spectra (deployments
  -- is live-only; no frozen record, so deployed_at / deployer / patches are
  -- NULL / 0 then).
  LEFT JOIN LATERAL (
    SELECT d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
    WHERE public.dr_browse_tag() IS NULL
      AND d.observation = o.name AND d.source_ids_filter IS NULL
      AND d.status = ANY(v_statuses)
    ORDER BY d.deployed_at DESC
    LIMIT 1
  ) full_dep ON true
  LEFT JOIN LATERAL (
    SELECT
      (SELECT x.crds_context FROM public.cat_spectra x
         WHERE x.observation = o.name AND x.deploy_status = ANY(v_statuses) AND x.crds_context IS NOT NULL
         GROUP BY x.crds_context ORDER BY count(*) DESC, x.crds_context LIMIT 1) AS crds_context,
      (SELECT x.cfpipe_version FROM public.cat_spectra x
         WHERE x.observation = o.name AND x.deploy_status = ANY(v_statuses) AND x.cfpipe_version IS NOT NULL
         GROUP BY x.cfpipe_version ORDER BY count(*) DESC, x.cfpipe_version LIMIT 1) AS cfpipe_version,
      (SELECT x.jwst_version FROM public.cat_spectra x
         WHERE x.observation = o.name AND x.deploy_status = ANY(v_statuses) AND x.jwst_version IS NOT NULL
         GROUP BY x.jwst_version ORDER BY count(*) DESC, x.jwst_version LIMIT 1) AS jwst_version,
      (SELECT max(x.reduced_at) FROM public.cat_spectra x
         WHERE x.observation = o.name AND x.deploy_status = ANY(v_statuses)) AS reduced_at
    WHERE public.dr_browse_tag() IS NOT NULL
  ) frozen_dep ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE public.dr_browse_tag() IS NULL
      AND d.observation = o.name
      AND d.source_ids_filter IS NOT NULL
      AND d.status = ANY(v_statuses)
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
  ) patches ON true
  WHERE o.program_slug = ANY(v_slugs)
    AND (NOT v_link OR o.name = (SELECT public.link_observation()))
    -- (P6b-2) a browsed release lists only the observations it holds spectra
    -- of: observations is live-only (members with no frozen rows, and
    -- observations created after the freeze, would show with zero counts)
    AND (public.dr_browse_tag() IS NULL OR s.observation IS NOT NULL)
  ORDER BY o.program_slug, o.name;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_photometry_for_sync (
  p_program_slugs       text[],
  p_updated_since       timestamp with time zone DEFAULT NULL::timestamp WITH time zone,
  p_limit               integer                  DEFAULT 1000,
  p_include_unpublished boolean                  DEFAULT false,
  p_include_counts      boolean                  DEFAULT true,
  p_after_id            integer                  DEFAULT NULL::integer
)
  RETURNS TABLE (
    photometry_records jsonb,
    total_count        bigint
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET statement_timeout TO '120s'
  AS $function$
BEGIN
  PERFORM public.dr_assert_live();  -- live-only (P6b-2)
  RETURN QUERY
  WITH matched AS (
    SELECT op.id, o.object_id, op.field, op.catalog_name, op.catalog_id,
           op.match_distance_arcsec, op.photometry, op.photo_z,
           op.photo_z_err_lo, op.photo_z_err_hi, op.has_pz,
           op.created_at,
           -- Sync timestamp: the later of the row's own and its object's, so a
           -- row re-sent because the object changed advances the client's
           -- cursor (see get_spectra_for_sync).
           GREATEST(op.updated_at, o.updated_at) AS updated_at
    FROM object_photometry op
    JOIN objects o ON o.id = op.object_id
    WHERE o.programs && p_program_slugs
      -- Soft-deleted objects are tombstoned by the objects stream (which
      -- cascades to their photometry locally); their rows must not ride the
      -- object-changed clause below back in.
      AND o.is_active = true
      -- B1: fail-closed publish gate (this RPC always bypasses RLS).
      AND (p_include_unpublished OR o.has_published_spectrum)
      -- Incremental: the row itself, or its object, changed since the cursor.
      -- The object clause is the resurrection path: photometry deleted along
      -- with a tombstoned object (un-published, or soft-deleted) comes back
      -- when the object does -- both flips stamp objects.updated_at -- even
      -- though the photometry row did not change. Mirrors get_line_fits_for_sync.
      AND (p_updated_since IS NULL
           OR op.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
      -- Keyset (#103): op.id is the PK, so a strict > needs no tiebreaker.
      AND (p_after_id IS NULL OR op.id > p_after_id)
    ORDER BY op.id
    LIMIT p_limit
  ),
  -- Count CTE gated on p_include_counts; when FALSE the planner collapses it to
  -- One-Time Filter: false and skips the scan/join.
  total AS (
    SELECT COUNT(*) AS cnt
    FROM object_photometry op
    JOIN objects o ON o.id = op.object_id
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      -- Incremental: the row itself, or its object, changed since the cursor.
      -- The object clause is the resurrection path: photometry deleted along
      -- with a tombstoned object (un-published, or soft-deleted) comes back
      -- when the object does -- both flips stamp objects.updated_at -- even
      -- though the photometry row did not change. Mirrors get_line_fits_for_sync.
      AND (p_updated_since IS NULL
           OR op.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'field', m.field,
        'catalog_name', m.catalog_name,
        'catalog_id', m.catalog_id,
        'match_distance_arcsec', m.match_distance_arcsec,
        'photometry', m.photometry,
        'photo_z', m.photo_z,
        'photo_z_err_lo', m.photo_z_err_lo,
        'photo_z_err_hi', m.photo_z_err_hi,
        'has_pz', m.has_pz,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
      -- Keyset (#103): page array must be id-ordered (client cursors on the
      -- last element).
      ORDER BY m.id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT
  FROM matched m;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_spectra_for_sync (
  p_program_slugs       text[],
  p_user_id             uuid                     DEFAULT NULL::uuid,
  p_updated_since       timestamp with time zone DEFAULT NULL::timestamp WITH time zone,
  p_limit               integer                  DEFAULT 1000,
  p_include_counts      boolean                  DEFAULT true,
  p_include_unpublished boolean                  DEFAULT false,
  p_after_spectrum_id   text                     DEFAULT NULL::text
)
  RETURNS TABLE (
    spectra                jsonb,
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
  PERFORM public.dr_assert_live();  -- live-only (P6b-2)
  RETURN QUERY
  WITH matched AS MATERIALIZED (
    SELECT s.id, s.spectrum_id, s.target_id, o.object_id AS object_id,
           s.grating, s.fits_path, s.file_hash, s.science_hash, s.file_size,
           s.signal_to_noise, s.exposure_time,
           s.cfpipe_version, s.crds_context, s.jwst_version, s.date_obs, s.reduced_at,
           s.redshift_auto, s.dq_flags,
           -- program_slug / observation are the trigger-owned row-local copies
           -- of the parent target's (perf T2-A, #504), so they read off the
           -- spectra row; targets is joined only for field and the object link.
           s.program_slug, s.observation, t.field,
           s.created_at,
           -- The row's sync timestamp is the later of its own and its
           -- object's: a row re-sent because the object changed must advance
           -- the client's cursor (MAX of mirrored updated_at) past that change,
           -- or it would be re-sent on every incremental sync. Same pattern
           -- as get_line_fits_for_sync's effective_updated_at.
           GREATEST(s.updated_at, o.updated_at) AS updated_at
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    -- Program scope on the row-local column: the keyset walk down
    -- idx_spectra_spectrum_id rejects out-of-scope rows without fetching the
    -- target first (a user with a few programs used to pay a targets lookup
    -- for every spectrum walked past).
    WHERE s.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      -- B1: fail-closed publish gate (this RPC always bypasses RLS). Drafts
      -- sync only for admins opting in; revoked never syncs -- it is the
      -- soft-deleted state and is tombstoned below instead.
      AND (s.deploy_status = 'published'
           OR (p_include_unpublished AND s.deploy_status = 'draft'))
      -- Incremental: the row itself, or its object, changed since the cursor.
      -- The object clause is the resurrection path: a spectrum tombstoned as
      -- a member of a soft-deleted object comes back when the object is
      -- reactivated (which stamps objects.updated_at) even though the
      -- spectrum row did not change. Mirrors get_line_fits_for_sync.
      AND (p_updated_since IS NULL
           OR s.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
      -- Keyset (#103): spectrum_id is UNIQUE (idx_spectra_spectrum_id), so a
      -- strict > needs no tiebreaker; keep the ordering column UNIQUE.
      AND (p_after_spectrum_id IS NULL OR s.spectrum_id > p_after_spectrum_id)
    ORDER BY s.spectrum_id
    LIMIT p_limit
  ),
  -- First-page counts, gated on p_include_counts (when FALSE the planner
  -- collapses the CTE to One-Time Filter: false). One pass over spectra
  -- yields both numbers: total_count (the incremental window) is a FILTER on
  -- the scan that produces total_accessible_count, and the soft-deleted-
  -- object exclusion is an anti-join against the (tiny, partial-indexed) set
  -- of inactive objects. Previously two spectra x targets x objects hash
  -- joins ran back to back on every first page.
  counts AS (
    SELECT COUNT(*) FILTER (WHERE p_updated_since IS NULL
                              OR s.updated_at > p_updated_since
                              -- same object clause as matched; the recently
                              -- changed objects are a small hashed set
                              OR s.target_id IN (
                                SELECT t.target_id
                                FROM targets t
                                JOIN objects o ON o.id = t.object_id
                                WHERE o.updated_at > p_updated_since)) AS total_cnt,
           COUNT(*) AS accessible_cnt
    FROM spectra s
    WHERE p_include_counts
      AND s.program_slug = ANY(p_program_slugs)
      AND (s.deploy_status = 'published'
           OR (p_include_unpublished AND s.deploy_status = 'draft'))
      AND NOT EXISTS (
        SELECT 1
        FROM targets t
        JOIN objects o ON o.id = t.object_id
        WHERE t.target_id = s.target_id
          AND o.is_active = false)
  ),
  -- Tombstones (see get_objects_for_sync): first incremental page only.
  deleted AS (
    SELECT COALESCE(array_agg(x.id ORDER BY x.id), '{}'::INTEGER[]) AS ids
    FROM (
      -- Un-published since the cursor: revoked for everyone, plus a draft an
      -- admin's mirror may carry. set_spectra_deploy_status's write reaches
      -- here because deploy_status is in bump_spectra_updated_at_trigger's
      -- column list.
      SELECT s.id
      FROM spectra s
      WHERE p_updated_since IS NOT NULL
        AND p_after_spectrum_id IS NULL
        AND s.program_slug = ANY(p_program_slugs)
        AND s.updated_at > p_updated_since
        AND NOT (s.deploy_status = 'published'
                 OR (p_include_unpublished AND s.deploy_status = 'draft'))
      UNION
      -- Members of an object soft-deleted since the cursor: the spectrum row
      -- itself did not change, so its own updated_at says nothing.
      SELECT s.id
      FROM spectra s
      JOIN targets t ON t.target_id = s.target_id
      JOIN objects o ON o.id = t.object_id
      WHERE p_updated_since IS NOT NULL
        AND p_after_spectrum_id IS NULL
        AND s.program_slug = ANY(p_program_slugs)
        AND o.is_active = false
        AND o.updated_at > p_updated_since
    ) x
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'spectrum_id', m.spectrum_id,
        'target_id', m.target_id,
        'object_id', m.object_id,
        'grating', m.grating,
        'fits_path', m.fits_path,
        'file_hash', m.file_hash,
        'science_hash', m.science_hash,
        'file_size', m.file_size,
        'signal_to_noise', m.signal_to_noise,
        'exposure_time', m.exposure_time,
        'cfpipe_version', m.cfpipe_version,
        'crds_context', m.crds_context,
        'jwst_version', m.jwst_version,
        'date_obs', m.date_obs,
        'reduced_at', m.reduced_at,
        'redshift_auto', m.redshift_auto,
        'dq_flags', m.dq_flags,
        'program_slug', m.program_slug,
        'observation', m.observation,
        'field', m.field,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
      -- Keyset (#103): page array must be spectrum_id-ordered (client cursors on
      -- the last element). matched has no post-ORDER joins, but pin it anyway.
      ORDER BY m.spectrum_id
    ), '[]'::jsonb),
    COALESCE((SELECT c.total_cnt FROM counts c), 0)::BIGINT,
    COALESCE((SELECT c.accessible_cnt FROM counts c), 0)::BIGINT,
    (SELECT d.ids FROM deleted d)
  FROM matched m;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_storage_objects_for_sync (
  p_program_slugs       text[],
  p_updated_since       timestamp with time zone DEFAULT NULL::timestamp WITH time zone,
  p_limit               integer                  DEFAULT 1000,
  p_include_counts      boolean                  DEFAULT true,
  p_include_unpublished boolean                  DEFAULT false,
  p_after_id            bigint                   DEFAULT NULL::bigint,
  p_product_types       text[]                   DEFAULT NULL::text[],
  p_observations        text[]                   DEFAULT NULL::text[],
  p_fields              text[]                   DEFAULT NULL::text[]
)
  RETURNS TABLE (
    objects                jsonb,
    total_count            bigint,
    total_accessible_count bigint,
    deleted_ids            bigint[]
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET statement_timeout TO '120s'
  AS $function$
BEGIN
  PERFORM public.dr_assert_live();  -- live-only (P6b-2)
  RETURN QUERY
  -- `scoped` carries the full published-scope filter and feeds the count CTEs
  -- only (gated on p_include_counts, so it is skipped entirely on keyset pages
  -- 2+). `matched` re-states the SAME scope inline against the base table so its
  -- keyset seek uses the PK index instead of reading a materialized full scan.
  -- The two copies must stay in sync (same house pattern as get_objects_for_sync's
  -- matched vs. total/accessible).
  WITH scoped AS (
    SELECT so.updated_at, so.spectrum_id
    FROM storage_objects so
    WHERE so.status = 'active'
      AND (p_product_types IS NULL OR so.product_type = ANY(p_product_types))
      AND ((p_observations IS NULL AND p_fields IS NULL)
           OR so.observation = ANY(COALESCE(p_observations, '{}'::TEXT[]))
           OR so.field = ANY(COALESCE(p_fields, '{}'::TEXT[])))
      AND (
        p_include_unpublished
        OR (so.spectrum_id IS NOT NULL AND EXISTS (
              -- Row-local program_slug (perf T2-A, #504): no targets hop, and
              -- idx_spectra_spectrum_id_scope answers it index-only.
              SELECT 1 FROM spectra s
              WHERE s.spectrum_id = so.spectrum_id
                AND s.deploy_status = 'published'
                AND s.program_slug = ANY(p_program_slugs)))
        OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM deployments d
              LEFT JOIN observations o ON o.name = d.observation
              WHERE d.id = so.deployment_id
                AND d.status = 'published'
                -- NIRCam field deploy (epic #261, N1): multi-program, public to all
                -- when published. NIRSpec obs deploy stays program-scoped.
                AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))))
      )
  ),
  matched AS MATERIALIZED (
    -- Explicit columns, not so.*: with the client's final product types this
    -- is an index-only scan of idx_storage_objects_sync_finals, whose INCLUDE
    -- list must cover everything read here.
    SELECT so.id, so.backend, so.bucket, so.storage_key, so.content_hash,
           so.sci_dq_hash, so.size_bytes, so.content_type, so.product_type,
           so.instrument, so.status, so.observation, so.field, so.filter,
           so.spectrum_id, so.exposure_ref, so.deployment_id, so.cfpipe_version,
           so.created_at, so.updated_at
    FROM storage_objects so
    WHERE so.status = 'active'
      AND (p_product_types IS NULL OR so.product_type = ANY(p_product_types))
      AND ((p_observations IS NULL AND p_fields IS NULL)
           OR so.observation = ANY(COALESCE(p_observations, '{}'::TEXT[]))
           OR so.field = ANY(COALESCE(p_fields, '{}'::TEXT[])))
      AND (
        p_include_unpublished
        OR (so.spectrum_id IS NOT NULL AND EXISTS (
              -- Row-local program_slug (perf T2-A, #504): no targets hop, and
              -- idx_spectra_spectrum_id_scope answers it index-only.
              SELECT 1 FROM spectra s
              WHERE s.spectrum_id = so.spectrum_id
                AND s.deploy_status = 'published'
                AND s.program_slug = ANY(p_program_slugs)))
        OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM deployments d
              LEFT JOIN observations o ON o.name = d.observation
              WHERE d.id = so.deployment_id
                AND d.status = 'published'
                AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))))
      )
      -- Incremental: the row itself changed, or its spectrum did. The
      -- spectrum clause is the resurrection path for non-admins: rows
      -- tombstoned when their spectrum was un-published come back on
      -- republish (which stamps spectra.updated_at) even though the registry
      -- row did not change. A semi-join, not a per-row EXISTS: the recently
      -- changed spectra are a small hashed set. Mirrors get_line_fits_for_sync.
      AND (p_updated_since IS NULL
           OR so.updated_at > p_updated_since
           OR so.spectrum_id IN (SELECT s2.spectrum_id FROM spectra s2
                                 WHERE s2.updated_at > p_updated_since))
      -- Keyset (#103): id is the PK, so a strict > needs no tiebreaker.
      AND (p_after_id IS NULL OR so.id > p_after_id)
    ORDER BY so.id
    LIMIT p_limit
  ),
  total AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
      AND (p_updated_since IS NULL
           OR scoped.updated_at > p_updated_since
           OR scoped.spectrum_id IN (SELECT s2.spectrum_id FROM spectra s2
                                     WHERE s2.updated_at > p_updated_since))
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
  ),
  -- Tombstones (see get_objects_for_sync): first incremental page only. Rows
  -- that left the active state, and active rows whose spectrum was
  -- un-published (invisible to non-admins from now on; the spectrum's
  -- updated_at moves on a deploy_status change). Program-scoped like the
  -- sibling CTEs: a non-admin's tombstones are drawn from rows whose
  -- observation (or spectrum) is in an accessible program, plus field-only
  -- products, which the main scope shows to everyone when published. Only
  -- integer ids travel, and an id the client never mirrored deletes nothing.
  -- Hard deletes (deploy remove) still need a full sync to clear.
  deleted AS (
    SELECT COALESCE(array_agg(x.id ORDER BY x.id), '{}'::BIGINT[]) AS ids
    FROM (
      SELECT so.id
      FROM storage_objects so
      WHERE p_updated_since IS NOT NULL
        AND p_after_id IS NULL
        AND so.status <> 'active'
        AND so.updated_at > p_updated_since
        AND (p_include_unpublished
             OR (so.observation IS NULL AND so.field IS NOT NULL)
             OR EXISTS (SELECT 1 FROM observations ob
                        WHERE ob.name = so.observation
                          AND ob.program_slug = ANY(p_program_slugs)))
        AND (p_product_types IS NULL OR so.product_type = ANY(p_product_types))
        AND ((p_observations IS NULL AND p_fields IS NULL)
             OR so.observation = ANY(COALESCE(p_observations, '{}'::TEXT[]))
             OR so.field = ANY(COALESCE(p_fields, '{}'::TEXT[])))
      UNION
      SELECT so.id
      FROM storage_objects so
      JOIN spectra s ON s.spectrum_id = so.spectrum_id
      WHERE p_updated_since IS NOT NULL
        AND p_after_id IS NULL
        AND NOT p_include_unpublished
        AND so.status = 'active'
        AND s.program_slug = ANY(p_program_slugs)
        AND s.deploy_status <> 'published'
        AND s.updated_at > p_updated_since
        AND (p_product_types IS NULL OR so.product_type = ANY(p_product_types))
        AND ((p_observations IS NULL AND p_fields IS NULL)
             OR so.observation = ANY(COALESCE(p_observations, '{}'::TEXT[]))
             OR so.field = ANY(COALESCE(p_fields, '{}'::TEXT[])))
    ) x
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'backend', m.backend,
        'bucket', m.bucket,
        'storage_key', m.storage_key,
        'content_hash', m.content_hash,
        'sci_dq_hash', m.sci_dq_hash,
        'size_bytes', m.size_bytes,
        'content_type', m.content_type,
        'product_type', m.product_type,
        'instrument', m.instrument,
        'status', m.status,
        'observation', m.observation,
        'field', m.field,
        'filter', m.filter,
        'spectrum_id', m.spectrum_id,
        'exposure_ref', m.exposure_ref,
        'deployment_id', m.deployment_id,
        'cfpipe_version', m.cfpipe_version,
        'created_at', m.created_at,
        -- Sync timestamp: the later of the row's own and its spectrum's, so a
        -- row re-sent because the spectrum changed advances the client's
        -- cursor (see get_spectra_for_sync).
        'updated_at', GREATEST(m.updated_at, s.updated_at)
      )
      -- Keyset (#103): page array must be id-ordered (client cursors on the
      -- last element).
      ORDER BY m.id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT,
    (SELECT d.ids FROM deleted d)
  FROM matched m
  LEFT JOIN spectra s ON s.spectrum_id = m.spectrum_id;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_sync_deletions (
  p_since timestamp with time zone
)
  RETURNS TABLE (
    stream  text,
    row_ids bigint[]
  )
  LANGUAGE sql
  STABLE
  SET search_path TO 'public', 'pg_catalog'
  AS $function$
  SELECT public.dr_assert_live();  -- live-only (P6b-2)
  SELECT d.stream, array_agg(DISTINCT d.row_id ORDER BY d.row_id)
  FROM public.sync_deletions d
  WHERE d.deleted_at > p_since
    AND NOT CASE d.stream
      WHEN 'objects' THEN EXISTS (SELECT 1 FROM public.objects o WHERE o.id = d.row_id)
      WHEN 'spectra' THEN EXISTS (SELECT 1 FROM public.spectra s WHERE s.id = d.row_id)
      WHEN 'storage' THEN EXISTS (SELECT 1 FROM public.storage_objects so WHERE so.id = d.row_id)
      WHEN 'photometry' THEN EXISTS (SELECT 1 FROM public.object_photometry p WHERE p.id = d.row_id)
      WHEN 'line_fits' THEN EXISTS (SELECT 1 FROM public.spectrum_line_fits f WHERE f.spectrum_id = d.row_id)
    END
  GROUP BY d.stream;
$function$;

CREATE OR REPLACE FUNCTION public.object_band_values (
  p_band                text,
  p_mag_min             double precision,
  p_mag_max             double precision,
  p_snr_min             double precision,
  p_snr_max             double precision,
  p_program_slugs       text[],
  p_include_unpublished boolean          DEFAULT false
)
  RETURNS TABLE (
    object_id integer,
    mag       double precision,
    snr       double precision
  )
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  ROWS 20000
  SET search_path TO 'public'
  AS $function$
  SELECT p.object_id, min(b.mag), max(b.snr)
  FROM public.cat_photometry_bands b
  JOIN public.cat_photometry p ON p.id = b.photometry_id
  JOIN public.cat_objects o ON o.id = p.object_id
  WHERE p_band IS NOT NULL
    AND b.band = p_band
    -- Gate 1: the caller's access-checked scope (what the RPCs apply themselves).
    AND o.programs && p_program_slugs
    AND (p_include_unpublished OR o.has_published_spectrum)
    -- Gate 2: verbatim select_objects_by_access (policies.sql) for every
    -- caller except the service role (the JWT role claim, the same test the
    -- deploy-side RPCs use; NOT auth.uid() IS NULL, which an `authenticated`
    -- request without a subject would also satisfy).
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
  GROUP BY p.object_id
  HAVING (p_mag_min IS NULL OR min(b.mag) >= p_mag_min)
     AND (p_mag_max IS NULL OR min(b.mag) <= p_mag_max)
     AND (p_snr_min IS NULL OR max(b.snr) >= p_snr_min)
     AND (p_snr_max IS NULL OR max(b.snr) <= p_snr_max);
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
  SELECT max(l.snr)
  FROM public.cat_targets t
  JOIN public.cat_spectra s ON s.target_id = t.target_id
  JOIN public.cat_spectrum_lines l ON l.spectrum_id = s.id AND l.line = p_line
  JOIN public.cat_line_fits f ON f.spectrum_id = s.id
  JOIN public.cat_objects o ON o.id = t.object_id
  WHERE t.object_id = p_object_id
    AND f.program_slug = ANY(p_program_slugs)
    AND (p_include_unpublished OR s.deploy_status = 'published')
    AND (p_include_stale OR NOT public.line_fit_stale_redshift(
           o.version, o.redshift_quality, (o.redshift)::double precision,
           f.object_version, f.z_quality, f.z_used));
$function$;

CREATE OR REPLACE FUNCTION public.object_scoped_aggregates (
  p_object_id           integer,
  p_program_slugs       text[],
  p_include_unpublished boolean DEFAULT false
)
  RETURNS TABLE (
    programs          text[],
    gratings          text[],
    observations      text[],
    n_targets         integer,
    n_spectra         integer,
    max_snr           double precision,
    max_exposure_time double precision
  )
  LANGUAGE sql
  STABLE
  ROWS 1
  AS $function$
  WITH stored AS MATERIALIZED (
    SELECT o.programs, o.gratings, o.observations,
           o.n_targets, o.n_spectra, o.max_snr, o.max_exposure_time
    FROM public.cat_objects o
    WHERE o.id = p_object_id
      AND NOT p_include_unpublished
      AND o.programs <@ p_program_slugs
      AND NOT EXISTS (
        SELECT 1
        FROM public.cat_targets t
        JOIN public.cat_spectra s ON s.target_id = t.target_id
        WHERE t.object_id = p_object_id
          AND s.deploy_status <> 'published'
      )
      AND o.n_spectra = (
        SELECT COUNT(*)::integer
        FROM public.cat_targets t
        JOIN public.cat_spectra s ON s.target_id = t.target_id
        WHERE t.object_id = p_object_id
      )
  ),
  m AS (
    SELECT t.target_id, t.program_slug, t.observation
    FROM public.cat_targets t
    WHERE t.object_id = p_object_id
      AND t.program_slug = ANY(p_program_slugs)
      -- B1: only count targets that contribute a published spectrum so
      -- n_targets / programs / observations don't include draft-only members.
      AND (p_include_unpublished OR t.has_published_spectrum)
  ),
  sp AS (
    SELECT s.grating, s.signal_to_noise, s.exposure_time
    FROM public.cat_spectra s
    WHERE s.target_id IN (SELECT target_id FROM m)
      AND (p_include_unpublished OR s.deploy_status = 'published')
  )
  SELECT st.programs, st.gratings, st.observations,
         st.n_targets, st.n_spectra, st.max_snr, st.max_exposure_time
  FROM stored st
  UNION ALL
  SELECT
    COALESCE((SELECT array_agg(DISTINCT m.program_slug ORDER BY m.program_slug) FROM m), '{}')::text[],
    COALESCE((SELECT array_agg(DISTINCT sp.grating ORDER BY sp.grating) FROM sp WHERE sp.grating IS NOT NULL), '{}')::text[],
    COALESCE((SELECT array_agg(DISTINCT m.observation ORDER BY m.observation) FROM m WHERE m.observation IS NOT NULL), '{}')::text[],
    (SELECT COUNT(*) FROM m)::integer,
    (SELECT COUNT(*) FROM sp)::integer,
    (SELECT MAX(sp.signal_to_noise) FROM sp),
    (SELECT MAX(sp.exposure_time) FROM sp)
  WHERE NOT EXISTS (SELECT 1 FROM stored);
$function$;

CREATE OR REPLACE FUNCTION public.objects_matching_grating_filter (
  p_gratings            text[],
  p_gratings_mode       text,
  p_program_slugs       text[],
  p_include_unpublished boolean DEFAULT false
)
  RETURNS SETOF integer
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  AS $function$
BEGIN
  IF p_gratings_mode = 'all' THEN
    RETURN QUERY
    SELECT t.object_id
    FROM public.cat_targets t
    JOIN public.cat_spectra s ON s.target_id = t.target_id
    WHERE t.object_id IS NOT NULL
      AND t.program_slug = ANY(p_program_slugs)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND s.grating = ANY(p_gratings)
    GROUP BY t.object_id
    HAVING COUNT(DISTINCT s.grating) = (SELECT COUNT(DISTINCT g) FROM unnest(p_gratings) g);
  ELSE
    -- 'any' and 'none' need the same set (callers negate for 'none').
    RETURN QUERY
    SELECT DISTINCT t.object_id
    FROM public.cat_targets t
    JOIN public.cat_spectra s ON s.target_id = t.target_id
    WHERE t.object_id IS NOT NULL
      AND t.program_slug = ANY(p_program_slugs)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND s.grating = ANY(p_gratings);
  END IF;
END;
$function$;

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
  SELECT t.object_id
  FROM public.cat_spectrum_lines l
  JOIN public.cat_line_fits f ON f.spectrum_id = l.spectrum_id
  JOIN public.cat_spectra s ON s.id = l.spectrum_id
  JOIN public.cat_targets t ON t.target_id = s.target_id
  JOIN public.cat_objects o ON o.id = t.object_id
  WHERE l.line = p_line
    AND l.snr IS NOT NULL
    AND f.program_slug = ANY(p_program_slugs)
    AND (p_include_unpublished OR s.deploy_status = 'published')
    AND (p_include_stale OR NOT public.line_fit_stale_redshift(
           o.version, o.redshift_quality, (o.redshift)::double precision,
           f.object_version, f.z_quality, f.z_used))
  GROUP BY t.object_id
  HAVING (p_snr_min IS NULL OR max(l.snr) >= p_snr_min)
     AND (p_snr_max IS NULL OR max(l.snr) <= p_snr_max);
$function$;

CREATE OR REPLACE FUNCTION public.objects_matching_observation_filter (
  p_observations        text[],
  p_program_slugs       text[],
  p_include_unpublished boolean DEFAULT false
)
  RETURNS SETOF integer
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  AS $function$
BEGIN
  RETURN QUERY
  SELECT DISTINCT t.object_id
  FROM public.cat_targets t
  WHERE t.object_id IS NOT NULL
    AND t.program_slug = ANY(p_program_slugs)
    AND (p_include_unpublished OR t.has_published_spectrum)
    AND t.observation = ANY(p_observations);
END;
$function$;

CREATE OR REPLACE FUNCTION public.spectra_matching_line_filter (
  p_line          text,
  p_snr_min       double precision,
  p_snr_max       double precision,
  p_include_stale boolean
)
  RETURNS SETOF integer
  LANGUAGE sql
  STABLE
  AS $function$
  SELECT l.spectrum_id
  FROM public.cat_spectrum_lines l
  JOIN public.cat_line_fits f ON f.spectrum_id = l.spectrum_id
  JOIN public.cat_spectra s ON s.id = l.spectrum_id
  LEFT JOIN public.cat_targets t ON t.target_id = s.target_id
  LEFT JOIN public.cat_objects o ON o.id = t.object_id
  WHERE l.line = p_line
    AND l.snr IS NOT NULL
    AND (p_snr_min IS NULL OR l.snr >= p_snr_min)
    AND (p_snr_max IS NULL OR l.snr <= p_snr_max)
    AND (p_include_stale OR o.id IS NULL OR NOT public.line_fit_stale_redshift(
           o.version, o.redshift_quality, (o.redshift)::double precision,
           f.object_version, f.z_quality, f.z_used));
$function$;

CREATE VIEW "public"."cat_fields" WITH (security_invoker=true) AS  SELECT fields.name,
    fields.display_name,
    fields.filters,
    fields.tiles,
    fields.fiducial_tiles,
    fields.epochs,
    fields.programs,
    fields.jwst_program_ids,
    fields.file_globs,
    fields.center_ra,
    fields.center_dec,
    fields.config,
    fields.coverage_area_arcmin2,
    fields.coverage_area_deg2,
    fields.latest_deployment_id,
    fields.created_at,
    fields.expmap_pixel_scale_arcsec,
    fields.config_hash,
    fields.config_updated_at,
    fields.retired_at
   FROM public.fields
  WHERE (public.dr_browse_tag() IS NULL)
UNION ALL
 SELECT dr_fields.name,
    dr_fields.display_name,
    dr_fields.filters,
    dr_fields.tiles,
    dr_fields.fiducial_tiles,
    dr_fields.epochs,
    dr_fields.programs,
    dr_fields.jwst_program_ids,
    dr_fields.file_globs,
    dr_fields.center_ra,
    dr_fields.center_dec,
    dr_fields.config,
    dr_fields.coverage_area_arcmin2,
    dr_fields.coverage_area_deg2,
    dr_fields.latest_deployment_id,
    dr_fields.created_at,
    dr_fields.expmap_pixel_scale_arcsec,
    dr_fields.config_hash,
    dr_fields.config_updated_at,
    dr_fields.retired_at
   FROM public.dr_fields
  WHERE ((public.dr_browse_tag() IS NOT NULL) AND (dr_fields.tag = public.dr_browse_tag()));

REVOKE ALL ON TABLE "public"."cat_fields" FROM "anon";

CREATE VIEW "public"."cat_filter_options" WITH (security_invoker=true) AS  SELECT mv_filter_options.id,
    mv_filter_options.fields,
    mv_filter_options.observations,
    mv_filter_options.gratings,
    mv_filter_options.photometry_bands
   FROM public.mv_filter_options
  WHERE (public.dr_browse_tag() IS NULL)
UNION ALL
 SELECT dr_filter_options.id,
    dr_filter_options.fields,
    dr_filter_options.observations,
    dr_filter_options.gratings,
    dr_filter_options.photometry_bands
   FROM public.dr_filter_options
  WHERE ((public.dr_browse_tag() IS NOT NULL) AND (dr_filter_options.tag = public.dr_browse_tag()));

REVOKE ALL ON TABLE "public"."cat_filter_options" FROM "anon";

CREATE VIEW "public"."cat_line_fits" WITH (security_invoker=true) AS  SELECT spectrum_line_fits.spectrum_id,
    spectrum_line_fits.target_id,
    spectrum_line_fits.grating,
    spectrum_line_fits.program_slug,
    spectrum_line_fits.observation,
    spectrum_line_fits.z_used,
    spectrum_line_fits.z_source,
    spectrum_line_fits.z_quality,
    spectrum_line_fits.object_id,
    spectrum_line_fits.object_version,
    spectrum_line_fits.z_fit,
    spectrum_line_fits.z_fit_err,
    spectrum_line_fits.dv,
    spectrum_line_fits.dv_err,
    spectrum_line_fits.sigma_v,
    spectrum_line_fits.sigma_v_err,
    spectrum_line_fits.kin_source,
    spectrum_line_fits.n_lines,
    spectrum_line_fits.n_detected,
    spectrum_line_fits.n_broad,
    spectrum_line_fits.chi2,
    spectrum_line_fits.dof,
    spectrum_line_fits.lines,
    spectrum_line_fits.fit_version,
    spectrum_line_fits.cfpipe_version,
    spectrum_line_fits.f_lsf,
    spectrum_line_fits.spectrum_hash,
    spectrum_line_fits.fitted_at,
    spectrum_line_fits.created_at,
    spectrum_line_fits.updated_at,
    spectrum_line_fits.spectrum_science_hash
   FROM public.spectrum_line_fits
  WHERE (public.dr_browse_tag() IS NULL)
UNION ALL
 SELECT dr_line_fits.spectrum_id,
    dr_line_fits.target_id,
    dr_line_fits.grating,
    dr_line_fits.program_slug,
    dr_line_fits.observation,
    dr_line_fits.z_used,
    dr_line_fits.z_source,
    dr_line_fits.z_quality,
    dr_line_fits.object_id,
    dr_line_fits.object_version,
    dr_line_fits.z_fit,
    dr_line_fits.z_fit_err,
    dr_line_fits.dv,
    dr_line_fits.dv_err,
    dr_line_fits.sigma_v,
    dr_line_fits.sigma_v_err,
    dr_line_fits.kin_source,
    dr_line_fits.n_lines,
    dr_line_fits.n_detected,
    dr_line_fits.n_broad,
    dr_line_fits.chi2,
    dr_line_fits.dof,
    dr_line_fits.lines,
    dr_line_fits.fit_version,
    dr_line_fits.cfpipe_version,
    dr_line_fits.f_lsf,
    dr_line_fits.spectrum_hash,
    dr_line_fits.fitted_at,
    dr_line_fits.created_at,
    dr_line_fits.updated_at,
    dr_line_fits.spectrum_science_hash
   FROM public.dr_line_fits
  WHERE ((public.dr_browse_tag() IS NOT NULL) AND (dr_line_fits.tag = public.dr_browse_tag()));

REVOKE ALL ON TABLE "public"."cat_line_fits" FROM "anon";

CREATE VIEW "public"."cat_nircam_images" WITH (security_invoker=true) AS  SELECT nircam_images.id,
    nircam_images.field,
    nircam_images.tile,
    nircam_images.filter,
    nircam_images.pixel_scale,
    nircam_images.extension,
    nircam_images.file_path,
    nircam_images.created_at,
    nircam_images.file_size,
    nircam_images.deploy_status,
    nircam_images.deployment_id,
    nircam_images.epoch
   FROM public.nircam_images
  WHERE (public.dr_browse_tag() IS NULL)
UNION ALL
 SELECT dr_nircam_images.id,
    dr_nircam_images.field,
    dr_nircam_images.tile,
    dr_nircam_images.filter,
    dr_nircam_images.pixel_scale,
    dr_nircam_images.extension,
    dr_nircam_images.file_path,
    dr_nircam_images.created_at,
    dr_nircam_images.file_size,
    dr_nircam_images.deploy_status,
    dr_nircam_images.deployment_id,
    dr_nircam_images.epoch
   FROM public.dr_nircam_images
  WHERE ((public.dr_browse_tag() IS NOT NULL) AND (dr_nircam_images.tag = public.dr_browse_tag()));

REVOKE ALL ON TABLE "public"."cat_nircam_images" FROM "anon";

CREATE VIEW "public"."cat_objects" WITH (security_invoker=true) AS  SELECT objects.id,
    objects.object_id,
    objects.field,
    objects.ra,
    objects."dec",
    objects.created_at,
    objects.n_targets,
    objects.n_spectra,
    objects.programs,
    objects.gratings,
    objects.max_snr,
    objects.max_exposure_time,
    objects.updated_at,
    objects.observations,
    objects.has_photometry,
    objects.photo_z,
    objects.photo_z_err_hi,
    objects.photo_z_err_lo,
    objects.redshift_auto,
    objects.redshift_inspected,
    objects.redshift_quality,
    objects.redshift,
    objects.last_inspected_at,
    objects.last_inspected_by,
    objects.last_data_change_at,
    objects.staleness_reason,
    objects.version,
    objects.is_active,
    objects.inspected_used_auto,
    objects.search_text,
    objects.has_published_spectrum,
    objects.reviewed_spectrum_ids,
    objects.needs_review
   FROM public.objects
  WHERE (public.dr_browse_tag() IS NULL)
UNION ALL
 SELECT dr_objects.id,
    dr_objects.object_id,
    dr_objects.field,
    dr_objects.ra,
    dr_objects."dec",
    dr_objects.created_at,
    dr_objects.n_targets,
    dr_objects.n_spectra,
    dr_objects.programs,
    dr_objects.gratings,
    dr_objects.max_snr,
    dr_objects.max_exposure_time,
    dr_objects.updated_at,
    dr_objects.observations,
    dr_objects.has_photometry,
    dr_objects.photo_z,
    dr_objects.photo_z_err_hi,
    dr_objects.photo_z_err_lo,
    dr_objects.redshift_auto,
    dr_objects.redshift_inspected,
    dr_objects.redshift_quality,
    dr_objects.redshift,
    dr_objects.last_inspected_at,
    dr_objects.last_inspected_by,
    dr_objects.last_data_change_at,
    dr_objects.staleness_reason,
    dr_objects.version,
    dr_objects.is_active,
    dr_objects.inspected_used_auto,
    dr_objects.search_text,
    dr_objects.has_published_spectrum,
    dr_objects.reviewed_spectrum_ids,
    dr_objects.needs_review
   FROM public.dr_objects
  WHERE ((public.dr_browse_tag() IS NOT NULL) AND (dr_objects.tag = public.dr_browse_tag()));

REVOKE ALL ON TABLE "public"."cat_objects" FROM "anon";

CREATE VIEW "public"."cat_photometry_bands" WITH (security_invoker=true) AS  SELECT object_photometry_bands.photometry_id,
    object_photometry_bands.band,
    object_photometry_bands.flux,
    object_photometry_bands.flux_err,
    object_photometry_bands.wav,
    object_photometry_bands.snr,
    object_photometry_bands.mag
   FROM public.object_photometry_bands
  WHERE (public.dr_browse_tag() IS NULL)
UNION ALL
 SELECT dr_photometry_bands.photometry_id,
    dr_photometry_bands.band,
    dr_photometry_bands.flux,
    dr_photometry_bands.flux_err,
    dr_photometry_bands.wav,
    dr_photometry_bands.snr,
    dr_photometry_bands.mag
   FROM public.dr_photometry_bands
  WHERE ((public.dr_browse_tag() IS NOT NULL) AND (dr_photometry_bands.tag = public.dr_browse_tag()));

REVOKE ALL ON TABLE "public"."cat_photometry_bands" FROM "anon";

CREATE VIEW "public"."cat_photometry" WITH (security_invoker=true) AS  SELECT object_photometry.id,
    object_photometry.object_id,
    object_photometry.field,
    object_photometry.ra,
    object_photometry."dec",
    object_photometry.catalog_name,
    object_photometry.catalog_id,
    object_photometry.match_distance_arcsec,
    object_photometry.photometry,
    object_photometry.photo_z,
    object_photometry.photo_z_err_lo,
    object_photometry.photo_z_err_hi,
    object_photometry.has_pz,
    object_photometry.created_at,
    object_photometry.updated_at
   FROM public.object_photometry
  WHERE (public.dr_browse_tag() IS NULL)
UNION ALL
 SELECT dr_photometry.id,
    dr_photometry.object_id,
    dr_photometry.field,
    dr_photometry.ra,
    dr_photometry."dec",
    dr_photometry.catalog_name,
    dr_photometry.catalog_id,
    dr_photometry.match_distance_arcsec,
    dr_photometry.photometry,
    dr_photometry.photo_z,
    dr_photometry.photo_z_err_lo,
    dr_photometry.photo_z_err_hi,
    dr_photometry.has_pz,
    dr_photometry.created_at,
    dr_photometry.updated_at
   FROM public.dr_photometry
  WHERE ((public.dr_browse_tag() IS NOT NULL) AND (dr_photometry.tag = public.dr_browse_tag()));

REVOKE ALL ON TABLE "public"."cat_photometry" FROM "anon";

CREATE VIEW "public"."cat_spectra" WITH (security_invoker=true) AS  SELECT spectra.id,
    spectra.grating,
    spectra.fits_path,
    spectra.signal_to_noise,
    spectra.created_at,
    spectra.target_id,
    spectra.thumbnail_svg_fnu,
    spectra.thumbnail_svg_flambda,
    spectra.file_hash,
    spectra.file_size,
    spectra.exposure_time,
    spectra.crds_context,
    spectra.jwst_version,
    spectra.cfpipe_version,
    spectra.date_obs,
    spectra.dq_flags,
    spectra.redshift_auto,
    spectra.spectrum_id,
    spectra.updated_at,
    spectra.search_text,
    spectra.reduced_at,
    spectra.deploy_status,
    spectra.program_slug,
    spectra.observation,
    spectra.chi2_min,
    spectra.confidence,
    spectra.science_hash
   FROM public.spectra
  WHERE (public.dr_browse_tag() IS NULL)
UNION ALL
 SELECT dr_spectra.id,
    dr_spectra.grating,
    dr_spectra.fits_path,
    dr_spectra.signal_to_noise,
    dr_spectra.created_at,
    dr_spectra.target_id,
    dr_spectra.thumbnail_svg_fnu,
    dr_spectra.thumbnail_svg_flambda,
    dr_spectra.file_hash,
    dr_spectra.file_size,
    dr_spectra.exposure_time,
    dr_spectra.crds_context,
    dr_spectra.jwst_version,
    dr_spectra.cfpipe_version,
    dr_spectra.date_obs,
    dr_spectra.dq_flags,
    dr_spectra.redshift_auto,
    dr_spectra.spectrum_id,
    dr_spectra.updated_at,
    dr_spectra.search_text,
    dr_spectra.reduced_at,
    dr_spectra.deploy_status,
    dr_spectra.program_slug,
    dr_spectra.observation,
    dr_spectra.chi2_min,
    dr_spectra.confidence,
    dr_spectra.science_hash
   FROM public.dr_spectra
  WHERE ((public.dr_browse_tag() IS NOT NULL) AND (dr_spectra.tag = public.dr_browse_tag()));

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "anon";

CREATE VIEW "public"."cat_spectrum_lines" WITH (security_invoker=true) AS  SELECT spectrum_lines.spectrum_id,
    spectrum_lines.line,
    spectrum_lines.component,
    spectrum_lines.wave_rest,
    spectrum_lines.flux,
    spectrum_lines.flux_err,
    spectrum_lines.snr,
    spectrum_lines.ew_rest,
    spectrum_lines.ew_rest_err,
    spectrum_lines.flags,
    spectrum_lines.blend_into
   FROM public.spectrum_lines
  WHERE (public.dr_browse_tag() IS NULL)
UNION ALL
 SELECT dr_spectrum_lines.spectrum_id,
    dr_spectrum_lines.line,
    dr_spectrum_lines.component,
    dr_spectrum_lines.wave_rest,
    dr_spectrum_lines.flux,
    dr_spectrum_lines.flux_err,
    dr_spectrum_lines.snr,
    dr_spectrum_lines.ew_rest,
    dr_spectrum_lines.ew_rest_err,
    dr_spectrum_lines.flags,
    dr_spectrum_lines.blend_into
   FROM public.dr_spectrum_lines
  WHERE ((public.dr_browse_tag() IS NOT NULL) AND (dr_spectrum_lines.tag = public.dr_browse_tag()));

REVOKE ALL ON TABLE "public"."cat_spectrum_lines" FROM "anon";

CREATE VIEW "public"."cat_targets" WITH (security_invoker=true) AS  SELECT targets.id,
    targets.target_id,
    targets.field,
    targets.ra,
    targets."dec",
    targets.redshift_auto,
    targets.redshift_quality,
    targets.spectral_features,
    targets.dq_flags,
    targets.created_at,
    targets.updated_at,
    targets.redshift_inspected,
    targets.last_inspected_at,
    targets.last_inspected_by,
    targets.max_snr,
    targets.redshift,
    targets.max_exposure_time,
    targets.program_slug,
    targets.observation,
    targets.object_id,
    targets.has_published_spectrum
   FROM public.targets
  WHERE (public.dr_browse_tag() IS NULL)
UNION ALL
 SELECT dr_targets.id,
    dr_targets.target_id,
    dr_targets.field,
    dr_targets.ra,
    dr_targets."dec",
    dr_targets.redshift_auto,
    dr_targets.redshift_quality,
    dr_targets.spectral_features,
    dr_targets.dq_flags,
    dr_targets.created_at,
    dr_targets.updated_at,
    dr_targets.redshift_inspected,
    dr_targets.last_inspected_at,
    dr_targets.last_inspected_by,
    dr_targets.max_snr,
    dr_targets.redshift,
    dr_targets.max_exposure_time,
    dr_targets.program_slug,
    dr_targets.observation,
    dr_targets.object_id,
    dr_targets.has_published_spectrum
   FROM public.dr_targets
  WHERE ((public.dr_browse_tag() IS NOT NULL) AND (dr_targets.tag = public.dr_browse_tag()));

REVOKE ALL ON TABLE "public"."cat_targets" FROM "anon";

CREATE INDEX idx_dr_objects_tag_redshift_quality ON public.dr_objects USING btree (TAG, redshift_quality);

CREATE INDEX idx_dr_spectra_observation ON public.dr_spectra USING btree (observation);

CREATE INDEX idx_dr_spectra_tag_dq_flags ON public.dr_spectra USING btree (TAG, dq_flags)
  WHERE (dq_flags <> 0);

CREATE INDEX idx_dr_spectra_tag_exposure_time ON public.dr_spectra USING btree (TAG, exposure_time DESC NULLS LAST);

CREATE INDEX idx_dr_spectra_tag_grating ON public.dr_spectra USING btree (TAG, grating);

CREATE INDEX idx_dr_spectra_tag_snr ON public.dr_spectra USING btree (TAG, signal_to_noise DESC NULLS LAST);

CREATE INDEX idx_dr_targets_target_id_trgm ON public.dr_targets USING gin (target_id public.gin_trgm_ops);

COMMENT ON FUNCTION "public"."dr_assert_live"() IS 'Raises PT400 when the request browses a data release (X-Campfire-DR). Called first by every sync stream: they mirror the live catalog only.';

COMMENT ON FUNCTION "public"."dr_browse_tag"() IS 'The data release the current request browses (X-Campfire-DR, set by dr_request_context), or NULL for the live catalog. Transaction-local, so pooled connections never carry it over.';

COMMENT ON FUNCTION "public"."dr_purge_release"(text) IS 'Delete an UNPUBLISHED data release and all its frozen rows (P4a; `campfire dr purge`). Admin JWT or service_role claims (dr_require_admin): a bare postgres superuser session is refused; from psql, set request.jwt.claims to {"role":"service_role"} first. Refuses published / withdrawn tags and a freeze that is still running. Archive bytes already copied for the tag are left in place (dedup by hash; gc is the retention path).';

COMMENT ON FUNCTION "public"."dr_request_context"() IS 'PostgREST pre-request hook: X-Campfire-DR selects the data release the request browses, stored transaction-locally in campfire.dr_tag for the cat_* views. No header, no effect. Header name case-insensitive (PostgREST lower-cases request.headers), tag case-sensitive; duplicate headers fail closed (the merged value matches no tag). Readers: published tags only; admins: every state; link accounts: refused; anon: a published tag passes the hook but every grant denies; service role: any release past its freeze (not draft / freezing / failed). Anything else is PT404.';

COMMENT ON VIEW "public"."cat_fields" IS 'Catalog source view: fields for the live catalog, dr_fields rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_filter_options" IS 'Catalog source view: mv_filter_options for the live catalog, the browsed release''s dr_filter_options row otherwise.';

COMMENT ON VIEW "public"."cat_line_fits" IS 'Catalog source view: spectrum_line_fits for the live catalog, dr_line_fits rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_nircam_images" IS 'Catalog source view: nircam_images for the live catalog, dr_nircam_images rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_objects" IS 'Catalog source view: objects for the live catalog, dr_objects rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_photometry" IS 'Catalog source view: object_photometry for the live catalog, dr_photometry rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_photometry_bands" IS 'Catalog source view: object_photometry_bands for the live catalog, dr_photometry_bands rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_spectra" IS 'Catalog source view: spectra for the live catalog, dr_spectra rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_spectrum_lines" IS 'Catalog source view: spectrum_lines for the live catalog, dr_spectrum_lines rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_targets" IS 'Catalog source view: targets for the live catalog, dr_targets rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

GRANT EXECUTE ON FUNCTION "public"."dr_assert_live"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_assert_live"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_assert_live"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_assert_live"() TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."dr_browse_tag"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_browse_tag"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_browse_tag"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_browse_tag"() TO "service_role";

REVOKE ALL ON FUNCTION "public"."dr_request_context"() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION "public"."dr_request_context"() TO "anon", "authenticated", "authenticator";

REVOKE ALL ON FUNCTION "public"."dr_request_context"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_request_context"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_request_context"() TO "service_role";

REVOKE ALL ON TABLE "public"."cat_fields" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_fields" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_fields" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_fields" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_fields" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_fields" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_filter_options" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_filter_options" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_filter_options" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_filter_options" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_filter_options" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_filter_options" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_line_fits" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_line_fits" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_line_fits" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_line_fits" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_line_fits" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_line_fits" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_nircam_images" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_nircam_images" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_nircam_images" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_nircam_images" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_nircam_images" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_nircam_images" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_objects" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_objects" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_objects" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_objects" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_objects" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_objects" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_photometry" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_photometry" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_photometry" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_photometry" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_photometry" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_photometry" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_photometry_bands" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_photometry_bands" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_photometry_bands" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_photometry_bands" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_photometry_bands" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_photometry_bands" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_spectra" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_spectra" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_spectra" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_spectrum_lines" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_spectrum_lines" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_spectrum_lines" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_spectrum_lines" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_spectrum_lines" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_spectrum_lines" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_targets" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_targets" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_targets" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_targets" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_targets" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_targets" TO "service_role";
