-- =============================================================================
-- Enable RLS on all unprotected tables + rewrite existing policies to use
-- helper functions (is_admin, can_comment, accessible_program_slugs).
-- =============================================================================
-- Wrapped in a transaction so RLS is never enabled without policies.
-- =============================================================================

BEGIN;

-- =============================================================================
-- 1. Enable RLS and add policies for previously unprotected tables
-- =============================================================================

-- -----------------------------------------------------------------------------
-- user_profiles
-- -----------------------------------------------------------------------------
ALTER TABLE user_profiles ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read all profiles (needed for comment author
-- names, inspection tracking "last inspected by", admin user list).
CREATE POLICY "authenticated_select_profiles"
  ON user_profiles FOR SELECT TO authenticated
  USING (true);

-- Users can update their own profile (name, preferences).
CREATE POLICY "self_update_profile"
  ON user_profiles FOR UPDATE TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- Admins can update any profile (is_admin, can_comment toggles).
CREATE POLICY "admin_update_profile"
  ON user_profiles FOR UPDATE TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());

-- Admins can delete profiles (user management).
CREATE POLICY "admin_delete_profile"
  ON user_profiles FOR DELETE TO authenticated
  USING (public.is_admin());

-- Admins can insert profiles (manual user creation).
CREATE POLICY "admin_insert_profile"
  ON user_profiles FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

-- -----------------------------------------------------------------------------
-- user_program_access
-- -----------------------------------------------------------------------------
ALTER TABLE user_program_access ENABLE ROW LEVEL SECURITY;

-- Users can see their own access grants.
CREATE POLICY "self_select_access"
  ON user_program_access FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- Admins can see all access grants (user management panel).
CREATE POLICY "admin_select_access"
  ON user_program_access FOR SELECT TO authenticated
  USING (public.is_admin());

-- Admins can grant program access.
CREATE POLICY "admin_insert_access"
  ON user_program_access FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

-- Admins can revoke program access.
CREATE POLICY "admin_delete_access"
  ON user_program_access FOR DELETE TO authenticated
  USING (public.is_admin());

-- -----------------------------------------------------------------------------
-- programs
-- -----------------------------------------------------------------------------
ALTER TABLE programs ENABLE ROW LEVEL SECURITY;

-- Public programs visible to all authenticated users.
-- Private programs visible only to users with explicit access.
CREATE POLICY "accessible_programs_select"
  ON programs FOR SELECT TO authenticated
  USING (
    is_public = true
    OR slug IN (SELECT program_slug FROM user_program_access WHERE user_id = auth.uid())
  );

-- Admins can see all programs (including private ones without access).
CREATE POLICY "admin_programs_select"
  ON programs FOR SELECT TO authenticated
  USING (public.is_admin());

-- Admins can update programs (toggle is_public, edit metadata).
CREATE POLICY "admin_programs_update"
  ON programs FOR UPDATE TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());

-- -----------------------------------------------------------------------------
-- observations
-- -----------------------------------------------------------------------------
ALTER TABLE observations ENABLE ROW LEVEL SECURITY;

-- Observations visible if the parent program is accessible.
CREATE POLICY "accessible_observations_select"
  ON observations FOR SELECT TO authenticated
  USING (
    program_slug = ANY(public.accessible_program_slugs())
  );

-- -----------------------------------------------------------------------------
-- nircam_images
-- -----------------------------------------------------------------------------
ALTER TABLE nircam_images ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read (reference data, no program association).
CREATE POLICY "authenticated_select_nircam"
  ON nircam_images FOR SELECT TO authenticated
  USING (true);

-- -----------------------------------------------------------------------------
-- flag_definitions
-- -----------------------------------------------------------------------------
ALTER TABLE flag_definitions ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read (reference data for flag display).
CREATE POLICY "authenticated_select_flags"
  ON flag_definitions FOR SELECT TO authenticated
  USING (true);

-- =============================================================================
-- 2. Rewrite existing policies to use helper functions
-- =============================================================================
-- This avoids recursion now that user_profiles has RLS, and centralizes
-- the access check pattern.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- objects
-- -----------------------------------------------------------------------------
DROP POLICY IF EXISTS "select_objects_by_access" ON objects;
CREATE POLICY "select_objects_by_access" ON objects FOR SELECT USING (
  program_slug = ANY(public.accessible_program_slugs())
);

DROP POLICY IF EXISTS "update_objects_by_access" ON objects;
CREATE POLICY "update_objects_by_access" ON objects FOR UPDATE USING (
  program_slug IN (SELECT program_slug FROM user_program_access WHERE user_id = auth.uid())
  AND public.can_comment()
);

-- -----------------------------------------------------------------------------
-- spectra
-- -----------------------------------------------------------------------------
DROP POLICY IF EXISTS "select_spectra_by_access" ON spectra;
CREATE POLICY "select_spectra_by_access" ON spectra FOR SELECT USING (
  object_id IN (
    SELECT o.object_id FROM objects o
    WHERE o.program_slug = ANY(public.accessible_program_slugs())
  )
);

-- -----------------------------------------------------------------------------
-- comments
-- -----------------------------------------------------------------------------
DROP POLICY IF EXISTS "select_comments_by_access" ON comments;
CREATE POLICY "select_comments_by_access" ON comments FOR SELECT USING (
  object_id IN (
    SELECT o.id FROM objects o
    WHERE o.program_slug IN (SELECT program_slug FROM user_program_access WHERE user_id = auth.uid())
  )
);

