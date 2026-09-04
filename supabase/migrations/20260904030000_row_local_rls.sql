-- Perf T2-A (#504, epic #515, decision D-A): row-local RLS.
--
-- Hand-authored: no local Docker for `supabase db diff`. Every function,
-- trigger and policy body below is copied verbatim from the schema files
-- (supabase/schemas/{tables,triggers,policies}.sql) -- they are the source of
-- truth; this file only adds the column backfills.
--
-- Why. Four read policies gated rows with `IN (SELECT ... FROM targets/objects
-- WHERE program_slug = ANY(...))`. Under PostgREST's generic plans that
-- subplan hashed every accessible target (~35 k rows, ~9.7 k buffers) or
-- object before the row's own predicate ran: comments-by-target was 37 % of
-- all DB time (230 ms mean), spectra-by-fits_path 73 ms, storage_objects on
-- the download path 122 ms, all for reads that return a handful of rows.
--
-- What.
--   1. spectra.program_slug / spectra.observation: trigger-owned copies of the
--      parent target's columns (backfilled here, NOT NULL after), so
--      select_spectra_by_access / update_spectra_dq_by_access test the row
--      itself -- the shape targets and objects already use.
--   2. shutters.has_published_spectrum: trigger-owned copy of the target's
--      flag, so the shutters policy stops probing objects once per row
--      (a 5 000-row map page cost ~30 k buffers for non-admins).
--   3. comments / object_photometry policies: correlated on the parent PRIMARY
--      KEY (tiny, per-parent reads -- below the audit's ~2 400-row crossover).
--   4. storage_objects: spectrum branch reads spectra's new columns instead of
--      joining targets; admin_select_storage_objects folded in as the first
--      disjunct of select_storage_objects_by_access (audit DB-17).
--
-- Measured on a production-scale fixture, non-admin, forced generic plans:
--   comments-by-target      888 buf / 7.3 ms  ->  11 buf / 0.16 ms
--   spectra-by-fits_path    885 buf / 7.5 ms  ->   9 buf / 0.11 ms
--   storage_objects by key  903 buf / 7.5 ms  ->  20 buf / 0.21 ms
--   photometry by object  1 130 buf / 21 ms   ->  16 buf / 0.19 ms
--   get_field_shutters page 32.5 k buf / 46 ms -> 4.3 k buf / 7.6 ms
--
-- The spectra backfill touches every row once. It does not bump updated_at
-- (bump_spectra_updated_at_trigger is scoped to user-visible columns) and
-- enforce_spectra_dq_user_update_scope passes because auth.uid() is NULL
-- under the migration runner.

-- ---------------------------------------------------------------------------
-- 1. Columns + backfill
-- ---------------------------------------------------------------------------

ALTER TABLE public.spectra
  ADD COLUMN IF NOT EXISTS program_slug text,
  ADD COLUMN IF NOT EXISTS observation text;

UPDATE public.spectra s
   SET program_slug = t.program_slug,
       observation  = t.observation
  FROM public.targets t
 WHERE t.target_id = s.target_id
   AND (s.program_slug IS DISTINCT FROM t.program_slug
        OR s.observation IS DISTINCT FROM t.observation);

ALTER TABLE public.spectra
  ALTER COLUMN program_slug SET NOT NULL,
  ALTER COLUMN observation SET NOT NULL;

ALTER TABLE public.shutters
  ADD COLUMN IF NOT EXISTS has_published_spectrum boolean NOT NULL DEFAULT true;

-- Only the unpublished minority needs a write; everything else keeps the default.
UPDATE public.shutters s
   SET has_published_spectrum = false
  FROM public.targets t
 WHERE t.target_id = s.object_id
   AND NOT t.has_published_spectrum
   AND s.has_published_spectrum;

-- ---------------------------------------------------------------------------
-- 2. Trigger functions (triggers.sql §5b / §5d)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.enforce_spectra_dq_user_update_scope() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    -- Service role (no JWT) and admins can write any column.
    IF auth.uid() IS NULL OR public.is_admin() THEN
        RETURN NEW;
    END IF;

    -- Non-admin users may only change dq_flags. updated_at is maintained
    -- by bump_spectra_updated_at; allow it through.
    IF OLD.grating IS DISTINCT FROM NEW.grating
       OR OLD.fits_path IS DISTINCT FROM NEW.fits_path
       OR OLD.signal_to_noise IS DISTINCT FROM NEW.signal_to_noise
       OR OLD.target_id IS DISTINCT FROM NEW.target_id
       OR OLD.thumbnail_svg_fnu IS DISTINCT FROM NEW.thumbnail_svg_fnu
       OR OLD.thumbnail_svg_flambda IS DISTINCT FROM NEW.thumbnail_svg_flambda
       OR OLD.file_hash IS DISTINCT FROM NEW.file_hash
       OR OLD.file_size IS DISTINCT FROM NEW.file_size
       OR OLD.exposure_time IS DISTINCT FROM NEW.exposure_time
       OR OLD.crds_context IS DISTINCT FROM NEW.crds_context
       OR OLD.jwst_version IS DISTINCT FROM NEW.jwst_version
       OR OLD.cfpipe_version IS DISTINCT FROM NEW.cfpipe_version
       OR OLD.date_obs IS DISTINCT FROM NEW.date_obs
       OR OLD.redshift_auto IS DISTINCT FROM NEW.redshift_auto
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
       OR OLD.program_slug IS DISTINCT FROM NEW.program_slug
       OR OLD.observation IS DISTINCT FROM NEW.observation
    THEN
        RAISE EXCEPTION 'Non-admin updates to spectra may only change dq_flags'
            USING ERRCODE = '42501';  -- insufficient_privilege
    END IF;

    RETURN NEW;
END;
$$;

-- 5d. Row-local RLS scope columns (perf T2-A, #504, decision D-A)
--
--     spectra.program_slug / spectra.observation and
--     shutters.has_published_spectrum are copies of parent-target state that
--     let the read policies on those tables test the row itself instead of
--     materializing every accessible target (spectra) or probing objects once
--     per row (shutters). Four triggers own the copies; nothing else writes
--     them, and a client-supplied value is only ever accepted when it already
--     agrees with the parent:
--
--       sync_spectra_target_scope            spectra  BEFORE INSERT / UPDATE OF target_id, program_slug, observation
--       propagate_target_scope_to_spectra    targets  AFTER  UPDATE OF program_slug, observation
--       sync_shutter_publication             shutters BEFORE INSERT / UPDATE OF object_id, has_published_spectrum
--       propagate_target_publication_to_shutters  targets AFTER UPDATE OF has_published_spectrum
--
--     shutters.object_id is the target_id namespace (both come from the
--     observation's ECSV object_id), so a shutter follows its TARGET's
--     publication state -- the same gate select_targets_by_access applies --
--     and one with no targets row stays visible (fail-closed default true),
--     matching the orphan behaviour of the old NOT EXISTS form.
--
--     SECURITY DEFINER: the BEFORE triggers run for non-admin DQ updates too,
--     and those callers only see targets through RLS. The parent lookup must
--     not depend on the caller's visibility.
DROP FUNCTION IF EXISTS public.sync_spectra_target_scope CASCADE;

CREATE OR REPLACE FUNCTION public.sync_spectra_target_scope() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    SELECT t.program_slug, t.observation
      INTO NEW.program_slug, NEW.observation
      FROM targets t
     WHERE t.target_id = NEW.target_id;
    -- No parent row: both land NULL and the NOT NULL constraints reject the
    -- row (the FK spectra_target_id_fkey would too). No RAISE needed here.
    RETURN NEW;
END;
$$;

DROP FUNCTION IF EXISTS public.propagate_target_scope_to_spectra CASCADE;

CREATE OR REPLACE FUNCTION public.propagate_target_scope_to_spectra() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    UPDATE spectra s
       SET program_slug = NEW.program_slug,
           observation  = NEW.observation
     WHERE s.target_id = NEW.target_id
       AND (s.program_slug IS DISTINCT FROM NEW.program_slug
            OR s.observation IS DISTINCT FROM NEW.observation);
    RETURN NULL;
END;
$$;

DROP FUNCTION IF EXISTS public.sync_shutter_publication CASCADE;

CREATE OR REPLACE FUNCTION public.sync_shutter_publication() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    NEW.has_published_spectrum := COALESCE(
        (SELECT t.has_published_spectrum FROM targets t WHERE t.target_id = NEW.object_id),
        true);
    RETURN NEW;
END;
$$;

DROP FUNCTION IF EXISTS public.propagate_target_publication_to_shutters CASCADE;

CREATE OR REPLACE FUNCTION public.propagate_target_publication_to_shutters() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    UPDATE shutters s
       SET has_published_spectrum = NEW.has_published_spectrum
     WHERE s.object_id = NEW.target_id
       AND s.has_published_spectrum IS DISTINCT FROM NEW.has_published_spectrum;
    RETURN NULL;
END;
$$;

-- ---------------------------------------------------------------------------
-- 3. Triggers
-- ---------------------------------------------------------------------------

-- Row-local RLS scope columns (perf T2-A, #504). See section 5d above.
-- spectra.program_slug / observation follow the parent target ...
DROP TRIGGER IF EXISTS sync_spectra_target_scope_trigger ON public.spectra;
CREATE TRIGGER sync_spectra_target_scope_trigger
  BEFORE INSERT OR UPDATE OF target_id, program_slug, observation ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.sync_spectra_target_scope();

-- ... and cascade when the target itself moves (never in practice; keeps the
-- invariant structural rather than procedural).
DROP TRIGGER IF EXISTS propagate_target_scope_to_spectra_trigger ON public.targets;
CREATE TRIGGER propagate_target_scope_to_spectra_trigger
  AFTER UPDATE OF program_slug, observation ON public.targets
  FOR EACH ROW
  WHEN (OLD.program_slug IS DISTINCT FROM NEW.program_slug
        OR OLD.observation IS DISTINCT FROM NEW.observation)
  EXECUTE FUNCTION public.propagate_target_scope_to_spectra();

-- shutters.has_published_spectrum follows the target on insert ...
DROP TRIGGER IF EXISTS sync_shutter_publication_trigger ON public.shutters;
CREATE TRIGGER sync_shutter_publication_trigger
  BEFORE INSERT OR UPDATE OF object_id, has_published_spectrum ON public.shutters
  FOR EACH ROW EXECUTE FUNCTION public.sync_shutter_publication();

-- ... and whenever recompute_has_published_spectrum flips the target
-- (publish / revoke / recover / reconcile).
DROP TRIGGER IF EXISTS propagate_target_publication_to_shutters_trigger ON public.targets;
CREATE TRIGGER propagate_target_publication_to_shutters_trigger
  AFTER UPDATE OF has_published_spectrum ON public.targets
  FOR EACH ROW
  WHEN (OLD.has_published_spectrum IS DISTINCT FROM NEW.has_published_spectrum)
  EXECUTE FUNCTION public.propagate_target_publication_to_shutters();

-- ---------------------------------------------------------------------------
-- 4. Policies
-- ---------------------------------------------------------------------------

-- Photometry visible if the linked object is accessible.
-- The object subquery mirrors select_objects_by_access, including its share-link
-- conjuncts (docs/design-public-mirror.md §5.2). Re-derived inline rather than
-- inherited, matching how this policy already re-derives the program check.
--
-- Perf T2-A (#504): correlated on the object PRIMARY KEY rather than
-- `object_id IN (SELECT o.id ...)`. Under PostgREST's generic plans the IN form
-- hashed every accessible object (~1 100 buffers, 21 ms) to answer a one-row
-- read; the PK probe costs ~4 buffers per photometry row. This is the shape
-- the audit's crossover analysis (~2 400 rows) endorses for a table that is
-- only ever read one object at a time -- it is not the blanket EXISTS rewrite
-- the audit warns against for spectra, which gets a denormalized column instead.
DROP POLICY IF EXISTS "select_object_photometry_by_access" ON object_photometry;
CREATE POLICY "select_object_photometry_by_access"
  ON object_photometry FOR SELECT
  USING (
    object_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM objects o
      WHERE o.id = object_photometry.object_id
        AND o.programs && (SELECT public.accessible_program_slugs())
        -- B1 (#217): no photometry for objects with no published spectrum.
        AND (o.has_published_spectrum OR (SELECT public.is_admin())
             OR (SELECT public.link_sees_drafts()))
        AND ((SELECT NOT public.is_link_account())
             OR o.observations && ARRAY[(SELECT public.link_observation())]::text[])
    )
  );

-- Spectra visible if their parent target is in an accessible program.
--
-- Perf T2-A (#504, decision D-A): row-local. program_slug and observation are
-- copies of the parent target's columns (trigger-owned, see triggers.sql §5d),
-- so the program gate is the same `= ANY((SELECT ...))` shape targets and
-- objects use (2-5 buffers) instead of `target_id IN (SELECT ... FROM targets)`,
-- which under PostgREST's generic plans hashed every accessible target
-- (~35 k rows, ~9.7 k buffers on prod) before looking at the row.
-- storage_objects inherits the win: its spectrum branch probes this table.
DROP POLICY IF EXISTS "select_spectra_by_access" ON spectra;
CREATE POLICY "select_spectra_by_access"
  ON spectra FOR SELECT
  USING (
    program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
    -- Share links (docs/design-public-mirror.md §5.2): narrow from the whole
    -- program to the one shared observation. The old targets subquery got this
    -- for free from select_targets_by_access; row-local means restating it.
    AND ((SELECT NOT public.is_link_account())
         OR observation = (SELECT public.link_observation()))
    -- B1 (#217): PRIMARY per-row gate. Only 'published' spectra reach non-admins;
    -- 'draft' and 'revoked' are hidden. This is the sole gate for the user-client
    -- web routes that read spectra directly and never call an RPC (/api/spectrum,
    -- /api/download, /api/redshift-fit, /api/spectrum-thumbnail). Admins see all.
    --
    -- ...and, since share links (docs/design-public-mirror.md §6), an
    -- include_drafts link account -- but only for rows already inside its scope,
    -- which the program + observation conjuncts above enforce. 'revoked' stays
    -- hidden from links too: link_sees_drafts() relaxes the gate to draft,
    -- never past it.
    AND (deploy_status = 'published' OR (SELECT public.is_admin())
         OR (deploy_status = 'draft' AND (SELECT public.link_sees_drafts())))
  );

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
    -- Perf T2-A (#504): row-local, same as select_spectra_by_access.
    AND program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
    -- B1 (#217): a non-admin inspector cannot set DQ flags on an unpublished
    -- spectrum (can't see it via the select policy either).
    AND (deploy_status = 'published' OR (SELECT public.is_admin()))
  )
  WITH CHECK (
    (SELECT public.can_inspect())
    AND program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
    AND (deploy_status = 'published' OR (SELECT public.is_admin()))
  );

-- Comments visible if their parent target or object is in an accessible program.
-- Link accounts (docs/design-public-mirror.md §5.4) see no comments at all.
-- Unlike photometry, a comment is not part of the data -- it is CAMPFIRE users
-- talking to each other about a source, often candidly and often about sources
-- the link holder can legitimately see. Scoping it to the shared observation
-- would still expose that discussion, so deny outright rather than narrow.
--
-- Perf T2-A (#504): both branches are correlated on the parent PRIMARY KEY
-- (targets.id / objects.id) instead of `IN (SELECT ... FROM targets/objects)`.
-- The IN form was prod's single most expensive statement: under PostgREST's
-- generic plans it hashed every accessible target (~35 k rows, ~9.7 k buffers,
-- 230 ms mean over 166 k calls = 37 % of all DB time) to answer a read that
-- returns a handful of rows. The PK probe is ~4 buffers per comment. comments
-- is a few hundred rows and is only ever read per target / object / author,
-- so it sits far below the audit's ~2 400-row crossover where a correlated
-- probe stops paying; a denormalized column here would add a sync surface
-- (objects.programs changes at every reconcile, comments are re-pointed by
-- rebuilds) for no measurable gain.
DROP POLICY IF EXISTS "select_comments_by_access" ON comments;
CREATE POLICY "select_comments_by_access"
  ON comments FOR SELECT
  USING (
    (SELECT NOT public.is_link_account())
    AND (
      -- Target-level comments
      (target_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM targets t
        WHERE t.id = comments.target_id
          AND t.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
          AND (t.has_published_spectrum OR (SELECT public.is_admin()))  -- B1 (#217)
      ))
      OR
      -- Object-level comments
      (target_id IS NULL AND object_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM objects o
        WHERE o.id = comments.object_id
          AND o.programs && (SELECT public.accessible_program_slugs())
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
      (target_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM targets t
        WHERE t.id = comments.target_id
          AND t.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
      ))
      OR
      -- Object-level comments
      (target_id IS NULL AND object_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM objects o
        WHERE o.id = comments.object_id
          AND o.programs && (SELECT public.accessible_program_slugs())
      ))
    )
    AND (SELECT public.can_comment())
  );

-- Perf T2-A (#504, audit DB-17): the former admin_select_storage_objects policy
-- is folded into select_storage_objects_by_access below as its first
-- disjunct. Two permissive SELECT policies on one table are OR-ed by the
-- planner anyway, but each is a separate qual and the admin one forced the
-- program-member branch to be evaluated alongside it on every admin read.
DROP POLICY IF EXISTS "admin_select_storage_objects" ON storage_objects;

-- Admins read everything. Program members can read PUBLISHED, active storage
-- objects in programs they can access. Mirrors the spectra/targets B1 (#217)
-- publish gate without depending on the (often-NULL) observation column:
-- spectrum-family rows simply inherit their parent spectrum's visibility
-- (program access + published); exposure/object-level rows are gated by their
-- deployment (program via its observation + published status). Drafts/revoked
-- and out-of-program rows stay hidden from non-admins. Rows with neither a
-- spectrum_id nor a deployment_id (e.g. backfilled NIRCam) are admin-only until
-- those land.
--
-- Perf T2-A (#504): the spectrum branch reads spectra.program_slug /
-- spectra.observation (trigger-owned copies of the target's columns) instead of
-- joining targets, so the probe is one index lookup on spectra(spectrum_id) and
-- the spectra RLS it runs under is itself row-local now. Before, the nested
-- select_spectra_by_access hashed every accessible target per row (~900
-- buffers for a one-row read on the download / manifest path).
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
    (SELECT public.is_admin())
    OR (
    status = 'active'
    AND ((SELECT NOT public.is_link_account())
         OR (SELECT public.link_allows_download()))
    AND (
      (storage_objects.spectrum_id IS NOT NULL AND EXISTS (
         SELECT 1 FROM spectra s
         WHERE s.spectrum_id = storage_objects.spectrum_id
           AND (s.deploy_status = 'published'
                OR (s.deploy_status = 'draft' AND (SELECT public.link_sees_drafts())))
           AND s.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
           AND ((SELECT NOT public.is_link_account())
                OR s.observation = (SELECT public.link_observation()))))
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
    )
  );

-- All authenticated users can read shutters, EXCEPT those belonging to a target
-- with no published spectrum (B1 #217).
--
-- Perf T2-A (#504, audit DB-V1): row-local. has_published_spectrum is a
-- trigger-owned copy of the shutter's target's flag (triggers.sql §5d), so the
-- gate is a column test instead of the old per-row NOT EXISTS probe into
-- objects, which cost a 5 000-row map page ~30 k buffers for non-admins (8× an
-- admin's). Semantics are the target's, not the object's: shutters.object_id
-- is the target_id namespace, and the old objects join only ever matched the
-- object's namesake target, so a draft re-observation of a published object
-- used to expose its MSA geometry. A shutter with no targets row keeps the
-- default (true) and stays visible, as orphans did before. NOTE: the
-- get_*_shutters RPCs are SECURITY INVOKER, so this policy is their gate;
-- /api/v1/shutters runs under the service role and gates separately.
-- Program-scoping of this table is tracked in #229.
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
      has_published_spectrum
      OR (SELECT public.is_admin())
      OR (SELECT public.link_sees_drafts())
    )
  );
