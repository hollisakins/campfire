CREATE OR REPLACE FUNCTION public.get_objects_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_offset INTEGER DEFAULT 0
)
RETURNS TABLE(objects JSONB, total_count BIGINT, total_accessible_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
AS $$
BEGIN
  RETURN QUERY
  WITH matched AS (
    SELECT o.id, o.object_id, o.field, o.ra, o.dec,
           o.n_targets, o.n_spectra, o.programs, o.gratings,
           o.max_snr, o.max_exposure_time,
           o.best_redshift, o.best_redshift_quality,
           o.created_at, o.updated_at
    FROM objects o
    WHERE o.programs && p_program_slugs
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
    ORDER BY o.object_id
    LIMIT p_limit OFFSET p_offset
  ),
  total AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE o.programs && p_program_slugs
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE o.programs && p_program_slugs
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'field', m.field,
        'ra', m.ra,
        'dec', m.dec,
        'n_targets', m.n_targets,
        'n_spectra', m.n_spectra,
        'programs', m.programs,
        'gratings', m.gratings,
        'max_snr', m.max_snr,
        'max_exposure_time', m.max_exposure_time,
        'best_redshift', m.best_redshift,
        'best_redshift_quality', m.best_redshift_quality,
        'created_at', m.created_at,
        'updated_at', m.updated_at,
        'member_target_ids', COALESCE(
          (SELECT jsonb_agg(t.target_id ORDER BY t.target_id)
           FROM targets t
           WHERE t.object_id = m.id
             AND t.program_slug = ANY(p_program_slugs)),
          '[]'::jsonb
        )
      )
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_objects_for_sync TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_objects_for_sync TO service_role;
