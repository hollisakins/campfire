-- Admin audit 2026-07-03, Phase 1 (docs/admin_audit_2026-07-03.md §3.E, §5).
--
-- 1. Admin list RPCs — one per admin list (deployments, deploy_events,
--    storage_objects, nircam_exposures) with whitelisted server-side sort,
--    windowed count(*) OVER() totals (replacing PostgREST count:'exact'),
--    page-size clamping, and an is_admin() gate. Plus:
--    * get_admin_exposure_neighbors — bounded ±window nav for the exposure
--      detail page (replaces the fetch-every-matching-id sessionStorage cache);
--    * get_admin_exposure_facets / get_admin_storage_facets — distinct facet
--      values for filter dropdowns (replacing fetch-all-then-dedupe-in-JS).
-- 2. Composite indexes for the RPCs' hot (filter, sort) shapes.
--    (No index on nircam_exposures(field,filter,filename): the
--    nircam_exposures_unique constraint already provides it.)
--
-- Hand-authored (additive-only DDL; function bodies match supabase/schemas/
-- verbatim): supabase db diff unavailable in the authoring environment.

CREATE INDEX IF NOT EXISTS idx_deployments_status_deployed
    ON public.deployments USING btree (status, deployed_at DESC);

CREATE INDEX IF NOT EXISTS idx_storage_objects_product_created
    ON public.storage_objects USING btree (product_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_storage_objects_status_created
    ON public.storage_objects USING btree (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_storage_objects_field
    ON public.storage_objects USING btree (field)
    WHERE field IS NOT NULL;

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_admin_deployments(
  p_status text DEFAULT NULL,
  p_instrument text DEFAULT NULL,       -- 'nirspec' | 'nircam' | NULL
  p_sort_column text DEFAULT 'deployed_at',
  p_sort_direction text DEFAULT 'desc',
  p_page integer DEFAULT 1,
  p_page_size integer DEFAULT 50
)
RETURNS TABLE (
  id integer,
  observation text,
  field text,
  status text,
  n_targets integer,
  n_spectra integer,
  cfpipe_version text,
  deployed_at timestamptz,
  published_at timestamptz,
  revoked_at timestamptz,
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
  IF p_sort_column NOT IN ('id', 'deployed_at', 'status', 'scope') THEN
    p_sort_column := 'deployed_at';
  END IF;

  RETURN QUERY
  SELECT d.id, d.observation, d.field, d.status, d.n_targets, d.n_spectra,
         d.cfpipe_version, d.deployed_at, d.published_at, d.revoked_at,
         count(*) OVER ()
  FROM deployments d
  WHERE (p_status IS NULL OR d.status = p_status)
    AND (p_instrument IS NULL
         OR (p_instrument = 'nirspec' AND d.observation IS NOT NULL)
         OR (p_instrument = 'nircam'  AND d.field IS NOT NULL))
  ORDER BY
    CASE WHEN p_sort_column = 'deployed_at' AND p_sort_direction = 'desc' THEN d.deployed_at END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'deployed_at' AND p_sort_direction = 'asc'  THEN d.deployed_at END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'status' AND p_sort_direction = 'desc' THEN d.status END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'status' AND p_sort_direction = 'asc'  THEN d.status END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'scope' AND p_sort_direction = 'desc' THEN COALESCE(d.observation, d.field) END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'scope' AND p_sort_direction = 'asc'  THEN COALESCE(d.observation, d.field) END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'id' AND p_sort_direction = 'asc' THEN d.id END ASC,
    d.id DESC
  OFFSET v_offset LIMIT v_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_deployments TO authenticated;

-- Audit-log browse. Folds the NIRCam scope (metadata->>'field' until
-- deploy_events grows a field column in Phase 3) and the actor display name
-- (full_name, falling back to username) into the row, replacing two extra
-- client round-trips.
CREATE OR REPLACE FUNCTION public.get_admin_deploy_events(
  p_action text DEFAULT NULL,
  p_observation text DEFAULT NULL,
  p_field text DEFAULT NULL,
  p_sort_column text DEFAULT 'occurred_at',
  p_sort_direction text DEFAULT 'desc',
  p_page integer DEFAULT 1,
  p_page_size integer DEFAULT 50
)
RETURNS TABLE (
  id uuid,
  action text,
  observation text,
  field text,
  deployment_id integer,
  status_to text,
  affected_count integer,
  occurred_at timestamptz,
  actor uuid,
  actor_name text,
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
  IF p_sort_column NOT IN ('occurred_at', 'action') THEN
    p_sort_column := 'occurred_at';
  END IF;

  RETURN QUERY
  SELECT e.id, e.action, e.observation,
         (e.metadata ->> 'field') AS field,
         e.deployment_id, e.status_to, e.affected_count, e.occurred_at,
         e.actor,
         COALESCE(up.full_name, up.username) AS actor_name,
         count(*) OVER ()
  FROM deploy_events e
  LEFT JOIN user_profiles up ON up.user_id = e.actor
  WHERE (p_action IS NULL OR e.action = p_action)
    AND (p_observation IS NULL OR e.observation = p_observation)
    AND (p_field IS NULL OR e.metadata ->> 'field' = p_field)
  ORDER BY
    CASE WHEN p_sort_column = 'occurred_at' AND p_sort_direction = 'desc' THEN e.occurred_at END DESC,
    CASE WHEN p_sort_column = 'occurred_at' AND p_sort_direction = 'asc'  THEN e.occurred_at END ASC,
    CASE WHEN p_sort_column = 'action' AND p_sort_direction = 'desc' THEN e.action END DESC,
    CASE WHEN p_sort_column = 'action' AND p_sort_direction = 'asc'  THEN e.action END ASC,
    e.occurred_at DESC
  OFFSET v_offset LIMIT v_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_deploy_events TO authenticated;

CREATE OR REPLACE FUNCTION public.get_admin_storage_objects(
  p_product_type text DEFAULT NULL,
  p_status text DEFAULT NULL,
  p_field text DEFAULT NULL,
  p_observation text DEFAULT NULL,
  p_backend text DEFAULT NULL,
  p_sort_column text DEFAULT 'created_at',
  p_sort_direction text DEFAULT 'desc',
  p_page integer DEFAULT 1,
  p_page_size integer DEFAULT 50
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

CREATE OR REPLACE FUNCTION public.get_admin_exposures(
  p_field text DEFAULT NULL,
  p_filter text DEFAULT NULL,
  p_detector text DEFAULT NULL,
  p_review_status text DEFAULT NULL,
  p_stage text DEFAULT NULL,
  p_masking text DEFAULT NULL,
  p_correction text DEFAULT NULL,
  p_sort_column text DEFAULT 'filename',   -- 'filename' = the compound (field, filter, filename) list order
  p_sort_direction text DEFAULT 'asc',
  p_page integer DEFAULT 1,
  p_page_size integer DEFAULT 50
)
RETURNS TABLE (
  id integer,
  field text,
  filter text,
  detector text,
  filename text,
  visit text,
  date_obs timestamp without time zone,
  ra_center double precision,
  dec_center double precision,
  stage text,
  review_status text,
  masking text,
  correction text,
  png_path text,
  full_png_path text,
  image_width integer,
  image_height integer,
  mask_regions jsonb,
  notes text,
  created_at timestamp without time zone,
  updated_at timestamp without time zone,
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
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF p_sort_column NOT IN ('filename', 'field', 'filter', 'detector', 'stage',
                           'review_status', 'date_obs', 'updated_at') THEN
    p_sort_column := 'filename';
  END IF;

  RETURN QUERY
  SELECT e.id, e.field, e.filter, e.detector, e.filename, e.visit, e.date_obs,
         e.ra_center, e.dec_center, e.stage, e.review_status, e.masking,
         e.correction, e.png_path, e.full_png_path, e.image_width,
         e.image_height, e.mask_regions, e.notes, e.created_at, e.updated_at,
         count(*) OVER ()
  FROM nircam_exposures e
  WHERE (p_field IS NULL OR e.field = p_field)
    AND (p_filter IS NULL OR e.filter = p_filter)
    AND (p_detector IS NULL OR e.detector = p_detector)
    AND (p_review_status IS NULL OR e.review_status = p_review_status)
    AND (p_stage IS NULL OR e.stage = p_stage)
    AND (p_masking IS NULL OR e.masking = p_masking)
    AND (p_correction IS NULL OR e.correction = p_correction)
  ORDER BY
    -- Keep in lockstep with get_admin_exposure_neighbors.
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.field END ASC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.filter END ASC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.filename END ASC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.field END DESC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.filter END DESC,
    CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.filename END DESC,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc'  THEN e.field END ASC,
    CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN e.field END DESC,
    CASE WHEN p_sort_column = 'filter' AND p_sort_direction = 'asc'  THEN e.filter END ASC,
    CASE WHEN p_sort_column = 'filter' AND p_sort_direction = 'desc' THEN e.filter END DESC,
    CASE WHEN p_sort_column = 'detector' AND p_sort_direction = 'asc'  THEN e.detector END ASC,
    CASE WHEN p_sort_column = 'detector' AND p_sort_direction = 'desc' THEN e.detector END DESC,
    CASE WHEN p_sort_column = 'stage' AND p_sort_direction = 'asc'  THEN e.stage END ASC,
    CASE WHEN p_sort_column = 'stage' AND p_sort_direction = 'desc' THEN e.stage END DESC,
    CASE WHEN p_sort_column = 'review_status' AND p_sort_direction = 'asc'  THEN e.review_status END ASC,
    CASE WHEN p_sort_column = 'review_status' AND p_sort_direction = 'desc' THEN e.review_status END DESC,
    CASE WHEN p_sort_column = 'date_obs' AND p_sort_direction = 'asc'  THEN e.date_obs END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'date_obs' AND p_sort_direction = 'desc' THEN e.date_obs END DESC NULLS LAST,
    CASE WHEN p_sort_column = 'updated_at' AND p_sort_direction = 'asc'  THEN e.updated_at END ASC NULLS LAST,
    CASE WHEN p_sort_column = 'updated_at' AND p_sort_direction = 'desc' THEN e.updated_at END DESC NULLS LAST,
    e.id ASC
  OFFSET v_offset LIMIT v_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_exposures TO authenticated;

-- The detail page's prev/next nav: absolute positions of the current exposure
-- and its ±p_window neighbors within the SAME filtered, ordered set the list
-- shows. Bounded transfer (≤ 2*window+1 rows) — replaces the unbounded
-- fetch-every-matching-id nav cache. Returns zero rows if p_current_id does
-- not match the filters.
CREATE OR REPLACE FUNCTION public.get_admin_exposure_neighbors(
  p_current_id integer,
  p_field text DEFAULT NULL,
  p_filter text DEFAULT NULL,
  p_detector text DEFAULT NULL,
  p_review_status text DEFAULT NULL,
  p_stage text DEFAULT NULL,
  p_masking text DEFAULT NULL,
  p_correction text DEFAULT NULL,
  p_sort_column text DEFAULT 'filename',
  p_sort_direction text DEFAULT 'asc',
  p_window integer DEFAULT 3
)
RETURNS TABLE (
  id integer,
  position bigint,
  total_count bigint
)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
  v_window integer := LEAST(GREATEST(COALESCE(p_window, 3), 1), 25);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF p_sort_column NOT IN ('filename', 'field', 'filter', 'detector', 'stage',
                           'review_status', 'date_obs', 'updated_at') THEN
    p_sort_column := 'filename';
  END IF;

  RETURN QUERY
  WITH ranked AS (
    SELECT e.id AS exp_id,
           row_number() OVER (ORDER BY
             -- Keep in lockstep with get_admin_exposures.
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.field END ASC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.filter END ASC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'asc'  THEN e.filename END ASC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.field END DESC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.filter END DESC,
             CASE WHEN p_sort_column = 'filename' AND p_sort_direction = 'desc' THEN e.filename END DESC,
             CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc'  THEN e.field END ASC,
             CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN e.field END DESC,
             CASE WHEN p_sort_column = 'filter' AND p_sort_direction = 'asc'  THEN e.filter END ASC,
             CASE WHEN p_sort_column = 'filter' AND p_sort_direction = 'desc' THEN e.filter END DESC,
             CASE WHEN p_sort_column = 'detector' AND p_sort_direction = 'asc'  THEN e.detector END ASC,
             CASE WHEN p_sort_column = 'detector' AND p_sort_direction = 'desc' THEN e.detector END DESC,
             CASE WHEN p_sort_column = 'stage' AND p_sort_direction = 'asc'  THEN e.stage END ASC,
             CASE WHEN p_sort_column = 'stage' AND p_sort_direction = 'desc' THEN e.stage END DESC,
             CASE WHEN p_sort_column = 'review_status' AND p_sort_direction = 'asc'  THEN e.review_status END ASC,
             CASE WHEN p_sort_column = 'review_status' AND p_sort_direction = 'desc' THEN e.review_status END DESC,
             CASE WHEN p_sort_column = 'date_obs' AND p_sort_direction = 'asc'  THEN e.date_obs END ASC NULLS LAST,
             CASE WHEN p_sort_column = 'date_obs' AND p_sort_direction = 'desc' THEN e.date_obs END DESC NULLS LAST,
             CASE WHEN p_sort_column = 'updated_at' AND p_sort_direction = 'asc'  THEN e.updated_at END ASC NULLS LAST,
             CASE WHEN p_sort_column = 'updated_at' AND p_sort_direction = 'desc' THEN e.updated_at END DESC NULLS LAST,
             e.id ASC
           ) AS rn,
           count(*) OVER () AS n
    FROM nircam_exposures e
    WHERE (p_field IS NULL OR e.field = p_field)
      AND (p_filter IS NULL OR e.filter = p_filter)
      AND (p_detector IS NULL OR e.detector = p_detector)
      AND (p_review_status IS NULL OR e.review_status = p_review_status)
      AND (p_stage IS NULL OR e.stage = p_stage)
      AND (p_masking IS NULL OR e.masking = p_masking)
      AND (p_correction IS NULL OR e.correction = p_correction)
  ),
  cur AS (
    SELECT r.rn AS rn0 FROM ranked r WHERE r.exp_id = p_current_id
  )
  SELECT r.exp_id, r.rn, r.n
  FROM ranked r, cur
  WHERE r.rn BETWEEN cur.rn0 - v_window AND cur.rn0 + v_window
  ORDER BY r.rn;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_exposure_neighbors TO authenticated;

-- Distinct facet values for the admin filter dropdowns, one grouped scan per
-- facet — replaces the fetch-every-row-then-Set() option builders.
CREATE OR REPLACE FUNCTION public.get_admin_exposure_facets()
RETURNS TABLE (kind text, value text)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  RETURN QUERY
  SELECT 'field'::text, e.field FROM nircam_exposures e GROUP BY e.field
  UNION ALL
  SELECT 'filter'::text, e.filter FROM nircam_exposures e GROUP BY e.filter
  UNION ALL
  SELECT 'detector'::text, e.detector FROM nircam_exposures e GROUP BY e.detector
  UNION ALL
  SELECT 'stage'::text, e.stage FROM nircam_exposures e GROUP BY e.stage
  ORDER BY 1, 2;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_exposure_facets() TO authenticated;

CREATE OR REPLACE FUNCTION public.get_admin_storage_facets()
RETURNS TABLE (kind text, value text)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  RETURN QUERY
  SELECT 'product_type'::text, so.product_type FROM storage_objects so GROUP BY so.product_type
  UNION ALL
  SELECT 'status'::text, so.status FROM storage_objects so GROUP BY so.status
  UNION ALL
  SELECT 'backend'::text, so.backend FROM storage_objects so GROUP BY so.backend
  UNION ALL
  SELECT 'field'::text, so.field FROM storage_objects so WHERE so.field IS NOT NULL GROUP BY so.field
  UNION ALL
  SELECT 'observation'::text, so.observation FROM storage_objects so WHERE so.observation IS NOT NULL GROUP BY so.observation
  ORDER BY 1, 2;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_storage_facets() TO authenticated;
