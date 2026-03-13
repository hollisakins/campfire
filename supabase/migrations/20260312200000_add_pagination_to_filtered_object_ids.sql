-- Add optional LIMIT/OFFSET pagination to get_filtered_object_ids().
--
-- When p_page/p_page_size are provided, uses a separate COUNT(*) subquery
-- and LIMIT/OFFSET instead of COUNT(*) OVER() + ROW_NUMBER(). This allows
-- PostgreSQL to use top-N heapsort (~O(N) instead of O(N log N)) and emit
-- only the requested page instead of all rows.
--
-- Callers that need all rows (map markers, inspection queue, CSV export,
-- adjacent objects) pass NULL for both params and get existing behavior.

-- =============================================================================
-- 1. Core function: get_filtered_object_ids (add pagination params)
-- =============================================================================

-- Drop the old signature (without p_page/p_page_size) to avoid overloaded functions
DROP FUNCTION IF EXISTS public.get_filtered_object_ids(
  INTEGER[], INTEGER[], TEXT[], TEXT[], TEXT, TEXT[], INTEGER[],
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  DOUBLE PRECISION, DOUBLE PRECISION,
  INTEGER, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER,
  TEXT, BOOLEAN, TEXT, TEXT, UUID,
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  TEXT, TEXT
);

