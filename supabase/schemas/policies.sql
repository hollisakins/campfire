-- =============================================================================
-- CAMPFIRE Supabase Schema: RLS Policies
-- =============================================================================
-- Canonical source of truth for all Row Level Security policies.
-- Do NOT read migration files to understand current signatures or behavior.
--
-- Workflow: edit here → run apply.sh → supabase db diff → commit migration
-- =============================================================================


-- NOTE: RLS helper functions (is_admin, can_comment, accessible_program_slugs)
-- are defined in functions.sql, which is applied before this file.


-- =============================================================================
-- user_profiles
-- =============================================================================

ALTER TABLE user_profiles ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read all profiles (needed for comment author
-- names, inspection tracking "last inspected by", admin user list).
DROP POLICY IF EXISTS "authenticated_select_profiles" ON user_profiles;
CREATE POLICY "authenticated_select_profiles"
  ON user_profiles FOR SELECT TO authenticated
  USING (true);

-- Users can update their own profile (name, preferences).
DROP POLICY IF EXISTS "self_update_profile" ON user_profiles;
CREATE POLICY "self_update_profile"
  ON user_profiles FOR UPDATE TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- Admins can update any profile (is_admin, can_comment toggles).
DROP POLICY IF EXISTS "admin_update_profile" ON user_profiles;
CREATE POLICY "admin_update_profile"
  ON user_profiles FOR UPDATE TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());

-- Admins can delete profiles (user management).
DROP POLICY IF EXISTS "admin_delete_profile" ON user_profiles;
CREATE POLICY "admin_delete_profile"
  ON user_profiles FOR DELETE TO authenticated
  USING (public.is_admin());

-- Admins can insert profiles (manual user creation).
DROP POLICY IF EXISTS "admin_insert_profile" ON user_profiles;
CREATE POLICY "admin_insert_profile"
  ON user_profiles FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());


-- =============================================================================
-- user_program_access
-- =============================================================================

ALTER TABLE user_program_access ENABLE ROW LEVEL SECURITY;

-- Users can see their own access grants.
DROP POLICY IF EXISTS "self_select_access" ON user_program_access;
CREATE POLICY "self_select_access"
  ON user_program_access FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- Admins can see all access grants (user management panel).
DROP POLICY IF EXISTS "admin_select_access" ON user_program_access;
CREATE POLICY "admin_select_access"
  ON user_program_access FOR SELECT TO authenticated
  USING (public.is_admin());

-- Admins can grant program access.
DROP POLICY IF EXISTS "admin_insert_access" ON user_program_access;
CREATE POLICY "admin_insert_access"
  ON user_program_access FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

-- Admins can revoke program access.
DROP POLICY IF EXISTS "admin_delete_access" ON user_program_access;
CREATE POLICY "admin_delete_access"
  ON user_program_access FOR DELETE TO authenticated
  USING (public.is_admin());


-- =============================================================================
-- programs
-- =============================================================================

ALTER TABLE programs ENABLE ROW LEVEL SECURITY;

-- Public programs visible to all authenticated users.
-- Private programs visible only to users with explicit access.
DROP POLICY IF EXISTS "accessible_programs_select" ON programs;
CREATE POLICY "accessible_programs_select"
  ON programs FOR SELECT TO authenticated
  USING (
    is_public = true
    OR slug IN (SELECT program_slug FROM user_program_access WHERE user_id = auth.uid())
  );

-- Admins can see all programs (including private ones without access).
DROP POLICY IF EXISTS "admin_programs_select" ON programs;
CREATE POLICY "admin_programs_select"
  ON programs FOR SELECT TO authenticated
  USING (public.is_admin());

-- Admins can insert programs (deploy CLI: sync-programs).
DROP POLICY IF EXISTS "admin_programs_insert" ON programs;
CREATE POLICY "admin_programs_insert"
  ON programs FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

-- Admins can update programs (toggle is_public, edit metadata).
DROP POLICY IF EXISTS "admin_programs_update" ON programs;
CREATE POLICY "admin_programs_update"
  ON programs FOR UPDATE TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());


