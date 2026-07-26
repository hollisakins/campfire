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
--
-- EXCEPT link accounts (docs/design-public-mirror.md §5.4), which see only
-- themselves. This is the sharpest edge in the whole share-link feature: a link
-- account is a real authenticated principal, so without this conjunct handing
-- someone a share link would also hand them the name and username of every
-- CAMPFIRE user. It reads its own row so AuthContext can resolve
-- is_link_account and strip the nav.
DROP POLICY IF EXISTS "authenticated_select_profiles" ON user_profiles;
CREATE POLICY "authenticated_select_profiles"
  ON user_profiles FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    OR user_id = (SELECT auth.uid())
  );

-- Users can update their own profile (name, preferences).
DROP POLICY IF EXISTS "self_update_profile" ON user_profiles;
CREATE POLICY "self_update_profile"
  ON user_profiles FOR UPDATE TO authenticated
  USING (user_id = (SELECT auth.uid()))
  WITH CHECK (user_id = (SELECT auth.uid()));

-- Admins can update any profile (is_admin, can_comment toggles).
DROP POLICY IF EXISTS "admin_update_profile" ON user_profiles;
CREATE POLICY "admin_update_profile"
  ON user_profiles FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can delete profiles (user management).
DROP POLICY IF EXISTS "admin_delete_profile" ON user_profiles;
CREATE POLICY "admin_delete_profile"
  ON user_profiles FOR DELETE TO authenticated
  USING ((SELECT public.is_admin()));

-- Admins can insert profiles (manual user creation).
DROP POLICY IF EXISTS "admin_insert_profile" ON user_profiles;
CREATE POLICY "admin_insert_profile"
  ON user_profiles FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));


-- =============================================================================
-- user_program_access
-- =============================================================================

ALTER TABLE user_program_access ENABLE ROW LEVEL SECURITY;

-- Users can see their own access grants.
DROP POLICY IF EXISTS "self_select_access" ON user_program_access;
CREATE POLICY "self_select_access"
  ON user_program_access FOR SELECT TO authenticated
  USING (user_id = (SELECT auth.uid()));

-- Admins can see all access grants (user management panel).
DROP POLICY IF EXISTS "admin_select_access" ON user_program_access;
CREATE POLICY "admin_select_access"
  ON user_program_access FOR SELECT TO authenticated
  USING ((SELECT public.is_admin()));

-- Admins can grant program access.
DROP POLICY IF EXISTS "admin_insert_access" ON user_program_access;
CREATE POLICY "admin_insert_access"
  ON user_program_access FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can revoke program access.
DROP POLICY IF EXISTS "admin_delete_access" ON user_program_access;
CREATE POLICY "admin_delete_access"
  ON user_program_access FOR DELETE TO authenticated
  USING ((SELECT public.is_admin()));


-- =============================================================================
-- programs
-- =============================================================================

ALTER TABLE programs ENABLE ROW LEVEL SECURITY;

-- Public programs visible to all authenticated users.
-- Private programs visible only to users with explicit access.
--
-- Link accounts (docs/design-public-mirror.md §5.4) see only the program their
-- scope belongs to -- accessible_program_slugs() already excludes the is_public
-- union for them, and this policy has to say the same thing directly since it
-- predates that helper and tests is_public inline.
DROP POLICY IF EXISTS "accessible_programs_select" ON programs;
CREATE POLICY "accessible_programs_select"
  ON programs FOR SELECT TO authenticated
  USING (
    CASE WHEN (SELECT public.is_link_account()) THEN
      slug = ANY((SELECT public.accessible_program_slugs())::text[])
    ELSE
      is_public = true
      OR slug IN (SELECT program_slug FROM user_program_access WHERE user_id = (SELECT auth.uid()))
    END
  );

-- Admins can see all programs (including private ones without access).
DROP POLICY IF EXISTS "admin_programs_select" ON programs;
CREATE POLICY "admin_programs_select"
  ON programs FOR SELECT TO authenticated
  USING ((SELECT public.is_admin()));

-- Admins can insert programs (deploy CLI: sync-programs).
DROP POLICY IF EXISTS "admin_programs_insert" ON programs;
CREATE POLICY "admin_programs_insert"
  ON programs FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can update programs (toggle is_public, edit metadata).
DROP POLICY IF EXISTS "admin_programs_update" ON programs;
CREATE POLICY "admin_programs_update"
  ON programs FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));


-- =============================================================================
-- fields  (issue #303 — NIRCam field registry)
-- =============================================================================

ALTER TABLE fields ENABLE ROW LEVEL SECURITY;

-- A field is visible once it has at least one published NIRCam mosaic — mirrors
-- the deploy_status gate on nircam_images so a field's config (name, filters,
-- tangent point) is not exposed while its data is still draft. Admins see all.
--
-- Link accounts (docs/design-public-mirror.md §5.3) see only their own field's
-- config row, and only when the link may see some mosaic of it -- an
-- include_drafts link can see a field whose data is still entirely in prep,
-- which is exactly the "shared but not published" case.
DROP POLICY IF EXISTS "accessible_fields_select" ON fields;
CREATE POLICY "accessible_fields_select"
  ON fields FOR SELECT TO authenticated
  USING (
    CASE WHEN (SELECT public.is_link_account()) THEN
      name = (SELECT public.link_field())
      AND EXISTS (
        SELECT 1 FROM nircam_images ni
        WHERE ni.field = fields.name
          AND (ni.deploy_status = 'published' OR (SELECT public.link_sees_drafts()))
      )
    ELSE
      EXISTS (
        SELECT 1 FROM nircam_images ni
        WHERE ni.field = fields.name AND ni.deploy_status = 'published'
      )
      OR (SELECT public.is_admin())
    END
  );

-- Admins can insert/update fields (deploy CLI: sync-fields + nircam deploy).
DROP POLICY IF EXISTS "admin_fields_insert" ON fields;
CREATE POLICY "admin_fields_insert"
  ON fields FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

DROP POLICY IF EXISTS "admin_fields_update" ON fields;
CREATE POLICY "admin_fields_update"
  ON fields FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));


-- =============================================================================
-- observations
-- =============================================================================

ALTER TABLE observations ENABLE ROW LEVEL SECURITY;

-- Observations visible if the parent program is accessible.
-- Share links (docs/design-public-mirror.md §5.2): a link sees only the one
-- observation it was minted for, not its program's siblings.
DROP POLICY IF EXISTS "accessible_observations_select" ON observations;
CREATE POLICY "accessible_observations_select"
  ON observations FOR SELECT TO authenticated
  USING (
    program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
    AND ((SELECT NOT public.is_link_account())
         OR name = (SELECT public.link_observation()))
  );

-- Admins can insert observations (deploy CLI).
DROP POLICY IF EXISTS "admin_observations_insert" ON observations;
CREATE POLICY "admin_observations_insert"
  ON observations FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can update observations (deploy CLI: last_deployed_at, counts).
