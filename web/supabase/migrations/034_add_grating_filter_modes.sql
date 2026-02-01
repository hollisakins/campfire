-- Migration 034: Add grating filter modes (any/all/none)
-- Extends the RPC function to support three-mode filtering for gratings:
--   - any: Object has at least one spectrum with a matching grating (current behavior)
--   - all: Object must have spectra for ALL selected gratings
--   - none: Object must NOT have any spectra with selected gratings
--
-- Also updates get_adjacent_objects for consistency.

DROP FUNCTION IF EXISTS get_filtered_objects_paginated;

CREATE OR REPLACE FUNCTION get_filtered_objects_paginated(
  -- Access control: array of program IDs the user can access
  p_program_ids INTEGER[],
  -- Optional program filter (intersection with accessible programs)
  p_filter_programs INTEGER[] DEFAULT NULL,
  -- Standard filters
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',  -- NEW: 'any', 'all', 'none'
  p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_min DOUBLE PRECISION DEFAULT NULL,
  p_max_snr_max DOUBLE PRECISION DEFAULT NULL,
  -- LEGACY: Single-mask parameters (for web app backward compatibility)
  p_spectral_features INTEGER DEFAULT NULL,
  p_object_flags INTEGER DEFAULT NULL,
  p_dq_flags INTEGER DEFAULT NULL,
  -- NEW: Three-mode flag filters for Python API
  p_spectral_features_include_any INTEGER DEFAULT NULL,
  p_spectral_features_include_all INTEGER DEFAULT NULL,
  p_spectral_features_exclude INTEGER DEFAULT NULL,
  p_object_flags_include_any INTEGER DEFAULT NULL,
  p_object_flags_include_all INTEGER DEFAULT NULL,
  p_object_flags_exclude INTEGER DEFAULT NULL,
  p_dq_flags_include_any INTEGER DEFAULT NULL,
  p_dq_flags_include_all INTEGER DEFAULT NULL,
  p_dq_flags_exclude INTEGER DEFAULT NULL,
  -- Object ID search
  p_search TEXT DEFAULT NULL,
  p_inspected_only BOOLEAN DEFAULT NULL,
  -- Comment search parameters (from migration 019)
  p_comment_search TEXT DEFAULT NULL,
  p_comment_search_scope TEXT DEFAULT NULL,  -- 'just_me' or 'everyone'
  p_comment_user_id UUID DEFAULT NULL,
  -- Coordinate search parameters
  p_coord_ra DOUBLE PRECISION DEFAULT NULL,
  p_coord_dec DOUBLE PRECISION DEFAULT NULL,
  p_radius_degrees DOUBLE PRECISION DEFAULT NULL,
  -- Sorting
  p_sort_column TEXT DEFAULT 'object_id',
  p_sort_direction TEXT DEFAULT 'asc',
  -- Pagination
  p_page INTEGER DEFAULT 1,
  p_page_size INTEGER DEFAULT 50,
  -- Include thumbnail SVGs in spectra (for web table views)
  p_include_thumbnails BOOLEAN DEFAULT FALSE
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
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  -- Effective flag masks (merge legacy + new params)
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
  -- Calculate offset
  v_offset := (p_page - 1) * p_page_size;

  -- Check if coordinate search is active
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);

  -- Check if comment search is active
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );

  -- Check if grating filter is active
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);

  -- Validate grating mode
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN
    v_gratings_mode := 'any';
  END IF;

  -- Merge legacy single-mask params with new multi-mode params
  -- Legacy params are treated as include_any when new params are NULL
  v_sf_include_any := COALESCE(p_spectral_features_include_any, p_spectral_features);
  v_sf_include_all := p_spectral_features_include_all;
  v_sf_exclude := p_spectral_features_exclude;

  v_of_include_any := COALESCE(p_object_flags_include_any, p_object_flags);
  v_of_include_all := p_object_flags_include_all;
  v_of_exclude := p_object_flags_exclude;

  v_dq_include_any := COALESCE(p_dq_flags_include_any, p_dq_flags);
  v_dq_include_all := p_dq_flags_include_all;
  v_dq_exclude := p_dq_flags_exclude;

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

  -- Handle grating filter based on mode
  IF v_grating_filter_active THEN
    IF v_gratings_mode = 'any' THEN
      -- Mode: ANY - object has at least one spectrum with a matching grating
      SELECT ARRAY(
        SELECT DISTINCT s.object_id
        FROM spectra s
        WHERE s.grating = ANY(p_gratings)
      ) INTO v_grating_object_ids;
    ELSIF v_gratings_mode = 'all' THEN
      -- Mode: ALL - object must have spectra for ALL selected gratings
      SELECT ARRAY(
        SELECT s.object_id
        FROM spectra s
        WHERE s.grating = ANY(p_gratings)
        GROUP BY s.object_id
        HAVING COUNT(DISTINCT s.grating) = array_length(p_gratings, 1)
      ) INTO v_grating_object_ids;
    ELSIF v_gratings_mode = 'none' THEN
      -- Mode: NONE - object must NOT have any spectra with selected gratings
      -- We'll handle this differently in the WHERE clause
      SELECT ARRAY(
        SELECT DISTINCT s.object_id
        FROM spectra s
        WHERE s.grating = ANY(p_gratings)
      ) INTO v_grating_object_ids;
    END IF;

    -- For 'any' and 'all' modes, if no matching objects, return empty
    IF v_gratings_mode IN ('any', 'all') AND (v_grating_object_ids IS NULL OR array_length(v_grating_object_ids, 1) IS NULL) THEN
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
      -- Grating filter (mode-aware)
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode IN ('any', 'all') AND o.object_id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'none' AND (v_grating_object_ids IS NULL OR NOT o.object_id = ANY(v_grating_object_ids)))
      )
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
                'created_at', s.created_at,
                -- Conditionally include thumbnails
                'thumbnail_svg_fnu', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_fnu ELSE NULL END,
                'thumbnail_svg_flambda', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_flambda ELSE NULL END
              )
              ORDER BY s.grating
            )
            FROM spectra s
            WHERE s.object_id = p.object_id
              -- For 'none' mode, show all spectra; for 'any'/'all' mode, filter to selected gratings
              AND (NOT v_grating_filter_active OR v_gratings_mode = 'none' OR s.grating = ANY(p_gratings))
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

