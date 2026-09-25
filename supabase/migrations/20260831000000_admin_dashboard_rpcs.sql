-- Admin dashboard control center (2026-08 redesign).
-- Hand-authored migration mirroring supabase/schemas/functions.sql exactly:
-- three new admin read models (get_admin_dashboard_summary,
-- get_admin_review_queues, get_admin_recent_activity) and a re-shaped
-- get_activity_feed (adds subject_kind; comments LEFT JOIN targets/objects so
-- object-parented comments surface). All new functions are SECURITY DEFINER
-- with an explicit is_admin() gate because objects/spectra/flag_audit_log RLS
-- is program-overlap scoped, not admin scoped.

-- Return shape changed (added subject_kind): a RETURNS TABLE change needs the
-- old signature dropped first.
DROP FUNCTION IF EXISTS public.get_activity_feed(boolean, boolean, uuid[], boolean, text[], integer, integer);

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
  subject_kind text,
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
      COALESCE(c.target_id, c.object_id, 0) AS target_db_id,
      COALESCE(t.target_id, oc.object_id, '') AS target_display_id,
      c.user_id,
      c.created_at AS ts,
      c.content,
      c.edited_at,
      NULL::text AS field_name,
      NULL::integer AS old_value,
      NULL::integer AS new_value,
      CASE WHEN c.object_id IS NOT NULL THEN 'object'
           WHEN c.target_id IS NOT NULL THEN 'target' END AS subject_kind
    FROM comments c
    LEFT JOIN targets t ON t.id = c.target_id
    LEFT JOIN objects oc ON oc.id = c.object_id
    WHERE p_include_comments
      AND NOT c.is_deleted
      AND (c.target_id IS NOT NULL OR c.object_id IS NOT NULL)
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
      f.new_value,
      CASE WHEN f.target_id IS NOT NULL THEN 'target'
           WHEN f.object_id IS NOT NULL THEN 'object'
           WHEN f.spectrum_id IS NOT NULL THEN 'spectrum' END
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
    feed.subject_kind,
    count(*) OVER ()
  FROM feed
  LEFT JOIN user_profiles up ON up.user_id = feed.user_id
  ORDER BY feed.ts DESC, feed.id DESC
  OFFSET v_offset
  LIMIT v_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_activity_feed TO authenticated;

