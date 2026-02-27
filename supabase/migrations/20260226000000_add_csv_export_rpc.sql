-- Lightweight RPC for CSV export
-- Returns flat rows with only the columns needed for CSV download.
-- Avoids the expensive JSONB object building + nested spectra subqueries
-- that cause statement timeouts on large result sets (7k+ objects).

CREATE OR REPLACE FUNCTION public.get_csv_export(
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
  p_sort_direction TEXT DEFAULT 'asc'
)
RETURNS TABLE(
  object_id TEXT,
  field TEXT,
  ra DOUBLE PRECISION,
  "dec" DOUBLE PRECISION,
  redshift DOUBLE PRECISION,
  redshift_quality INTEGER,
  max_snr DOUBLE PRECISION,
  max_exposure_time DOUBLE PRECISION,
  num_gratings INTEGER,
  program_id INTEGER,
  program_name TEXT,
  last_inspected_at TIMESTAMPTZ,
  last_inspected_by UUID,
  distance DOUBLE PRECISION
)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_ids INTEGER[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
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

  RETURN QUERY
  WITH filtered_objects AS (
    SELECT
      o.object_id,
      o.field,
      o.ra,
      o.dec,
      o.redshift,
      o.redshift_quality,
      o.max_snr,
      o.max_exposure_time,
      (SELECT COUNT(*)::INTEGER FROM spectra s WHERE s.object_id = o.object_id) AS num_gratings,
      o.program_id,
      o.last_inspected_at,
      o.last_inspected_by,
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
    df.field,
    df.ra,
    df.dec,
    df.redshift,
    df.redshift_quality,
    df.max_snr,
    df.max_exposure_time,
    df.num_gratings,
    df.program_id,
    pr.program_name,
    df.last_inspected_at,
    df.last_inspected_by,
    df.distance
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.program_id = df.program_id
  ORDER BY
    CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
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
    df.object_id ASC;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_csv_export TO authenticated;
