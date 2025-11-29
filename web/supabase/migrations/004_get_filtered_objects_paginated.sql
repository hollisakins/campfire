-- Migration: Add RPC function for server-side filtering, sorting, and pagination of objects
-- This function handles all filtering including bitmask operations (which aren't
-- supported by Supabase's client-side filters) and returns paginated results.
--
-- Supports adaptive sorting: when fetching all data (pageSize >= total), client can
-- sort locally. When paginated, server-side sorting ensures correct results.

DROP FUNCTION IF EXISTS get_filtered_objects_paginated;

CREATE OR REPLACE FUNCTION get_filtered_objects_paginated(
  -- Access control: array of program IDs the user can access
  p_program_ids INTEGER[],
  -- Optional program filter (intersection with accessible programs)
  p_filter_programs INTEGER[] DEFAULT NULL,
  -- Standard filters
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
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
  p_sort_direction TEXT DEFAULT 'asc',
  -- Pagination
  p_page INTEGER DEFAULT 1,
  p_page_size INTEGER DEFAULT 50
)
RETURNS TABLE(
  objects JSONB,
  total_count BIGINT,
  page INTEGER,
  page_size INTEGER
) AS $$
DECLARE
  v_offset INTEGER;
  v_filtered_program_ids INTEGER[];
  v_grating_object_ids TEXT[];
BEGIN
  -- Calculate offset
  v_offset := (p_page - 1) * p_page_size;

  -- Validate sort direction
  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  -- Validate sort column (whitelist for security)
  IF p_sort_column NOT IN ('object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality') THEN
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

  -- If no programs to query, return empty result
  IF v_filtered_program_ids IS NULL OR array_length(v_filtered_program_ids, 1) IS NULL THEN
    RETURN QUERY SELECT
      '[]'::JSONB as objects,
      0::BIGINT as total_count,
      p_page as page,
      p_page_size as page_size;
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
      RETURN QUERY SELECT
        '[]'::JSONB as objects,
        0::BIGINT as total_count,
        p_page as page,
        p_page_size as page_size;
      RETURN;
    END IF;
  END IF;

  -- Return paginated results with count
  RETURN QUERY
  WITH filtered_objects AS (
    SELECT o.*
    FROM objects o
    WHERE
      -- Program access control
      o.program_id = ANY(v_filtered_program_ids)
      -- Grating filter (via pre-queried object IDs)
      AND (v_grating_object_ids IS NULL OR o.object_id = ANY(v_grating_object_ids))
      -- Field filter
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      -- Redshift quality filter
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality =
ANY(p_redshift_quality))
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
  counted AS (
    SELECT COUNT(*) as cnt FROM filtered_objects
  ),
  sorted_objects AS (
    SELECT fo.*
    FROM filtered_objects fo
    ORDER BY
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN fo.object_id END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN fo.object_id END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN fo.field END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN fo.field END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN fo.ra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN fo.ra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN fo.dec END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN fo.dec END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN fo.redshift END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN fo.redshift END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN fo.redshift_quality END ASC
NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN fo.redshift_quality END DESC
  NULLS LAST,
      fo.object_id ASC
  ),
  paginated AS (
    SELECT so.*
    FROM sorted_objects so
    LIMIT p_page_size
    OFFSET v_offset
  ),
  with_relations AS (
    SELECT
      jsonb_build_object(
        'id', p.id,
        'object_id', p.object_id,
        'program_id', p.program_id,
        'field', p.field,
        'ra', p.ra,
        'dec', p.dec,
        'redshift', p.redshift,
        'redshift_auto', p.redshift_auto,
        'redshift_inspected', p.redshift_inspected,
        'redshift_quality', p.redshift_quality,
        'spectral_features', p.spectral_features,
        'object_flags', p.object_flags,
        'dq_flags', p.dq_flags,
        'last_inspected_at', p.last_inspected_at,
        'last_inspected_by', p.last_inspected_by,
        'created_at', p.created_at,
        'updated_at', p.updated_at,
        'distance', CASE
          WHEN p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL THEN
            degrees(2 * asin(sqrt(
              pow(sin(radians(p_coord_dec - p.dec) / 2), 2) +
              cos(radians(p_coord_dec)) * cos(radians(p.dec)) *
              pow(sin(radians(p_coord_ra - p.ra) / 2), 2)
            )))
          ELSE NULL
        END,
        'program_name', pr.program_name,
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
                'created_at', s.created_at
              )
              ORDER BY s.grating
            )
            FROM spectra s
            WHERE s.object_id = p.object_id
              AND (p_gratings IS NULL OR array_length(p_gratings, 1) IS NULL OR s.grating = ANY(p_gratings))
          ),
          '[]'::jsonb
        )
      ) as obj
    FROM paginated p
    LEFT JOIN programs pr ON pr.program_id = p.program_id
  )
  SELECT
    COALESCE(
      (
        SELECT jsonb_agg(wr.obj ORDER BY
          CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN wr.obj->>'object_id' END ASC
NULLS LAST,
          CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN wr.obj->>'object_id' END DESC
NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN wr.obj->>'field' END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN wr.obj->>'field' END DESC NULLS
LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN (wr.obj->>'ra')::numeric END ASC NULLS
LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN (wr.obj->>'ra')::numeric END DESC
NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN (wr.obj->>'dec')::numeric END ASC
NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN (wr.obj->>'dec')::numeric END DESC
NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN (wr.obj->>'redshift')::numeric
END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN (wr.obj->>'redshift')::numeric
END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN
(wr.obj->>'redshift_quality')::integer END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN
(wr.obj->>'redshift_quality')::integer END DESC NULLS LAST
        )
        FROM with_relations wr
      ),
      '[]'::jsonb
    ) as objects,
    (SELECT cnt FROM counted) as total_count,
    p_page as page,
    p_page_size as page_size;
END;
$$ LANGUAGE plpgsql STABLE;

-- Grant execute permission to authenticated users
GRANT EXECUTE ON FUNCTION get_filtered_objects_paginated TO authenticated;

-- Add comment for documentation
COMMENT ON FUNCTION get_filtered_objects_paginated IS
'Server-side filtering, sorting, and pagination for the spectra table.
Handles bitmask filters (spectral_features, object_flags, dq_flags) that cannot be done via PostgREST.
Supports dynamic sorting by: object_id, field, ra, dec, redshift, redshift_quality.
Returns objects with nested spectra and program name, along with total count for pagination.
Use with large page_size (e.g., 5000) to fetch all data for client-side sorting when result set is small.';