-- get_admin_dashboard_summary: every scalar the attention rail, health list,
-- people panel and scopes panel need, plus two small arrays (recent signups,
-- newest observations/fields). One pass with FILTER over each large table.
-- Timestamp-type note: user_profiles.created_at, comments.created_at and
-- flag_audit_log.changed_at are `timestamp without time zone` (naive UTC), so
-- window predicates on them compare against (now() AT TIME ZONE 'utc') — an
-- implicit cast would shift the boundary by the session TimeZone.
CREATE OR REPLACE FUNCTION public.get_admin_dashboard_summary()
RETURNS json
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_deploy record;
  v_storage record;
  v_nircam record;
  v_rate record;
  v_nods record;
  v_users record;
  v_objects record;
  result json;
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT
    count(*) FILTER (WHERE d.status = 'draft') AS drafts,
    min(d.deployed_at) FILTER (WHERE d.status = 'draft') AS oldest_draft_at,
    count(*) FILTER (WHERE d.deployed_at > now() - interval '7 days') AS deploys_7d,
    count(*) FILTER (WHERE d.status = 'published' AND d.cfpipe_version IS NOT NULL
                     AND d.cfpipe_version !~ '^[0-9]+\.[0-9]+\.[0-9]+$') AS unreleased_published,
    count(*) FILTER (WHERE d.status = 'published'
                     AND (d.cfpipe_version IS NULL OR d.crds_context IS NULL)) AS missing_provenance,
    count(DISTINCT d.crds_context) FILTER (WHERE d.status = 'published') AS distinct_crds_contexts,
    max(d.deployed_at) AS latest_deploy_at
  INTO v_deploy
  FROM deployments d;

  SELECT
    COALESCE(sum(so.size_bytes) FILTER (WHERE so.status IN ('superseded', 'revoked')), 0) AS reclaimable_bytes,
    count(*) FILTER (WHERE so.status IN ('superseded', 'revoked')) AS reclaimable_count,
    count(*) FILTER (WHERE so.status = 'active' AND so.content_hash LIKE 'etag:%') AS provisional_hashes,
    -- "Pushed, not deployed": deployment_id is the exact linkage (FK, set when
    -- deploy attaches the object) — bounded to a recent window so pre-linkage
    -- backfilled rows don't read as a permanent alarm.
    count(*) FILTER (WHERE so.status = 'active' AND so.deployment_id IS NULL
                     AND so.created_at > now() - interval '14 days') AS pushed_undeployed_14d,
    count(*) FILTER (WHERE so.created_at > now() - interval '7 days') AS registered_7d,
    COALESCE(sum(so.size_bytes) FILTER (WHERE so.created_at > now() - interval '7 days'), 0) AS bytes_added_7d
  INTO v_storage
  FROM storage_objects so;

  SELECT
    count(*) AS total,
    count(*) FILTER (WHERE ne.review_status = 'pending') AS pending,
    count(*) FILTER (WHERE ne.review_status IN ('approved', 'excluded')) AS done,
    count(*) FILTER (WHERE ne.correction = 'needed') AS needs_correction
  INTO v_nircam
  FROM nircam_exposures ne;

  SELECT
    count(*) AS total,
    count(*) FILTER (WHERE re.review_status = 'pending') AS pending,
    count(*) FILTER (WHERE re.review_status IN ('approved', 'excluded')) AS done
  INTO v_rate
  FROM nirspec_rate_exposures re;

  SELECT
    count(*) AS total,
    count(*) FILTER (WHERE se.review_status = 'pending') AS pending,
    count(*) FILTER (WHERE se.review_status IN ('approved', 'excluded')) AS done
  INTO v_nods
  FROM spectrum_exposures se;

  SELECT
    count(*) FILTER (WHERE NOT up.is_link_account) AS total,
    count(*) FILTER (WHERE up.is_admin AND NOT up.is_link_account) AS admins,
    count(*) FILTER (WHERE up.can_inspect AND NOT up.is_link_account) AS inspectors,
    count(*) FILTER (WHERE up.is_group_account) AS group_accounts,
    count(*) FILTER (WHERE NOT up.is_link_account
                     AND up.created_at > (now() AT TIME ZONE 'utc') - interval '30 days') AS signups_30d,
    count(*) FILTER (WHERE NOT up.is_link_account AND NOT up.is_admin AND NOT up.is_group_account
                     AND up.created_at > (now() AT TIME ZONE 'utc') - interval '30 days'
                     AND NOT EXISTS (SELECT 1 FROM user_program_access upa
                                     WHERE upa.user_id = up.user_id)) AS unprovisioned_30d
  INTO v_users
  FROM user_profiles up;

  SELECT
    count(*) FILTER (WHERE NOT o.is_active) AS inactive,
    count(*) FILTER (WHERE o.is_active AND o.staleness_reason IS NOT NULL) AS stale,
    count(*) FILTER (WHERE o.is_active AND o.has_published_spectrum
                     AND o.redshift_quality = 0) AS uninspected_published
  INTO v_objects
  FROM objects o;

  SELECT json_build_object(
    'deployments', json_build_object(
      'drafts', v_deploy.drafts,
      'oldest_draft_at', v_deploy.oldest_draft_at,
      'deploys_7d', v_deploy.deploys_7d,
      'unreleased_published', v_deploy.unreleased_published,
      'missing_provenance', v_deploy.missing_provenance,
      'distinct_crds_contexts', v_deploy.distinct_crds_contexts,
      'latest_deploy_at', v_deploy.latest_deploy_at
    ),
    'review', json_build_object(
      'nircam_total', v_nircam.total,
      'nircam_pending', v_nircam.pending,
      'nircam_done', v_nircam.done,
      'nircam_needs_correction', v_nircam.needs_correction,
      'rate_total', v_rate.total,
      'rate_pending', v_rate.pending,
      'rate_done', v_rate.done,
      'nods_total', v_nods.total,
      'nods_pending', v_nods.pending,
      'nods_done', v_nods.done,
      -- The inspection mandate has no DB enforcement: a scope with a published
      -- deployment and pending exposures is live, uninspected science.
      'published_fields_with_pending', (
        SELECT count(DISTINCT ne.field) FROM nircam_exposures ne
        WHERE ne.review_status = 'pending'
          AND EXISTS (SELECT 1 FROM deployments d
                      WHERE d.field = ne.field AND d.status = 'published')),
      'published_obs_with_pending', (
        SELECT count(DISTINCT re.observation) FROM nirspec_rate_exposures re
        WHERE re.review_status = 'pending'
          AND EXISTS (SELECT 1 FROM deployments d
                      WHERE d.observation = re.observation AND d.status = 'published'))
    ),
    'storage', json_build_object(
      'reclaimable_bytes', v_storage.reclaimable_bytes,
      'reclaimable_count', v_storage.reclaimable_count,
      'provisional_hashes', v_storage.provisional_hashes,
      'pushed_undeployed_14d', v_storage.pushed_undeployed_14d,
      'registered_7d', v_storage.registered_7d,
      'bytes_added_7d', v_storage.bytes_added_7d
    ),
    'access', json_build_object(
      'pending_requests', (SELECT count(*) FROM inspection_access_requests r
                           WHERE r.status = 'pending'),
      'oldest_request_at', (SELECT min(r.created_at) FROM inspection_access_requests r
                            WHERE r.status = 'pending'),
      'open_invites', (SELECT count(*) FROM pending_invites pi WHERE pi.accepted_at IS NULL),
      'stale_invites', (SELECT count(*) FROM pending_invites pi
                        WHERE pi.accepted_at IS NULL
                          AND pi.created_at < now() - interval '14 days'),
      'active_share_links', (SELECT count(*) FROM share_links sl
                             WHERE sl.revoked_at IS NULL
                               AND (sl.expires_at IS NULL OR sl.expires_at > now())),
      'links_exposing_drafts', (SELECT count(*) FROM share_links sl
                                WHERE sl.revoked_at IS NULL
                                  AND (sl.expires_at IS NULL OR sl.expires_at > now())
                                  AND sl.include_drafts),
      'active_codes', (SELECT count(*) FROM access_codes ac
                       WHERE ac.is_active
                         AND (ac.expires_at IS NULL OR ac.expires_at > now())
                         AND (ac.max_uses IS NULL OR ac.use_count < ac.max_uses)),
      'codes_all_programs', (SELECT count(*) FROM access_codes ac
                             WHERE ac.is_active AND ac.grants_all_programs
                               AND (ac.expires_at IS NULL OR ac.expires_at > now())
                               AND (ac.max_uses IS NULL OR ac.use_count < ac.max_uses)),
      'codes_expiring_soon', (SELECT count(*) FROM access_codes ac
                              WHERE ac.is_active
                                AND (ac.expires_at IS NULL OR ac.expires_at > now())
                                AND (ac.max_uses IS NULL OR ac.use_count < ac.max_uses)
                                AND ((ac.expires_at IS NOT NULL AND ac.expires_at < now() + interval '7 days')
                                     OR (ac.max_uses IS NOT NULL AND ac.use_count >= ceil(ac.max_uses * 0.8))))
    ),
    'users', json_build_object(
      'total', v_users.total,
      'admins', v_users.admins,
      'inspectors', v_users.inspectors,
      'group_accounts', v_users.group_accounts,
      'signups_30d', v_users.signups_30d,
      'unprovisioned_30d', v_users.unprovisioned_30d,
      'recent_signups', (
        SELECT COALESCE(json_agg(t), '[]'::json) FROM (
          SELECT up.user_id, up.username, up.full_name,
                 up.created_at AT TIME ZONE 'utc' AS created_at,
                 up.can_inspect, up.is_admin, up.is_group_account,
                 (SELECT count(*)::int FROM user_program_access upa
                  WHERE upa.user_id = up.user_id) AS n_programs
          FROM user_profiles up
          WHERE NOT up.is_link_account
          ORDER BY up.created_at DESC
          LIMIT 5
        ) t)
    ),
    'objects', json_build_object(
      'inactive', v_objects.inactive,
      'stale', v_objects.stale,
      'uninspected_published', v_objects.uninspected_published
    ),
    'activity', json_build_object(
      'inspections_7d', (SELECT count(*) FROM flag_audit_log f
                         WHERE f.changed_at > (now() AT TIME ZONE 'utc') - interval '7 days'),
      'comments_7d', (SELECT count(*) FROM comments c
                      WHERE NOT c.is_deleted
                        AND c.created_at > (now() AT TIME ZONE 'utc') - interval '7 days'),
      'active_inspectors_7d', (SELECT count(DISTINCT f.user_id) FROM flag_audit_log f
                               WHERE f.user_id IS NOT NULL
                                 AND f.changed_at > (now() AT TIME ZONE 'utc') - interval '7 days')
    ),
    'scopes', json_build_object(
      'config_never_pushed', (
        (SELECT count(*) FROM observations o WHERE o.config_hash IS NULL AND o.retired_at IS NULL)
        + (SELECT count(*) FROM fields fl WHERE fl.config_hash IS NULL AND fl.retired_at IS NULL)),
      'retired_with_live_deployment', (
        (SELECT count(*) FROM observations o
         WHERE o.retired_at IS NOT NULL AND o.latest_deployment_id IS NOT NULL)
        + (SELECT count(*) FROM fields fl
           WHERE fl.retired_at IS NOT NULL AND fl.latest_deployment_id IS NOT NULL)),
      'never_deployed', (
        (SELECT count(*) FROM observations o
         WHERE o.latest_deployment_id IS NULL AND o.retired_at IS NULL)
        + (SELECT count(*) FROM fields fl
           WHERE fl.latest_deployment_id IS NULL AND fl.retired_at IS NULL)),
      'new_scopes', (
        SELECT COALESCE(json_agg(t), '[]'::json) FROM (
          SELECT s.kind, s.name, s.program, s.created_at, s.last_deploy_at,
                 s.last_deploy_status, s.config_never_pushed, s.retired
          FROM (
            SELECT 'observation'::text AS kind, o.name, o.program_slug AS program,
                   o.created_at, d.deployed_at AS last_deploy_at,
                   d.status AS last_deploy_status,
                   (o.config_hash IS NULL) AS config_never_pushed,
                   (o.retired_at IS NOT NULL) AS retired
            FROM observations o
            LEFT JOIN deployments d ON d.id = o.latest_deployment_id
            UNION ALL
            SELECT 'field', fl.name, array_to_string(fl.programs, ', '),
                   fl.created_at, d.deployed_at, d.status,
                   (fl.config_hash IS NULL), (fl.retired_at IS NOT NULL)
            FROM fields fl
            LEFT JOIN deployments d ON d.id = fl.latest_deployment_id
          ) s
          ORDER BY s.created_at DESC NULLS LAST
          LIMIT 6
        ) t)
    )
  ) INTO result;

  RETURN result;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_dashboard_summary() TO authenticated;

