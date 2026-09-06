-- PR #495 review fixes to get_admin_dashboard_summary (CREATE OR REPLACE,
-- same signature — no drop needed):
--   * published_obs_with_pending now unions both NIRSpec review grains
--     (nirspec_rate_exposures + spectrum_exposures), so a published
--     observation with a nods-only backlog still fires the act-severity
--     "published scopes with uninspected exposures" rule.
--   * retired_with_live_deployment now requires the latest deployment to be
--     status='published' — a retired scope whose deployment was revoked or
--     never left draft is not a standing attention item.
-- Mirrors supabase/schemas/functions.sql exactly.

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
      -- Both NIRSpec review grains: an observation whose rate files are fully
      -- triaged can still carry pending nod exposures, and the mandate covers
      -- both (PR #495 review).
      'published_obs_with_pending', (
        SELECT count(DISTINCT p.obs) FROM (
          SELECT re.observation AS obs FROM nirspec_rate_exposures re
          WHERE re.review_status = 'pending'
          UNION
          SELECT se.observation FROM spectrum_exposures se
          WHERE se.review_status = 'pending'
        ) p
        WHERE EXISTS (SELECT 1 FROM deployments d
                      WHERE d.observation = p.obs AND d.status = 'published'))
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
      -- "Live" means the latest deployment is PUBLISHED — a retired scope
      -- whose deployment was already revoked (or never left draft) is not an
      -- attention item (PR #495 review).
      'retired_with_live_deployment', (
        (SELECT count(*) FROM observations o
         JOIN deployments d ON d.id = o.latest_deployment_id
         WHERE o.retired_at IS NOT NULL AND d.status = 'published')
        + (SELECT count(*) FROM fields fl
           JOIN deployments d ON d.id = fl.latest_deployment_id
           WHERE fl.retired_at IS NOT NULL AND d.status = 'published')),
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
