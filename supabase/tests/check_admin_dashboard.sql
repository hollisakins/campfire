-- Guard: get_admin_dashboard / get_admin_recent_activity (the /admin page).
--
-- The dashboard's numbers are SECURITY DEFINER aggregates; this checks each
-- one against the direct count it claims to summarize, that a live lease and
-- its staleness surface, and that a non-admin is refused by both RPCs.
--
-- Fixtures build their own scope; the actor is the seed admin (11111111-...).
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_admin_dashboard.sql
BEGIN;

INSERT INTO programs (slug, program_name, is_public) VALUES ('zzz_ad_pub', 'AD pub', true)
ON CONFLICT (slug) DO NOTHING;
INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES
  ('zzz_ad_obs', 'zzz_ad_pub', 9403, 'zzz_ad_field');
INSERT INTO fields (name, display_name) VALUES ('zzz_ad_field', 'AD field')
ON CONFLICT (name) DO NOTHING;

-- Objects: two uninspected with a published spectrum, one inspected, one
-- inactive (never counted), one with no published spectrum (never counted).
INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum, redshift_quality, is_active) VALUES
  ('ZZZ-AD-1', 'zzz_ad_field', 152.00, 2.0, '{zzz_ad_pub}', '{zzz_ad_obs}', true, 0, true),
  ('ZZZ-AD-2', 'zzz_ad_field', 152.01, 2.0, '{zzz_ad_pub}', '{zzz_ad_obs}', true, 0, true),
  ('ZZZ-AD-3', 'zzz_ad_field', 152.02, 2.0, '{zzz_ad_pub}', '{zzz_ad_obs}', true, 4, true),
  ('ZZZ-AD-4', 'zzz_ad_field', 152.03, 2.0, '{zzz_ad_pub}', '{zzz_ad_obs}', true, 0, false),
  ('ZZZ-AD-5', 'zzz_ad_field', 152.04, 2.0, '{zzz_ad_pub}', '{zzz_ad_obs}', false, 0, true);

-- NIRCam exposures: three pending (one needing correction), one approved.
INSERT INTO nircam_exposures (field, filter, detector, filename, stage, review_status, correction) VALUES
  ('zzz_ad_field', 'f444w', 'nrca1', 'zzz_ad_1.fits', 'cal', 'pending', 'none'),
  ('zzz_ad_field', 'f444w', 'nrca2', 'zzz_ad_2.fits', 'cal', 'pending', 'needed'),
  ('zzz_ad_field', 'f277w', 'nrca1', 'zzz_ad_3.fits', 'cal', 'pending', 'none'),
  ('zzz_ad_field', 'f277w', 'nrca2', 'zzz_ad_4.fits', 'cal', 'approved', 'none');

