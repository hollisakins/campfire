-- Hotfix: the 20260319100000 migration incorrectly referenced s.thumbnail_path
-- which does not exist. The correct columns are thumbnail_svg_fnu and
-- thumbnail_svg_flambda. This recreates get_filtered_objects_paginated with
-- the correct column names.

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
              'thumbnail_svg_fnu', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_fnu ELSE NULL END,
              'thumbnail_svg_flambda', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_flambda ELSE NULL END
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

GRANT EXECUTE ON FUNCTION public.get_filtered_objects_paginated TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_filtered_objects_paginated TO service_role;