-- =============================================================================
-- observations
-- =============================================================================

ALTER TABLE observations ENABLE ROW LEVEL SECURITY;

-- Observations visible if the parent program is accessible.
DROP POLICY IF EXISTS "accessible_observations_select" ON observations;
CREATE POLICY "accessible_observations_select"
  ON observations FOR SELECT TO authenticated
  USING (
    program_slug = ANY(public.accessible_program_slugs())
  );

-- Admins can insert observations (deploy CLI).
DROP POLICY IF EXISTS "admin_observations_insert" ON observations;
CREATE POLICY "admin_observations_insert"
  ON observations FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

-- Admins can update observations (deploy CLI: last_deployed_at, counts).
DROP POLICY IF EXISTS "admin_observations_update" ON observations;
CREATE POLICY "admin_observations_update"
  ON observations FOR UPDATE TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());


-- =============================================================================
-- targets (renamed from objects)
-- =============================================================================

ALTER TABLE targets ENABLE ROW LEVEL SECURITY;

-- Targets visible if their program is accessible.
DROP POLICY IF EXISTS "select_targets_by_access" ON targets;
CREATE POLICY "select_targets_by_access"
  ON targets FOR SELECT
  USING (
    program_slug = ANY(public.accessible_program_slugs())
  );

-- Users with can_comment permission can update targets in accessible programs.
DROP POLICY IF EXISTS "update_targets_by_access" ON targets;
CREATE POLICY "update_targets_by_access"
  ON targets FOR UPDATE
  USING (
    program_slug = ANY(public.accessible_program_slugs())
    AND public.can_comment()
  );

-- Admins can insert targets (deploy CLI).
DROP POLICY IF EXISTS "admin_targets_insert" ON targets;
CREATE POLICY "admin_targets_insert"
  ON targets FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

-- Admins can update all target fields (deploy CLI: pipeline fields, redshift drift reset).
DROP POLICY IF EXISTS "admin_targets_update" ON targets;
CREATE POLICY "admin_targets_update"
  ON targets FOR UPDATE TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());

-- Admins can delete targets (deploy CLI: remove/un-deploy observation).
DROP POLICY IF EXISTS "admin_targets_delete" ON targets;
CREATE POLICY "admin_targets_delete"
  ON targets FOR DELETE TO authenticated
  USING (public.is_admin());


-- =============================================================================
-- objects
-- =============================================================================

ALTER TABLE objects ENABLE ROW LEVEL SECURITY;

-- Objects visible if any of their member programs are accessible.
-- Uses the programs[] array column (populated at deploy time) to avoid
-- a JOIN to targets on every read.
DROP POLICY IF EXISTS "select_objects_by_access" ON objects;
CREATE POLICY "select_objects_by_access"
  ON objects FOR SELECT
  USING (
    programs && public.accessible_program_slugs()
  );

-- Admins can insert objects (deploy CLI: objects rebuild).
DROP POLICY IF EXISTS "admin_objects_insert" ON objects;
CREATE POLICY "admin_objects_insert"
  ON objects FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

-- Admins can update objects (deploy CLI: objects rebuild).
DROP POLICY IF EXISTS "admin_objects_update" ON objects;
CREATE POLICY "admin_objects_update"
  ON objects FOR UPDATE TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());

-- Phase A: users with can_comment permission can update objects whose
-- programs[] overlaps their accessible programs. Mirrors the targets
-- update_targets_by_access policy. Field-level restriction (only allow
-- writing redshift_inspected, redshift_quality, last_inspected_*) is
-- enforced by the `enforce_object_user_update_scope` trigger in
-- triggers.sql — Postgres RLS does not support per-column UPDATE policies.
-- WITH CHECK mirrors USING so a row can't be moved out of the caller's
-- program access.
DROP POLICY IF EXISTS "update_objects_by_access" ON objects;
CREATE POLICY "update_objects_by_access"
  ON objects FOR UPDATE
  USING (
    programs && public.accessible_program_slugs()
    AND public.can_comment()
  )
  WITH CHECK (
    programs && public.accessible_program_slugs()
    AND public.can_comment()
  );

