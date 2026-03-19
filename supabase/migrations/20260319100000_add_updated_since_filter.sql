-- Add updated_since filter for incremental sync.
--
-- Allows the Python CLI/client to fetch only objects modified since
-- the last sync, reducing sync time from ~47s to near-instant for
-- subsequent syncs when few objects have changed.
--
-- Adds p_updated_since parameter to get_filtered_object_ids and
-- get_filtered_objects_paginated. Both default to NULL (no filter),
-- so all existing callers are unaffected.
--
-- Also adds an index on objects.updated_at for efficient filtering.

-- Index for updated_since queries
CREATE INDEX IF NOT EXISTS idx_objects_updated_at ON objects(updated_at);

-- ---- get_filtered_object_ids (add p_updated_since) ----

CREATE OR REPLACE FUNCTION public.get_filtered_object_ids(
  p_program_slugs TEXT[],
  p_filter_programs TEXT[] DEFAULT NULL,
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
  p_page INTEGER DEFAULT NULL,
  p_page_size INTEGER DEFAULT NULL,
  p_updated_since TIMESTAMP WITHOUT TIME ZONE DEFAULT NULL
)
RETURNS TABLE(object_id TEXT, distance DOUBLE PRECISION, row_num BIGINT, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
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

  -- When coord search is active but no explicit sort requested, default to distance
  IF v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  -- Determine pagination mode
  v_paginate := (p_page IS NOT NULL AND p_page_size IS NOT NULL);
  IF v_paginate THEN
    v_offset := (p_page - 1) * p_page_size;
  END IF;

  -- Determine which programs to query
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

  -- =========================================================================
  -- Paginated path
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
        o.program_slug = ANY(v_filtered_program_slugs)
        AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
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
          CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN df.distance END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN df.distance END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
          df.object_id ASC
      ) AS row_num,
      (SELECT COUNT(*) FROM distance_filtered)::BIGINT AS total_count
    FROM distance_filtered df
    ORDER BY
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN df.distance END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN df.distance END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
      df.object_id ASC
    LIMIT p_page_size OFFSET v_offset;

  -- =========================================================================
  -- Full path (all rows)
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
        o.program_slug = ANY(v_filtered_program_slugs)
        AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
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
          CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN df.distance END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN df.distance END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
          df.object_id ASC
      ) AS row_num,
      COUNT(*) OVER () AS total_count
    FROM distance_filtered df;
  END IF;
END;
$$;


-- ---- get_filtered_objects_paginated (pass through p_updated_since) ----

-- Must drop first because we are adding a new parameter
DROP FUNCTION IF EXISTS public.get_filtered_objects_paginated(
  TEXT[], TEXT[], TEXT[], TEXT[], TEXT, TEXT[], INTEGER[], DOUBLE PRECISION, DOUBLE PRECISION,
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  INTEGER, INTEGER, INTEGER,
  INTEGER, INTEGER, INTEGER,
  INTEGER, INTEGER, INTEGER,
  TEXT, BOOLEAN, TEXT, TEXT, UUID,
  DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION,
  TEXT, TEXT, INTEGER, INTEGER, BOOLEAN
);

CREATE OR REPLACE FUNCTION public.get_filtered_objects_paginated(
  p_program_slugs TEXT[],
  p_filter_programs TEXT[] DEFAULT NULL,
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
  p_include_thumbnails BOOLEAN DEFAULT false,
  p_updated_since TIMESTAMP WITHOUT TIME ZONE DEFAULT NULL
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
      p_program_slugs, p_filter_programs, p_fields, p_gratings, p_gratings_mode,
      p_observations, p_redshift_quality, p_redshift_min, p_redshift_max,
      p_max_snr_min, p_max_snr_max, p_max_exposure_time_min, p_max_exposure_time_max,
      v_sf_include_any, v_sf_include_all, v_sf_exclude,
      v_of_include_any, v_of_include_all, v_of_exclude,
      v_dq_include_any, v_dq_include_all, v_dq_exclude,
      p_search, p_inspected_only, p_comment_search, p_comment_search_scope, p_comment_user_id,
      p_coord_ra, p_coord_dec, p_radius_degrees,
      p_sort_column, p_sort_direction,
      p_page, p_page_size,
      p_updated_since
    )
  ),
  with_relations AS (
    SELECT
      jsonb_build_object(
        'id', o.id,
        'object_id', o.object_id,
        'program_slug', o.program_slug,
        'program_name', pr.program_name,
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
        'distance', CASE WHEN v_coord_search_active THEN ids.distance ELSE NULL END,
        'spectra', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'id', s.id,
              'object_id', s.object_id,
              'grating', s.grating,
              'fits_path', s.fits_path,
              'signal_to_noise', s.signal_to_noise,
              'file_hash', s.file_hash,
              'file_size', s.file_size,
              'thumbnail_path', CASE WHEN p_include_thumbnails THEN s.thumbnail_path ELSE NULL END
            )
          )
          FROM spectra s
          WHERE s.object_id = o.object_id
            AND (
              NOT v_grating_filter_active
              OR v_gratings_mode = 'none'
              OR s.grating = ANY(p_gratings)
            )
          ),
          '[]'::jsonb
        ),
        'row_num', ids.row_num
      ) AS obj_json,
      ids.total_count,
      ids.row_num
    FROM ids
    JOIN objects o ON o.object_id = ids.object_id
    LEFT JOIN programs pr ON o.program_slug = pr.slug
  )
  SELECT
    COALESCE(jsonb_agg(wr.obj_json ORDER BY wr.row_num), '[]'::jsonb),
    COALESCE(MAX(wr.total_count), 0)::BIGINT,
    p_page,
    p_page_size
  FROM with_relations wr;
END;
$$;

-- Grant execute to authenticated users
GRANT EXECUTE ON FUNCTION public.get_filtered_objects_paginated TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_objects_paginated TO service_role;
