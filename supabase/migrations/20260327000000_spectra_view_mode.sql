-- Spectra view mode: per-grating row display on the spectra list page.
--
-- Two new RPC functions that return one row per (object_id, grating) pair
-- instead of one row per object. Uses bespoke inline filtering (same WHERE
-- clause as get_csv_export / get_filtered_object_ids) for single-pass
-- query efficiency — avoids materializing all matching objects before
-- joining to spectra.

-- ============================================================
-- 1. get_filtered_spectra_paginated
--    Returns paginated JSONB results with one spectrum per entry.
-- ============================================================

CREATE OR REPLACE FUNCTION public.get_filtered_spectra_paginated(
  p_program_slugs TEXT[],
  p_filter_programs TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL,
  p_gratings TEXT[] DEFAULT NULL,
  -- In spectra mode, gratings_mode is forced to 'any' by the caller.
  -- The mode still controls object-level filtering (which objects qualify),
  -- but the join to spectra additionally filters to only show matching gratings.
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
  p_page INTEGER DEFAULT 1,
  p_page_size INTEGER DEFAULT 50,
  p_include_thumbnails BOOLEAN DEFAULT false
)
RETURNS TABLE(objects JSONB, total_count BIGINT, page INTEGER, page_size INTEGER)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
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

  -- Validate sort column — spectra mode supports spectrum-level columns
  IF NOT (p_sort_column IN (
    'object_id', 'field', 'observation', 'ra', 'dec', 'redshift',
    'redshift_quality', 'signal_to_noise', 'exposure_time', 'grating'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  -- When coord search is active but no explicit sort requested, default to distance
  IF v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  v_offset := (COALESCE(p_page, 1) - 1) * COALESCE(p_page_size, 50);

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
    -- Return empty result
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size;
    RETURN;
  END IF;

  RETURN QUERY
  WITH filtered_spectra AS (
    SELECT
      o.id AS obj_db_id,
      o.object_id,
      o.program_slug,
      o.field,
      o.observation,
      o.ra,
      o.dec,
      o.redshift,
      o.redshift_auto,
      o.redshift_inspected,
      o.redshift_quality,
      COALESCE(o.spectral_features, 0) AS spectral_features,
      COALESCE(o.object_flags, 0) AS object_flags,
      COALESCE(o.dq_flags, 0) AS dq_flags,
      o.max_snr,
      o.max_exposure_time,
      o.last_inspected_at,
      o.last_inspected_by,
      o.created_at,
      o.updated_at,
      s.id AS spectrum_id,
      s.grating,
      s.fits_path,
      s.signal_to_noise,
      s.exposure_time,
      s.file_hash,
      s.file_size,
      s.thumbnail_path,
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
    JOIN spectra s ON s.object_id = o.object_id
    WHERE
      o.program_slug = ANY(v_filtered_program_slugs)
      -- Grating filter: in spectra mode, always filter the spectrum rows directly
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
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
    SELECT fs.*
    FROM filtered_spectra fs
    WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees
  ),
  counted AS (
    SELECT *, COUNT(*) OVER () AS cnt
    FROM distance_filtered
  ),
  sorted AS (
    SELECT *
    FROM counted
    ORDER BY
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN distance END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN distance END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN object_id END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN object_id END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN field END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN field END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN observation END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN observation END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN ra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN ra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN "dec" END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN "dec" END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN redshift END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN redshift END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN redshift_quality END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN redshift_quality END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'signal_to_noise' AND p_sort_direction = 'asc' THEN signal_to_noise END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'signal_to_noise' AND p_sort_direction = 'desc' THEN signal_to_noise END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'exposure_time' AND p_sort_direction = 'asc' THEN exposure_time END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'exposure_time' AND p_sort_direction = 'desc' THEN exposure_time END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'grating' AND p_sort_direction = 'asc' THEN grating END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'grating' AND p_sort_direction = 'desc' THEN grating END DESC NULLS LAST,
      object_id ASC, grating ASC
    LIMIT p_page_size OFFSET v_offset
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', r.obj_db_id,
        'object_id', r.object_id,
        'program_slug', r.program_slug,
        'program_name', pr.program_name,
        'field', r.field,
        'observation', r.observation,
        'ra', r.ra,
        'dec', r.dec,
        'redshift', r.redshift,
        'redshift_auto', r.redshift_auto,
        'redshift_inspected', r.redshift_inspected,
        'redshift_quality', r.redshift_quality,
        'spectral_features', r.spectral_features,
        'object_flags', r.object_flags,
        'dq_flags', r.dq_flags,
        'max_snr', r.max_snr,
        'max_exposure_time', r.max_exposure_time,
        'last_inspected_at', r.last_inspected_at,
        'last_inspected_by', r.last_inspected_by,
        'created_at', r.created_at,
        'updated_at', r.updated_at,
        'distance', CASE WHEN v_coord_search_active THEN r.distance ELSE NULL END,
        'spectra', jsonb_build_array(
          jsonb_build_object(
            'id', r.spectrum_id,
            'object_id', r.object_id,
            'grating', r.grating,
            'fits_path', r.fits_path,
            'signal_to_noise', r.signal_to_noise,
            'exposure_time', r.exposure_time,
            'file_hash', r.file_hash,
            'file_size', r.file_size,
            'thumbnail_path', CASE WHEN p_include_thumbnails THEN r.thumbnail_path ELSE NULL END
          )
        )
      )
    ), '[]'::jsonb),
    COALESCE(MAX(r.cnt), 0)::BIGINT,
    p_page,
    p_page_size
  FROM sorted r
  LEFT JOIN programs pr ON pr.slug = r.program_slug;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_filtered_spectra_paginated TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_spectra_paginated TO service_role;


