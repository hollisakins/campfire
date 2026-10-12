SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_storage_budget()
  RETURNS json
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
  result JSON;
  is_admin BOOLEAN;
  cap_bytes BIGINT := 20::BIGINT * 1024 * 1024 * 1024 * 1024;  -- 20 TB (OSN allocation)
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

  -- Everything that still occupies the bucket, at its stored size (see the
  -- header comment: every status but 'collected').
  SELECT COALESCE(SUM(COALESCE(stored_size_bytes, size_bytes)), 0) INTO registry_bytes
  FROM storage_objects
  WHERE status <> 'collected';

  SELECT COALESCE(SUM(total_size_bytes), 0) INTO tile_bytes
  FROM map_layers;

  total_bytes := registry_bytes + tile_bytes;

  SELECT json_build_object(
    'cap_bytes', cap_bytes,
    'total_bytes', total_bytes,
    'pct_used', ROUND((total_bytes::NUMERIC / NULLIF(cap_bytes, 0)) * 100, 2),
    'registry_bytes', registry_bytes,
    'tile_bytes', tile_bytes,
    -- P2 classes. live + archive + gc_pending = registry_bytes; collected occupies nothing.
    'by_class', json_build_object(
      'live', (SELECT json_build_object('count', COUNT(*), 'bytes', COALESCE(SUM(COALESCE(stored_size_bytes, size_bytes)), 0))
               FROM storage_objects WHERE status = 'active'),
      'archive', (SELECT json_build_object('count', COUNT(*), 'bytes', COALESCE(SUM(COALESCE(stored_size_bytes, size_bytes)), 0))
                  FROM storage_objects WHERE status = 'archived'),
      'gc_pending', (SELECT json_build_object('count', COUNT(*), 'bytes', COALESCE(SUM(COALESCE(stored_size_bytes, size_bytes)), 0))
                     FROM storage_objects WHERE status IN ('superseded', 'revoked')),
      'collected', (SELECT json_build_object('count', COUNT(*), 'bytes', 0,
                                             'logical_bytes', COALESCE(SUM(size_bytes), 0))
                    FROM storage_objects WHERE status = 'collected')
    ),
    'by_backend', (
      SELECT COALESCE(json_object_agg(backend, bytes), '{}'::json)
      FROM (
        SELECT backend, SUM(COALESCE(stored_size_bytes, size_bytes)) AS bytes
        FROM storage_objects WHERE status <> 'collected'
        GROUP BY backend
      ) t
    ),
    'by_bucket', (
      SELECT COALESCE(json_object_agg(bucket, bytes), '{}'::json)
      FROM (
        SELECT bucket, SUM(COALESCE(stored_size_bytes, size_bytes)) AS bytes
        FROM storage_objects WHERE status <> 'collected'
        GROUP BY bucket
      ) t
    ),
    'by_product_type', (
      SELECT COALESCE(json_object_agg(product_type, bytes), '{}'::json)
      FROM (
        SELECT product_type, SUM(COALESCE(stored_size_bytes, size_bytes)) AS bytes
        FROM storage_objects WHERE status <> 'collected'
        GROUP BY product_type
      ) t
    ),
    -- Every status, counted and at logical size (what the files measure
    -- locally); the bucket cost of each class is in by_class.
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
$function$;