-- Admins can delete objects (deploy CLI: objects rebuild wipes before re-insert).
DROP POLICY IF EXISTS "admin_objects_delete" ON objects;
CREATE POLICY "admin_objects_delete"
  ON objects FOR DELETE TO authenticated
  USING (public.is_admin());


-- =============================================================================
-- object_photometry
-- =============================================================================

ALTER TABLE object_photometry ENABLE ROW LEVEL SECURITY;

-- Photometry visible if the linked object is accessible.
DROP POLICY IF EXISTS "select_object_photometry_by_access" ON object_photometry;
CREATE POLICY "select_object_photometry_by_access"
  ON object_photometry FOR SELECT
  USING (
    object_id IN (
      SELECT o.id FROM objects o
      WHERE o.programs && public.accessible_program_slugs()
    )
  );

-- Admins can insert photometry (deploy CLI).
DROP POLICY IF EXISTS "admin_object_photometry_insert" ON object_photometry;
CREATE POLICY "admin_object_photometry_insert"
  ON object_photometry FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

-- Admins can update photometry (deploy CLI).
DROP POLICY IF EXISTS "admin_object_photometry_update" ON object_photometry;
CREATE POLICY "admin_object_photometry_update"
  ON object_photometry FOR UPDATE TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());

-- Admins can delete photometry (deploy CLI).
DROP POLICY IF EXISTS "admin_object_photometry_delete" ON object_photometry;
CREATE POLICY "admin_object_photometry_delete"
  ON object_photometry FOR DELETE TO authenticated
  USING (public.is_admin());


-- =============================================================================
-- object_lists
-- =============================================================================

ALTER TABLE object_lists ENABLE ROW LEVEL SECURITY;

-- Users can see: their own lists + public lists + public_edit lists.
DROP POLICY IF EXISTS "select_lists" ON object_lists;
CREATE POLICY "select_lists"
  ON object_lists FOR SELECT TO authenticated
  USING (
    created_by = auth.uid()
    OR visibility IN ('public_read', 'public_edit')
  );

-- Users can create lists (owned by them, non-system, non-group-account).
DROP POLICY IF EXISTS "insert_lists" ON object_lists;
CREATE POLICY "insert_lists"
  ON object_lists FOR INSERT TO authenticated
  WITH CHECK (
    created_by = auth.uid()
    AND is_system = false
    AND public.can_comment()
    AND NOT public.is_group_account()
  );

-- Owners can update their own lists (but not system lists).
DROP POLICY IF EXISTS "update_own_lists" ON object_lists;
CREATE POLICY "update_own_lists"
  ON object_lists FOR UPDATE TO authenticated
  USING (created_by = auth.uid() AND is_system = false)
  WITH CHECK (created_by = auth.uid() AND is_system = false);

-- Owners can delete their own lists (but not system lists).
DROP POLICY IF EXISTS "delete_own_lists" ON object_lists;
CREATE POLICY "delete_own_lists"
  ON object_lists FOR DELETE TO authenticated
  USING (created_by = auth.uid() AND is_system = false);

-- Admins can manage all lists including system lists.
DROP POLICY IF EXISTS "admin_manage_lists" ON object_lists;
CREATE POLICY "admin_manage_lists"
  ON object_lists
  USING (public.is_admin());


-- =============================================================================
-- object_list_members
-- =============================================================================

ALTER TABLE object_list_members ENABLE ROW LEVEL SECURITY;

-- Members visible if:
--   1. The list is visible to the user, AND
--   2. The matched object (if any) has at least one accessible program
-- Members with NULL object_id (orphaned) are visible to the list owner
-- OR to anyone if the list is public_edit (so co-editors can see orphans).
DROP POLICY IF EXISTS "select_list_members" ON object_list_members;
CREATE POLICY "select_list_members"
  ON object_list_members FOR SELECT TO authenticated
  USING (
    list_id IN (
      SELECT id FROM object_lists
      WHERE created_by = auth.uid()
         OR visibility IN ('public_read', 'public_edit')
    )
    AND (
      (object_id IS NULL AND list_id IN (
        SELECT id FROM object_lists
        WHERE created_by = auth.uid() OR visibility = 'public_edit'
      ))
      OR object_id IN (
        SELECT o.id FROM objects o
        WHERE o.programs && public.accessible_program_slugs()
      )
    )
  );

