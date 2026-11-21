SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- deployments.completed_at: the deploy CLI's completion marker (stamped as the
-- last write of a deploy with no failed upload), and get_admin_deployments
-- returns it so the admin views can flag incomplete deploys. Nullable, no
-- default: a catalog-only ALTER. Older rows: scripts/backfill_deployments_completed_at.sql.

SET local check_function_bodies = off;

DROP FUNCTION "public"."get_admin_deployments"(text, text, text, text, integer, integer);

ALTER TABLE "public"."deployments"
  ADD COLUMN "completed_at" timestamp WITH time zone;

CREATE OR REPLACE FUNCTION public.get_admin_deployments (
  p_status         text    DEFAULT NULL::text,
  p_instrument     text    DEFAULT NULL::text,
  p_sort_column    text    DEFAULT 'deployed_at'::text,
  p_sort_direction text    DEFAULT 'desc'::text,
  p_page           integer DEFAULT 1,
  p_page_size      integer DEFAULT 50
)
  RETURNS TABLE (
    id             integer,
    observation    text,
    field          text,
    status         text,
    n_targets      integer,
    n_spectra      integer,
    cfpipe_version text,
    deployed_at    timestamp with time zone,
    published_at   timestamp with time zone,
    revoked_at     timestamp with time zone,
    completed_at   timestamp with time zone,
    total_count    bigint
  )
  LANGUAGE plpgsql
  STABLE
  SET search_path TO 'public', 'pg_temp'
  AS $function$
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
         d.completed_at, count(*) OVER ()
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
$function$;

REVOKE ALL ON FUNCTION "public"."get_admin_deployments"(text, text, text, text, integer, integer) FROM PUBLIC, "anon";

GRANT EXECUTE ON FUNCTION "public"."get_admin_deployments"(text, text, text, text, integer, integer) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."get_admin_deployments"(text, text, text, text, integer, integer) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."get_admin_deployments"(text, text, text, text, integer, integer) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."get_admin_deployments"(text, text, text, text, integer, integer) TO "service_role";