CREATE OR REPLACE FUNCTION public.get_filtered_object_ids(
  p_program_ids INTEGER[],
  p_filter_programs INTEGER[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',
  p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_spectral_features_include_any INTEGER DEFAULT NULL,
  p_spectral_features_include_all INTEGER DEFAULT NULL,
  p_spectral_features_exclude INTEGER DEFAULT NULL,
  p_object_flags_include_any INTEGER DEFAULT NULL,
  p_object_flags_include_all INTEGER DEFAULT NULL,
  p_object_flags_exclude INTEGER DEFAULT NULL,
  p_dq_flags_include_any INTEGER DEFAULT NULL,
  p_dq_flags_include_all INTEGER DEFAULT NULL,
  p_dq_flags_exclude INTEGER DEFAULT NULL,
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc',
  -- New: optional pagination params
  p_page INTEGER DEFAULT NULL,
  p_page_size INTEGER DEFAULT NULL
)
RETURNS TABLE(object_id TEXT, distance DOUBLE PRECISION, row_num BIGINT, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_ids INTEGER[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_paginate BOOLEAN;
  v_offset INTEGER;
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

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN ('object_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr', 'max_exposure_time')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  -- Determine pagination mode
  v_paginate := (p_page IS NOT NULL AND p_page_size IS NOT NULL);
  IF v_paginate THEN
    v_offset := (p_page - 1) * p_page_size;
  END IF;

  -- Determine which programs to query
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_ids)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_ids;
  ELSE
    v_filtered_program_ids := p_program_ids;
  END IF;

  IF v_filtered_program_ids IS NULL OR array_length(v_filtered_program_ids, 1) IS NULL THEN
    RETURN;
  END IF;

  -- =========================================================================
  -- Paginated path: LIMIT/OFFSET with separate COUNT(*)
  -- Enables top-N heapsort and emits only the requested page.
  -- =========================================================================
  IF v_paginate THEN
    RETURN QUERY
    WITH filtered_objects AS (
      SELECT
        o.object_id,
        CASE
          WHEN v_coord_search_active THEN
            2 * DEGREES(ASIN(SQRT(
              POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
              COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
              POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
            )))
          ELSE NULL
        END AS distance,
        o.field, o.observation, o.ra, o.dec, o.redshift, o.redshift_quality, o.max_snr, o.max_exposure_time
      FROM objects o
      WHERE
        o.program_id = ANY(v_filtered_program_ids)
        AND (
          NOT v_grating_filter_active
          OR (v_gratings_mode = 'any' AND EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
          ))
          OR (v_gratings_mode = 'all' AND (
            SELECT COUNT(DISTINCT gs.grating) FROM spectra gs
            WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
          ) = array_length(p_gratings, 1))
          OR (v_gratings_mode = 'none' AND NOT EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
          ))
        )
        AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
        AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observation = ANY(p_observations))
        AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
        AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
        AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
        AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
        AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
        AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
        AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
        AND (p_spectral_features_include_any IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_include_any) != 0)
        AND (p_spectral_features_include_all IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
        AND (p_spectral_features_exclude IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_exclude) = 0)
        AND (p_object_flags_include_any IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_include_any) != 0)
        AND (p_object_flags_include_all IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
        AND (p_object_flags_exclude IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_exclude) = 0)
        AND (p_dq_flags_include_any IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_include_any) != 0)
        AND (p_dq_flags_include_all IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
        AND (p_dq_flags_exclude IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_exclude) = 0)
        AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
        AND (
          p_inspected_only IS NULL
          OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
          OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
        )
        AND (
          NOT v_comment_search_active
          OR EXISTS (
            SELECT 1 FROM comments c
            WHERE c.object_id = o.id
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
            o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
            AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
          )
        )
    ),
    distance_filtered AS (
      SELECT fo.*
      FROM filtered_objects fo
      WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees
    )
    SELECT
      df.object_id,
      df.distance,
      ROW_NUMBER() OVER () AS row_num,
      (SELECT COUNT(*) FROM distance_filtered)::BIGINT AS total_count
    FROM distance_filtered df
    ORDER BY
      CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
      df.object_id ASC
    LIMIT p_page_size OFFSET v_offset;

  -- =========================================================================
  -- Full path: existing behavior (all rows with ROW_NUMBER + COUNT(*) OVER)
  -- Used by: map markers, inspection queue, CSV export, adjacent objects
  -- =========================================================================
  ELSE
    RETURN QUERY
    WITH filtered_objects AS (
      SELECT
        o.object_id,
        CASE
          WHEN v_coord_search_active THEN
            2 * DEGREES(ASIN(SQRT(
              POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
              COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
              POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
            )))
          ELSE NULL
        END AS distance,
        o.field, o.observation, o.ra, o.dec, o.redshift, o.redshift_quality, o.max_snr, o.max_exposure_time
      FROM objects o
      WHERE
        o.program_id = ANY(v_filtered_program_ids)
        AND (
          NOT v_grating_filter_active
          OR (v_gratings_mode = 'any' AND EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
          ))
          OR (v_gratings_mode = 'all' AND (
            SELECT COUNT(DISTINCT gs.grating) FROM spectra gs
            WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
          ) = array_length(p_gratings, 1))
          OR (v_gratings_mode = 'none' AND NOT EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.object_id = o.object_id AND gs.grating = ANY(p_gratings)
          ))
        )
        AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
        AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observation = ANY(p_observations))
        AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
        AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
        AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
        AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
        AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
        AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
        AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
        AND (p_spectral_features_include_any IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_include_any) != 0)
        AND (p_spectral_features_include_all IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
        AND (p_spectral_features_exclude IS NULL OR (COALESCE(o.spectral_features, 0) & p_spectral_features_exclude) = 0)
        AND (p_object_flags_include_any IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_include_any) != 0)
        AND (p_object_flags_include_all IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
        AND (p_object_flags_exclude IS NULL OR (COALESCE(o.object_flags, 0) & p_object_flags_exclude) = 0)
        AND (p_dq_flags_include_any IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_include_any) != 0)
        AND (p_dq_flags_include_all IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
        AND (p_dq_flags_exclude IS NULL OR (COALESCE(o.dq_flags, 0) & p_dq_flags_exclude) = 0)
        AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
        AND (
          p_inspected_only IS NULL
          OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
          OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
        )
        AND (
          NOT v_comment_search_active
          OR EXISTS (
            SELECT 1 FROM comments c
            WHERE c.object_id = o.id
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
            o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
            AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
          )
        )
    ),
    distance_filtered AS (
      SELECT fo.*
      FROM filtered_objects fo
      WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees
    )
    SELECT
      df.object_id,
      df.distance,
      ROW_NUMBER() OVER (
        ORDER BY
          CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
          df.object_id ASC
      ) AS row_num,
      COUNT(*) OVER () AS total_count
    FROM distance_filtered df;
  END IF;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_filtered_object_ids TO authenticated;


-- =============================================================================
-- 2. Wrapper: get_filtered_objects_paginated (push pagination into core function)
-- =============================================================================
DROP FUNCTION IF EXISTS public.get_filtered_objects_paginated;