GRANT EXECUTE ON FUNCTION get_filtered_objects_paginated TO authenticated;

COMMENT ON FUNCTION get_filtered_objects_paginated IS
'Server-side filtering, sorting, and pagination for the NIRSpec objects catalog.

GRATING FILTERING (three modes):
- any: Object has at least one spectrum with a matching grating (OR)
- all: Object must have spectra for ALL selected gratings (AND)
- none: Object must NOT have any spectra with selected gratings (NOT)

FLAG FILTERING (three modes per flag type):
- include_any: (flags & mask) != 0 - match any of these flags (OR)
- include_all: (flags & mask) = mask - must have all of these flags (AND)
- exclude: (flags & mask) = 0 - must not have any of these flags (NOT)

THUMBNAIL SUPPORT:
- p_include_thumbnails: When TRUE, includes thumbnail_svg_fnu and thumbnail_svg_flambda
  in the spectra JSONB. Defaults to FALSE for lean API responses.

BACKWARD COMPATIBILITY:
- Legacy params (p_spectral_features, p_object_flags, p_dq_flags) treated as include_any
- p_gratings_mode defaults to "any" for backward compatibility

RETURNS:
- objects: JSONB array of objects with nested spectra and program name
- total_count: Total matching rows
- page/page_size: Pagination info';


-- Also update get_adjacent_objects to support grating modes
DROP FUNCTION IF EXISTS get_adjacent_objects;

