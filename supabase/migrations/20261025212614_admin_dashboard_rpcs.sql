-- Admin dashboard RPCs: get_admin_dashboard (leases, review backlogs, access queue,
-- provenance, sync snapshot, 7-day activity) and get_admin_recent_activity (LIMIT-only feed).

SET lock_timeout = '5s'; SET statement_timeout = '60s';

SET local check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_admin_dashboard()
  RETURNS jsonb
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  v_is_admin boolean;
  v_deploy record;
  v_nircam record;
  v_objects record;
  v_access record;
  v_activity record;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT
    count(*) FILTER (WHERE d.status = 'draft') AS drafts,
    count(*) FILTER (WHERE d.deployed_at > now() - interval '7 days') AS deployed_7d,
    count(*) FILTER (WHERE d.status = 'published' AND d.cfpipe_version IS NOT NULL
                     AND d.cfpipe_version !~ '^[0-9]+\.[0-9]+\.[0-9]+$') AS unreleased_published,
    count(*) FILTER (WHERE d.status = 'published'
                     AND (d.cfpipe_version IS NULL OR d.crds_context IS NULL)) AS missing_provenance,
    count(DISTINCT d.crds_context) FILTER (WHERE d.status = 'published') AS crds_contexts,
    max(d.deployed_at) AS latest_deployed_at
  INTO v_deploy
  FROM deployments d;

  SELECT
    count(*) AS total,
    count(*) FILTER (WHERE ne.review_status = 'pending') AS pending,
    count(*) FILTER (WHERE ne.review_status IN ('approved', 'excluded')) AS done,
    count(*) FILTER (WHERE ne.correction = 'needed') AS needs_correction
  INTO v_nircam
  FROM nircam_exposures ne;

  -- Uninspected = quality 0 (the "Not Inspected" enum value) on a live object
  -- with a published spectrum; needs_review is the derived P3 flag.
  SELECT
    count(*) FILTER (WHERE o.is_active AND o.has_published_spectrum) AS published,
    count(*) FILTER (WHERE o.is_active AND o.has_published_spectrum
                     AND COALESCE(o.redshift_quality, 0) = 0) AS uninspected,
    count(*) FILTER (WHERE o.is_active AND o.needs_review) AS needs_review
  INTO v_objects
  FROM objects o;

  SELECT count(*) AS pending, min(r.created_at) AS oldest_at
  INTO v_access
  FROM inspection_access_requests r
  WHERE r.status = 'pending';

  -- comments.created_at and flag_audit_log.changed_at are naive UTC, so the
  -- window compares against now() AT TIME ZONE 'utc'.
  SELECT
    (SELECT count(*) FROM flag_audit_log f
     WHERE f.changed_at > (now() AT TIME ZONE 'utc') - interval '7 days') AS inspections_7d,
    (SELECT count(DISTINCT f.user_id) FROM flag_audit_log f
     WHERE f.user_id IS NOT NULL
       AND f.changed_at > (now() AT TIME ZONE 'utc') - interval '7 days') AS inspectors_7d,
    (SELECT count(*) FROM comments c
     WHERE NOT c.is_deleted
       AND c.created_at > (now() AT TIME ZONE 'utc') - interval '7 days') AS comments_7d
  INTO v_activity;

  RETURN jsonb_build_object(
    'generated_at', now(),
    -- Live deploy leases; `stale` is the dr_readiness rule (heartbeat older
    -- than deploy_lease_ttl()).
    'leases', (
      SELECT COALESCE(jsonb_agg(jsonb_build_object(
          'scope_type', s.scope_type,
          'scope_key', s.scope_key,
          'since', s.in_progress_since,
          'heartbeat_at', s.heartbeat_at,
          'host', s.in_progress_host,
          'actor_name', up.full_name,
          'stale', COALESCE(s.heartbeat_at, s.in_progress_since) < now() - deploy_lease_ttl())
        ORDER BY s.in_progress_since), '[]'::jsonb)
      FROM deploy_scope_state s
      LEFT JOIN user_profiles up ON up.user_id = s.in_progress_by
      WHERE s.in_progress_since IS NOT NULL),
    'deployments', jsonb_build_object(
      'drafts', v_deploy.drafts,
      'deployed_7d', v_deploy.deployed_7d,
      'unreleased_published', v_deploy.unreleased_published,
      'missing_provenance', v_deploy.missing_provenance,
      'crds_contexts', v_deploy.crds_contexts,
      'latest_deployed_at', v_deploy.latest_deployed_at),
    'review', jsonb_build_object(
      'nircam', jsonb_build_object(
        'total', v_nircam.total,
        'pending', v_nircam.pending,
        'done', v_nircam.done,
        'needs_correction', v_nircam.needs_correction,
        'top', (
          SELECT COALESCE(jsonb_agg(jsonb_build_object(
              'field', t.field, 'filter', t.filter, 'pending', t.pending)
            ORDER BY t.pending DESC, t.field, t.filter), '[]'::jsonb)
          FROM (SELECT ne.field, ne.filter, count(*) AS pending
                FROM nircam_exposures ne
                WHERE ne.review_status = 'pending'
                GROUP BY ne.field, ne.filter
                ORDER BY count(*) DESC, ne.field, ne.filter
                LIMIT 4) t)),
      'objects', jsonb_build_object(
        'published', v_objects.published,
        'uninspected', v_objects.uninspected,
        'needs_review', v_objects.needs_review,
        'top', (
          SELECT COALESCE(jsonb_agg(jsonb_build_object(
              'field', t.field, 'uninspected', t.uninspected)
            ORDER BY t.uninspected DESC, t.field), '[]'::jsonb)
          FROM (SELECT o.field, count(*) AS uninspected
                FROM objects o
                WHERE o.is_active AND o.has_published_spectrum
                  AND COALESCE(o.redshift_quality, 0) = 0
                GROUP BY o.field
                ORDER BY count(*) DESC, o.field
                LIMIT 4) t))),
    'access', jsonb_build_object(
      'pending_requests', v_access.pending,
      'oldest_request_at', v_access.oldest_at),
    'activity', jsonb_build_object(
      'inspections_7d', v_activity.inspections_7d,
      'inspectors_7d', v_activity.inspectors_7d,
      'comments_7d', v_activity.comments_7d),
    'sync_snapshot', (
      SELECT to_jsonb(x) FROM (
        SELECT s.status, s.started_at, s.completed_at, s.error
        FROM sync_snapshots s
        ORDER BY s.created_at DESC
        LIMIT 1) x)
  );
