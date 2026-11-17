SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.db_stall_status()
  RETURNS jsonb
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'pg_catalog'
  AS $function$
  SELECT jsonb_build_object(
           'wal_waiters', count(*) FILTER (WHERE s.wal AND s.age_s >= 5),
           'wal_wait_s', COALESCE(round(max(s.age_s) FILTER (WHERE s.wal)), 0),
           'lock_waiters', count(*) FILTER (WHERE s.lck AND s.age_s >= 30),
           'lock_wait_s', COALESCE(round(max(s.age_s) FILTER (WHERE s.lck)), 0))
  FROM (SELECT extract(epoch FROM now() - a.state_change) AS age_s,
               (a.wait_event_type = 'IO' AND a.wait_event IN ('WalSync', 'WalWrite', 'WalInitSync', 'WalInitWrite'))
                 OR (a.wait_event_type = 'LWLock' AND a.wait_event IN ('WALWrite', 'WALInsert')) AS wal,
               a.wait_event_type = 'Lock' AS lck
        FROM pg_stat_activity a
        WHERE a.state = 'active'
          AND a.backend_type = 'client backend'
          AND a.pid <> pg_backend_pid()) s;
$function$;

REVOKE ALL ON FUNCTION "public"."db_stall_status"() FROM PUBLIC, "anon", "authenticated";

COMMENT ON FUNCTION "public"."db_stall_status"() IS 'Backends stuck on the WAL flush (>= 5 s) or on a lock (>= 30 s), with the oldest wait of each, for the web status probe behind /api/health and the degraded banner. A read, so it answers while commits hang on a stalled disk.';

REVOKE ALL ON FUNCTION "public"."db_stall_status"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."db_stall_status"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."db_stall_status"() TO "service_role";
