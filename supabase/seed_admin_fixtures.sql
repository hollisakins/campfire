-- Admin-plane fixtures for local resets.
--
-- seed.sql is regenerated from a production sample (scripts/generate_seed.py)
-- and carries the catalog: programs, observations, fields, objects, spectra,
-- deployments in every lifecycle state, storage rows, a data release. The
-- operator tables the sampler cannot cover are filled here so /admin renders
-- populated locally: the NIRCam exposure-review queue, deploy events, deploy
-- leases, an inspection access request, a sync snapshot and a fresh burst of
-- inspection activity for the 7-day counters.
--
-- Everything derives from the rows seed.sql inserted (listed in
-- supabase/config.toml [db.seed] sql_paths after it), so a seed regeneration
-- never needs to touch this file. Timestamps are now()-relative; identities
-- are the synthetic seed accounts only.

-- --- NIRCam exposure review queue (2 fields x 2 filters x 4 detectors x 8) ---
INSERT INTO public.nircam_exposures (field, filter, detector, filename, visit, stage,
                                     review_status, correction, mask_regions, notes,
                                     review_decided_at, date_obs)
SELECT f.name, flt.f, det.d,
       format('jw%s001001_02101_%s_%s_%s_cal.fits',
              lpad((100 + f.rn)::text, 5, '0'), lpad((10000 + n.i)::text, 5, '0'), det.d, flt.f),
       format('visit_%s', 1 + n.i % 4),
       'cal',
       CASE WHEN n.i % 8 IN (0, 1, 2, 3) THEN 'approved'
            WHEN n.i % 8 = 4 THEN 'excluded'
            ELSE 'pending' END,
       CASE WHEN n.i % 8 = 5 THEN 'needed'
            WHEN n.i % 8 = 4 THEN 'done'
            ELSE 'none' END,
       CASE WHEN n.i % 8 = 3
            THEN '[{"kind": "polygon", "points": [[64, 64], [192, 64], [192, 192]]}]'::jsonb END,
       CASE WHEN n.i % 8 = 5 THEN 'seed fixture: cosmic-ray cluster near detector edge' END,
       CASE WHEN n.i % 8 < 5 THEN now() - (n.i % 21) * interval '1 day' - interval '3 hours' END,
       (now() AT TIME ZONE 'utc') - interval '200 days' + (n.i * interval '1 hour')
FROM (SELECT name, row_number() OVER (ORDER BY name) AS rn
      FROM public.fields ORDER BY name LIMIT 2) f
CROSS JOIN (VALUES ('f277w'), ('f444w')) AS flt(f)
CROSS JOIN (VALUES ('nrca1'), ('nrca2'), ('nrcb1'), ('nrcb2')) AS det(d)
CROSS JOIN generate_series(0, 7) AS n(i);

-- --- Deploy events (audit log derived from the seeded deployments) ----------
INSERT INTO public.deploy_events (actor, action, deployment_id, observation, field, status_to, affected_count, occurred_at)
SELECT '11111111-1111-1111-1111-111111111111', 'upload', d.id, d.observation, d.field, d.status,
       COALESCE(d.n_spectra, d.n_targets), COALESCE(d.deployed_at, now() - interval '30 days')
FROM public.deployments d;

INSERT INTO public.deploy_events (actor, action, deployment_id, observation, field, status_from, status_to, affected_count, occurred_at)
SELECT '11111111-1111-1111-1111-111111111111', 'publish', d.id, d.observation, d.field, 'draft', 'published',
       COALESCE(d.n_spectra, d.n_targets), COALESCE(d.published_at, d.deployed_at, now() - interval '30 days')
FROM public.deployments d WHERE d.status = 'published';

INSERT INTO public.deploy_events (actor, action, deployment_id, observation, field, status_from, status_to, affected_count, occurred_at)
SELECT '11111111-1111-1111-1111-111111111111', 'revoke', d.id, d.observation, d.field, 'published', 'revoked',
       COALESCE(d.n_spectra, d.n_targets), COALESCE(d.revoked_at, now() - interval '9 days')
FROM public.deployments d WHERE d.status = 'revoked';

INSERT INTO public.deploy_events (actor, action, occurred_at, metadata)
VALUES ('11111111-1111-1111-1111-111111111111', 'config_sync', now() - interval '14 hours',
        '{"kinds": ["observations", "fields"]}'::jsonb);