CREATE OR REPLACE FUNCTION public.get_filtered_objects_paginated(
  p_program_ids INTEGER[],
  p_filter_programs INTEGER[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',
  p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_min DOUBLE PRECISION DEFAULT NULL,
  p_max_exposure_time_max DOUBLE PRECISION DEFAULT NULL,
  p_spectral_features INTEGER DEFAULT NULL,
  p_object_flags INTEGER DEFAULT NULL,
  p_dq_flags INTEGER DEFAULT NULL,
  p_spectral_features_include_any INTEGER DEFAULT NULL,
  p_spectral_features_include_all INTEGER DEFAULT NULL,
  p_spectral_features_exclude INTEGER DEFAULT NULL,
  p_object_flags_include_any INTEGER DEFAULT NULL,
  p_object_flags_include_all INTEGER DEFAULT NULL,
  p_object_flags_exclude INTEGER DEFAULT NULL,
  p_dq_flags_include_any INTEGER DEFAULT NULL,
  p_dq_flags_include_all INTEGER DEFAULT NULL,
  p_dq_flags_exclude INTEGER DEFAULT NULL,
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,
  p_comment_user_id UUID DEFAULT NULL,
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_page INTEGER DEFAULT 1,
  p_page_size INTEGER DEFAULT 50,
  p_include_thumbnails BOOLEAN DEFAULT false
)
RETURNS TABLE(objects JSONB, total_count BIGINT, page INTEGER, page_size INTEGER)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_sf_include_any INTEGER;
  v_sf_include_all INTEGER;
  v_sf_exclude INTEGER;
  v_of_include_any INTEGER;
  v_of_include_all INTEGER;
  v_of_exclude INTEGER;
  v_dq_include_any INTEGER;
  v_dq_include_all INTEGER;
  v_dq_exclude INTEGER;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_coord_search_active BOOLEAN;
BEGIN
  -- Backward-compat: normalize old single-integer flag params into new _include_any
  v_sf_include_any := COALESCE(p_spectral_features_include_any, p_spectral_features);
  v_sf_include_all := p_spectral_features_include_all;
  v_sf_exclude := p_spectral_features_exclude;
  v_of_include_any := COALESCE(p_object_flags_include_any, p_object_flags);
  v_of_include_all := p_object_flags_include_all;
  v_of_exclude := p_object_flags_exclude;
  v_dq_include_any := COALESCE(p_dq_flags_include_any, p_dq_flags);
  v_dq_include_all := p_dq_flags_include_all;
  v_dq_exclude := p_dq_flags_exclude;

  -- Need these for spectra subquery filtering in JSONB output
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);

  RETURN QUERY
  WITH ids AS (
    SELECT *
    FROM public.get_filtered_object_ids(
      p_program_ids, p_filter_programs, p_fields, p_gratings, p_gratings_mode,
      p_observations, p_redshift_quality, p_redshift_min, p_redshift_max,
      p_max_snr_min, p_max_snr_max, p_max_exposure_time_min, p_max_exposure_time_max,
      v_sf_include_any, v_sf_include_all, v_sf_exclude,
      v_of_include_any, v_of_include_all, v_of_exclude,
      v_dq_include_any, v_dq_include_all, v_dq_exclude,
      p_search, p_inspected_only, p_comment_search, p_comment_search_scope, p_comment_user_id,
      p_coord_ra, p_coord_dec, p_radius_degrees,
      p_sort_column, p_sort_direction,
      p_page, p_page_size
    )
  ),
  with_relations AS (
    SELECT
      jsonb_build_object(
        'id', o.id,
        'object_id', o.object_id,
        'program_id', o.program_id,
        'field', o.field,
        'observation', o.observation,
        'ra', o.ra,
        'dec', o.dec,
        'redshift', o.redshift,
        'redshift_auto', o.redshift_auto,
        'redshift_inspected', o.redshift_inspected,
        'redshift_quality', o.redshift_quality,
        'spectral_features', o.spectral_features,
        'object_flags', o.object_flags,
        'dq_flags', o.dq_flags,
        'max_snr', o.max_snr,
        'max_exposure_time', o.max_exposure_time,
        'last_inspected_at', o.last_inspected_at,
        'last_inspected_by', o.last_inspected_by,
        'created_at', o.created_at,
        'updated_at', o.updated_at,
        'program_name', pr.program_name,
        'distance', CASE WHEN v_coord_search_active THEN i.distance ELSE NULL END,
        'spectra', COALESCE(
          (
            SELECT jsonb_agg(
              jsonb_build_object(
                'id', s.id,
                'object_id', s.object_id,
                'grating', s.grating,
                'fits_path', s.fits_path,
                'reduction_version', s.reduction_version,
                'signal_to_noise', s.signal_to_noise,
                'created_at', s.created_at,
                'thumbnail_svg_fnu', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_fnu ELSE NULL END,
                'thumbnail_svg_flambda', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_flambda ELSE NULL END
              )
              ORDER BY s.grating
            )
            FROM spectra s
            WHERE s.object_id = o.object_id
              AND (NOT v_grating_filter_active OR v_gratings_mode = 'none' OR s.grating = ANY(p_gratings))
          ),
          '[]'::jsonb
        )
      ) as obj,
      i.row_num
    FROM ids i
    JOIN objects o ON o.object_id = i.object_id
    LEFT JOIN programs pr ON pr.program_id = o.program_id
  )
  SELECT
    COALESCE(
      (SELECT jsonb_agg(wr.obj ORDER BY wr.row_num) FROM with_relations wr),
      '[]'::jsonb
    ) as objects,
    COALESCE((SELECT i.total_count FROM ids i LIMIT 1), 0::BIGINT) as total_count,
    p_page as page,
    p_page_size as page_size;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_filtered_objects_paginated TO authenticated;
