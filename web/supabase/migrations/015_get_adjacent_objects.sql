-- Migration 015: Add lightweight RPC for adjacent object lookup
-- This function efficiently finds prev/next object IDs using window functions
-- Much faster than fetching all objects - only returns 4 values

DROP FUNCTION IF EXISTS get_adjacent_objects;

CREATE OR REPLACE FUNCTION get_adjacent_objects(
  p_current_object_id TEXT,
  -- Access control: array of program IDs the user can access
  p_program_ids INTEGER[],
  -- Optional program filter (intersection with accessible programs)
  p_filter_programs INTEGER[] DEFAULT NULL,
  -- Standard filters
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  -- Bitmask filters (combined masks - any matching bit returns true)
  p_spectral_features INTEGER DEFAULT NULL,
  p_object_flags INTEGER DEFAULT NULL,
  p_dq_flags INTEGER DEFAULT NULL,
  -- Other filters
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  -- Coordinate search (spatial filter)
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  -- Sorting
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc'
)
RETURNS TABLE(
  prev_object_id TEXT,
  next_object_id TEXT,
  current_index BIGINT,
  total_count BIGINT
) AS $$
DECLARE
  v_filtered_program_ids INTEGER[];
  v_grating_object_ids TEXT[];
BEGIN
  -- Validate sort direction
  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  -- Validate sort column (whitelist for security)
  IF p_sort_column NOT IN ('object_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr') THEN
    p_sort_column := 'object_id';
  END IF;

  -- Determine which programs to query (intersection of accessible and filtered)
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_ids)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_ids;
  ELSE
    v_filtered_program_ids := p_program_ids;
  END IF;

  -- If no programs to query, return null result
  IF v_filtered_program_ids IS NULL OR array_length(v_filtered_program_ids, 1) IS NULL THEN
    RETURN QUERY SELECT NULL::TEXT, NULL::TEXT, 0::BIGINT, 0::BIGINT;
    RETURN;
  END IF;

  -- If grating filter is active, get object_ids that have matching spectra
  IF p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0 THEN
    SELECT ARRAY(
      SELECT DISTINCT s.object_id
      FROM spectra s
      WHERE s.grating = ANY(p_gratings)
    ) INTO v_grating_object_ids;

    IF v_grating_object_ids IS NULL OR array_length(v_grating_object_ids, 1) IS NULL THEN
      RETURN QUERY SELECT NULL::TEXT, NULL::TEXT, 0::BIGINT, 0::BIGINT;
      RETURN;
    END IF;
  END IF;

  -- Use window functions to find position and neighbors efficiently
  RETURN QUERY
  WITH filtered_objects AS (
    SELECT
      o.object_id,
      o.field,
      o.observation,
      o.ra,
      o.dec,
      o.redshift,
      o.redshift_quality,
      o.max_snr
    FROM objects o
    WHERE
      -- Program access control
      o.program_id = ANY(v_filtered_program_ids)
      -- Grating filter (via pre-queried object IDs)
      AND (v_grating_object_ids IS NULL OR o.object_id = ANY(v_grating_object_ids))
      -- Field filter
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      -- Observation filter
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observation = ANY(p_observations))
      -- Redshift quality filter
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      -- Redshift range filters
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      -- Bitmask filters
      AND (p_spectral_features IS NULL OR (o.spectral_features & p_spectral_features) > 0)
      AND (p_object_flags IS NULL OR (o.object_flags & p_object_flags) > 0)
      AND (p_dq_flags IS NULL OR (o.dq_flags & p_dq_flags) > 0)
      -- Search filter
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
      -- Coordinate search (spatial filter)
      AND (
        p_coord_ra IS NULL OR p_coord_dec IS NULL OR p_radius_degrees IS NULL
        OR (
          2 * asin(sqrt(
            pow(sin(radians(p_coord_dec - o.dec) / 2), 2) +
            cos(radians(p_coord_dec)) * cos(radians(o.dec)) *
            pow(sin(radians(p_coord_ra - o.ra) / 2), 2)
          )) <= radians(p_radius_degrees)
        )
      )
      -- Inspected only filter
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
  ),
  sorted_with_row_num AS (
    SELECT
      fo.object_id,
      ROW_NUMBER() OVER (
        ORDER BY
          CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN fo.object_id END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN fo.object_id END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN fo.field END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN fo.field END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN fo.observation END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN fo.observation END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN fo.ra END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN fo.ra END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN fo.dec END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN fo.dec END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN fo.redshift END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN fo.redshift END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN fo.redshift_quality END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN fo.redshift_quality END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN fo.max_snr END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN fo.max_snr END DESC NULLS LAST,
          fo.object_id ASC
      ) as rn
    FROM filtered_objects fo
  ),
  with_neighbors AS (
    SELECT
      s.object_id,
      s.rn,
      LAG(s.object_id) OVER (ORDER BY s.rn) as prev_id,
      LEAD(s.object_id) OVER (ORDER BY s.rn) as next_id,
      COUNT(*) OVER () as total
    FROM sorted_with_row_num s
  )
  SELECT
    wn.prev_id as prev_object_id,
    wn.next_id as next_object_id,
    wn.rn as current_index,
    wn.total as total_count
  FROM with_neighbors wn
  WHERE wn.object_id = p_current_object_id;
END;
$$ LANGUAGE plpgsql STABLE;

-- Grant execute permission to authenticated users
GRANT EXECUTE ON FUNCTION get_adjacent_objects TO authenticated;

-- Add comment for documentation
COMMENT ON FUNCTION get_adjacent_objects IS
'Lightweight function to find adjacent object IDs for detail page navigation.
Uses window functions (LAG/LEAD) to efficiently find prev/next without fetching all data.
Returns: prev_object_id, next_object_id, current_index (1-based), total_count.';