DROP POLICY IF EXISTS "insert_comments_by_access" ON comments;
CREATE POLICY "insert_comments_by_access" ON comments FOR INSERT WITH CHECK (
  object_id IN (
    SELECT o.id FROM objects o
    WHERE o.program_slug IN (SELECT program_slug FROM user_program_access WHERE user_id = auth.uid())
  )
  AND public.can_comment()
);

-- -----------------------------------------------------------------------------
-- flag_audit_log
-- -----------------------------------------------------------------------------
DROP POLICY IF EXISTS "select_audit_by_access" ON flag_audit_log;
CREATE POLICY "select_audit_by_access" ON flag_audit_log FOR SELECT USING (
  object_id IN (
    SELECT o.id FROM objects o
    WHERE o.program_slug IN (SELECT program_slug FROM user_program_access WHERE user_id = auth.uid())
  )
);

DROP POLICY IF EXISTS "insert_audit_by_access" ON flag_audit_log;
CREATE POLICY "insert_audit_by_access" ON flag_audit_log FOR INSERT TO authenticated WITH CHECK (
  object_id IN (
    SELECT o.id FROM objects o
    WHERE o.program_slug = ANY(public.accessible_program_slugs())
  )
);

-- -----------------------------------------------------------------------------
-- pending_invites (admin policies)
-- -----------------------------------------------------------------------------
DROP POLICY IF EXISTS "Admins can create invites" ON pending_invites;
CREATE POLICY "admin_insert_invites" ON pending_invites
  FOR INSERT TO authenticated WITH CHECK (public.is_admin());

DROP POLICY IF EXISTS "Admins can delete invites" ON pending_invites;
CREATE POLICY "admin_delete_invites" ON pending_invites
  FOR DELETE TO authenticated USING (public.is_admin());

DROP POLICY IF EXISTS "Admins can update invites" ON pending_invites;
CREATE POLICY "admin_update_invites" ON pending_invites
  FOR UPDATE TO authenticated USING (public.is_admin());

DROP POLICY IF EXISTS "Admins can view invites" ON pending_invites;
CREATE POLICY "admin_select_invites" ON pending_invites
  FOR SELECT TO authenticated USING (public.is_admin());

-- "Users can read own invite by email" remains unchanged (no user_profiles reference)

-- -----------------------------------------------------------------------------
-- access_codes (admin policy)
-- -----------------------------------------------------------------------------
DROP POLICY IF EXISTS "Admins can manage codes" ON access_codes;
CREATE POLICY "admin_manage_codes" ON access_codes
  USING (public.is_admin());

-- "Anyone can read active codes" remains unchanged

-- -----------------------------------------------------------------------------
-- code_redemptions (admin policy)
-- -----------------------------------------------------------------------------
DROP POLICY IF EXISTS "Admins can see all redemptions" ON code_redemptions;
CREATE POLICY "admin_select_redemptions" ON code_redemptions
  FOR SELECT USING (public.is_admin());

-- "Users can see own redemptions" and "Users can redeem codes" remain unchanged

-- -----------------------------------------------------------------------------
-- account_requests (admin policies)
-- -----------------------------------------------------------------------------
DROP POLICY IF EXISTS "Admins can update requests" ON account_requests;
CREATE POLICY "admin_update_requests" ON account_requests
  FOR UPDATE TO authenticated USING (public.is_admin());

DROP POLICY IF EXISTS "Admins can view requests" ON account_requests;
CREATE POLICY "admin_select_requests" ON account_requests
  FOR SELECT TO authenticated USING (public.is_admin());

-- "Anyone can submit requests" and "Users can check own request status" remain unchanged

-- -----------------------------------------------------------------------------
-- download_log (admin policy)
-- -----------------------------------------------------------------------------
DROP POLICY IF EXISTS "Admins can view all downloads" ON download_log;
CREATE POLICY "admin_select_downloads" ON download_log
  FOR SELECT TO authenticated USING (public.is_admin());

-- "Users can view own downloads" remains unchanged

-- -----------------------------------------------------------------------------
-- password_reset_log (admin policy)
-- -----------------------------------------------------------------------------
DROP POLICY IF EXISTS "Admins can view all reset logs" ON password_reset_log;
CREATE POLICY "admin_select_reset_logs" ON password_reset_log
  FOR SELECT USING (public.is_admin());

-- "Users can view own reset logs" remains unchanged

-- =============================================================================
-- 3. Fix get_objects_in_viewport: remove SECURITY DEFINER so RLS applies
-- =============================================================================

DROP FUNCTION IF EXISTS public.get_objects_in_viewport;

CREATE OR REPLACE FUNCTION public.get_objects_in_viewport(
    p_ra_min double precision,
    p_ra_max double precision,
    p_dec_min double precision,
    p_dec_max double precision,
    p_field text DEFAULT NULL,
    p_limit integer DEFAULT 5000
)
RETURNS TABLE (
    "object_id" text,
    "ra" double precision,
    "dec" double precision,
    "redshift" double precision,
    "redshift_quality" integer,
    "field" text,
    "program_slug" text
)
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    RETURN QUERY
    SELECT
        o.object_id,
        o.ra,
        o.dec,
        o.redshift::double precision,
        o.redshift_quality,
        o.field,
        o.program_slug
    FROM public.objects o
    WHERE
        o.ra BETWEEN p_ra_min AND p_ra_max
        AND o.dec BETWEEN p_dec_min AND p_dec_max
        AND (p_field IS NULL OR o.field = p_field)
    ORDER BY o.ra
    LIMIT p_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_objects_in_viewport TO authenticated;

COMMIT;