-- can_comment users can add members to own lists + public_edit lists.
DROP POLICY IF EXISTS "insert_list_members" ON object_list_members;
CREATE POLICY "insert_list_members"
  ON object_list_members FOR INSERT TO authenticated
  WITH CHECK (
    public.can_comment()
    AND list_id IN (
      SELECT id FROM object_lists
      WHERE created_by = auth.uid()
         OR visibility = 'public_edit'
    )
  );

-- can_comment users can update members in own lists + public_edit lists
-- (needed for upsert ON CONFLICT DO UPDATE when re-linking coordinate entries).
DROP POLICY IF EXISTS "update_list_members" ON object_list_members;
CREATE POLICY "update_list_members"
  ON object_list_members FOR UPDATE TO authenticated
  USING (
    public.can_comment()
    AND list_id IN (
      SELECT id FROM object_lists
      WHERE created_by = auth.uid()
         OR visibility = 'public_edit'
    )
  )
  WITH CHECK (
    public.can_comment()
    AND list_id IN (
      SELECT id FROM object_lists
      WHERE created_by = auth.uid()
         OR visibility = 'public_edit'
    )
  );

-- can_comment users can remove members from own lists + public_edit lists.
DROP POLICY IF EXISTS "delete_list_members" ON object_list_members;
CREATE POLICY "delete_list_members"
  ON object_list_members FOR DELETE TO authenticated
  USING (
    public.can_comment()
    AND list_id IN (
      SELECT id FROM object_lists
      WHERE created_by = auth.uid()
         OR visibility = 'public_edit'
    )
  );

-- Admins can manage all list members.
DROP POLICY IF EXISTS "admin_manage_list_members" ON object_list_members;
CREATE POLICY "admin_manage_list_members"
  ON object_list_members
  USING (public.is_admin());


-- =============================================================================
-- list_audit_log
-- =============================================================================

ALTER TABLE list_audit_log ENABLE ROW LEVEL SECURITY;

-- Audit log visible if the parent list is visible.
DROP POLICY IF EXISTS "select_list_audit" ON list_audit_log;
CREATE POLICY "select_list_audit"
  ON list_audit_log FOR SELECT TO authenticated
  USING (
    list_id IN (
      SELECT id FROM object_lists
      WHERE created_by = auth.uid()
         OR visibility IN ('public_read', 'public_edit')
    )
  );

-- Admins can see all list audit entries.
DROP POLICY IF EXISTS "admin_select_list_audit" ON list_audit_log;
CREATE POLICY "admin_select_list_audit"
  ON list_audit_log FOR SELECT TO authenticated
  USING (public.is_admin());


-- =============================================================================
-- spectra
-- =============================================================================

ALTER TABLE spectra ENABLE ROW LEVEL SECURITY;

-- Spectra visible if their parent target is in an accessible program.
DROP POLICY IF EXISTS "select_spectra_by_access" ON spectra;
CREATE POLICY "select_spectra_by_access"
  ON spectra FOR SELECT
  USING (
    target_id IN (
      SELECT t.target_id FROM targets t
      WHERE t.program_slug = ANY(public.accessible_program_slugs())
    )
  );

-- Admins can insert spectra (deploy CLI).
DROP POLICY IF EXISTS "admin_spectra_insert" ON spectra;
CREATE POLICY "admin_spectra_insert"
  ON spectra FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

-- Admins can update spectra (deploy CLI: thumbnails, provenance).
DROP POLICY IF EXISTS "admin_spectra_update" ON spectra;
CREATE POLICY "admin_spectra_update"
  ON spectra FOR UPDATE TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());