DROP POLICY IF EXISTS "admin_observations_update" ON observations;
CREATE POLICY "admin_observations_update"
  ON observations FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));


-- =============================================================================
-- targets (renamed from objects)
-- =============================================================================

ALTER TABLE targets ENABLE ROW LEVEL SECURITY;

-- Targets visible if their program is accessible.
DROP POLICY IF EXISTS "select_targets_by_access" ON targets;
CREATE POLICY "select_targets_by_access"
  ON targets FOR SELECT
  USING (
    program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
    -- B1 (#217): hide targets whose only spectra are unpublished. Covers the
    -- target-derived readers that never join spectra (map markers, sed-plot,
    -- /api/targets/[id], tile-thumbnail). Admins see all.
    AND (has_published_spectrum OR (SELECT public.is_admin())
         OR (SELECT public.link_sees_drafts()))
    -- Share links (docs/design-public-mirror.md §5.2): narrow from the whole
    -- program to the one shared observation. accessible_program_slugs() already
    -- pinned a link to its scope's program, but a program can hold many
    -- observations and only one of them was shared.
    AND ((SELECT NOT public.is_link_account())
         OR observation = (SELECT public.link_observation()))
  );

-- Users with can_inspect permission can update targets in accessible programs.
DROP POLICY IF EXISTS "update_targets_by_access" ON targets;
CREATE POLICY "update_targets_by_access"
  ON targets FOR UPDATE
  USING (
    program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
    AND (SELECT public.can_inspect())
    -- B1 (#217): a non-admin inspector cannot mutate a target with no published
    -- spectrum (defense-in-depth; can't see it anyway via select policy).
    AND (has_published_spectrum OR (SELECT public.is_admin()))
  );

-- Admins can insert targets (deploy CLI).
DROP POLICY IF EXISTS "admin_targets_insert" ON targets;
CREATE POLICY "admin_targets_insert"
  ON targets FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can update all target fields (deploy CLI: pipeline fields, redshift drift reset).
DROP POLICY IF EXISTS "admin_targets_update" ON targets;
CREATE POLICY "admin_targets_update"
  ON targets FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can delete targets (deploy CLI: remove/un-deploy observation).
DROP POLICY IF EXISTS "admin_targets_delete" ON targets;
CREATE POLICY "admin_targets_delete"
  ON targets FOR DELETE TO authenticated
  USING ((SELECT public.is_admin()));


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
    programs && (SELECT public.accessible_program_slugs())
    -- B1 (#217): hide objects whose only member spectra are unpublished. Admins
    -- see all. has_published_spectrum is recomputed by reconcile from members.
    AND (has_published_spectrum OR (SELECT public.is_admin())
         OR (SELECT public.link_sees_drafts()))
    -- Share links (docs/design-public-mirror.md §5.2). Array-overlap form: an
    -- object is a cross-observation merge, so it is in scope when the shared
    -- observation is ANY of its members. The object's OTHER members stay hidden
    -- at the target/spectrum level, so a link sees the object as a stub carrying
    -- only its own observation's spectra -- which is the intended behaviour, not
    -- an accident: the colleague sees their reduction of a source, not everyone
    -- else's.
    AND ((SELECT NOT public.is_link_account())
         OR observations && ARRAY[(SELECT public.link_observation())]::text[])
  );

-- Admins can insert objects (deploy CLI: objects rebuild).
DROP POLICY IF EXISTS "admin_objects_insert" ON objects;
CREATE POLICY "admin_objects_insert"
  ON objects FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can update objects (deploy CLI: objects rebuild).
DROP POLICY IF EXISTS "admin_objects_update" ON objects;
CREATE POLICY "admin_objects_update"
  ON objects FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));

-- Phase A: users with can_inspect permission can update objects whose
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
    programs && (SELECT public.accessible_program_slugs())
    AND (SELECT public.can_inspect())
    -- B1 (#217): non-admin inspectors cannot mutate an object with no published
    -- spectrum (defense-in-depth; not visible via select policy either).
    AND (has_published_spectrum OR (SELECT public.is_admin()))
  )
  WITH CHECK (
    programs && (SELECT public.accessible_program_slugs())
    AND (SELECT public.can_inspect())
    AND (has_published_spectrum OR (SELECT public.is_admin()))
  );

-- Admins can delete objects (deploy CLI: objects rebuild wipes before re-insert).
DROP POLICY IF EXISTS "admin_objects_delete" ON objects;
CREATE POLICY "admin_objects_delete"
  ON objects FOR DELETE TO authenticated
  USING ((SELECT public.is_admin()));


-- =============================================================================
-- object_photometry
-- =============================================================================

ALTER TABLE object_photometry ENABLE ROW LEVEL SECURITY;

-- Photometry visible if the linked object is accessible.
-- The object subquery mirrors select_objects_by_access, including its share-link
-- conjuncts (docs/design-public-mirror.md §5.2). Re-derived inline rather than
-- inherited, matching how this policy already re-derives the program check.
DROP POLICY IF EXISTS "select_object_photometry_by_access" ON object_photometry;
CREATE POLICY "select_object_photometry_by_access"
  ON object_photometry FOR SELECT
  USING (
    object_id IN (
      SELECT o.id FROM objects o
      WHERE o.programs && (SELECT public.accessible_program_slugs())
        -- B1 (#217): no photometry for objects with no published spectrum.
        AND (o.has_published_spectrum OR (SELECT public.is_admin())
             OR (SELECT public.link_sees_drafts()))
        AND ((SELECT NOT public.is_link_account())
             OR o.observations && ARRAY[(SELECT public.link_observation())]::text[])
    )
  );

-- Admins can insert photometry (deploy CLI).
DROP POLICY IF EXISTS "admin_object_photometry_insert" ON object_photometry;
CREATE POLICY "admin_object_photometry_insert"
  ON object_photometry FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can update photometry (deploy CLI).
DROP POLICY IF EXISTS "admin_object_photometry_update" ON object_photometry;
CREATE POLICY "admin_object_photometry_update"
  ON object_photometry FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can delete photometry (deploy CLI).
DROP POLICY IF EXISTS "admin_object_photometry_delete" ON object_photometry;
CREATE POLICY "admin_object_photometry_delete"
  ON object_photometry FOR DELETE TO authenticated
  USING ((SELECT public.is_admin()));


-- =============================================================================
-- object_lists
-- =============================================================================

ALTER TABLE object_lists ENABLE ROW LEVEL SECURITY;

-- Users can see: their own lists + public lists + public_edit lists.
--
-- Link accounts (docs/design-public-mirror.md §5.4) see no lists at all. A list
-- is a curation artifact of the CAMPFIRE community, not part of a data scope --
-- its name and description would leak collaborators' working notes to an
-- outside viewer. Members are separately gated (select_list_members).
DROP POLICY IF EXISTS "select_lists" ON object_lists;
CREATE POLICY "select_lists"
  ON object_lists FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    AND (
      created_by = (SELECT auth.uid())
      OR visibility IN ('public_read', 'public_edit')
    )
  );

-- Users can create lists (owned by them, non-system, non-group-account).
DROP POLICY IF EXISTS "insert_lists" ON object_lists;
CREATE POLICY "insert_lists"
  ON object_lists FOR INSERT TO authenticated
  WITH CHECK (
    created_by = (SELECT auth.uid())
    AND is_system = false
    AND (SELECT public.can_comment())
    AND NOT (SELECT public.is_group_account())
  );

-- Owners can update their own lists (but not system lists).
DROP POLICY IF EXISTS "update_own_lists" ON object_lists;
CREATE POLICY "update_own_lists"
  ON object_lists FOR UPDATE TO authenticated
  USING (created_by = (SELECT auth.uid()) AND is_system = false)
  WITH CHECK (created_by = (SELECT auth.uid()) AND is_system = false);

-- Owners can delete their own lists (but not system lists).
DROP POLICY IF EXISTS "delete_own_lists" ON object_lists;
CREATE POLICY "delete_own_lists"
  ON object_lists FOR DELETE TO authenticated
  USING (created_by = (SELECT auth.uid()) AND is_system = false);

-- Admins can manage all lists including system lists.
DROP POLICY IF EXISTS "admin_manage_lists" ON object_lists;
CREATE POLICY "admin_manage_lists"
  ON object_lists
  USING ((SELECT public.is_admin()));


-- =============================================================================
-- object_list_members
-- =============================================================================

ALTER TABLE object_list_members ENABLE ROW LEVEL SECURITY;

-- Members visible if:
--   1. The list is visible to the user, AND
--   2. The matched object (if any) has at least one accessible program
-- Members with NULL object_id (orphaned) are visible to the list owner
-- OR to anyone if the list is public_edit (so co-editors can see orphans).
-- Link accounts (docs/design-public-mirror.md §5.4) see no list members: they
-- see no lists at all (select_lists), and membership would leak which sources
-- CAMPFIRE users have curated together.
DROP POLICY IF EXISTS "select_list_members" ON object_list_members;
CREATE POLICY "select_list_members"
  ON object_list_members FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    AND list_id IN (
      SELECT id FROM object_lists
      WHERE created_by = (SELECT auth.uid())
         OR visibility IN ('public_read', 'public_edit')
    )
    AND (
      (object_id IS NULL AND list_id IN (
        SELECT id FROM object_lists
        WHERE created_by = (SELECT auth.uid()) OR visibility = 'public_edit'
      ))
      OR object_id IN (
        SELECT o.id FROM objects o
        WHERE o.programs && (SELECT public.accessible_program_slugs())
          -- B1 (#217): list members matched to an unpublished-only object are
          -- hidden from non-admins (the object itself is invisible).
          AND (o.has_published_spectrum OR (SELECT public.is_admin()))
      )
    )
  );

-- can_comment users can add members to own lists + public_edit lists.
DROP POLICY IF EXISTS "insert_list_members" ON object_list_members;
CREATE POLICY "insert_list_members"
  ON object_list_members FOR INSERT TO authenticated
  WITH CHECK (
    (SELECT public.can_comment())
    AND list_id IN (
      SELECT id FROM object_lists
      WHERE created_by = (SELECT auth.uid())
         OR visibility = 'public_edit'
    )
  );

-- can_comment users can update members in own lists + public_edit lists
-- (needed for upsert ON CONFLICT DO UPDATE when re-linking coordinate entries).
DROP POLICY IF EXISTS "update_list_members" ON object_list_members;
CREATE POLICY "update_list_members"
  ON object_list_members FOR UPDATE TO authenticated
  USING (
    (SELECT public.can_comment())
    AND list_id IN (
      SELECT id FROM object_lists
      WHERE created_by = (SELECT auth.uid())
         OR visibility = 'public_edit'
    )
  )
  WITH CHECK (
    (SELECT public.can_comment())
    AND list_id IN (
      SELECT id FROM object_lists
      WHERE created_by = (SELECT auth.uid())
         OR visibility = 'public_edit'
    )
  );

-- can_comment users can remove members from own lists + public_edit lists.
DROP POLICY IF EXISTS "delete_list_members" ON object_list_members;
CREATE POLICY "delete_list_members"
  ON object_list_members FOR DELETE TO authenticated
  USING (
    (SELECT public.can_comment())
    AND list_id IN (
      SELECT id FROM object_lists
      WHERE created_by = (SELECT auth.uid())
         OR visibility = 'public_edit'
    )
  );

-- Admins can manage all list members.
DROP POLICY IF EXISTS "admin_manage_list_members" ON object_list_members;
CREATE POLICY "admin_manage_list_members"
  ON object_list_members
  USING ((SELECT public.is_admin()));


-- =============================================================================
-- list_audit_log
-- =============================================================================

ALTER TABLE list_audit_log ENABLE ROW LEVEL SECURITY;

-- Audit log visible if the parent list is visible. Link accounts see no lists
-- (select_lists), so the same conjunct keeps their edit history hidden too --
-- restated here rather than inherited, matching how every other policy in this
-- file re-derives its access check inline.
DROP POLICY IF EXISTS "select_list_audit" ON list_audit_log;
CREATE POLICY "select_list_audit"
  ON list_audit_log FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    AND list_id IN (
      SELECT id FROM object_lists
      WHERE created_by = (SELECT auth.uid())
         OR visibility IN ('public_read', 'public_edit')
    )
  );

