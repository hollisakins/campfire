-- Reconcile can_inspect() and handle_new_user() with the declarative schema
-- files (fixes #228).
--
-- Root cause: the applied definitions from
-- 20260625000000_open_registration_and_can_inspect.sql are functionally
-- identical to supabase/schemas/functions.sql (can_inspect) and
-- supabase/schemas/triggers.sql (handle_new_user), but differ in keyword case
-- and inline comments. migra (under `supabase db diff`) compares function
-- bodies (pg_proc.prosrc) verbatim, so those cosmetic differences made every
-- unrelated migration re-emit both functions.
--
-- This is NOT a search_path issue: both the applied migration and the schema
-- files already pin `SET search_path = public`. This migration only aligns the
-- stored body text with the schema files so future `db diff` runs are clean.
-- No behavioral change; CREATE OR REPLACE keeps the on_auth_user_created
-- trigger's dependency on handle_new_user() intact (no DROP ... CASCADE).

CREATE OR REPLACE FUNCTION public.can_inspect()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT can_inspect FROM user_profiles WHERE user_id = auth.uid()),
    false
  );
$$;

CREATE OR REPLACE FUNCTION public.handle_new_user() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_base text;
    v_username text;
    v_full_name text;
    v_suffix integer := 0;
BEGIN
    -- Only handle genuine self-service signups.
    IF NEW.raw_user_meta_data->>'self_signup' IS DISTINCT FROM 'true' THEN
        RETURN NEW;
    END IF;

    -- Derive a username candidate from metadata or the email local-part, then
    -- coerce it to satisfy user_profiles_username_check
    -- (^[a-z0-9][a-z0-9._-]{0,38}[a-z0-9]$).
    v_base := lower(coalesce(
        nullif(NEW.raw_user_meta_data->>'username', ''),
        split_part(NEW.email, '@', 1)
    ));
    v_base := regexp_replace(v_base, '[^a-z0-9._-]', '', 'g');
    v_base := regexp_replace(v_base, '^[._-]+', '');
    v_base := regexp_replace(v_base, '[._-]+$', '');
    IF length(v_base) < 2 THEN
        v_base := 'user' || v_base;
    END IF;
    v_base := left(v_base, 38);
    v_base := regexp_replace(v_base, '[._-]+$', '');

    v_full_name := coalesce(nullif(NEW.raw_user_meta_data->>'full_name', ''), v_base);

    -- De-duplicate with a numeric suffix if needed.
    v_username := v_base;
    WHILE EXISTS (SELECT 1 FROM public.user_profiles WHERE username = v_username) LOOP
        v_suffix := v_suffix + 1;
        v_username := left(v_base, 39 - length(v_suffix::text)) || v_suffix::text;
    END LOOP;

    INSERT INTO public.user_profiles (
        user_id, username, full_name,
        is_group_account, can_comment, can_inspect, is_admin
    )
    VALUES (
        NEW.id, v_username, v_full_name,
        false, true, false, false
    )
    ON CONFLICT (user_id) DO NOTHING;

    RETURN NEW;
END;
$$;