-- Leases: one live, one with a heartbeat older than the ttl, one idle.
-- (The access request is filed by the seed admin: one pending request per user,
-- and the fixtures already hold the viewer's.)
INSERT INTO deploy_scope_state (scope_type, scope_key, version, in_progress_since, in_progress_by, in_progress_host, heartbeat_at) VALUES
  ('observation', 'zzz_ad_obs', 1, now() - interval '5 minutes', '11111111-1111-1111-1111-111111111111', 'zzz-host', now() - interval '10 seconds'),
  ('field', 'zzz_ad_field', 1, now() - interval '3 days', '11111111-1111-1111-1111-111111111111', 'zzz-host', now() - interval '3 days');
INSERT INTO deploy_scope_state (scope_type, scope_key, version, last_deploy_at) VALUES
  ('observation', 'zzz_ad_idle', 1, now() - interval '1 day');

INSERT INTO inspection_access_requests (user_id, status, message, created_at) VALUES
  ('11111111-1111-1111-1111-111111111111', 'pending', 'zzz-ad', now() - interval '9 days');

-- Read as the seed admin.
SET LOCAL role authenticated;
SELECT set_config('request.jwt.claims',
  json_build_object('sub', '11111111-1111-1111-1111-111111111111', 'role', 'authenticated')::text, true);

DO $$
DECLARE
  d jsonb;
  n bigint;
  lease jsonb;
BEGIN
  d := public.get_admin_dashboard();

  -- review.nircam against the table
  SELECT count(*) INTO n FROM nircam_exposures WHERE review_status = 'pending';
  IF (d #>> '{review,nircam,pending}')::bigint <> n THEN
    RAISE EXCEPTION 'nircam.pending % <> %', d #>> '{review,nircam,pending}', n;
  END IF;
  SELECT count(*) INTO n FROM nircam_exposures WHERE correction = 'needed';
  IF (d #>> '{review,nircam,needs_correction}')::bigint <> n THEN
    RAISE EXCEPTION 'nircam.needs_correction % <> %', d #>> '{review,nircam,needs_correction}', n;
  END IF;
  -- top: the four (field, filter) scopes with the most pending rows, as the table orders them
  IF (d #> '{review,nircam,top}') <> (
       SELECT COALESCE(jsonb_agg(jsonb_build_object('field', t.field, 'filter', t.filter, 'pending', t.pending)
                                 ORDER BY t.pending DESC, t.field, t.filter), '[]'::jsonb)
       FROM (SELECT field, filter, count(*) AS pending FROM nircam_exposures
             WHERE review_status = 'pending' GROUP BY field, filter
             ORDER BY count(*) DESC, field, filter LIMIT 4) t) THEN
    RAISE EXCEPTION 'nircam.top disagrees with the table: %', d #> '{review,nircam,top}';
  END IF;

  -- review.objects against the table
  SELECT count(*) INTO n FROM objects WHERE is_active AND has_published_spectrum AND COALESCE(redshift_quality, 0) = 0;
  IF (d #>> '{review,objects,uninspected}')::bigint <> n THEN
    RAISE EXCEPTION 'objects.uninspected % <> %', d #>> '{review,objects,uninspected}', n;
  END IF;
  SELECT count(*) INTO n FROM objects WHERE is_active AND has_published_spectrum;
  IF (d #>> '{review,objects,published}')::bigint <> n THEN
    RAISE EXCEPTION 'objects.published % <> %', d #>> '{review,objects,published}', n;
  END IF;
  IF (d #> '{review,objects,top}') <> (
       SELECT COALESCE(jsonb_agg(jsonb_build_object('field', t.field, 'uninspected', t.uninspected)
                                 ORDER BY t.uninspected DESC, t.field), '[]'::jsonb)
       FROM (SELECT field, count(*) AS uninspected FROM objects
             WHERE is_active AND has_published_spectrum AND COALESCE(redshift_quality, 0) = 0
             GROUP BY field ORDER BY count(*) DESC, field LIMIT 4) t) THEN
    RAISE EXCEPTION 'objects.top disagrees with the table: %', d #> '{review,objects,top}';
  END IF;

  -- leases: only in-progress rows, with the ttl rule
  SELECT count(*) INTO n FROM deploy_scope_state WHERE in_progress_since IS NOT NULL;
  IF jsonb_array_length(d -> 'leases') <> n THEN
    RAISE EXCEPTION 'leases % <> %', jsonb_array_length(d -> 'leases'), n;
  END IF;
  SELECT e INTO lease FROM jsonb_array_elements(d -> 'leases') e WHERE e->>'scope_key' = 'zzz_ad_obs';
  IF lease IS NULL OR (lease->>'stale')::boolean THEN RAISE EXCEPTION 'live lease missing or stale: %', lease; END IF;
  SELECT e INTO lease FROM jsonb_array_elements(d -> 'leases') e WHERE e->>'scope_key' = 'zzz_ad_field';
  IF lease IS NULL OR NOT (lease->>'stale')::boolean THEN RAISE EXCEPTION 'dead lease missing or live: %', lease; END IF;
  IF EXISTS (SELECT 1 FROM jsonb_array_elements(d -> 'leases') e WHERE e->>'scope_key' = 'zzz_ad_idle') THEN
    RAISE EXCEPTION 'idle scope reported as a lease';
  END IF;

  -- access queue
  SELECT count(*) INTO n FROM inspection_access_requests WHERE status = 'pending';
  IF (d #>> '{access,pending_requests}')::bigint <> n THEN
    RAISE EXCEPTION 'access.pending_requests % <> %', d #>> '{access,pending_requests}', n;
  END IF;
  IF (d #>> '{access,oldest_request_at}')::timestamptz > now() - interval '9 days' + interval '1 minute' THEN
    RAISE EXCEPTION 'access.oldest_request_at is not the oldest pending request';
  END IF;

  -- deployments.drafts and the activity window against the tables
  SELECT count(*) INTO n FROM deployments WHERE status = 'draft';
  IF (d #>> '{deployments,drafts}')::bigint <> n THEN
    RAISE EXCEPTION 'deployments.drafts % <> %', d #>> '{deployments,drafts}', n;
  END IF;
  SELECT count(*) INTO n FROM flag_audit_log WHERE changed_at > (now() AT TIME ZONE 'utc') - interval '7 days';
  IF (d #>> '{activity,inspections_7d}')::bigint <> n THEN
    RAISE EXCEPTION 'activity.inspections_7d % <> %', d #>> '{activity,inspections_7d}', n;
  END IF;

  -- recent activity: bounded, every row with a subject carries its kind
  SELECT count(*) INTO n FROM public.get_admin_recent_activity(5);
  IF n > 5 THEN RAISE EXCEPTION 'recent activity returned % rows for limit 5', n; END IF;
  SELECT count(*) INTO n FROM public.get_admin_recent_activity(50) r WHERE r.display_id <> '' AND r.subject_kind IS NULL;
  IF n > 0 THEN RAISE EXCEPTION '% recent activity row(s) with a subject but no kind', n; END IF;

  RAISE NOTICE 'check_admin_dashboard: ok';
END $$;

-- A non-admin is refused by both.
SELECT set_config('request.jwt.claims',
  json_build_object('sub', '22222222-2222-2222-2222-222222222222', 'role', 'authenticated')::text, true);
DO $$
BEGIN
  PERFORM public.get_admin_dashboard();
  RAISE EXCEPTION 'non-admin read get_admin_dashboard';
EXCEPTION WHEN insufficient_privilege OR raise_exception THEN
  IF SQLERRM = 'non-admin read get_admin_dashboard' THEN RAISE; END IF;
END $$;
DO $$
BEGIN
  PERFORM * FROM public.get_admin_recent_activity(5);
  RAISE EXCEPTION 'non-admin read get_admin_recent_activity';
EXCEPTION WHEN insufficient_privilege OR raise_exception THEN
  IF SQLERRM = 'non-admin read get_admin_recent_activity' THEN RAISE; END IF;
END $$;

ROLLBACK;