-- Admins can see all list audit entries.
DROP POLICY IF EXISTS "admin_select_list_audit" ON list_audit_log;
CREATE POLICY "admin_select_list_audit"
  ON list_audit_log FOR SELECT TO authenticated
  USING ((SELECT public.is_admin()));


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
      WHERE t.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
    )
    -- B1 (#217): PRIMARY per-row gate. Only 'published' spectra reach non-admins;
    -- 'draft' and 'revoked' are hidden. This is the sole gate for the user-client
    -- web routes that read spectra directly and never call an RPC (/api/spectrum,
    -- /api/download, /api/redshift-fit, /api/spectrum-thumbnail). Admins see all.
    --
    -- ...and, since share links (docs/design-public-mirror.md §6), an
    -- include_drafts link account -- but only for rows already inside its scope,
    -- which the targets subquery above enforces. 'revoked' stays hidden from
    -- links too: link_sees_drafts() relaxes the gate to draft, never past it.
    AND (deploy_status = 'published' OR (SELECT public.is_admin())
         OR (deploy_status = 'draft' AND (SELECT public.link_sees_drafts())))
  );

-- Admins can insert spectra (deploy CLI).
DROP POLICY IF EXISTS "admin_spectra_insert" ON spectra;
CREATE POLICY "admin_spectra_insert"
  ON spectra FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can update spectra (deploy CLI: thumbnails, provenance).
DROP POLICY IF EXISTS "admin_spectra_update" ON spectra;
CREATE POLICY "admin_spectra_update"
  ON spectra FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can delete spectra (deploy CLI: remove/un-deploy observation).
