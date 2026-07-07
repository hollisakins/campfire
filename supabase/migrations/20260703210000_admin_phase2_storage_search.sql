-- Admin audit 2026-07-03, Phase 2 (docs/admin_audit_2026-07-03.md Theme C / §5):
-- substring key search on the registry browser.
--
-- Appending p_search CHANGES the arity of get_admin_storage_objects, so a bare
-- CREATE OR REPLACE would leave the old 9-arg overload in place and PostgREST
-- would then see two candidates (PGRST203). DROP the old overload first, then
-- create the 10-arg version. The DROP signature must match the deployed one
-- exactly (7 text + 2 integer).
--
-- Hand-authored (function body matches supabase/schemas/functions.sql verbatim;
-- supabase db diff unavailable in the authoring environment).

DROP FUNCTION IF EXISTS public.get_admin_storage_objects(
  text, text, text, text, text, text, text, integer, integer);

-- Trigram GIN for substring ILIKE on storage_key (opclass schema-qualified —
-- pg_trgm lives in public, like idx_comments_content_trgm).
CREATE INDEX IF NOT EXISTS idx_storage_objects_key_trgm
    ON public.storage_objects USING gin (storage_key public.gin_trgm_ops);

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_admin_storage_objects(
  p_product_type text DEFAULT NULL,
  p_status text DEFAULT NULL,
  p_field text DEFAULT NULL,
  p_observation text DEFAULT NULL,
  p_backend text DEFAULT NULL,
  p_sort_column text DEFAULT 'created_at',
  p_sort_direction text DEFAULT 'desc',
  p_page integer DEFAULT 1,
  p_page_size integer DEFAULT 50,
  p_search text DEFAULT NULL              -- substring ILIKE on storage_key
)
RETURNS TABLE (
  id bigint,
  storage_key text,
  product_type text,
  instrument text,
  observation text,
  field text,
  exposure_ref text,
  size_bytes bigint,
  content_hash text,
  backend text,
  status text,
  cfpipe_version text,
  created_at timestamptz,
  total_count bigint
)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
  v_limit integer := LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
  v_offset integer := GREATEST(COALESCE(p_page, 1) - 1, 0) * LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'desc'; END IF;
  IF p_sort_column NOT IN ('created_at', 'size_bytes', 'product_type', 'storage_key',
                           'observation', 'field', 'status') THEN
    p_sort_column := 'created_at';
  END IF;

  RETURN QUERY
  SELECT so.id, so.storage_key, so.product_type, so.instrument, so.observation,
         so.field, so.exposure_ref, so.size_bytes, so.content_hash, so.backend,
         so.status, so.cfpipe_version, so.created_at,
         count(*) OVER ()
  FROM storage_objects so
  WHERE (p_product_type IS NULL OR so.product_type = p_product_type)
    AND (p_status IS NULL OR so.status = p_status)
    AND (p_field IS NULL OR so.field = p_field)
    AND (p_observation IS NULL OR so.observation = p_observation)
    AND (p_backend IS NULL OR so.backend = p_backend)
    AND (p_search IS NULL OR so.storage_key ILIKE '%' || p_search || '%')
  ORDER BY
    CASE WHEN p_sort_column = 'created_at' AND p_sort_direction = 'desc' THEN so.created_at END DESC,
    CASE WHEN p_sort_column = 'created_at' AND p_sort_direction = 'asc'  THEN so.created_at END ASC,
    CASE WHEN p_sort_column = 'size_bytes' AND p_sort_direction = 'desc' THEN so.size_bytes END DESC,
    CASE WHEN p_sort_column = 'size_bytes' AND p_sort_direction = 'asc'  THEN so.size_bytes END ASC,
    CASE WHEN p_sort_column = 'product_type' AND p_sort_direction = 'desc' THEN so.product_type END DESC,
    CASE WHEN p_sort_column = 'product_type' AND p_sort_direction = 'asc'  THEN so.product_type END ASC,
    CASE WHEN p_sort_column = 'storage_key' AND p_sort_direction = 'desc' THEN so.storage_key END DESC,
    CASE WHEN p_sort_column = 'storage_key' AND p_sort_direction = 'asc'  THEN so.storage_key END ASC,
    CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN so.observation END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc'  THEN so.observation END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN so.field END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc'  THEN so.field END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'status' AND p_sort_direction = 'desc' THEN so.status END DESC,
    CASE WHEN p_sort_column = 'status' AND p_sort_direction = 'asc'  THEN so.status END ASC,
    so.id DESC
  OFFSET v_offset LIMIT v_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_storage_objects TO authenticated;