END;
$function$;

REVOKE ALL ON FUNCTION "public"."get_admin_dashboard"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.get_admin_recent_activity (
  p_limit integer DEFAULT 10
)
  RETURNS TABLE (
    id             text,
    type           text,
    subject_kind   text,
    display_id     text,
    user_id        uuid,
    user_full_name text,
    ts             timestamp with time zone,
    content        text,
    field_name     text,
    old_value      integer,
    new_value      integer
  )
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  v_limit integer := LEAST(GREATEST(COALESCE(p_limit, 10), 1), 50);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  RETURN QUERY
  WITH recent AS (
    (SELECT 'audit-' || f.id AS r_id, 'inspection'::text AS r_type,
            CASE WHEN f.object_id IS NOT NULL THEN 'object'
                 WHEN f.spectrum_id IS NOT NULL THEN 'spectrum'
                 WHEN f.target_id IS NOT NULL THEN 'target' END AS r_subject_kind,
            f.object_id AS r_object_id, f.spectrum_id AS r_spectrum_id,
            f.target_id AS r_target_id, f.user_id AS r_user_id,
            (f.changed_at AT TIME ZONE 'utc') AS r_ts,
            NULL::text AS r_content, f.field_name AS r_field_name,
            f.old_value AS r_old_value, f.new_value AS r_new_value
     FROM flag_audit_log f
     ORDER BY f.changed_at DESC
     LIMIT v_limit)
    UNION ALL
    (SELECT 'comment-' || c.id, 'comment'::text,
            CASE WHEN c.object_id IS NOT NULL THEN 'object'
                 WHEN c.target_id IS NOT NULL THEN 'target' END,
            c.object_id, NULL::integer, c.target_id, c.user_id,
            (c.created_at AT TIME ZONE 'utc'),
            left(c.content, 240), NULL::text, NULL::integer, NULL::integer
     FROM comments c
     WHERE NOT c.is_deleted
     ORDER BY c.created_at DESC
     LIMIT v_limit)
  )
  SELECT r.r_id, r.r_type, r.r_subject_kind,
         COALESCE(
           o.object_id,
           t.target_id,
           CASE WHEN s.id IS NOT NULL THEN s.target_id || '/' || s.grating END,
           ''
         ) AS display_id,
         r.r_user_id, up.full_name, r.r_ts, r.r_content,
         r.r_field_name, r.r_old_value, r.r_new_value
  FROM recent r
  LEFT JOIN objects o ON o.id = r.r_object_id
  LEFT JOIN targets t ON t.id = r.r_target_id
  LEFT JOIN spectra s ON s.id = r.r_spectrum_id
  LEFT JOIN user_profiles up ON up.user_id = r.r_user_id
  ORDER BY r.r_ts DESC, r.r_id DESC
  LIMIT v_limit;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."get_admin_recent_activity"(integer) FROM PUBLIC, "anon";

GRANT EXECUTE ON FUNCTION "public"."get_admin_dashboard"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."get_admin_dashboard"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."get_admin_dashboard"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."get_admin_dashboard"() TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."get_admin_recent_activity"(integer) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."get_admin_recent_activity"(integer) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."get_admin_recent_activity"(integer) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."get_admin_recent_activity"(integer) TO "service_role";