-- --- Deploy leases: one live, one with a dead heartbeat, one finished -------
INSERT INTO public.deploy_scope_state (scope_type, scope_key, version, last_actor, last_deploy_at,
                                       in_progress_since, in_progress_by, in_progress_host, heartbeat_at)
SELECT 'observation', o.name, 3, '11111111-1111-1111-1111-111111111111', now() - interval '12 days',
       now() - interval '18 minutes', '11111111-1111-1111-1111-111111111111', 'candide-node12', now() - interval '20 seconds'
FROM (SELECT name FROM public.observations ORDER BY name LIMIT 1) o
ON CONFLICT (scope_type, scope_key) DO NOTHING;

INSERT INTO public.deploy_scope_state (scope_type, scope_key, version, last_actor, last_deploy_at,
                                       in_progress_since, in_progress_by, in_progress_host, heartbeat_at)
SELECT 'field', f.name, 1, '11111111-1111-1111-1111-111111111111', now() - interval '40 days',
       now() - interval '3 hours', '11111111-1111-1111-1111-111111111111', 'laptop', now() - interval '2 hours 50 minutes'
FROM (SELECT name FROM public.fields ORDER BY name LIMIT 1) f
ON CONFLICT (scope_type, scope_key) DO NOTHING;

INSERT INTO public.deploy_scope_state (scope_type, scope_key, version, last_actor, last_deploy_at)
SELECT 'observation', o.name, 2, '11111111-1111-1111-1111-111111111111', now() - interval '2 days'
FROM (SELECT name FROM public.observations ORDER BY name OFFSET 1 LIMIT 1) o
ON CONFLICT (scope_type, scope_key) DO NOTHING;

-- --- Access queue -------------------------------------------------------------
INSERT INTO public.inspection_access_requests (user_id, status, message, created_at)
VALUES ('33333333-3333-3333-3333-333333333333', 'pending',
        'Requesting inspection access to help with the triage backlog.',
        now() - interval '3 days');

INSERT INTO public.inspection_access_requests (user_id, status, message, created_at, reviewed_at, reviewed_by)
VALUES ('22222222-2222-2222-2222-222222222222', 'approved', 'Joining the inspection effort.',
        now() - interval '20 days', now() - interval '19 days', '11111111-1111-1111-1111-111111111111');

-- --- Sync snapshot (the nightly public-catalog build) -------------------------
INSERT INTO public.sync_snapshots (started_at, completed_at, created_at, format_version, public_programs, files, status)
SELECT now() - interval '19 hours' - interval '5 minutes', now() - interval '18 hours' - interval '50 minutes',
       now() - interval '19 hours', 1,
       COALESCE(array_agg(p.slug ORDER BY p.slug) FILTER (WHERE p.is_public), '{}'::text[]),
       '[]'::jsonb, 'ready'
FROM public.programs p;

-- --- Recent inspection activity ----------------------------------------------
-- The sampled flag_audit_log rows carry production timestamps (months old),
-- so the 7-day counters would read zero; add a fresh burst on seeded objects.
INSERT INTO public.flag_audit_log (object_id, user_id, field_name, old_value, new_value, changed_at)
SELECT o.id,
       (ARRAY['11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222'])[1 + o.rn % 2]::uuid,
       'redshift_quality', 0, 1 + o.rn % 4,
       (now() AT TIME ZONE 'utc') - (o.rn % 7) * interval '1 day' - (o.rn * interval '31 minutes')
FROM (SELECT id, row_number() OVER (ORDER BY id) AS rn
      FROM public.objects WHERE is_active ORDER BY id LIMIT 10) o;

INSERT INTO public.comments (object_id, user_id, content, created_at)
SELECT o.id, '22222222-2222-2222-2222-222222222222',
       'Seed fixture: line identifications look consistent with the quoted redshift.',
       (now() AT TIME ZONE 'utc') - interval '2 days'
FROM (SELECT id FROM public.objects WHERE is_active ORDER BY id LIMIT 1) o;

INSERT INTO public.comments (target_id, user_id, content, created_at)
SELECT t.id, '11111111-1111-1111-1111-111111111111',
       'Seed fixture: check the continuum slope before sign-off.',
       (now() AT TIME ZONE 'utc') - interval '5 days'
FROM (SELECT id FROM public.targets ORDER BY id LIMIT 1) t;
