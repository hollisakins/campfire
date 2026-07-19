-- Add sci_dq_hash to the get_storage_objects_for_sync payload (storage
-- unification): the client mirror needs the science-only NIRCam identity to
-- plan pushes (dedup) without a live registry query. Column already exists on
-- storage_objects (epic #261, N1); this only extends the sync JSONB.
--
-- NOTE: hand-trimmed after `supabase db diff` — the diff engine's untracked-MV
-- limitation (AGENTS.md) emitted spurious drop/recreate statements for
-- mv_filter_options / mv_programs_overview / two views; those are unchanged in
-- the schema files and are omitted here (recreating the MVs would drop their
-- indexes and data).

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_storage_objects_for_sync(p_program_slugs text[], p_updated_since timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0, p_include_counts boolean DEFAULT true, p_include_unpublished boolean DEFAULT false)
 RETURNS TABLE(objects jsonb, total_count bigint, total_accessible_count bigint)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
 SET statement_timeout TO '120s'
AS $function$
BEGIN
  RETURN QUERY
  WITH scoped AS (
    SELECT so.*
    FROM storage_objects so
    WHERE so.status = 'active'
      AND (
        p_include_unpublished
        OR (so.spectrum_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM spectra s
              JOIN targets t ON t.target_id = s.target_id
              WHERE s.spectrum_id = so.spectrum_id
                AND s.deploy_status = 'published'
                AND t.program_slug = ANY(p_program_slugs)))
        OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM deployments d
              LEFT JOIN observations o ON o.name = d.observation
              WHERE d.id = so.deployment_id
                AND d.status = 'published'
                -- NIRCam field deploy (epic #261, N1): multi-program, public to all
                -- when published. NIRSpec obs deploy stays program-scoped.
                AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))))
      )
  ),
  matched AS MATERIALIZED (
    SELECT * FROM scoped
    WHERE (p_updated_since IS NULL OR scoped.updated_at > p_updated_since)
    ORDER BY scoped.storage_key
    LIMIT p_limit OFFSET p_offset
  ),
  total AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
      AND (p_updated_since IS NULL OR scoped.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'backend', m.backend,
        'bucket', m.bucket,
        'storage_key', m.storage_key,
        'content_hash', m.content_hash,
        'sci_dq_hash', m.sci_dq_hash,
        'size_bytes', m.size_bytes,
        'content_type', m.content_type,
        'product_type', m.product_type,
        'instrument', m.instrument,
        'status', m.status,
        'observation', m.observation,
        'field', m.field,
        'filter', m.filter,
        'spectrum_id', m.spectrum_id,
        'exposure_ref', m.exposure_ref,
        'deployment_id', m.deployment_id,
        'cfpipe_version', m.cfpipe_version,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$function$
;