DROP POLICY IF EXISTS "admin_spectra_delete" ON spectra;
CREATE POLICY "admin_spectra_delete"
  ON spectra FOR DELETE TO authenticated
  USING ((SELECT public.is_admin()));

-- Users with can_inspect may update spectra whose parent target is in an
-- accessible program. Column scope is restricted to dq_flags (and the
-- trigger-maintained updated_at) by enforce_spectra_dq_user_update_scope
-- in triggers.sql — Postgres RLS does not support per-column UPDATE
-- policies. Mirrors update_objects_by_access.
DROP POLICY IF EXISTS "update_spectra_dq_by_access" ON spectra;
CREATE POLICY "update_spectra_dq_by_access"
  ON spectra FOR UPDATE TO authenticated
  USING (
    (SELECT public.can_inspect())
    AND target_id IN (
      SELECT t.target_id FROM targets t
      WHERE t.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
    )
    -- B1 (#217): a non-admin inspector cannot set DQ flags on an unpublished
    -- spectrum (can't see it via the select policy either).
    AND (deploy_status = 'published' OR (SELECT public.is_admin()))
  )
  WITH CHECK (
    (SELECT public.can_inspect())
    AND target_id IN (
      SELECT t.target_id FROM targets t
      WHERE t.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
    )
    AND (deploy_status = 'published' OR (SELECT public.is_admin()))
  );


-- =============================================================================
-- comments
-- =============================================================================

ALTER TABLE comments ENABLE ROW LEVEL SECURITY;

-- Comments visible if their parent target or object is in an accessible program.
-- Link accounts (docs/design-public-mirror.md §5.4) see no comments at all.
-- Unlike photometry, a comment is not part of the data -- it is CAMPFIRE users
-- talking to each other about a source, often candidly and often about sources
-- the link holder can legitimately see. Scoping it to the shared observation
-- would still expose that discussion, so deny outright rather than narrow.
DROP POLICY IF EXISTS "select_comments_by_access" ON comments;
CREATE POLICY "select_comments_by_access"
  ON comments FOR SELECT
  USING (
    (SELECT NOT public.is_link_account())
    AND (
      -- Target-level comments
      (target_id IS NOT NULL AND target_id IN (
        SELECT t.id FROM targets t
        WHERE t.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
          AND (t.has_published_spectrum OR (SELECT public.is_admin()))  -- B1 (#217)
      ))
      OR
      -- Object-level comments
      (target_id IS NULL AND object_id IS NOT NULL AND object_id IN (
        SELECT o.id FROM objects o
        WHERE o.programs && (SELECT public.accessible_program_slugs())
          AND (o.has_published_spectrum OR (SELECT public.is_admin()))  -- B1 (#217)
      ))
    )
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
        WHERE t.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
      ))
      OR
      -- Object-level comments
      (target_id IS NULL AND object_id IS NOT NULL AND object_id IN (
        SELECT o.id FROM objects o
        WHERE o.programs && (SELECT public.accessible_program_slugs())
      ))
    )
    AND (SELECT public.can_comment())
  );


-- =============================================================================
-- flag_audit_log
-- =============================================================================

ALTER TABLE flag_audit_log ENABLE ROW LEVEL SECURITY;

-- Audit log visible if the parent target/object/spectrum is in an accessible
-- program. Rows now point at exactly one of the three subject columns
-- (enforced by the table check constraint), so we OR across them.
-- Link accounts (docs/design-public-mirror.md §5.4) see no audit history, for
-- the same reason they see no comments: it is a record of CAMPFIRE users'
-- inspection decisions (who changed what quality flag, when), not data about
-- the scope.
DROP POLICY IF EXISTS "select_audit_by_access" ON flag_audit_log;
CREATE POLICY "select_audit_by_access"
  ON flag_audit_log FOR SELECT
  USING (
    (SELECT NOT public.is_link_account())
    AND (
      (target_id IS NOT NULL AND target_id IN (
        SELECT t.id FROM targets t
        WHERE t.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
          AND (t.has_published_spectrum OR (SELECT public.is_admin()))  -- B1 (#217)
      ))
      OR (object_id IS NOT NULL AND object_id IN (
        SELECT o.id FROM objects o
        WHERE o.programs && (SELECT public.accessible_program_slugs())
          AND (o.has_published_spectrum OR (SELECT public.is_admin()))  -- B1 (#217)
      ))
      OR (spectrum_id IS NOT NULL AND spectrum_id IN (
        SELECT s.id FROM spectra s
        JOIN targets t ON t.target_id = s.target_id
        WHERE t.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
          AND (s.deploy_status = 'published' OR (SELECT public.is_admin()))  -- B1 (#217)
      ))
    )
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
      WHERE t.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
        AND (t.has_published_spectrum OR (SELECT public.is_admin()))  -- B1 (#217)
    ))
    OR (object_id IS NOT NULL AND object_id IN (
      SELECT o.id FROM objects o
      WHERE o.programs && (SELECT public.accessible_program_slugs())
        AND (o.has_published_spectrum OR (SELECT public.is_admin()))  -- B1 (#217)
    ))
    OR (spectrum_id IS NOT NULL AND spectrum_id IN (
      SELECT s.id FROM spectra s
      JOIN targets t ON t.target_id = s.target_id
      WHERE t.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
        AND (s.deploy_status = 'published' OR (SELECT public.is_admin()))  -- B1 (#217)
    ))
  );


-- =============================================================================
-- nircam_images
-- =============================================================================

ALTER TABLE nircam_images ENABLE ROW LEVEL SECURITY;

-- Published mosaics are public science (a field spans multiple programs, so there
-- is no per-program scope — published => visible to everyone); draft/revoked are
-- admin-only (epic #261, N2). Mirrors the spectra deploy_status gate.
-- Link accounts (docs/design-public-mirror.md §5.3) see only their own field's
-- mosaics, plus that field's drafts when the link opted in. NIRCam carries no
-- program gating at all -- a field spans programs, so accessible_program_slugs()
-- has nothing to say here -- which makes the field name the ONLY thing standing
-- between a link and every published mosaic in the archive.
DROP POLICY IF EXISTS "authenticated_select_nircam" ON nircam_images;
CREATE POLICY "authenticated_select_nircam"
  ON nircam_images FOR SELECT TO authenticated
  USING (
    CASE WHEN (SELECT public.is_link_account()) THEN
      field = (SELECT public.link_field())
      AND (deploy_status = 'published' OR (SELECT public.link_sees_drafts()))
    ELSE
      deploy_status = 'published' OR (SELECT public.is_admin())
    END
  );

-- Admins write the mosaic index. The NIRCam mosaic deploy (`campfire deploy
-- --field`) runs in login mode through RLS and upserts these rows
-- (_upsert_nircam_images = INSERT ... ON CONFLICT DO UPDATE), so BOTH an INSERT
-- and an UPDATE policy are required. Mirrors nircam_exposures / fields. Publish
-- and revoke flip deploy_status via the SECURITY DEFINER set_deployment_status
-- RPC, which bypasses RLS and so is unaffected.
DROP POLICY IF EXISTS "admin_insert_nircam" ON nircam_images;
CREATE POLICY "admin_insert_nircam"
  ON nircam_images FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

DROP POLICY IF EXISTS "admin_update_nircam" ON nircam_images;
CREATE POLICY "admin_update_nircam"
  ON nircam_images FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));


-- =============================================================================
-- nircam_exposures (admin-only)
-- =============================================================================

ALTER TABLE nircam_exposures ENABLE ROW LEVEL SECURITY;

-- Admins can read all exposures.
DROP POLICY IF EXISTS "admin_select_exposures" ON nircam_exposures;
CREATE POLICY "admin_select_exposures"
  ON nircam_exposures FOR SELECT TO authenticated
  USING ((SELECT public.is_admin()));

-- Admins can insert exposures.
DROP POLICY IF EXISTS "admin_insert_exposures" ON nircam_exposures;
CREATE POLICY "admin_insert_exposures"
  ON nircam_exposures FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can update exposures.
DROP POLICY IF EXISTS "admin_update_exposures" ON nircam_exposures;
CREATE POLICY "admin_update_exposures"
  ON nircam_exposures FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));


-- =============================================================================
-- nirspec_rate_exposures (admin-only — NIRSpec rate-mask triage, design §3.2)
-- =============================================================================

ALTER TABLE nirspec_rate_exposures ENABLE ROW LEVEL SECURITY;

-- Admins can read all rate exposures.
DROP POLICY IF EXISTS "admin_select_nirspec_rate_exposures" ON nirspec_rate_exposures;
CREATE POLICY "admin_select_nirspec_rate_exposures"
  ON nirspec_rate_exposures FOR SELECT TO authenticated
  USING ((SELECT public.is_admin()));

-- Admins can insert rate exposures.
DROP POLICY IF EXISTS "admin_insert_nirspec_rate_exposures" ON nirspec_rate_exposures;
CREATE POLICY "admin_insert_nirspec_rate_exposures"
  ON nirspec_rate_exposures FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can update rate exposures.
DROP POLICY IF EXISTS "admin_update_nirspec_rate_exposures" ON nirspec_rate_exposures;
CREATE POLICY "admin_update_nirspec_rate_exposures"
  ON nirspec_rate_exposures FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));