-- ============================================================
-- 2. get_csv_export_spectra
--    Flat rows for CSV download in spectra view mode.
-- ============================================================

CREATE OR REPLACE FUNCTION public.get_csv_export_spectra(
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
  p_sort_direction TEXT DEFAULT 'asc'
)
RETURNS TABLE(
  object_id TEXT,
  grating TEXT,
  field TEXT,
  ra DOUBLE PRECISION,
  "dec" DOUBLE PRECISION,
  redshift NUMERIC,
  redshift_quality INTEGER,
  signal_to_noise DOUBLE PRECISION,
  exposure_time DOUBLE PRECISION,
  fits_path TEXT,
  program_slug TEXT,
  program_name TEXT,
  last_inspected_at TIMESTAMPTZ,
  last_inspected_by TEXT,
  distance DOUBLE PRECISION,
  spectral_features INTEGER,
  object_flags INTEGER,
  dq_flags INTEGER
)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);

  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );

  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  -- Validate sort column for spectra mode
  IF NOT (p_sort_column IN (
    'object_id', 'field', 'observation', 'ra', 'dec', 'redshift',
    'redshift_quality', 'signal_to_noise', 'exposure_time', 'grating'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
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

  RETURN QUERY
  WITH filtered_spectra AS (
    SELECT
      o.object_id,
      s.grating,
      o.field,
      o.ra,
      o.dec,
      o.redshift,
      o.redshift_quality,
      s.signal_to_noise,
      s.exposure_time,
      s.fits_path,
      o.program_slug,
      o.observation,
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
      END AS distance,
      COALESCE(o.spectral_features, 0) AS spectral_features,
      COALESCE(o.object_flags, 0) AS object_flags,
      COALESCE(o.dq_flags, 0) AS dq_flags
    FROM objects o
    JOIN spectra s ON s.object_id = o.object_id
    WHERE
      o.program_slug = ANY(v_filtered_program_slugs)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
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
    SELECT fs.*
    FROM filtered_spectra fs
    WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees
  )
  SELECT
    df.object_id,
    df.grating,
    df.field,
    df.ra,
    df.dec,
    df.redshift,
    df.redshift_quality,
    df.signal_to_noise,
    df.exposure_time,
    df.fits_path,
    df.program_slug,
    pr.program_name,
    df.last_inspected_at,
    up.full_name AS last_inspected_by,
    df.distance,
    df.spectral_features,
    df.object_flags,
    df.dq_flags
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  LEFT JOIN user_profiles up ON up.user_id = df.last_inspected_by
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
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'signal_to_noise' AND p_sort_direction = 'asc' THEN df.signal_to_noise END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'signal_to_noise' AND p_sort_direction = 'desc' THEN df.signal_to_noise END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'exposure_time' AND p_sort_direction = 'asc' THEN df.exposure_time END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'exposure_time' AND p_sort_direction = 'desc' THEN df.exposure_time END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'grating' AND p_sort_direction = 'asc' THEN df.grating END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'grating' AND p_sort_direction = 'desc' THEN df.grating END DESC NULLS LAST,
    df.object_id ASC, df.grating ASC;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_csv_export_spectra TO authenticated;