-- Admins can delete spectra (deploy CLI: remove/un-deploy observation).
DROP POLICY IF EXISTS "admin_spectra_delete" ON spectra;
CREATE POLICY "admin_spectra_delete"
  ON spectra FOR DELETE TO authenticated
  USING (public.is_admin());

-- Users with can_comment may update spectra whose parent target is in an
-- accessible program. Column scope is restricted to dq_flags (and the
-- trigger-maintained updated_at) by enforce_spectra_dq_user_update_scope
-- in triggers.sql — Postgres RLS does not support per-column UPDATE
-- policies. Mirrors update_objects_by_access.
DROP POLICY IF EXISTS "update_spectra_dq_by_access" ON spectra;
CREATE POLICY "update_spectra_dq_by_access"
  ON spectra FOR UPDATE TO authenticated
  USING (
    public.can_comment()
    AND target_id IN (
      SELECT t.target_id FROM targets t
      WHERE t.program_slug = ANY(public.accessible_program_slugs())
    )
  )
  WITH CHECK (
    public.can_comment()
    AND target_id IN (
      SELECT t.target_id FROM targets t
      WHERE t.program_slug = ANY(public.accessible_program_slugs())
    )
  );


-- =============================================================================
-- comments
-- =============================================================================

ALTER TABLE comments ENABLE ROW LEVEL SECURITY;

-- Comments visible if their parent target or object is in an accessible program.
DROP POLICY IF EXISTS "select_comments_by_access" ON comments;
CREATE POLICY "select_comments_by_access"
  ON comments FOR SELECT
  USING (
    -- Target-level comments
    (target_id IS NOT NULL AND target_id IN (
      SELECT t.id FROM targets t
      WHERE t.program_slug = ANY(public.accessible_program_slugs())
    ))
    OR
    -- Object-level comments
    (target_id IS NULL AND object_id IS NOT NULL AND object_id IN (
      SELECT o.id FROM objects o
      WHERE o.programs && public.accessible_program_slugs()
    ))
  );

-- Users with can_comment permission can insert comments on accessible targets or objects.
DROP POLICY IF EXISTS "insert_comments_by_access" ON comments;
CREATE POLICY "insert_comments_by_access"
  ON comments FOR INSERT
  WITH CHECK (
    (
      -- Target-level comments
      (target_id IS NOT NULL AND target_id IN (
        SELECT t.id FROM targets t
        WHERE t.program_slug = ANY(public.accessible_program_slugs())
      ))
      OR
      -- Object-level comments
      (target_id IS NULL AND object_id IS NOT NULL AND object_id IN (
        SELECT o.id FROM objects o
        WHERE o.programs && public.accessible_program_slugs()
      ))
    )
    AND public.can_comment()
  );


-- =============================================================================
-- flag_audit_log
-- =============================================================================

ALTER TABLE flag_audit_log ENABLE ROW LEVEL SECURITY;

-- Audit log visible if the parent target/object/spectrum is in an accessible
-- program. Rows now point at exactly one of the three subject columns
-- (enforced by the table check constraint), so we OR across them.
DROP POLICY IF EXISTS "select_audit_by_access" ON flag_audit_log;
CREATE POLICY "select_audit_by_access"
  ON flag_audit_log FOR SELECT
  USING (
    (target_id IS NOT NULL AND target_id IN (
      SELECT t.id FROM targets t
      WHERE t.program_slug = ANY(public.accessible_program_slugs())
    ))
    OR (object_id IS NOT NULL AND object_id IN (
      SELECT o.id FROM objects o
      WHERE o.programs && public.accessible_program_slugs()
    ))
    OR (spectrum_id IS NOT NULL AND spectrum_id IN (
      SELECT s.id FROM spectra s
      JOIN targets t ON t.target_id = s.target_id
      WHERE t.program_slug = ANY(public.accessible_program_slugs())
    ))
  );