-- =============================================================================
-- spectrum_exposures (admin-only — NIRSpec intermediates, epic #210 B2)
-- =============================================================================
-- Reduction intermediates, never user-facing science. Admin-only, mirroring
-- nircam_exposures. Deploy writes them under service_role (RLS bypassed).

ALTER TABLE spectrum_exposures ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "admin_select_spectrum_exposures" ON spectrum_exposures;
CREATE POLICY "admin_select_spectrum_exposures"
  ON spectrum_exposures FOR SELECT TO authenticated
  USING ((SELECT public.is_admin()));

DROP POLICY IF EXISTS "admin_insert_spectrum_exposures" ON spectrum_exposures;
CREATE POLICY "admin_insert_spectrum_exposures"
  ON spectrum_exposures FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

DROP POLICY IF EXISTS "admin_update_spectrum_exposures" ON spectrum_exposures;
CREATE POLICY "admin_update_spectrum_exposures"
  ON spectrum_exposures FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));


-- =============================================================================
-- nirspec_source_review (admin-only — NIRSpec editable flag channel, P6)
-- =============================================================================
-- Reviewer-editable stuck-shutter / bkg-override flags. Admin-only, web-editable
-- (unlike the deploy-only intermediate tables, admins INSERT/UPDATE directly here).

ALTER TABLE nirspec_source_review ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "admin_select_nirspec_source_review" ON nirspec_source_review;
CREATE POLICY "admin_select_nirspec_source_review"
  ON nirspec_source_review FOR SELECT TO authenticated
  USING ((SELECT public.is_admin()));

DROP POLICY IF EXISTS "admin_insert_nirspec_source_review" ON nirspec_source_review;
CREATE POLICY "admin_insert_nirspec_source_review"
  ON nirspec_source_review FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

DROP POLICY IF EXISTS "admin_update_nirspec_source_review" ON nirspec_source_review;
CREATE POLICY "admin_update_nirspec_source_review"
  ON nirspec_source_review FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));


