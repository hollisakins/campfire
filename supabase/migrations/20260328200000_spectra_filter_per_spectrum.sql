-- In spectra view mode, S/N and exposure time filters should operate on
-- per-spectrum values (s.signal_to_noise, s.exposure_time) rather than
-- target-level aggregates (t.max_snr, t.max_exposure_time).
-- This makes filter behavior match what the user sees in the table.

CREATE OR REPLACE FUNCTION public.get_filtered_spectra_paginated(
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
  p_sort_column TEXT DEFAULT 'target_id',
  p_sort_direction TEXT DEFAULT 'asc',
  p_page INTEGER DEFAULT 1,
  p_page_size INTEGER DEFAULT 50,
  p_include_thumbnails BOOLEAN DEFAULT false
)
RETURNS TABLE(targets JSONB, total_count BIGINT, page INTEGER, page_size INTEGER)
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
  v_total_count BIGINT;
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

  IF NOT (p_sort_column IN (
    'target_id', 'field', 'observation', 'ra', 'dec', 'redshift',
    'redshift_quality', 'signal_to_noise', 'exposure_time', 'grating'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'target_id';
  END IF;

  IF v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  v_offset := (COALESCE(p_page, 1) - 1) * COALESCE(p_page_size, 50);

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
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size;
    RETURN;
  END IF;

  -- Step 1: compute total count separately (avoids window function on full result set)
  SELECT COUNT(*) INTO v_total_count
  FROM targets t
  JOIN spectra s ON s.target_id = t.target_id
  WHERE
    t.program_slug = ANY(v_filtered_program_slugs)
    AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
    AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
    AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
    AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
    AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min)
    AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
    -- Per-spectrum filtering (not target-level max)
    AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min)
    AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
    AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min)
    AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
    AND (p_spectral_features_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_any) != 0)
    AND (p_spectral_features_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
    AND (p_spectral_features_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_exclude) = 0)
    AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
    AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
    AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
    AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
    AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
    AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
    AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
    AND (
      p_inspected_only IS NULL
      OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
      OR (p_inspected_only = FALSE AND t.redshift_quality = 0)
    )
    AND (
      NOT v_comment_search_active
      OR EXISTS (
        SELECT 1 FROM comments c
        WHERE c.target_id = t.id
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
        t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        AND 2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
          POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
        ))) <= p_radius_degrees
      )
    );

  -- Step 2: fetch just the page rows (sort + LIMIT without window function overhead)
  RETURN QUERY
  WITH filtered_spectra AS (
    SELECT
      t.id AS tgt_db_id,
      t.target_id,
      t.program_slug,
      t.field,
      t.observation,
      t.ra,
      t.dec,
      t.redshift,
      t.redshift_auto,
      t.redshift_inspected,
      t.redshift_quality,
      COALESCE(t.spectral_features, 0) AS spectral_features,
      COALESCE(t.object_flags, 0) AS object_flags,
      COALESCE(t.dq_flags, 0) AS dq_flags,
      t.max_snr,
      t.max_exposure_time,
      t.last_inspected_at,
      t.last_inspected_by,
      t.created_at,
      t.updated_at,
      s.id AS spectrum_id,
      s.grating,
      s.fits_path,
      s.signal_to_noise,
      s.exposure_time,
      s.file_hash,
      s.file_size,
      s.thumbnail_svg_fnu,
      s.thumbnail_svg_flambda,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
            POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM targets t
    JOIN spectra s ON s.target_id = t.target_id
    WHERE
      t.program_slug = ANY(v_filtered_program_slugs)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
      -- Per-spectrum filtering (not target-level max)
      AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
      AND (p_spectral_features_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_any) != 0)
      AND (p_spectral_features_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
      AND (p_spectral_features_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_exclude) = 0)
      AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
      AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
      AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND t.redshift_quality = 0)
      )
      AND (
        NOT v_comment_search_active
        OR EXISTS (
          SELECT 1 FROM comments c
          WHERE c.target_id = t.id
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
          t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
          AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        )
      )
  ),
  distance_filtered AS (
    SELECT fs.*
    FROM filtered_spectra fs
    WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees
  ),
  page_rows AS (
    SELECT *, ROW_NUMBER() OVER () as row_num
    FROM (
      SELECT * FROM distance_filtered
      ORDER BY
        CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN distance END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN distance END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN target_id END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN target_id END DESC NULLS LAST,
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
        target_id ASC, grating ASC
      LIMIT p_page_size OFFSET v_offset
    ) sorted_page
  )
  SELECT
    COALESCE(jsonb_agg(jsonb_build_object(
      'id', r.tgt_db_id,
      'target_id', r.target_id,
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
      'spectra', jsonb_build_array(jsonb_build_object(
        'id', r.spectrum_id,
        'target_id', r.target_id,
        'grating', r.grating,
        'fits_path', r.fits_path,
        'signal_to_noise', r.signal_to_noise,
        'exposure_time', r.exposure_time,
        'file_hash', r.file_hash,
        'file_size', r.file_size,
        'thumbnail_svg_fnu', CASE WHEN p_include_thumbnails THEN r.thumbnail_svg_fnu ELSE NULL END,
        'thumbnail_svg_flambda', CASE WHEN p_include_thumbnails THEN r.thumbnail_svg_flambda ELSE NULL END
      ))
    ) ORDER BY r.row_num), '[]'::jsonb),
    v_total_count,
    p_page,
    p_page_size
  FROM page_rows r
  LEFT JOIN programs pr ON pr.slug = r.program_slug;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_filtered_spectra_paginated TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_spectra_paginated TO service_role;