-- get_admin_review_queues: the four review grains rolled up per scope,
-- server-side (the old dashboard shipped every detector-grain row of
-- nircam_reduction_progress to the client and reduced in JS). One row shape
-- per grain: totals plus the top scopes by backlog. NIRSpec gets its first
-- progress model here; objects is the science-side inspection backlog
-- (uninspected published / stale / reconciliation-orphaned per field).
CREATE OR REPLACE FUNCTION public.get_admin_review_queues()
RETURNS json
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  result json;
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  WITH nc AS (
    SELECT ne.field, ne.filter,
           count(*) AS total,
           count(*) FILTER (WHERE ne.review_status = 'pending') AS pending,
           count(*) FILTER (WHERE ne.review_status IN ('approved', 'excluded')) AS done,
           count(*) FILTER (WHERE ne.mask_regions IS NOT NULL) AS masked,
           count(*) FILTER (WHERE ne.correction = 'needed') AS needs_correction
    FROM nircam_exposures ne
    GROUP BY ne.field, ne.filter
  ),
  rate AS (
    SELECT re.observation,
           count(*) AS total,
           count(*) FILTER (WHERE re.review_status = 'pending') AS pending,
           count(*) FILTER (WHERE re.review_status IN ('approved', 'excluded')) AS done,
           count(*) FILTER (WHERE re.mask_regions IS NOT NULL) AS masked
    FROM nirspec_rate_exposures re
    GROUP BY re.observation
  ),
  nods AS (
    SELECT se.observation,
           count(*) AS total,
           count(*) FILTER (WHERE se.review_status = 'pending') AS pending,
           count(*) FILTER (WHERE se.review_status IN ('approved', 'excluded')) AS done,
           count(DISTINCT se.source_id) AS sources
    FROM spectrum_exposures se
    GROUP BY se.observation
  ),
  obj AS (
    SELECT o.field,
           count(*) FILTER (WHERE o.is_active AND o.has_published_spectrum) AS published,
           count(*) FILTER (WHERE o.is_active AND o.has_published_spectrum
                            AND o.redshift_quality = 0) AS uninspected,
           count(*) FILTER (WHERE o.is_active AND o.staleness_reason IS NOT NULL) AS stale,
           count(*) FILTER (WHERE NOT o.is_active) AS inactive
    FROM objects o
    GROUP BY o.field
  )
  SELECT json_build_object(
    'nircam', json_build_object(
      'total', (SELECT COALESCE(sum(nc.total), 0) FROM nc),
      'pending', (SELECT COALESCE(sum(nc.pending), 0) FROM nc),
      'done', (SELECT COALESCE(sum(nc.done), 0) FROM nc),
      'needs_correction', (SELECT COALESCE(sum(nc.needs_correction), 0) FROM nc),
      'top', (SELECT COALESCE(json_agg(t), '[]'::json) FROM (
        SELECT * FROM nc WHERE nc.pending > 0 ORDER BY nc.pending DESC LIMIT 4) t)
    ),
    'rate', json_build_object(
      'total', (SELECT COALESCE(sum(rate.total), 0) FROM rate),
      'pending', (SELECT COALESCE(sum(rate.pending), 0) FROM rate),
      'done', (SELECT COALESCE(sum(rate.done), 0) FROM rate),
      'top', (SELECT COALESCE(json_agg(t), '[]'::json) FROM (
        SELECT * FROM rate WHERE rate.pending > 0 ORDER BY rate.pending DESC LIMIT 4) t)
    ),
    'nods', json_build_object(
      'total', (SELECT COALESCE(sum(nods.total), 0) FROM nods),
      'pending', (SELECT COALESCE(sum(nods.pending), 0) FROM nods),
      'done', (SELECT COALESCE(sum(nods.done), 0) FROM nods),
      'top', (SELECT COALESCE(json_agg(t), '[]'::json) FROM (
        SELECT * FROM nods WHERE nods.pending > 0 ORDER BY nods.pending DESC LIMIT 4) t)
    ),
    'objects', json_build_object(
      'published', (SELECT COALESCE(sum(obj.published), 0) FROM obj),
      'uninspected', (SELECT COALESCE(sum(obj.uninspected), 0) FROM obj),
      'stale', (SELECT COALESCE(sum(obj.stale), 0) FROM obj),
      'inactive', (SELECT COALESCE(sum(obj.inactive), 0) FROM obj),
      'top', (SELECT COALESCE(json_agg(t), '[]'::json) FROM (
        SELECT * FROM obj WHERE obj.uninspected > 0 ORDER BY obj.uninspected DESC LIMIT 4) t)
    )
  ) INTO result;

  RETURN result;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_review_queues() TO authenticated;

-- get_admin_recent_activity: the dashboard's activity feed. A LIMIT-only
-- variant of get_activity_feed — two index-served LIMIT scans (idx_audit_time,
-- idx_comments_created) instead of materializing the full comments ∪
-- flag_audit_log union for a window count. Unlike the feed it also includes
-- object-parented comments, and it returns subject_kind so the client can
-- build a link that resolves (object → /nirspec/objects/<id>, target → the
-- /nirspec/targets/<id> redirect shim, spectrum → no route, render as text).
-- Naive-timestamp columns are converted AT TIME ZONE 'utc' so both sources
-- merge on a common timestamptz.
CREATE OR REPLACE FUNCTION public.get_admin_recent_activity(p_limit integer DEFAULT 10)
RETURNS TABLE (
  id text,
  type text,
  subject_kind text,
  display_id text,
  user_id uuid,
  user_full_name text,
  ts timestamptz,
  content text,
  field_name text,
  old_value integer,
  new_value integer
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
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
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_recent_activity TO authenticated;
