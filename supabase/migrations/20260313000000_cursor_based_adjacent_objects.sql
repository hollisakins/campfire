-- Rewrite get_adjacent_objects as a standalone cursor-based function.
--
-- Previously, this function called get_filtered_object_ids which sorted ALL
-- matching rows (O(N log N) at 30k objects) just to look up 3 positions.
--
-- The new approach materializes the filtered set once, then uses cursor
-- queries (LIMIT 1) for prev/next and COUNT queries for position/total.
-- This replaces O(N log N) full sort with ~4 O(N) scans over a materialized
-- CTE. At N=30k: ~3x improvement.

DROP FUNCTION IF EXISTS public.get_adjacent_objects;

CREATE OR REPLACE FUNCTION public.get_adjacent_objects(
  p_current_object_id TEXT,
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
  p_sort_direction TEXT DEFAULT 'asc'
)
RETURNS TABLE(
  prev_object_id TEXT,
  next_object_id TEXT,
  current_index BIGINT,
  total_count BIGINT
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
  v_sort_is_text BOOLEAN;
  -- Backward-compat flag normalization
  v_sf_include_any INTEGER;
  v_sf_include_all INTEGER;
  v_sf_exclude INTEGER;
  v_of_include_any INTEGER;
  v_of_include_all INTEGER;
  v_of_exclude INTEGER;
  v_dq_include_any INTEGER;
  v_dq_include_all INTEGER;
  v_dq_exclude INTEGER;
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

  -- Coord search always sorts by distance ASC
  IF v_coord_search_active THEN
    p_sort_column := 'distance';
    p_sort_direction := 'asc';
  END IF;

  v_sort_is_text := p_sort_column IN ('object_id', 'field', 'observation');

  -- Backward-compat: normalize old single-integer flag params
  v_sf_include_any := COALESCE(p_spectral_features_include_any, p_spectral_features);
  v_sf_include_all := p_spectral_features_include_all;
  v_sf_exclude := p_spectral_features_exclude;
  v_of_include_any := COALESCE(p_object_flags_include_any, p_object_flags);
  v_of_include_all := p_object_flags_include_all;
  v_of_exclude := p_object_flags_exclude;
  v_dq_include_any := COALESCE(p_dq_flags_include_any, p_dq_flags);
  v_dq_include_all := p_dq_flags_include_all;
  v_dq_exclude := p_dq_flags_exclude;

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
    RETURN QUERY SELECT NULL::TEXT, NULL::TEXT, 0::BIGINT, 0::BIGINT;
    RETURN;
  END IF;

  RETURN QUERY
  WITH filtered_objects AS MATERIALIZED (
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
      -- Grating filter
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
      -- Spectral features filter (three modes)
      AND (v_sf_include_any IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_include_any) != 0)
      AND (v_sf_include_all IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_include_all) = v_sf_include_all)
      AND (v_sf_exclude IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_exclude) = 0)
      -- Object flags filter (three modes)
      AND (v_of_include_any IS NULL OR (COALESCE(o.object_flags, 0) & v_of_include_any) != 0)
      AND (v_of_include_all IS NULL OR (COALESCE(o.object_flags, 0) & v_of_include_all) = v_of_include_all)
      AND (v_of_exclude IS NULL OR (COALESCE(o.object_flags, 0) & v_of_exclude) = 0)
      -- DQ flags filter (three modes)
      AND (v_dq_include_any IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_include_any) != 0)
      AND (v_dq_include_all IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_include_all) = v_dq_include_all)
      AND (v_dq_exclude IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_exclude) = 0)
      -- Object ID text search
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
      -- Inspected only filter
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      -- Comment search filter
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
      -- Coordinate search bounding box pre-filter
      AND (
        NOT v_coord_search_active
        OR (
          o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        )
      )
  ),
  distance_filtered AS MATERIALIZED (
    SELECT
      fo.*,
      -- Normalize sort value into typed columns for cursor comparisons
      CASE p_sort_column
        WHEN 'object_id' THEN fo.object_id
        WHEN 'field' THEN fo.field
        WHEN 'observation' THEN fo.observation
        ELSE NULL
      END AS sort_text,
      CASE p_sort_column
        WHEN 'ra' THEN fo.ra
        WHEN 'dec' THEN fo.dec
        WHEN 'redshift' THEN fo.redshift::DOUBLE PRECISION
        WHEN 'redshift_quality' THEN fo.redshift_quality::DOUBLE PRECISION
        WHEN 'max_snr' THEN fo.max_snr
        WHEN 'max_exposure_time' THEN fo.max_exposure_time
        WHEN 'distance' THEN fo.distance
        ELSE NULL
      END AS sort_num
    FROM filtered_objects fo
    WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees
  ),
  current_obj AS (
    SELECT df.sort_text, df.sort_num, df.object_id
    FROM distance_filtered df
    WHERE df.object_id = p_current_object_id
  )
  SELECT
    -- prev: last row that sorts before current (reversed ORDER BY, LIMIT 1)
    (SELECT df.object_id
     FROM distance_filtered df, current_obj c
     WHERE
       CASE WHEN v_sort_is_text THEN
         (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text < c.sort_text
               ELSE df.sort_text > c.sort_text END)
         OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.object_id < c.object_id)
         OR (df.sort_text IS NOT NULL AND c.sort_text IS NULL)
       ELSE
         (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num < c.sort_num
               ELSE df.sort_num > c.sort_num END)
         OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.object_id < c.object_id)
         OR (df.sort_num IS NOT NULL AND c.sort_num IS NULL)
       END
     ORDER BY
       CASE WHEN v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_text END DESC NULLS FIRST,
       CASE WHEN v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_text END ASC NULLS FIRST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_num END DESC NULLS FIRST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_num END ASC NULLS FIRST,
       df.object_id DESC
     LIMIT 1
    ) AS prev_object_id,

    -- next: first row that sorts after current (forward ORDER BY, LIMIT 1)
    (SELECT df.object_id
     FROM distance_filtered df, current_obj c
     WHERE
       CASE WHEN v_sort_is_text THEN
         (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text > c.sort_text
               ELSE df.sort_text < c.sort_text END)
         OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.object_id > c.object_id)
         OR (c.sort_text IS NOT NULL AND df.sort_text IS NULL)
       ELSE
         (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num > c.sort_num
               ELSE df.sort_num < c.sort_num END)
         OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.object_id > c.object_id)
         OR (c.sort_num IS NOT NULL AND df.sort_num IS NULL)
       END
     ORDER BY
       CASE WHEN v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_text END ASC NULLS LAST,
       CASE WHEN v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_text END DESC NULLS LAST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_num END ASC NULLS LAST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_num END DESC NULLS LAST,
       df.object_id ASC
     LIMIT 1
    ) AS next_object_id,

    -- current_index: count of rows before current + 1 (0 if current not found)
    CASE WHEN EXISTS (SELECT 1 FROM current_obj)
      THEN (
        SELECT COUNT(*) + 1
        FROM distance_filtered df, current_obj c
        WHERE
          CASE WHEN v_sort_is_text THEN
            (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text < c.sort_text
                  ELSE df.sort_text > c.sort_text END)
            OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.object_id < c.object_id)
            OR (df.sort_text IS NOT NULL AND c.sort_text IS NULL)
          ELSE
            (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num < c.sort_num
                  ELSE df.sort_num > c.sort_num END)
            OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.object_id < c.object_id)
            OR (df.sort_num IS NOT NULL AND c.sort_num IS NULL)
          END
      )::BIGINT
      ELSE 0::BIGINT
    END AS current_index,

    -- total_count
    (SELECT COUNT(*) FROM distance_filtered)::BIGINT AS total_count;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_adjacent_objects TO authenticated;