-- Authenticated users can insert audit entries when they have access to the
-- subject. New writes set object_id (object inspection) or spectrum_id
-- (per-spectrum DQ); legacy writes targeting target_id are still permitted
-- so the audit history table can hold pre-Phase-D rows.
DROP POLICY IF EXISTS "insert_audit_by_access" ON flag_audit_log;
CREATE POLICY "insert_audit_by_access"
  ON flag_audit_log FOR INSERT TO authenticated
  WITH CHECK (
    (target_id IS NOT NULL AND target_id IN (
      SELECT t.id FROM targets t
      WHERE t.program_slug = ANY(public.accessible_program_slugs())
    ))
    OR (object_id IS NOT NULL AND object_id IN (
      SELECT o.id FROM objects o
      WHERE o.programs && public.accessible_program_slugs()
    ))
    OR (spectrum_id IS NOT NULL AND spectrum_id IN (
      SELECT s.id FROM spectra s
      JOIN targets t ON t.target_id = s.target_id
      WHERE t.program_slug = ANY(public.accessible_program_slugs())
    ))
  );


-- =============================================================================
-- nircam_images
-- =============================================================================

ALTER TABLE nircam_images ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read (reference data, no program association).
DROP POLICY IF EXISTS "authenticated_select_nircam" ON nircam_images;
CREATE POLICY "authenticated_select_nircam"
  ON nircam_images FOR SELECT TO authenticated
  USING (true);


-- =============================================================================
-- flag_definitions
-- =============================================================================

ALTER TABLE flag_definitions ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read (reference data for flag display).
DROP POLICY IF EXISTS "authenticated_select_flags" ON flag_definitions;
CREATE POLICY "authenticated_select_flags"
  ON flag_definitions FOR SELECT TO authenticated
  USING (true);


-- =============================================================================
-- map_layers
-- =============================================================================

ALTER TABLE map_layers ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read map layers.
DROP POLICY IF EXISTS "Authenticated users can read map layers" ON map_layers;
CREATE POLICY "Authenticated users can read map layers"
  ON map_layers FOR SELECT TO authenticated
  USING (true);

-- Admins have full access to map layers (deploy CLI: tile registration).
DROP POLICY IF EXISTS "admin_map_layers_all" ON map_layers;
CREATE POLICY "admin_map_layers_all"
  ON map_layers FOR ALL TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());

-- Service role has full access to map layers (backward compat).
DROP POLICY IF EXISTS "Service role has full access to map layers" ON map_layers;
CREATE POLICY "Service role has full access to map layers"
  ON map_layers FOR ALL TO service_role
  USING (true)
  WITH CHECK (true);


-- =============================================================================
-- slit_regions
-- =============================================================================

ALTER TABLE slit_regions ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read slit regions.
DROP POLICY IF EXISTS "Authenticated users can view slit regions" ON slit_regions;
CREATE POLICY "Authenticated users can view slit regions"
  ON slit_regions FOR SELECT TO authenticated
  USING (true);

-- Admins can insert slit regions (deploy CLI).
DROP POLICY IF EXISTS "admin_slit_regions_insert" ON slit_regions;
CREATE POLICY "admin_slit_regions_insert"
  ON slit_regions FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

-- Admins can delete slit regions (deploy CLI: delete-then-insert pattern).
DROP POLICY IF EXISTS "admin_slit_regions_delete" ON slit_regions;
CREATE POLICY "admin_slit_regions_delete"
  ON slit_regions FOR DELETE TO authenticated
  USING (public.is_admin());


-- =============================================================================
-- shutters
-- =============================================================================

ALTER TABLE shutters ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read shutters.
DROP POLICY IF EXISTS "Authenticated users can view shutters" ON shutters;
CREATE POLICY "Authenticated users can view shutters"
  ON shutters FOR SELECT TO authenticated
  USING (true);

-- Admins can insert shutters (deploy CLI).
DROP POLICY IF EXISTS "admin_shutters_insert" ON shutters;
CREATE POLICY "admin_shutters_insert"
  ON shutters FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

-- Admins can delete shutters (deploy CLI: delete-then-insert pattern).
DROP POLICY IF EXISTS "admin_shutters_delete" ON shutters;
CREATE POLICY "admin_shutters_delete"
  ON shutters FOR DELETE TO authenticated
  USING (public.is_admin());


