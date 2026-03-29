-- =============================================================================
-- RLS Helper Functions
-- =============================================================================
-- SECURITY DEFINER functions that bypass RLS to read the calling user's own
-- profile and access grants. These prevent infinite recursion once
-- user_profiles and user_program_access have RLS enabled, and centralize
-- repeated subquery patterns used across many policies.
-- =============================================================================

-- Check if the current user is an admin
CREATE OR REPLACE FUNCTION public.is_admin()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT is_admin FROM user_profiles WHERE user_id = auth.uid()),
    false
  );
$$;

-- Check if the current user has comment/inspection permission
CREATE OR REPLACE FUNCTION public.can_comment()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT can_comment FROM user_profiles WHERE user_id = auth.uid()),
    false
  );
$$;

-- Return all program slugs the current user can access (explicit grants + public)
CREATE OR REPLACE FUNCTION public.accessible_program_slugs()
RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(array_agg(DISTINCT slug), '{}')
  FROM (
    SELECT program_slug AS slug
    FROM user_program_access
    WHERE user_id = auth.uid()
    UNION
    SELECT slug
    FROM programs
    WHERE is_public = true
  ) sub;
$$;

GRANT EXECUTE ON FUNCTION public.is_admin() TO authenticated;
GRANT EXECUTE ON FUNCTION public.can_comment() TO authenticated;
GRANT EXECUTE ON FUNCTION public.accessible_program_slugs() TO authenticated;