CREATE OR REPLACE FUNCTION get_adjacent_objects(
  p_current_object_id TEXT,
  p_program_ids INTEGER[],
  p_filter_programs INTEGER[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  p_gratings_mode TEXT DEFAULT 'any',  -- NEW: 'any', 'all', 'none'
  p_observations TEXT[] DEFAULT NULL,
  p_redshift_quality INTEGER[] DEFAULT NULL,
  p_redshift_min DOUBLE PRECISION DEFAULT NULL,
  p_redshift_max DOUBLE PRECISION DEFAULT NULL,
  p_spectral_features INTEGER DEFAULT NULL,
  p_object_flags INTEGER DEFAULT NULL,
  p_dq_flags INTEGER DEFAULT NULL,
  -- Three-mode flag filters
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
) AS $$
DECLARE
  v_filtered_program_ids INTEGER[];
  v_grating_object_ids TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
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

  v_sf_include_any := COALESCE(p_spectral_features_include_any, p_spectral_features);
  v_sf_include_all := p_spectral_features_include_all;
  v_sf_exclude := p_spectral_features_exclude;
  v_of_include_any := COALESCE(p_object_flags_include_any, p_object_flags);
  v_of_include_all := p_object_flags_include_all;
  v_of_exclude := p_object_flags_exclude;
  v_dq_include_any := COALESCE(p_dq_flags_include_any, p_dq_flags);
  v_dq_include_all := p_dq_flags_include_all;
  v_dq_exclude := p_dq_flags_exclude;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN ('object_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

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

  -- Handle grating filter based on mode
  IF v_grating_filter_active THEN
    IF v_gratings_mode = 'any' THEN
      SELECT ARRAY(
        SELECT DISTINCT s.object_id FROM spectra s WHERE s.grating = ANY(p_gratings)
      ) INTO v_grating_object_ids;
    ELSIF v_gratings_mode = 'all' THEN
      SELECT ARRAY(
        SELECT s.object_id FROM spectra s WHERE s.grating = ANY(p_gratings)
        GROUP BY s.object_id HAVING COUNT(DISTINCT s.grating) = array_length(p_gratings, 1)
      ) INTO v_grating_object_ids;
    ELSIF v_gratings_mode = 'none' THEN
      SELECT ARRAY(
        SELECT DISTINCT s.object_id FROM spectra s WHERE s.grating = ANY(p_gratings)
      ) INTO v_grating_object_ids;
    END IF;

    IF v_gratings_mode IN ('any', 'all') AND (v_grating_object_ids IS NULL OR array_length(v_grating_object_ids, 1) IS NULL) THEN
      RETURN QUERY SELECT NULL::TEXT, NULL::TEXT, 0::BIGINT, 0::BIGINT;
      RETURN;
    END IF;
  END IF;

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
      o.field, o.observation, o.ra, o.dec, o.redshift, o.redshift_quality, o.max_snr
    FROM objects o
    WHERE
      o.program_id = ANY(v_filtered_program_ids)
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode IN ('any', 'all') AND o.object_id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'none' AND (v_grating_object_ids IS NULL OR NOT o.object_id = ANY(v_grating_object_ids)))
      )
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (v_sf_include_any IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_include_any) != 0)
      AND (v_sf_include_all IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_include_all) = v_sf_include_all)
      AND (v_sf_exclude IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_exclude) = 0)
      AND (v_of_include_any IS NULL OR (COALESCE(o.object_flags, 0) & v_of_include_any) != 0)
      AND (v_of_include_all IS NULL OR (COALESCE(o.object_flags, 0) & v_of_include_all) = v_of_include_all)
      AND (v_of_exclude IS NULL OR (COALESCE(o.object_flags, 0) & v_of_exclude) = 0)
      AND (v_dq_include_any IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_include_any) != 0)
      AND (v_dq_include_all IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_include_all) = v_dq_include_all)
      AND (v_dq_exclude IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_exclude) = 0)
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
  ),
  sorted_with_row AS (
    SELECT
      df.object_id,
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
          df.object_id ASC
      ) as row_num
    FROM distance_filtered df
  ),
  current_row AS (
    SELECT row_num FROM sorted_with_row WHERE object_id = p_current_object_id
  ),
  total AS (
    SELECT COUNT(*) as cnt FROM sorted_with_row
  )
  SELECT
    (SELECT object_id FROM sorted_with_row WHERE row_num = (SELECT row_num - 1 FROM current_row)) as prev_object_id,
    (SELECT object_id FROM sorted_with_row WHERE row_num = (SELECT row_num + 1 FROM current_row)) as next_object_id,
    COALESCE((SELECT row_num FROM current_row), 0) as current_index,
    (SELECT cnt FROM total) as total_count;
END;
$$ LANGUAGE plpgsql STABLE;

GRANT EXECUTE ON FUNCTION get_adjacent_objects TO authenticated;