-- =============================================================================
-- deploy_events (admin-only — lifecycle audit log, epic #210 B2/B3)
-- =============================================================================
-- Append-only audit log, written only by the lifecycle RPCs (SECURITY DEFINER,
-- service_role) — never a direct client INSERT. Admins read it; no INSERT/UPDATE
-- policy for authenticated, so a non-RPC write attempt is denied (RLS fail-closed).

ALTER TABLE deploy_events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "admin_select_deploy_events" ON deploy_events;
CREATE POLICY "admin_select_deploy_events"
  ON deploy_events FOR SELECT TO authenticated
  USING ((SELECT public.is_admin()));


-- =============================================================================
-- deploy_scope_state (admin-only — multi-reducer concurrency, epic #210 B4)
-- =============================================================================
-- Mutated only by the claim_deploy_scope RPC (SECURITY DEFINER, service_role/
-- admin). Admin-readable for inspection; defense-in-depth RLS.

ALTER TABLE deploy_scope_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "admin_select_deploy_scope_state" ON deploy_scope_state;
CREATE POLICY "admin_select_deploy_scope_state"
  ON deploy_scope_state FOR SELECT TO authenticated
  USING ((SELECT public.is_admin()));


-- =============================================================================
-- storage_objects (program-scoped reads, epic #210 — the client download layer)
-- =============================================================================
-- The registry is the single file-availability authority. Reads are program-
-- scoped (like targets/observations) so the Python client can mirror it as its
-- one download/availability layer. Writes stay admin-only; deploy backfill/
-- reconcile run under the service role (RLS bypassed). RLS is also the
-- belt-and-suspenders companion to get_storage_objects_for_sync /
-- filter_accessible_storage_keys, which restate this scope for the API path.

ALTER TABLE storage_objects ENABLE ROW LEVEL SECURITY;

-- Admins can read all storage objects.
DROP POLICY IF EXISTS "admin_select_storage_objects" ON storage_objects;
CREATE POLICY "admin_select_storage_objects"
  ON storage_objects FOR SELECT TO authenticated
  USING ((SELECT public.is_admin()));

-- Program members can read PUBLISHED, active storage objects in programs they can
-- access. Mirrors the spectra/targets B1 (#217) publish gate without depending on
-- the (often-NULL) observation column: spectrum-family rows simply inherit their
-- parent spectrum's visibility (program access + published); exposure/object-level
-- rows are gated by their deployment (program via its observation + published
-- status). Drafts/revoked and out-of-program rows stay hidden (admins see them via
-- admin_select_storage_objects above). Rows with neither a spectrum_id nor a
-- deployment_id (e.g. backfilled NIRCam) are admin-only until those land.
--
-- Share links (docs/design-public-mirror.md §5.2/§5.3) enter here in two ways.
-- allow_download gates the whole policy: a link minted with downloads off sees
-- catalog rows and plots but cannot presign a single byte, because every
-- download path in the portal and the API runs through storage_objects. Then
-- each branch is narrowed to the link's own scope -- spectrum-family rows by
-- their target's observation, deployment-level rows by the deployment's scope.
--
-- The `d.field IS NOT NULL` shortcut below is load-bearing for the opposite
-- reason it usually is: a published field deployment is public to every
-- CAMPFIRE user precisely because it has no program scope, which means the link
-- narrowing here is the ONLY thing keeping a field link off every other field's
-- FITS.
DROP POLICY IF EXISTS "select_storage_objects_by_access" ON storage_objects;
CREATE POLICY "select_storage_objects_by_access"
  ON storage_objects FOR SELECT TO authenticated
  USING (
    status = 'active'
    AND ((SELECT NOT public.is_link_account())
         OR (SELECT public.link_allows_download()))
    AND (
      (storage_objects.spectrum_id IS NOT NULL AND EXISTS (
         SELECT 1 FROM spectra s
         JOIN targets t ON t.target_id = s.target_id
         WHERE s.spectrum_id = storage_objects.spectrum_id
           AND (s.deploy_status = 'published'
                OR (s.deploy_status = 'draft' AND (SELECT public.link_sees_drafts())))
           AND t.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
           AND ((SELECT NOT public.is_link_account())
                OR t.observation = (SELECT public.link_observation()))))
      OR
      (storage_objects.spectrum_id IS NULL AND storage_objects.deployment_id IS NOT NULL AND EXISTS (
         SELECT 1 FROM deployments d
         LEFT JOIN observations o ON o.name = d.observation
         WHERE d.id = storage_objects.deployment_id
           AND (d.status = 'published'
                OR (d.status = 'draft' AND (SELECT public.link_sees_drafts())))
           AND (
             -- NIRCam field-scoped deploy (epic #261, N1): a field spans multiple
             -- programs, so there is no per-program scope — a published field
             -- deployment is public to everyone. Draft/revoked stay admin-only.
             d.field IS NOT NULL
             OR o.program_slug = ANY((SELECT public.accessible_program_slugs())::text[]))
           AND ((SELECT NOT public.is_link_account())
                OR d.observation = (SELECT public.link_observation())
                OR d.field = (SELECT public.link_field()))))
    )
  );

-- Admins can insert storage objects.
DROP POLICY IF EXISTS "admin_insert_storage_objects" ON storage_objects;
CREATE POLICY "admin_insert_storage_objects"
  ON storage_objects FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can update storage objects.
DROP POLICY IF EXISTS "admin_update_storage_objects" ON storage_objects;
CREATE POLICY "admin_update_storage_objects"
  ON storage_objects FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));


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
--
-- A link account (docs/design-public-mirror.md §5.3) sees only its own field's
-- layers. The tiles themselves are served from a public CDN base URL with no
-- auth, so this controls which layers are DISCOVERABLE -- exactly the same
-- protection every other user has, no more and no less. An observation-scoped
-- link gets nothing here: link_field() is NULL for it, and NULL = field is
-- never true.
DROP POLICY IF EXISTS "Authenticated users can read map layers" ON map_layers;
CREATE POLICY "Authenticated users can read map layers"
  ON map_layers FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    OR field = (SELECT public.link_field())
  );

-- Admins have full access to map layers (deploy CLI: tile registration).
DROP POLICY IF EXISTS "admin_map_layers_all" ON map_layers;
CREATE POLICY "admin_map_layers_all"
  ON map_layers FOR ALL TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));

-- Service role has full access to map layers (backward compat).
DROP POLICY IF EXISTS "Service role has full access to map layers" ON map_layers;
CREATE POLICY "Service role has full access to map layers"
  ON map_layers FOR ALL TO service_role
  USING (true)
  WITH CHECK (true);


-- =============================================================================
-- fitsgl_datasets (epic #337, Phase 3)
-- =============================================================================

ALTER TABLE fitsgl_datasets ENABLE ROW LEVEL SECURITY;

-- Visibility DERIVES from the backing mosaics — the table carries no
-- deploy_status of its own. A dataset is public iff EVERY nircam_images mosaic it
-- was built from (same field, one of its `tiles`, one of its `bands`, same
-- pixel_scale, full-field epoch) is published — the pyramid in the public tiles
-- bucket is built from all of them, so a composite mixing published + draft mosaics
-- must stay hidden until they all publish. The all-published check lives in the
-- SECURITY DEFINER fitsgl_dataset_is_public() so it can see the draft rows a
-- non-admin's own RLS would hide (mirrors how the PNG map only shows deliberately-
-- published tiles). Admins see every dataset.
-- Link accounts (docs/design-public-mirror.md §5.3) see only their own field's
-- datasets. The is_public derivation still applies on top: a link account is not
-- an admin, so a composite mixing published and draft mosaics stays hidden the
-- same way it does for everyone else. Deliberately NOT relaxed for
-- include_drafts links -- fitsgl_dataset_is_public() guards a pyramid built from
-- ALL the dataset's backing mosaics, and there is no per-link way to serve a
-- partially-draft pyramid without leaking the draft imagery wholesale.
DROP POLICY IF EXISTS "authenticated_select_fitsgl_datasets" ON fitsgl_datasets;
CREATE POLICY "authenticated_select_fitsgl_datasets"
  ON fitsgl_datasets FOR SELECT TO authenticated
  USING (
    (
      (SELECT NOT public.is_link_account())
      OR field = (SELECT public.link_field())
    )
    AND (
      (SELECT public.is_admin())
      OR public.fitsgl_dataset_is_public(field, tiles, bands, pixel_scale)
    )
  );

-- Admins have full access (login-mode deploy CLI upserts through RLS).
DROP POLICY IF EXISTS "admin_fitsgl_datasets_all" ON fitsgl_datasets;
CREATE POLICY "admin_fitsgl_datasets_all"
  ON fitsgl_datasets FOR ALL TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));

-- Service role has full access (service-role / local deploy mode).
DROP POLICY IF EXISTS "service_role_fitsgl_datasets_all" ON fitsgl_datasets;
CREATE POLICY "service_role_fitsgl_datasets_all"
  ON fitsgl_datasets FOR ALL TO service_role
  USING (true)
  WITH CHECK (true);


-- =============================================================================
-- slit_regions
-- =============================================================================