-- =============================================================================
-- deployments
-- =============================================================================

ALTER TABLE deployments ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read the deployment log (transparency).
DROP POLICY IF EXISTS "authenticated_select_deployments" ON deployments;
CREATE POLICY "authenticated_select_deployments"
  ON deployments FOR SELECT TO authenticated
  USING (true);

-- Admins can insert deployment log entries (deploy CLI).
DROP POLICY IF EXISTS "admin_deployments_insert" ON deployments;
CREATE POLICY "admin_deployments_insert"
  ON deployments FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());


-- =============================================================================
-- pending_invites
-- =============================================================================

ALTER TABLE pending_invites ENABLE ROW LEVEL SECURITY;

-- Admins can view invites.
DROP POLICY IF EXISTS "admin_select_invites" ON pending_invites;
CREATE POLICY "admin_select_invites"
  ON pending_invites FOR SELECT TO authenticated
  USING (public.is_admin());

-- Users can read own invite by email.
DROP POLICY IF EXISTS "Users can read own invite by email" ON pending_invites;
CREATE POLICY "Users can read own invite by email"
  ON pending_invites FOR SELECT TO authenticated
  USING (email = (SELECT users.email FROM auth.users WHERE users.id = auth.uid())::text);

-- Admins can create invites.
DROP POLICY IF EXISTS "admin_insert_invites" ON pending_invites;
CREATE POLICY "admin_insert_invites"
  ON pending_invites FOR INSERT TO authenticated
  WITH CHECK (public.is_admin());

-- Admins can update invites.
DROP POLICY IF EXISTS "admin_update_invites" ON pending_invites;
CREATE POLICY "admin_update_invites"
  ON pending_invites FOR UPDATE TO authenticated
  USING (public.is_admin());

-- Admins can delete invites.
DROP POLICY IF EXISTS "admin_delete_invites" ON pending_invites;
CREATE POLICY "admin_delete_invites"
  ON pending_invites FOR DELETE TO authenticated
  USING (public.is_admin());


-- =============================================================================
-- access_codes
-- =============================================================================

ALTER TABLE access_codes ENABLE ROW LEVEL SECURITY;

-- Admins can manage all access codes (all operations).
DROP POLICY IF EXISTS "admin_manage_codes" ON access_codes;
CREATE POLICY "admin_manage_codes"
  ON access_codes
  USING (public.is_admin());

-- Anyone can read active codes (for code redemption flow).
DROP POLICY IF EXISTS "Anyone can read active codes" ON access_codes;
CREATE POLICY "Anyone can read active codes"
  ON access_codes FOR SELECT
  USING (is_active = true);


-- =============================================================================
-- code_redemptions
-- =============================================================================

ALTER TABLE code_redemptions ENABLE ROW LEVEL SECURITY;

-- Admins can see all redemptions.
DROP POLICY IF EXISTS "admin_select_redemptions" ON code_redemptions;
CREATE POLICY "admin_select_redemptions"
  ON code_redemptions FOR SELECT
  USING (public.is_admin());

-- Users can see own redemptions.
DROP POLICY IF EXISTS "Users can see own redemptions" ON code_redemptions;
CREATE POLICY "Users can see own redemptions"
  ON code_redemptions FOR SELECT
  USING (user_id = auth.uid());

-- Users can redeem codes.
DROP POLICY IF EXISTS "Users can redeem codes" ON code_redemptions;
CREATE POLICY "Users can redeem codes"
  ON code_redemptions FOR INSERT
  WITH CHECK (user_id = auth.uid());


-- =============================================================================
-- account_requests
-- =============================================================================

ALTER TABLE account_requests ENABLE ROW LEVEL SECURITY;

-- Admins can view requests.
DROP POLICY IF EXISTS "admin_select_requests" ON account_requests;
CREATE POLICY "admin_select_requests"
  ON account_requests FOR SELECT TO authenticated
  USING (public.is_admin());

-- Admins can update requests.
DROP POLICY IF EXISTS "admin_update_requests" ON account_requests;
CREATE POLICY "admin_update_requests"
  ON account_requests FOR UPDATE TO authenticated
  USING (public.is_admin());

