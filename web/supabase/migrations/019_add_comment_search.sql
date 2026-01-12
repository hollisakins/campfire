-- Migration 019: Add comment search functionality
-- Extends the RPC function to support searching objects by comment content
-- with scope options: 'just_me' (user's own comments) or 'everyone' (all comments)

-- Step 1: Create trigram index on comments.content for efficient ILIKE search
-- pg_trgm extension already enabled in migration 007
CREATE INDEX IF NOT EXISTS idx_comments_content_trgm
ON comments USING gin (content gin_trgm_ops);

-- Step 2: Update the RPC function with comment search parameters
DROP FUNCTION IF EXISTS get_filtered_objects_paginated;

CREATE OR REPLACE FUNCTION get_filtered_objects_paginated(
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
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  -- Bitmask filters (combined masks - any matching bit returns true)
  p_spectral_features INTEGER DEFAULT NULL,
  p_object_flags INTEGER DEFAULT NULL,
  p_dq_flags INTEGER DEFAULT NULL,
  -- Object ID search
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  -- NEW: Comment search parameters
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,  -- 'just_me' or 'everyone'
  p_comment_user_id UUID DEFAULT NULL,       -- Required when scope = 'just_me'
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
  v_comment_search_active BOOLEAN;
BEGIN
  -- Calculate offset
  v_offset := (p_page - 1) * p_page_size;

  -- Check if coordinate search is active
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);

  -- Check if comment search is active (requires both search text and valid scope)
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );

  -- Validate sort direction
  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  -- Validate sort column (whitelist for security)
  IF NOT (p_sort_column IN ('object_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
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
    SELECT
      o.*,
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
      -- Program access control
      o.program_id = ANY(v_filtered_program_ids)
      -- Grating filter
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
      -- Max S/N range filters
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      -- Bitmask filters
      AND (p_spectral_features IS NULL OR (o.spectral_features & p_spectral_features) > 0)
      AND (p_object_flags IS NULL OR (o.object_flags & p_object_flags) > 0)
      AND (p_dq_flags IS NULL OR (o.dq_flags & p_dq_flags) > 0)
      -- Object ID text search
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
      -- Inspected only filter
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      -- NEW: Comment search filter (only when active)
      -- Uses EXISTS subquery to avoid row multiplication and benefit from indexes
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
  distance_filtered AS (
    SELECT fo.*
    FROM filtered_objects fo
    WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees
  ),
  counted AS (
    SELECT COUNT(*) as cnt FROM distance_filtered
  ),
  sorted_objects AS (
    SELECT df.*
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
      df.object_id ASC
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
        'observation', p.observation,
        'ra', p.ra,
        'dec', p.dec,
        'redshift', p.redshift,
        'redshift_auto', p.redshift_auto,
        'redshift_inspected', p.redshift_inspected,
        'redshift_quality', p.redshift_quality,
        'spectral_features', p.spectral_features,
        'object_flags', p.object_flags,
        'dq_flags', p.dq_flags,
        'max_snr', p.max_snr,
        'last_inspected_at', p.last_inspected_at,
        'last_inspected_by', p.last_inspected_by,
        'created_at', p.created_at,
        'updated_at', p.updated_at,
        'program_name', pr.program_name,
        'distance', CASE WHEN v_coord_search_active THEN p.distance ELSE NULL END,
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
      ) as obj,
      p.object_id AS sort_object_id,
      p.field AS sort_field,
      p.observation AS sort_observation,
      p.ra AS sort_ra,
      p.dec AS sort_dec,
      p.redshift AS sort_redshift,
      p.redshift_quality AS sort_redshift_quality,
      p.max_snr AS sort_max_snr,
      p.distance AS sort_distance
    FROM paginated p
    LEFT JOIN programs pr ON pr.program_id = p.program_id
  )
  SELECT
    COALESCE(
      (
        SELECT jsonb_agg(wr.obj ORDER BY
          CASE WHEN v_coord_search_active THEN wr.sort_distance END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN wr.sort_object_id END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN wr.sort_object_id END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN wr.sort_field END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN wr.sort_field END DESC NULLS LAST,
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
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN wr.sort_max_snr END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN wr.sort_max_snr END DESC NULLS LAST,
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
- Row-level security via p_program_ids parameter
- Bitmask filters (spectral_features, object_flags, dq_flags)
- Haversine cone search for coordinate-based queries
- Observation and Max S/N filters
- Comment search with scope: just_me (user comments) or everyone (all comments)
- Dynamic sorting by multiple columns
- Adaptive pagination

NEW in v019:
- p_comment_search: Text to search in comment content (ILIKE)
- p_comment_search_scope: "just_me" or "everyone"
- p_comment_user_id: User ID for "just_me" scope
- idx_comments_content_trgm: Trigram index for efficient comment search

RETURNS:
- objects: JSONB array of objects with nested spectra and program name
- total_count: Total matching rows
- page: Current page number
- page_size: Rows per page';

-- Step 3: Update get_adjacent_objects with comment search parameters
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
  -- NEW: Comment search parameters
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,  -- 'just_me' or 'everyone'
  p_comment_user_id UUID DEFAULT NULL,       -- Required when scope = 'just_me'
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
  v_comment_search_active BOOLEAN;
BEGIN
  -- Validate sort direction
  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  -- Validate sort column (whitelist for security)
  IF p_sort_column NOT IN ('object_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr') THEN
    p_sort_column := 'object_id';
  END IF;

  -- Check if comment search is active (requires both search text and valid scope)
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );

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
      o.id,
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
      -- NEW: Comment search filter (only when active)
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

NEW in v019: Comment search parameters (p_comment_search, p_comment_search_scope, p_comment_user_id)

Returns: prev_object_id, next_object_id, current_index (1-based), total_count.';
