drop function if exists "public"."get_objects_for_sync"(p_program_slugs text[], p_updated_since timestamp with time zone, p_limit integer, p_offset integer);

drop function if exists "public"."get_targets_for_sync"(p_program_slugs text[], p_updated_since timestamp without time zone, p_limit integer, p_offset integer);

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_objects_for_sync(p_program_slugs text[], p_user_id uuid DEFAULT NULL::uuid, p_updated_since timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0)
 RETURNS TABLE(objects jsonb, total_count bigint, total_accessible_count bigint)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
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
        ),
        'lists', COALESCE(
          (SELECT jsonb_agg(ol.slug ORDER BY ol.slug)
           FROM object_list_members olm
           JOIN object_lists ol ON ol.id = olm.list_id
           WHERE olm.object_id = m.id
             AND (ol.created_by = p_user_id OR ol.visibility IN ('public_read', 'public_edit'))),
          '[]'::jsonb
        )
      )
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_targets_for_sync(p_program_slugs text[], p_user_id uuid DEFAULT NULL::uuid, p_updated_since timestamp without time zone DEFAULT NULL::timestamp without time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0)
 RETURNS TABLE(targets jsonb, total_count bigint, total_accessible_count bigint)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
BEGIN
  RETURN QUERY
  WITH matched AS (
    SELECT t.id, t.target_id, t.program_slug, t.field, t.observation,
           t.ra, t.dec, t.redshift, t.redshift_auto, t.redshift_inspected,
           t.redshift_quality, t.spectral_features, t.dq_flags,
           t.max_snr, t.max_exposure_time,
           t.last_inspected_at, t.last_inspected_by, t.created_at, t.updated_at,
           t.object_id
    FROM targets t
    WHERE t.program_slug = ANY(p_program_slugs)
      AND (p_updated_since IS NULL OR t.updated_at > p_updated_since)
    ORDER BY t.target_id
    LIMIT p_limit OFFSET p_offset
  ),
  total AS (
    SELECT COUNT(*) AS cnt
    FROM targets t
    WHERE t.program_slug = ANY(p_program_slugs)
      AND (p_updated_since IS NULL OR t.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM targets t
    WHERE t.program_slug = ANY(p_program_slugs)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'target_id', m.target_id,
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
        'dq_flags', m.dq_flags,
        'max_snr', m.max_snr,
        'max_exposure_time', m.max_exposure_time,
        'last_inspected_at', m.last_inspected_at,
        'last_inspected_by', m.last_inspected_by,
        'created_at', m.created_at,
        'updated_at', m.updated_at,
        'spectra', COALESCE(
          (SELECT jsonb_agg(jsonb_build_object(
            'id', s.id,
            'target_id', s.target_id,
            'grating', s.grating,
            'fits_path', s.fits_path,
            'file_hash', s.file_hash,
            'file_size', s.file_size,
            'signal_to_noise', s.signal_to_noise,
            'exposure_time', s.exposure_time,
            'reduction_version', s.reduction_version
          )) FROM spectra s WHERE s.target_id = m.target_id),
          '[]'::jsonb
        ),
        'lists', COALESCE(
          (SELECT jsonb_agg(ol.slug ORDER BY ol.slug)
           FROM object_list_members olm
           JOIN object_lists ol ON ol.id = olm.list_id
           WHERE olm.object_id = m.object_id
             AND (ol.created_by = p_user_id OR ol.visibility IN ('public_read', 'public_edit'))),
          '[]'::jsonb
        )
      )
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m
  LEFT JOIN programs pr ON m.program_slug = pr.slug;
END;
$function$
;

GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_targets_for_sync(TEXT[], UUID, TIMESTAMP WITHOUT TIME ZONE, INTEGER, INTEGER) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_targets_for_sync(TEXT[], UUID, TIMESTAMP WITHOUT TIME ZONE, INTEGER, INTEGER) TO service_role;