-- Anyone can submit requests (including anonymous users).
DROP POLICY IF EXISTS "Anyone can submit requests" ON account_requests;
CREATE POLICY "Anyone can submit requests"
  ON account_requests FOR INSERT TO authenticated, anon
  WITH CHECK (true);

-- Anyone can check own request status.
DROP POLICY IF EXISTS "Users can check own request status" ON account_requests;
CREATE POLICY "Users can check own request status"
  ON account_requests FOR SELECT TO authenticated, anon
  USING (true);


-- =============================================================================
-- download_log
-- =============================================================================

ALTER TABLE download_log ENABLE ROW LEVEL SECURITY;

-- Admins can view all downloads.
DROP POLICY IF EXISTS "admin_select_downloads" ON download_log;
CREATE POLICY "admin_select_downloads"
  ON download_log FOR SELECT TO authenticated
  USING (public.is_admin());

-- Users can view own downloads.
DROP POLICY IF EXISTS "Users can view own downloads" ON download_log;
CREATE POLICY "Users can view own downloads"
  ON download_log FOR SELECT TO authenticated
  USING (auth.uid() = user_id);


-- =============================================================================
-- password_reset_log
-- =============================================================================

ALTER TABLE password_reset_log ENABLE ROW LEVEL SECURITY;

-- Admins can view all reset logs.
DROP POLICY IF EXISTS "admin_select_reset_logs" ON password_reset_log;
CREATE POLICY "admin_select_reset_logs"
  ON password_reset_log FOR SELECT
  USING (public.is_admin());

-- Users can view own reset logs.
DROP POLICY IF EXISTS "Users can view own reset logs" ON password_reset_log;
CREATE POLICY "Users can view own reset logs"
  ON password_reset_log FOR SELECT
  USING (user_id = auth.uid());


-- =============================================================================
-- device_codes
-- =============================================================================

ALTER TABLE device_codes ENABLE ROW LEVEL SECURITY;

-- Service role has full access (device auth flow managed server-side).
DROP POLICY IF EXISTS "Service role full access" ON device_codes;
CREATE POLICY "Service role full access"
  ON device_codes TO service_role
  USING (true)
  WITH CHECK (true);


-- =============================================================================
-- refresh_tokens
-- =============================================================================

ALTER TABLE refresh_tokens ENABLE ROW LEVEL SECURITY;

-- Service role has full access (token management server-side).
DROP POLICY IF EXISTS "Service role full access" ON refresh_tokens;
CREATE POLICY "Service role full access"
  ON refresh_tokens TO service_role
  USING (true)
  WITH CHECK (true);

-- Users can view own tokens.
DROP POLICY IF EXISTS "Users can view own tokens" ON refresh_tokens;
CREATE POLICY "Users can view own tokens"
  ON refresh_tokens FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

-- Users can update own tokens.
DROP POLICY IF EXISTS "Users can update own tokens" ON refresh_tokens;
CREATE POLICY "Users can update own tokens"
  ON refresh_tokens FOR UPDATE TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);


-- =============================================================================
-- api_keys
-- =============================================================================

ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;

-- Users can view own API keys.
DROP POLICY IF EXISTS "Users can view own API keys" ON api_keys;
CREATE POLICY "Users can view own API keys"
  ON api_keys FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

-- Users can create own API keys.
DROP POLICY IF EXISTS "Users can create own API keys" ON api_keys;
CREATE POLICY "Users can create own API keys"
  ON api_keys FOR INSERT TO authenticated
  WITH CHECK (auth.uid() = user_id);

-- Users can update own API keys.
DROP POLICY IF EXISTS "Users can update own API keys" ON api_keys;
CREATE POLICY "Users can update own API keys"
  ON api_keys FOR UPDATE TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- Users can delete own API keys.
DROP POLICY IF EXISTS "Users can delete own API keys" ON api_keys;
CREATE POLICY "Users can delete own API keys"
  ON api_keys FOR DELETE TO authenticated
  USING (auth.uid() = user_id);