ALTER TABLE slit_regions ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read slit regions, EXCEPT those belonging to an
-- object whose spectra are all unpublished (B1 #217). NOT EXISTS form: a slit is
-- hidden only when a matching objects row exists AND is unpublished — orphan
-- slits (no objects row) stay visible, so there is zero change while everything
-- is published. Program-scoping of this table is tracked in #229.
-- Link accounts (docs/design-public-mirror.md §5.2) see only their own
-- observation's slits. This policy has no program gate at all -- it is
-- admin-or-not-unpublished -- so without the scope conjunct a NIRSpec link would
-- expose slit geometry for every observation in the archive. The table carries
-- `observation` directly (fk_slit_regions_observation), so the narrowing is a
-- plain equality; a field-scoped link gets nothing, since link_observation() is
-- NULL for it.
DROP POLICY IF EXISTS "Authenticated users can view slit regions" ON slit_regions;
CREATE POLICY "Authenticated users can view slit regions"
  ON slit_regions FOR SELECT TO authenticated
  USING (
    (
      (SELECT NOT public.is_link_account())
      OR observation = (SELECT public.link_observation())
    )
    AND (
      (SELECT public.is_admin())
      OR (SELECT public.link_sees_drafts())
      OR NOT EXISTS (
        SELECT 1 FROM objects o
        WHERE o.object_id = slit_regions.object_id AND o.has_published_spectrum = false
      )
    )
  );

-- Admins can insert slit regions (deploy CLI).
DROP POLICY IF EXISTS "admin_slit_regions_insert" ON slit_regions;
CREATE POLICY "admin_slit_regions_insert"
  ON slit_regions FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can delete slit regions (deploy CLI: delete-then-insert pattern).
DROP POLICY IF EXISTS "admin_slit_regions_delete" ON slit_regions;
CREATE POLICY "admin_slit_regions_delete"
  ON slit_regions FOR DELETE TO authenticated
  USING ((SELECT public.is_admin()));


-- =============================================================================
-- shutters
-- =============================================================================

ALTER TABLE shutters ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read shutters, EXCEPT those belonging to an object
-- whose spectra are all unpublished (B1 #217). NOT EXISTS form: hidden only when a
-- matching objects row exists AND is unpublished — orphan shutters stay visible,
-- zero change while everything is published. NOTE: the /api/v1/shutters route and
-- the get_*_shutters RPCs run under the service role (RLS bypassed) and gate
-- separately. Program-scoping of this table is tracked in #229.
-- Same shape as slit_regions above: no program gate of its own, so the share
-- link scope conjunct is what keeps a NIRSpec link off every other
-- observation's shutter layout (docs/design-public-mirror.md §5.2).
DROP POLICY IF EXISTS "Authenticated users can view shutters" ON shutters;
CREATE POLICY "Authenticated users can view shutters"
  ON shutters FOR SELECT TO authenticated
  USING (
    (
      (SELECT NOT public.is_link_account())
      OR observation = (SELECT public.link_observation())
    )
    AND (
      (SELECT public.is_admin())
      OR (SELECT public.link_sees_drafts())
      OR NOT EXISTS (
        SELECT 1 FROM objects o
        WHERE o.object_id = shutters.object_id AND o.has_published_spectrum = false
      )
    )
  );

-- Admins can insert shutters (deploy CLI).
DROP POLICY IF EXISTS "admin_shutters_insert" ON shutters;
CREATE POLICY "admin_shutters_insert"
  ON shutters FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can delete shutters (deploy CLI: delete-then-insert pattern).
DROP POLICY IF EXISTS "admin_shutters_delete" ON shutters;
CREATE POLICY "admin_shutters_delete"
  ON shutters FOR DELETE TO authenticated
  USING ((SELECT public.is_admin()));


-- =============================================================================
-- deployments
-- =============================================================================

ALTER TABLE deployments ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read the deployment log (transparency).
--
-- A link account (docs/design-public-mirror.md §5.4) sees only deployments of
-- its OWN scope. Narrowed rather than denied outright, deliberately: the
-- provenance a colleague looking at someone else's reduction most needs
-- (cfpipe_version, CRDS context, who deployed it and when) lives on these rows,
-- and the scope metadata block will read them. Without the conjunct, though, a
-- link would expose the full deploy history of every field and observation in
-- the archive.
--
-- Draft deployments stay hidden unless the link opted into drafts -- otherwise
-- the mere existence of an unpublished re-reduction leaks through the log even
-- though none of its data does.
DROP POLICY IF EXISTS "authenticated_select_deployments" ON deployments;
CREATE POLICY "authenticated_select_deployments"
  ON deployments FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    OR (
      (
        observation = (SELECT public.link_observation())
        OR field = (SELECT public.link_field())
      )
      AND (status = 'published' OR (SELECT public.link_sees_drafts()))
    )
  );

-- Admins can insert deployment log entries (deploy CLI).
DROP POLICY IF EXISTS "admin_deployments_insert" ON deployments;
CREATE POLICY "admin_deployments_insert"
  ON deployments FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can update a deployment's lifecycle (status/published_at/revoked_at).
-- B2 (#218): the publish/revoke flow flips these via the set_deployment_status
-- RPC (SECURITY DEFINER) under service_role, but the policy lets an admin web
-- session do it too. Defense-in-depth — the RPC is the intended path.
DROP POLICY IF EXISTS "admin_deployments_update" ON deployments;
CREATE POLICY "admin_deployments_update"
  ON deployments FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));


-- =============================================================================
-- pending_invites
-- =============================================================================

ALTER TABLE pending_invites ENABLE ROW LEVEL SECURITY;

-- Admins can view invites.
DROP POLICY IF EXISTS "admin_select_invites" ON pending_invites;
CREATE POLICY "admin_select_invites"
  ON pending_invites FOR SELECT TO authenticated
  USING ((SELECT public.is_admin()));

-- Users can read own invite by email.
DROP POLICY IF EXISTS "Users can read own invite by email" ON pending_invites;
CREATE POLICY "Users can read own invite by email"
  ON pending_invites FOR SELECT TO authenticated
  USING (email = (SELECT users.email FROM auth.users WHERE users.id = (SELECT auth.uid()))::text);

-- Admins can create invites.
DROP POLICY IF EXISTS "admin_insert_invites" ON pending_invites;
CREATE POLICY "admin_insert_invites"
  ON pending_invites FOR INSERT TO authenticated
  WITH CHECK ((SELECT public.is_admin()));

-- Admins can update invites.
DROP POLICY IF EXISTS "admin_update_invites" ON pending_invites;
CREATE POLICY "admin_update_invites"
  ON pending_invites FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()));

-- Admins can delete invites.
DROP POLICY IF EXISTS "admin_delete_invites" ON pending_invites;
CREATE POLICY "admin_delete_invites"
  ON pending_invites FOR DELETE TO authenticated
  USING ((SELECT public.is_admin()));


-- =============================================================================
-- access_codes
-- =============================================================================

ALTER TABLE access_codes ENABLE ROW LEVEL SECURITY;

-- Admins can manage all access codes (all operations). Codes are secrets:
-- there is deliberately NO broader SELECT policy — redemption goes through the
-- SECURITY DEFINER redeem_access_code() RPC, so non-admins can never enumerate
-- codes (a public "read active codes" policy previously allowed exactly that).
DROP POLICY IF EXISTS "admin_manage_codes" ON access_codes;
CREATE POLICY "admin_manage_codes"
  ON access_codes
  USING ((SELECT public.is_admin()));

