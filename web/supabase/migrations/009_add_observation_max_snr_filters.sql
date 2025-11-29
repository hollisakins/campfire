-- Migration 009: Add observation and max_snr filters and sorting
-- Extends the RPC function to support:
--   1. Observation filter (categorical filter like field/grating)
--   2. Max S/N range filter (min/max like redshift range)
--   3. Server-side sorting by observation and max_snr columns

DROP FUNCTION IF EXISTS get_filtered_objects_paginated;

CREATE OR REPLACE FUNCTION get_filtered_objects_paginated(
  -- Access control: array of program IDs the user can access
  p_program_ids INTEGER[],
  -- Optional program filter (intersection with accessible programs)
  p_filter_programs INTEGER[] DEFAULT NULL,
  -- Standard filters
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_observations TEXT[] DEFAULT NULL,  -- NEW: Observation filter
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL,  -- NEW: Max S/N min filter
  p_max_snr_max DOUBLE PRECISION DEFAULT NULL,  -- NEW: Max S/N max filter
  -- Bitmask filters (combined masks - any matching bit returns true)
  p_spectral_features INTEGER DEFAULT NULL,
  p_object_flags INTEGER DEFAULT NULL,
  p_dq_flags INTEGER DEFAULT NULL,
  -- Other filters
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  -- Coordinate search parameters (cone search with Haversine distance)
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
  v_coord_search_active BOOLEAN;
BEGIN
  -- Calculate offset
  v_offset := (p_page - 1) * p_page_size;

  -- Check if coordinate search is active
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);

  -- Validate sort direction
  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  -- Validate sort column (whitelist for security)
  -- Allow 'distance' as a sort column when coordinate search is active
  -- NEW: Added 'observation' and 'max_snr' to sort whitelist
  IF NOT (p_sort_column IN ('object_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  -- Determine which programs to query (intersection of accessible and filtered)
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    -- Intersect user's filter selection with accessible programs
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
  -- PERFORMANCE: Uses idx_spectra_grating index
  IF p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0 THEN
    SELECT ARRAY(
      SELECT DISTINCT s.object_id
      FROM spectra s
      WHERE s.grating = ANY(p_gratings)
    ) INTO v_grating_object_ids;

    -- If no objects have matching gratings, return empty
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
    SELECT
      o.*,
      -- Calculate Haversine distance ONCE (not duplicated like in old version)
      -- PERFORMANCE: Uses idx_objects_coords(ra, dec) for bounding box pre-filter
      CASE
        WHEN v_coord_search_active THEN
          -- Haversine formula: great-circle distance in degrees
          -- Formula: 2 * arcsin(sqrt(sin²(Δlat/2) + cos(lat1) * cos(lat2) * sin²(Δlon/2)))
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM objects o
    WHERE
      -- Program access control (uses idx_objects_program)
      o.program_id = ANY(v_filtered_program_ids)
      -- Grating filter (via pre-queried object IDs, uses unique index on object_id)
      AND (v_grating_object_ids IS NULL OR o.object_id = ANY(v_grating_object_ids))
      -- Field filter (uses idx_objects_field)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      -- NEW: Observation filter (uses idx_objects_observation)
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observation = ANY(p_observations))
      -- Redshift quality filter (uses idx_objects_redshift_quality)
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      -- Redshift range filters (uses idx_objects_redshift_generated - generated column)
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      -- NEW: Max S/N range filters (uses idx_objects_max_snr)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      -- Bitmask filters (cannot use indexes - evaluated on filtered rows)
      AND (p_spectral_features IS NULL OR (o.spectral_features & p_spectral_features) > 0)
      AND (p_object_flags IS NULL OR (o.object_flags & p_object_flags) > 0)
      AND (p_dq_flags IS NULL OR (o.dq_flags & p_dq_flags) > 0)
      -- Text search (uses idx_objects_object_id_trgm if created for fuzzy matching)
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
      -- Inspected only filter (uses idx_objects_redshift_quality)
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      -- Coordinate search: bounding box pre-filter (uses idx_objects_coords)
      -- Reduces rows before expensive Haversine calculation
      AND (
        NOT v_coord_search_active
        OR (
          o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        )
      )
  ),
  -- Filter by actual Haversine distance (after calculation in previous CTE)
  distance_filtered AS (
    SELECT fo.*
    FROM filtered_objects fo
    WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees
  ),
  -- Count total matching rows (before pagination)
  counted AS (
    SELECT COUNT(*) as cnt FROM distance_filtered
  ),
  -- Sort all filtered objects
  -- PERFORMANCE: For large result sets, this sorts ALL rows before pagination
  sorted_objects AS (
    SELECT df.*
    FROM distance_filtered df
    ORDER BY
      -- When coordinate search is active, always sort by distance first
      CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
      -- Otherwise, apply user-requested sorting
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
      -- NEW: Observation sorting
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
      -- NEW: Max S/N sorting
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
      -- Fallback sort for stability (ensures consistent pagination)
      df.object_id ASC
  ),
  -- Take only the requested page
  paginated AS (
    SELECT so.*
    FROM sorted_objects so
    LIMIT p_page_size
    OFFSET v_offset
  ),
  -- Join related data and build JSONB objects
  -- OPTIMIZATION: Carry sort columns to avoid JSONB extraction in final aggregation
  -- PERFORMANCE: Correlated subquery for spectra runs ONCE per paginated row (not per filtered row)
  with_relations AS (
    SELECT
      jsonb_build_object(
        'id', p.id,
        'object_id', p.object_id,
        'program_id', p.program_id,
        'field', p.field,
        'observation', p.observation,  -- NEW: Include observation in response
        'ra', p.ra,
        'dec', p.dec,
        'redshift', p.redshift,
        'redshift_auto', p.redshift_auto,
        'redshift_inspected', p.redshift_inspected,
        'redshift_quality', p.redshift_quality,
        'spectral_features', p.spectral_features,
        'object_flags', p.object_flags,
        'dq_flags', p.dq_flags,
        'max_snr', p.max_snr,  -- NEW: Include max_snr in response
        'last_inspected_at', p.last_inspected_at,
        'last_inspected_by', p.last_inspected_by,
        'created_at', p.created_at,
        'updated_at', p.updated_at,
        'program_name', pr.program_name,
        -- Include distance when coordinate search is active
        'distance', CASE WHEN v_coord_search_active THEN p.distance ELSE NULL END,
        -- Nested spectra array (filtered by grating if applicable)
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
              -- Filter spectra by grating if filter is active
              AND (p_gratings IS NULL OR array_length(p_gratings, 1) IS NULL OR s.grating = ANY(p_gratings))
          ),
          '[]'::jsonb
        )
      ) as obj,
      -- Carry sort columns for efficient sorting in final aggregation
      -- OPTIMIZATION: Avoids extracting from JSONB like (obj->>'field')
      p.object_id AS sort_object_id,
      p.field AS sort_field,
      p.observation AS sort_observation,  -- NEW: Carry observation for sorting
      p.ra AS sort_ra,
      p.dec AS sort_dec,
      p.redshift AS sort_redshift,
      p.redshift_quality AS sort_redshift_quality,
      p.max_snr AS sort_max_snr,  -- NEW: Carry max_snr for sorting
      p.distance AS sort_distance
    FROM paginated p
    LEFT JOIN programs pr ON pr.program_id = p.program_id
  )
  -- Aggregate into JSONB array with proper sorting
  -- Uses carried columns instead of JSONB extraction for better performance
  SELECT
    COALESCE(
      (
        SELECT jsonb_agg(wr.obj ORDER BY
          -- When coordinate search is active, sort by distance
          CASE WHEN v_coord_search_active THEN wr.sort_distance END ASC NULLS LAST,
          -- Otherwise, apply user-requested sorting
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN wr.sort_object_id END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN wr.sort_object_id END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN wr.sort_field END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN wr.sort_field END DESC NULLS LAST,
          -- NEW: Observation sorting in aggregation
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN wr.sort_observation END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN wr.sort_observation END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN wr.sort_ra END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN wr.sort_ra END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN wr.sort_dec END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN wr.sort_dec END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN wr.sort_redshift END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN wr.sort_redshift END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN wr.sort_redshift_quality END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN wr.sort_redshift_quality END DESC NULLS LAST,
          -- NEW: Max S/N sorting in aggregation
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN wr.sort_max_snr END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN wr.sort_max_snr END DESC NULLS LAST,
          -- Fallback for stability
          wr.sort_object_id ASC
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

-- Update function comment
COMMENT ON FUNCTION get_filtered_objects_paginated IS
'Server-side filtering, sorting, and pagination for the NIRSpec objects catalog.

FEATURES:
- Row-level security via p_program_ids parameter (public + user-accessible programs)
- Bitmask filters (spectral_features, object_flags, dq_flags) not supported by PostgREST
- Haversine cone search for coordinate-based queries
- Observation filter (categorical filter extracted from object_id)
- Max S/N range filter (min/max signal-to-noise ratio)
- Dynamic sorting by: object_id, field, observation, ra, dec, redshift, redshift_quality, max_snr, distance (when coordinate search active)
- Adaptive pagination: use large page_size (e.g., 5000) to fetch all data for client-side sorting when result set is small

PERFORMANCE OPTIMIZATIONS:
- Uses indexes for all filter columns (see migration 008 for observation and max_snr indexes)
- Single haversine calculation per object (no duplication)
- Efficient grating filter via pre-query with index
- Correlated spectra subquery runs only on paginated rows (not all filtered rows)
- Carries sort columns through CTEs instead of JSONB extraction (faster sorting)
- Trigram index support for fuzzy text search on object_id

RETURNS:
- objects: JSONB array of objects with nested spectra, program name, observation, and max_snr
- total_count: Total matching rows (for pagination UI)
- page: Current page number
- page_size: Rows per page';
