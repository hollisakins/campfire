-- Perf T1-5 (#501, epic #515): the three read-only admin RPCs are STABLE, not VOLATILE.
--
-- Hand-authored: no local Docker for `supabase db diff`. Matches
-- supabase/schemas/functions.sql.

CREATE OR REPLACE FUNCTION public.get_download_stats(p_days integer DEFAULT 30)
RETURNS json
-- STABLE (perf T1-5 / #501): read-only, so the planner may cache and
-- fold the call; it was declared VOLATILE by default.
LANGUAGE plpgsql STABLE SECURITY DEFINER
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

CREATE OR REPLACE FUNCTION public.get_storage_budget()
RETURNS json
-- STABLE (perf T1-5 / #501): read-only, so the planner may cache and
-- fold the call; it was declared VOLATILE by default.
LANGUAGE plpgsql STABLE SECURITY DEFINER
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

CREATE OR REPLACE FUNCTION public.get_lifecycle_status()
RETURNS json
-- STABLE (perf T1-5 / #501): read-only, so the planner may cache and
-- fold the call; it was declared VOLATILE by default.
LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $$
DECLARE
  v_is_admin boolean;
  v_has_status_col boolean;
  v_has_target_flag boolean;
  v_reader_threaded boolean;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'spectra' AND column_name = 'deploy_status'
  ) INTO v_has_status_col;

  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'targets' AND column_name = 'has_published_spectrum'
  ) INTO v_has_target_flag;

  -- A representative reader RPC must carry the predicate parameter — proof that
  -- B1's reader-threading (not just the column) is deployed.
  SELECT EXISTS (
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.proname = 'get_filtered_object_ids'
      AND pg_get_function_arguments(p.oid) LIKE '%p_include_unpublished%'
  ) INTO v_reader_threaded;

  RETURN json_build_object(
    'enabled', (v_has_status_col AND v_has_target_flag AND v_reader_threaded),
    'version', 1,
    'checks', json_build_object(
      'spectra_deploy_status', v_has_status_col,
      'targets_has_published_spectrum', v_has_target_flag,
      'reader_p_include_unpublished', v_reader_threaded
    )
  );
END;
$$;