-- Users can read codes they have already redeemed (the profile page's
-- redemption history embeds access_codes via code_redemptions). Redeeming
-- required knowing the code, so this reveals nothing new; unredeemed codes
-- stay invisible to non-admins.
DROP POLICY IF EXISTS "Users can read own redeemed codes" ON access_codes;
CREATE POLICY "Users can read own redeemed codes"
  ON access_codes FOR SELECT
  USING (EXISTS (
    SELECT 1 FROM code_redemptions
    WHERE code_redemptions.code_id = access_codes.id
      AND code_redemptions.user_id = (SELECT auth.uid())
  ));


-- =============================================================================
-- code_redemptions
-- =============================================================================

ALTER TABLE code_redemptions ENABLE ROW LEVEL SECURITY;

-- Admins can see all redemptions.
DROP POLICY IF EXISTS "admin_select_redemptions" ON code_redemptions;
CREATE POLICY "admin_select_redemptions"
  ON code_redemptions FOR SELECT
  USING ((SELECT public.is_admin()));

-- Users can see own redemptions.
DROP POLICY IF EXISTS "Users can see own redemptions" ON code_redemptions;
CREATE POLICY "Users can see own redemptions"
  ON code_redemptions FOR SELECT
  USING (user_id = (SELECT auth.uid()));

-- Users can redeem codes.
DROP POLICY IF EXISTS "Users can redeem codes" ON code_redemptions;
CREATE POLICY "Users can redeem codes"
  ON code_redemptions FOR INSERT
  WITH CHECK (user_id = (SELECT auth.uid()));


-- =============================================================================
-- inspection_access_requests
-- =============================================================================

ALTER TABLE inspection_access_requests ENABLE ROW LEVEL SECURITY;

-- Users can see their own requests; admins can see all.
DROP POLICY IF EXISTS "select_own_inspection_requests" ON inspection_access_requests;
CREATE POLICY "select_own_inspection_requests"
  ON inspection_access_requests FOR SELECT TO authenticated
  USING (user_id = (SELECT auth.uid()) OR (SELECT public.is_admin()));

-- Users can submit a request for themselves.
DROP POLICY IF EXISTS "insert_own_inspection_request" ON inspection_access_requests;
CREATE POLICY "insert_own_inspection_request"
  ON inspection_access_requests FOR INSERT TO authenticated
  WITH CHECK (user_id = (SELECT auth.uid()) AND status = 'pending');

-- Admins can review (update) requests.
DROP POLICY IF EXISTS "admin_update_inspection_requests" ON inspection_access_requests;
CREATE POLICY "admin_update_inspection_requests"
  ON inspection_access_requests FOR UPDATE TO authenticated
  USING ((SELECT public.is_admin()));


-- =============================================================================
-- download_log
-- =============================================================================

ALTER TABLE download_log ENABLE ROW LEVEL SECURITY;

-- Admins can view all downloads.
DROP POLICY IF EXISTS "admin_select_downloads" ON download_log;
CREATE POLICY "admin_select_downloads"
  ON download_log FOR SELECT TO authenticated
  USING ((SELECT public.is_admin()));

-- Users can view own downloads.
DROP POLICY IF EXISTS "Users can view own downloads" ON download_log;
CREATE POLICY "Users can view own downloads"
  ON download_log FOR SELECT TO authenticated
  USING ((SELECT auth.uid()) = user_id);


-- =============================================================================
-- password_reset_log
-- =============================================================================

ALTER TABLE password_reset_log ENABLE ROW LEVEL SECURITY;

-- Admins can view all reset logs.
DROP POLICY IF EXISTS "admin_select_reset_logs" ON password_reset_log;
CREATE POLICY "admin_select_reset_logs"
  ON password_reset_log FOR SELECT
  USING ((SELECT public.is_admin()));

-- Users can view own reset logs.
DROP POLICY IF EXISTS "Users can view own reset logs" ON password_reset_log;
CREATE POLICY "Users can view own reset logs"
  ON password_reset_log FOR SELECT
  USING (user_id = (SELECT auth.uid()));


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
  USING ((SELECT auth.uid()) = user_id);

-- Users can update own tokens.
DROP POLICY IF EXISTS "Users can update own tokens" ON refresh_tokens;
CREATE POLICY "Users can update own tokens"
  ON refresh_tokens FOR UPDATE TO authenticated
  USING ((SELECT auth.uid()) = user_id)
  WITH CHECK ((SELECT auth.uid()) = user_id);


-- =============================================================================
-- api_keys
-- =============================================================================

ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;

-- Users can view own API keys.
DROP POLICY IF EXISTS "Users can view own API keys" ON api_keys;
CREATE POLICY "Users can view own API keys"
  ON api_keys FOR SELECT TO authenticated
  USING ((SELECT auth.uid()) = user_id);

-- Users can create own API keys.
DROP POLICY IF EXISTS "Users can create own API keys" ON api_keys;
CREATE POLICY "Users can create own API keys"
  ON api_keys FOR INSERT TO authenticated
  WITH CHECK ((SELECT auth.uid()) = user_id);

-- Users can update own API keys.
DROP POLICY IF EXISTS "Users can update own API keys" ON api_keys;
CREATE POLICY "Users can update own API keys"
  ON api_keys FOR UPDATE TO authenticated
  USING ((SELECT auth.uid()) = user_id)
  WITH CHECK ((SELECT auth.uid()) = user_id);

-- Users can delete own API keys.
DROP POLICY IF EXISTS "Users can delete own API keys" ON api_keys;
CREATE POLICY "Users can delete own API keys"
  ON api_keys FOR DELETE TO authenticated
  USING ((SELECT auth.uid()) = user_id);


-- =============================================================================
-- share_links  (docs/design-public-mirror.md)
-- =============================================================================

ALTER TABLE share_links ENABLE ROW LEVEL SECURITY;

-- Admin-only, in every direction. A share link is an access grant, so only the
-- operators who mint them can see or change them.
--
-- Note what is deliberately absent: no "link accounts can read their own row"
-- policy. A link account never needs it -- the four link_* helpers in
-- functions.sql are SECURITY DEFINER precisely so they can resolve the caller's
-- scope without the caller being able to read share_links itself. Giving a link
-- account read access here would hand it its own token and, worse, the
-- link_password column.
--
-- The password is additionally withheld from `authenticated` by a column-level
-- REVOKE in tables.sql, so it stays unreadable even for admins -- RLS is
-- row-level and cannot express that on its own.
DROP POLICY IF EXISTS "admin_manage_share_links" ON share_links;
CREATE POLICY "admin_manage_share_links"
  ON share_links TO authenticated
  USING ((SELECT public.is_admin()))
  WITH CHECK ((SELECT public.is_admin()));
