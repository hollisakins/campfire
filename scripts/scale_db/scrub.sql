-- Scrub a freshly loaded production data dump for local use (P-1, PR 5).
-- Runs after load.sh restored the public-schema data with triggers disabled.
--
-- Goals: every user identity becomes synthetic, no free text survives, the
-- FK/RLS plumbing still works (auth.users rows exist for every referenced uuid,
-- user_profiles keep their is_admin / can_inspect flags), and three fixed
-- test accounts exist with known uuids (same as seed.sql).
BEGIN;

-- 1. auth.users for every uuid the catalog references (profiles, grants,
--    inspection stamps, audit rows, comments, deploy provenance).
WITH referenced AS (
  SELECT user_id AS id FROM public.user_profiles
  UNION SELECT user_id FROM public.user_program_access
  UNION SELECT last_inspected_by FROM public.objects WHERE last_inspected_by IS NOT NULL
  UNION SELECT last_inspected_by FROM public.targets WHERE last_inspected_by IS NOT NULL
  UNION SELECT user_id FROM public.flag_audit_log WHERE user_id IS NOT NULL
  UNION SELECT user_id FROM public.comments WHERE user_id IS NOT NULL
  UNION SELECT deployed_by FROM public.deployments WHERE deployed_by IS NOT NULL
  UNION SELECT uploaded_by FROM public.storage_objects WHERE uploaded_by IS NOT NULL
  UNION SELECT actor FROM public.deploy_events WHERE actor IS NOT NULL
)
INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, email_confirmed_at,
                        raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
SELECT r.id, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
       'user-' || left(r.id::text, 8) || '@campfire.dev', '', now(),
       '{"provider":"email","providers":["email"]}'::jsonb, '{}'::jsonb, now(), now()
FROM referenced r
WHERE r.id IS NOT NULL
ON CONFLICT (id) DO NOTHING;

-- 2. Profiles: keep the flags, replace the identity.
UPDATE public.user_profiles
SET username  = 'user-' || left(user_id::text, 8),
    full_name = 'Scale DB user ' || left(user_id::text, 8);

-- 3. Free text: every user-authored text column in a dumped table
--    (the invite / access-request / share-link tables are never dumped).
UPDATE public.comments SET content = 'scrubbed comment ' || id;
UPDATE public.object_lists SET name = 'list ' || id, slug = 'list-' || id, description = NULL WHERE NOT is_system;
UPDATE public.object_list_members SET notes = NULL WHERE notes IS NOT NULL;
UPDATE public.nircam_exposures SET notes = NULL WHERE notes IS NOT NULL;
UPDATE public.nirspec_rate_exposures SET notes = NULL WHERE notes IS NOT NULL;
UPDATE public.spectrum_exposures SET notes = NULL WHERE notes IS NOT NULL;
UPDATE public.nirspec_source_review SET notes = NULL WHERE notes IS NOT NULL;

-- 4. The three fixed test accounts from seed.sql (admin / user / viewer),
--    with the same password (password123) and the auth.identities rows
--    GoTrue needs for an email sign-in. A uuid already present from step 1
--    (a production account that happens to share it) is upgraded in place.
INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, email_confirmed_at,
                        raw_app_meta_data, raw_user_meta_data, created_at, updated_at,
                        confirmation_token, recovery_token, email_change_token_new, email_change) VALUES
  ('11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'admin@campfire.dev',  extensions.crypt('password123', extensions.gen_salt('bf')), now(), '{"provider":"email","providers":["email"]}', '{}', now(), now(), '', '', '', ''),
  ('22222222-2222-2222-2222-222222222222', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'user@campfire.dev',   extensions.crypt('password123', extensions.gen_salt('bf')), now(), '{"provider":"email","providers":["email"]}', '{}', now(), now(), '', '', '', ''),
  ('33333333-3333-3333-3333-333333333333', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'viewer@campfire.dev', extensions.crypt('password123', extensions.gen_salt('bf')), now(), '{"provider":"email","providers":["email"]}', '{}', now(), now(), '', '', '', '')
ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email, encrypted_password = EXCLUDED.encrypted_password,
                               email_confirmed_at = EXCLUDED.email_confirmed_at;
INSERT INTO auth.identities (id, user_id, identity_data, provider, provider_id, last_sign_in_at, created_at, updated_at)
SELECT gen_random_uuid(), u.id, jsonb_build_object('sub', u.id::text, 'email', u.email), 'email', u.id::text, now(), now(), now()
FROM auth.users u
WHERE u.id IN ('11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222', '33333333-3333-3333-3333-333333333333')
  AND NOT EXISTS (SELECT 1 FROM auth.identities i WHERE i.user_id = u.id AND i.provider = 'email');
INSERT INTO public.user_profiles (user_id, username, full_name, is_admin, can_comment, can_inspect) VALUES
  ('11111111-1111-1111-1111-111111111111', 'admin',  'Scale DB Admin',  true,  true, true),
  ('22222222-2222-2222-2222-222222222222', 'user',   'Scale DB User',   false, true, true),
  ('33333333-3333-3333-3333-333333333333', 'viewer', 'Scale DB Viewer', false, true, false)
ON CONFLICT (user_id) DO UPDATE SET is_admin = EXCLUDED.is_admin, can_inspect = EXCLUDED.can_inspect;

-- 5. Sanity: no production email survives.
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM auth.users WHERE email NOT LIKE '%@campfire.dev';
  IF n > 0 THEN RAISE EXCEPTION 'scrub: % auth.users rows keep a non-synthetic email', n; END IF;
  SELECT (SELECT count(*) FROM public.comments WHERE content NOT LIKE 'scrubbed comment %')
       + (SELECT count(*) FROM public.object_lists WHERE description IS NOT NULL)
       + (SELECT count(*) FROM public.object_list_members WHERE notes IS NOT NULL)
       + (SELECT count(*) FROM public.nircam_exposures WHERE notes IS NOT NULL)
       + (SELECT count(*) FROM public.nirspec_rate_exposures WHERE notes IS NOT NULL)
       + (SELECT count(*) FROM public.spectrum_exposures WHERE notes IS NOT NULL)
       + (SELECT count(*) FROM public.nirspec_source_review WHERE notes IS NOT NULL)
    INTO n;
  IF n > 0 THEN RAISE EXCEPTION 'scrub: % free-text values survived', n; END IF;
  SELECT count(*) INTO n FROM auth.users u
   WHERE u.email IN ('admin@campfire.dev', 'user@campfire.dev', 'viewer@campfire.dev')
     AND (u.encrypted_password = '' OR NOT EXISTS (SELECT 1 FROM auth.identities i WHERE i.user_id = u.id));
  IF n > 0 THEN RAISE EXCEPTION 'scrub: % test accounts cannot sign in', n; END IF;
END $$;

COMMIT;
ANALYZE;
