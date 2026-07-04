-- storage_objects.filter — a typed, indexed scope column for per-filter NIRCam
-- products, denormalized from the campfire_layout key's <field>/<filter>/ segment.
--
-- Motivation: `campfire download --field <f> --filters <...>` (and any per-filter
-- registry aggregate/MV) needs to scope by filter without parsing storage keys at
-- query time. This mirrors the existing observation/field/spectrum_id/exposure_ref
-- scope columns. deploy/row_for_key populates it from parse_key(...).scope.filt;
-- the sync RPC now ships it to the client mirror.
--
-- Additive + nullable: NULL for every existing row, for NIRSpec, and for field-
-- level products (no filter concept). Existing NIRCam rows are repopulated by
-- re-running `campfire deploy registry backfill` / a re-deploy (both route through
-- row_for_key). No seed change required.
--
-- Companion layout change (same PR): NIRCam expmaps moved from
-- products/nircam/<field>/expmaps/ into the canonical filter dir
-- products/nircam/<field>/<filter>/expmap_*.fits, so they too carry a real filter.
-- Their old-key rows are superseded on the next deploy.

alter table "public"."storage_objects" add column "filter" text;

comment on column "public"."storage_objects"."filter" is
  'Typed, indexed scope column for per-filter NIRCam products (nircam_exposure/_preview/_full, nircam_mosaic, nircam_expmap), denormalized from the campfire_layout key''s <field>/<filter>/ segment by deploy/row_for_key. NULL for NIRSpec and field-level products (no filter concept). Mirrors observation/field/spectrum_id — lets the registry filter/aggregate by filter without parsing keys.';

-- Partial: only per-filter NIRCam rows carry a filter (most rows are NIRSpec).
create index if not exists idx_storage_objects_field_filter
    on public.storage_objects using btree ("field", "filter")
    where "filter" is not null;

-- Ship the new column to the Python client's local storage_objects mirror.
CREATE OR REPLACE FUNCTION public.get_storage_objects_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_offset INTEGER DEFAULT 0,
  p_include_counts BOOLEAN DEFAULT TRUE,
  p_include_unpublished BOOLEAN DEFAULT FALSE
)
RETURNS TABLE(objects JSONB, total_count BIGINT, total_accessible_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
SET statement_timeout = '120s'
AS $$
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
$$;
