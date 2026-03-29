-- Lightweight RPC for Python client catalog sync.
--
-- Unlike get_filtered_objects_paginated (designed for web UI with complex
-- sorting, distance computation, and window functions), this function does
-- a simple paginated fetch with optional updated_since filtering.
--
-- Used for both full sync (updated_since = NULL) and incremental sync.

CREATE OR REPLACE FUNCTION public.get_objects_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMP WITHOUT TIME ZONE DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_offset INTEGER DEFAULT 0
)
RETURNS TABLE(objects JSONB, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
BEGIN
  RETURN QUERY
  WITH matched AS (
    SELECT o.id, o.object_id, o.program_slug, o.field, o.observation,
           o.ra, o.dec, o.redshift, o.redshift_auto, o.redshift_inspected,
           o.redshift_quality, o.spectral_features, o.object_flags, o.dq_flags,
           o.max_snr, o.max_exposure_time,
           o.last_inspected_at, o.created_at, o.updated_at
    FROM objects o
    WHERE o.program_slug = ANY(p_program_slugs)
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
    ORDER BY o.object_id
    LIMIT p_limit OFFSET p_offset
  ),
  total AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE o.program_slug = ANY(p_program_slugs)
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'program_slug', m.program_slug,
        'program_name', pr.program_name,
        'field', m.field,
        'observation', m.observation,
        'ra', m.ra,
        'dec', m.dec,
        'redshift', m.redshift,
        'redshift_auto', m.redshift_auto,
        'redshift_inspected', m.redshift_inspected,
        'redshift_quality', m.redshift_quality,
        'spectral_features', m.spectral_features,
        'object_flags', m.object_flags,
        'dq_flags', m.dq_flags,
        'max_snr', m.max_snr,
        'max_exposure_time', m.max_exposure_time,
        'last_inspected_at', m.last_inspected_at,
        'created_at', m.created_at,
        'updated_at', m.updated_at,
        'spectra', COALESCE(
          (SELECT jsonb_agg(jsonb_build_object(
            'id', s.id,
            'object_id', s.object_id,
            'grating', s.grating,
            'fits_path', s.fits_path,
            'file_hash', s.file_hash,
            'file_size', s.file_size,
            'signal_to_noise', s.signal_to_noise,
            'exposure_time', s.exposure_time,
            'reduction_version', s.reduction_version
          )) FROM spectra s WHERE s.object_id = m.object_id),
          '[]'::jsonb
        )
      )
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT
  FROM matched m
  LEFT JOIN programs pr ON m.program_slug = pr.slug;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_objects_for_sync TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_objects_for_sync TO service_role;
