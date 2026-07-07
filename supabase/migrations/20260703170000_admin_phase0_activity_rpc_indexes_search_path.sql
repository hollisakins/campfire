-- Admin audit 2026-07-03, Phase 0 (docs/admin_audit_2026-07-03.md).
--
-- 1. Indexes for admin-panel access paths that full-scanned:
--    * storage_objects(created_at DESC)      — registry browser default sort (P4)
--    * deploy_events(observation)            — audit-log observation filter (P4)
--    * deployments(field)                    — NIRCam field-scoped lookups (B5)
-- 2. get_activity_feed / get_activity_users — server-side replacement for the
--    /api/admin/activity route that previously fetched every comment + audit
--    row per page load and sorted in JS (P1).
-- 3. SET search_path on get_download_stats / get_storage_budget — SECURITY
--    DEFINER functions previously had a mutable search path (F2). Bodies
--    otherwise unchanged.
--
-- Hand-authored (additive-only DDL; matches supabase/schemas/ verbatim):
-- supabase db diff was unavailable in the authoring environment.

CREATE INDEX IF NOT EXISTS idx_storage_objects_created_at
    ON public.storage_objects USING btree (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_deploy_events_observation
    ON public.deploy_events USING btree (observation)
    WHERE observation IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_deployments_field
    ON public.deployments USING btree (field)
    WHERE field IS NOT NULL;

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_download_stats(p_days integer DEFAULT 30)
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  result JSON;
  is_admin BOOLEAN;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO is_admin
  FROM user_profiles up
  WHERE up.user_id = auth.uid();

  IF NOT is_admin THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT json_build_object(
    'total_downloads', (
      SELECT COUNT(*) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'unique_users', (
      SELECT COUNT(DISTINCT user_id) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'by_type', (
      SELECT json_object_agg(download_type, count)
      FROM (
        SELECT download_type, COUNT(*) as count
        FROM download_log
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY download_type
      ) t
    ),
    'total_files', (
      SELECT COALESCE(SUM(file_count), 0) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'total_targets', (
      SELECT COALESCE(SUM(target_count), 0) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'recent_downloads', (
      SELECT json_agg(t)
      FROM (
        SELECT
          dl.id,
          dl.download_type,
          dl.target_count,
          dl.file_count,
          dl.requested_at,
          au.email,
          up.full_name
        FROM download_log dl
        LEFT JOIN auth.users au ON dl.user_id = au.id
        LEFT JOIN user_profiles up ON dl.user_id = up.user_id
        WHERE dl.requested_at >= NOW() - (p_days || ' days')::INTERVAL
        ORDER BY dl.requested_at DESC
        LIMIT 50
      ) t
    ),
    'most_downloaded_targets', (
      SELECT json_agg(t)
      FROM (
        SELECT
          target_id,
          COUNT(*) as download_count
        FROM download_log, unnest(target_ids) as target_id
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY target_id
        ORDER BY download_count DESC
        LIMIT 20
      ) t
    ),
    'downloads_by_day', (
      SELECT json_agg(t ORDER BY day)
      FROM (
        SELECT
          DATE(requested_at) as day,
          COUNT(*) as count
        FROM download_log
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY DATE(requested_at)
      ) t
    )
  ) INTO result;

  RETURN result;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_download_stats TO authenticated;


-- =============================================================================
-- get_activity_feed / get_activity_users (admin activity feed)
-- =============================================================================
-- Server-side replacement for the /api/admin/activity route's fetch-everything-
-- then-sort-in-JS approach: UNION the two activity sources (comments +
-- flag_audit_log), filter/sort/paginate in one scan, and return the page total
-- via a window count. Semantics mirror the route exactly:
--   * comments are joined to targets (inner — object-level comments are not
--     surfaced in the feed, matching the previous targets!inner embed);
--   * inspection rows label their subject as target -> object -> spectrum
--     (spectra label = target_id/grating), degrading to '' if all FKs are NULL;
--   * the user filter matches rows whose user_id is in p_user_ids, plus (for
--     inspections) NULL-user system rows when p_include_system. No user filter
--     at all (empty p_user_ids, p_include_system=false) means everything.
-- SECURITY DEFINER because flag_audit_log's RLS is access-scoped, not
-- admin-scoped; the admin gate here is the authorization boundary.

CREATE OR REPLACE FUNCTION public.get_activity_feed(
  p_include_comments boolean DEFAULT true,
  p_include_inspections boolean DEFAULT true,
  p_user_ids uuid[] DEFAULT NULL,
  p_include_system boolean DEFAULT false,
  p_field_names text[] DEFAULT NULL,
  p_page integer DEFAULT 1,
  p_page_size integer DEFAULT 50
)
RETURNS TABLE (
  id text,
  type text,
  target_db_id integer,
  target_display_id text,
  user_id uuid,
  ts timestamp without time zone,
  content text,
  edited_at timestamp without time zone,
  field_name text,
  old_value integer,
  new_value integer,
  user_full_name text,
  user_is_group_account boolean,
  total_count bigint
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_has_user_filter boolean :=
    (p_user_ids IS NOT NULL AND array_length(p_user_ids, 1) > 0) OR p_include_system;
  v_limit integer := LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 100);
  v_offset integer := GREATEST(COALESCE(p_page, 1) - 1, 0) * LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 100);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  RETURN QUERY
  WITH feed AS (
    SELECT
      'comment-' || c.id AS id,
      'comment'::text AS type,
      c.target_id AS target_db_id,
      t.target_id AS target_display_id,
      c.user_id,
      c.created_at AS ts,
      c.content,
      c.edited_at,
      NULL::text AS field_name,
      NULL::integer AS old_value,
      NULL::integer AS new_value
    FROM comments c
    JOIN targets t ON t.id = c.target_id
    WHERE p_include_comments
      AND NOT c.is_deleted
      AND (NOT v_has_user_filter
           OR c.user_id = ANY(COALESCE(p_user_ids, '{}'::uuid[])))

    UNION ALL

    SELECT
      'audit-' || f.id,
      'inspection'::text,
      COALESCE(f.target_id, f.object_id, f.spectrum_id, 0),
      COALESCE(
        t.target_id,
        o.object_id,
        CASE WHEN s.id IS NOT NULL THEN s.target_id || '/' || s.grating END,
        ''
      ),
      f.user_id,
      f.changed_at,
      NULL::text,
      NULL::timestamp without time zone,
      f.field_name,
      f.old_value,
      f.new_value
    FROM flag_audit_log f
    LEFT JOIN targets t ON t.id = f.target_id
    LEFT JOIN objects o ON o.id = f.object_id
    LEFT JOIN spectra s ON s.id = f.spectrum_id
    WHERE p_include_inspections
      AND (p_field_names IS NULL OR f.field_name = ANY(p_field_names))
      AND (NOT v_has_user_filter
           OR f.user_id = ANY(COALESCE(p_user_ids, '{}'::uuid[]))
           OR (p_include_system AND f.user_id IS NULL))
  )
  SELECT
    feed.id,
    feed.type,
    feed.target_db_id,
    feed.target_display_id,
    feed.user_id,
    feed.ts,
    feed.content,
    feed.edited_at,
    feed.field_name,
    feed.old_value,
    feed.new_value,
    up.full_name,
    COALESCE(up.is_group_account, false),
    count(*) OVER ()
  FROM feed
  LEFT JOIN user_profiles up ON up.user_id = feed.user_id
  ORDER BY feed.ts DESC, feed.id DESC
  OFFSET v_offset
  LIMIT v_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_activity_feed TO authenticated;

-- The activity page's user-filter dropdown: distinct users with any activity.
-- A NULL user_id row signals system-generated activity (NULL-user audit rows);
-- the route maps it to its synthetic "System" entry.
CREATE OR REPLACE FUNCTION public.get_activity_users()
RETURNS TABLE (
  user_id uuid,
  full_name text
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  RETURN QUERY
  WITH active AS (
    SELECT DISTINCT c.user_id FROM comments c WHERE NOT c.is_deleted
    UNION
    SELECT DISTINCT f.user_id FROM flag_audit_log f
  )
  SELECT a.user_id, up.full_name
  FROM active a
  LEFT JOIN user_profiles up ON up.user_id = a.user_id
  ORDER BY (a.user_id IS NULL) DESC, up.full_name ASC NULLS LAST;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_activity_users TO authenticated;


-- =============================================================================
-- get_storage_budget (epic #210, F1)
-- =============================================================================
-- Bytes-at-rest budget against the 20 TB cap. Egress is free (OSN academic
-- service), so the budget tracks storage only. Sums active storage_objects rows
-- (data bucket) plus aggregated tile bytes from map_layers (tiles are kept on R2
-- and intentionally not indexed per-object). SECURITY DEFINER + admin gate so a
-- non-admin cannot enumerate the registry. Used by `campfire deploy registry budget`.

CREATE OR REPLACE FUNCTION public.get_storage_budget()
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  result JSON;
  is_admin BOOLEAN;
  cap_bytes BIGINT := 20::BIGINT * 1024 * 1024 * 1024 * 1024;  -- 20 TB
  registry_bytes BIGINT;
  tile_bytes BIGINT;
  total_bytes BIGINT;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO is_admin
  FROM user_profiles up
  WHERE up.user_id = auth.uid();

  -- Admin (web/CLI login) OR service_role (CLI --local / headless deploy). Both are
  -- trusted callers of the registry; everyone else is denied. NULL is_admin (no uid /
  -- no profile) coalesces to false so the gate is fail-closed — stronger than the
  -- get_download_stats pattern, which relies on authenticated-only EXECUTE to mask it.
  IF NOT (COALESCE(is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT COALESCE(SUM(size_bytes), 0) INTO registry_bytes
  FROM storage_objects
  WHERE status = 'active';

  SELECT COALESCE(SUM(total_size_bytes), 0) INTO tile_bytes
  FROM map_layers;

  total_bytes := registry_bytes + tile_bytes;

  SELECT json_build_object(
    'cap_bytes', cap_bytes,
    'total_bytes', total_bytes,
    'pct_used', ROUND((total_bytes::NUMERIC / NULLIF(cap_bytes, 0)) * 100, 2),
    'registry_bytes', registry_bytes,
    'tile_bytes', tile_bytes,
    'by_backend', (
      SELECT COALESCE(json_object_agg(backend, bytes), '{}'::json)
      FROM (
        SELECT backend, SUM(size_bytes) AS bytes
        FROM storage_objects WHERE status = 'active'
        GROUP BY backend
      ) t
    ),
    'by_bucket', (
      SELECT COALESCE(json_object_agg(bucket, bytes), '{}'::json)
      FROM (
        SELECT bucket, SUM(size_bytes) AS bytes
        FROM storage_objects WHERE status = 'active'
        GROUP BY bucket
      ) t
    ),
    'by_product_type', (
      SELECT COALESCE(json_object_agg(product_type, bytes), '{}'::json)
      FROM (
        SELECT product_type, SUM(size_bytes) AS bytes
        FROM storage_objects WHERE status = 'active'
        GROUP BY product_type
      ) t
    ),
    'by_status', (
      SELECT COALESCE(json_object_agg(status, json_build_object('count', cnt, 'bytes', bytes)), '{}'::json)
      FROM (
        SELECT status, COUNT(*) AS cnt, SUM(size_bytes) AS bytes
        FROM storage_objects
        GROUP BY status
      ) t
    )
  ) INTO result;

  RETURN result;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_storage_budget TO authenticated, service_role;
