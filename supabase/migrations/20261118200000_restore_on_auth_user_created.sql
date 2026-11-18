SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- The 2026-10-25 cutover lost on_auth_user_created: dump-hosted.sh's schema
-- dump carries no auth.* DDL (GoTrue recreates the auth schema), and a trigger
-- ON auth.users is auth DDL, so the restore never recreated it. Every open
-- self-registration since then got an auth.users row and no user_profiles
-- row: invisible on /admin/users and stuck at the /welcome redirect.
-- supabase/schemas/triggers.sql already declares the trigger; this puts it
-- back where the schema says it is.
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- Provision the self-signups the missing trigger skipped, exactly as
-- handle_new_user would have. Limited to signups since the cutover: an older
-- auth user without a profile may be one an admin deleted (the admin delete
-- removes the profile and keeps the auth identity), which must stay deleted.
-- A few dozen rows; idempotent (ON CONFLICT DO NOTHING).
DO $$
DECLARE
    r record;
    v_base text;
    v_username text;
    v_full_name text;
    v_suffix integer;
BEGIN
    FOR r IN
        SELECT u.id, u.email, u.raw_user_meta_data AS meta
        FROM auth.users u
        LEFT JOIN public.user_profiles p ON p.user_id = u.id
        WHERE p.user_id IS NULL
          AND u.raw_user_meta_data->>'self_signup' = 'true'
          AND u.created_at >= '2026-10-25'
        ORDER BY u.created_at
    LOOP
        v_base := lower(coalesce(nullif(r.meta->>'username', ''), split_part(r.email, '@', 1)));
        v_base := regexp_replace(v_base, '[^a-z0-9._-]', '', 'g');
        v_base := regexp_replace(v_base, '^[._-]+', '');
        v_base := regexp_replace(v_base, '[._-]+$', '');
        IF length(v_base) < 2 THEN
            v_base := 'user' || v_base;
        END IF;
        v_base := left(v_base, 38);
        v_base := regexp_replace(v_base, '[._-]+$', '');

        v_full_name := coalesce(nullif(r.meta->>'full_name', ''), v_base);

        v_username := v_base;
        v_suffix := 0;
        WHILE EXISTS (SELECT 1 FROM public.user_profiles WHERE username = v_username) LOOP
            v_suffix := v_suffix + 1;
            v_username := left(v_base, 39 - length(v_suffix::text)) || v_suffix::text;
        END LOOP;

        INSERT INTO public.user_profiles (
            user_id, username, full_name,
            is_group_account, can_comment, can_inspect, is_admin
        )
        VALUES (r.id, v_username, v_full_name, false, true, false, false)
        ON CONFLICT (user_id) DO NOTHING;
    END LOOP;
END $$;
