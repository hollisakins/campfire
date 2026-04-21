-- =============================================================================
-- CAMPFIRE Supabase Schema: Triggers
-- =============================================================================
-- Canonical source of truth for all trigger functions and triggers.
-- Do NOT read migration files to understand current signatures or behavior.
--
-- Workflow: edit here → run apply.sh → supabase db diff → commit migration
-- =============================================================================


-- ============================================================
-- TRIGGER FUNCTIONS
-- ============================================================

-- 1. log_object_inspection_changes
--    Logs object-level redshift_quality changes into flag_audit_log
--    (now subject = object_id) and bumps updated_at. Replaces the targets
--    flavor of log_flag_changes for inspection state.
DROP FUNCTION IF EXISTS public.log_object_inspection_changes CASCADE;

CREATE OR REPLACE FUNCTION public.log_object_inspection_changes() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    IF OLD.redshift_quality IS DISTINCT FROM NEW.redshift_quality THEN
        INSERT INTO flag_audit_log (object_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'redshift_quality', OLD.redshift_quality, NEW.redshift_quality);
    END IF;
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


-- 2. bump_object_version
--    Optimistic-locking counter: increments objects.version *only* when
--    user-editable inspection fields change. Aggregate column updates from
--    reconcile_field_objects() (n_targets, programs, max_snr, etc.) do not
--    bump the version, so the deploy pipeline never invalidates an
--    in-progress edit.
DROP FUNCTION IF EXISTS public.bump_object_version CASCADE;

CREATE OR REPLACE FUNCTION public.bump_object_version() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.redshift_inspected IS DISTINCT FROM NEW.redshift_inspected
       OR OLD.redshift_quality IS DISTINCT FROM NEW.redshift_quality THEN
        NEW.version = OLD.version + 1;
    END IF;
    RETURN NEW;
END;
$$;


-- 3. log_spectrum_dq_changes
--    Logs per-spectrum dq_flags changes into flag_audit_log
--    (subject = spectrum_id). DQ flags are now per-spectrum; target-level
--    DQ logging is gone with the targets-list view.
DROP FUNCTION IF EXISTS public.log_spectrum_dq_changes CASCADE;

CREATE OR REPLACE FUNCTION public.log_spectrum_dq_changes() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    IF OLD.dq_flags IS DISTINCT FROM NEW.dq_flags THEN
        INSERT INTO flag_audit_log (spectrum_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'dq_flags', OLD.dq_flags, NEW.dq_flags);
    END IF;
    RETURN NEW;
END;
$$;


-- 4. enforce_object_user_update_scope
--    Non-admin users (via `update_objects_by_access` RLS) can legitimately
--    write inspection fields. The RLS policy has no WITH CHECK and no
--    column-level filter, so without this trigger a user with can_comment
--    can hit PostgREST directly and rewrite anything on objects
--    (programs, is_active, aggregates, etc.). This trigger enforces the
--    column scope at the DB level: anything except the inspection set
--    raises an exception for non-admin callers. Admins and service-role
--    writes (auth.uid() IS NULL) pass through.
DROP FUNCTION IF EXISTS public.enforce_object_user_update_scope CASCADE;

CREATE OR REPLACE FUNCTION public.enforce_object_user_update_scope() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    -- Service role (no JWT) and admins can write any column.
    IF auth.uid() IS NULL OR public.is_admin() THEN
        RETURN NEW;
    END IF;

    -- Non-admin users may only touch the inspection set:
    --   redshift_inspected, redshift_quality, last_inspected_at,
    --   last_inspected_by. version and updated_at are maintained by sibling
    --   triggers; we explicitly allow them to change so this trigger
    --   doesn't reject writes that went through the legitimate path.
    IF OLD.object_id IS DISTINCT FROM NEW.object_id
       OR OLD.field IS DISTINCT FROM NEW.field
       OR OLD.ra IS DISTINCT FROM NEW.ra
       OR OLD.dec IS DISTINCT FROM NEW.dec
       OR OLD.n_targets IS DISTINCT FROM NEW.n_targets
       OR OLD.n_spectra IS DISTINCT FROM NEW.n_spectra
       OR OLD.programs IS DISTINCT FROM NEW.programs
       OR OLD.gratings IS DISTINCT FROM NEW.gratings
       OR OLD.observations IS DISTINCT FROM NEW.observations
       OR OLD.max_snr IS DISTINCT FROM NEW.max_snr
       OR OLD.max_exposure_time IS DISTINCT FROM NEW.max_exposure_time
       OR OLD.best_redshift IS DISTINCT FROM NEW.best_redshift
       OR OLD.best_redshift_quality IS DISTINCT FROM NEW.best_redshift_quality
       OR OLD.photo_z IS DISTINCT FROM NEW.photo_z
       OR OLD.photo_z_err_lo IS DISTINCT FROM NEW.photo_z_err_lo
       OR OLD.photo_z_err_hi IS DISTINCT FROM NEW.photo_z_err_hi
       OR OLD.has_photometry IS DISTINCT FROM NEW.has_photometry
       OR OLD.redshift_auto IS DISTINCT FROM NEW.redshift_auto
       OR OLD.last_data_change_at IS DISTINCT FROM NEW.last_data_change_at
       OR OLD.staleness_reason IS DISTINCT FROM NEW.staleness_reason
       OR OLD.is_active IS DISTINCT FROM NEW.is_active
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
    THEN
        RAISE EXCEPTION 'Non-admin updates to objects may only change inspection fields (redshift_inspected, redshift_quality, last_inspected_at, last_inspected_by)'
            USING ERRCODE = '42501';  -- insufficient_privilege
    END IF;

    RETURN NEW;
END;
$$;


-- 5. bump_spectra_updated_at
--    Sets spectra.updated_at = NOW() on any UPDATE so incremental sync
--    (p_updated_since) can pick up per-spectrum changes regardless of
--    which column was touched.
DROP FUNCTION IF EXISTS public.bump_spectra_updated_at CASCADE;

CREATE OR REPLACE FUNCTION public.bump_spectra_updated_at() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


-- 5b. enforce_spectra_dq_user_update_scope
--     Mirrors enforce_object_user_update_scope for the spectra table.
--     Non-admin users authenticated under `update_spectra_dq_by_access`
--     RLS may update spectra whose parent target is in an accessible
--     program, but only to change dq_flags. This trigger rejects any
--     other column delta for non-admin callers so PostgREST writes can't
--     rewrite fits_path, thumbnails, provenance, etc.
DROP FUNCTION IF EXISTS public.enforce_spectra_dq_user_update_scope CASCADE;

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
       OR OLD.reduction_version IS DISTINCT FROM NEW.reduction_version
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
    THEN
        RAISE EXCEPTION 'Non-admin updates to spectra may only change dq_flags'
            USING ERRCODE = '42501';  -- insufficient_privilege
    END IF;

    RETURN NEW;
END;
$$;


-- 6. log_list_membership_change
--    Logs additions and removals from object lists into list_audit_log.
DROP FUNCTION IF EXISTS public.log_list_membership_change CASCADE;

CREATE OR REPLACE FUNCTION public.log_list_membership_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
    v_object_id integer;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_object_id := NEW.object_id;
        INSERT INTO list_audit_log (list_id, object_id, user_id, action, ra, dec)
        VALUES (NEW.list_id, NEW.object_id, auth.uid(), 'add', NEW.ra, NEW.dec);
    ELSIF TG_OP = 'DELETE' THEN
        v_object_id := OLD.object_id;
        INSERT INTO list_audit_log (list_id, object_id, user_id, action, ra, dec)
        VALUES (OLD.list_id, OLD.object_id, auth.uid(), 'remove', OLD.ra, OLD.dec);
    END IF;

    -- Bump objects.updated_at so incremental sync picks up tag changes
    UPDATE objects SET updated_at = NOW() WHERE id = v_object_id;

    IF TG_OP = 'INSERT' THEN
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$$;


-- ============================================================
-- TRIGGERS
-- ============================================================

-- Phase D: drop the old targets-side log/aggregate triggers. Inspection
-- state has moved to objects; targets are stateless provenance now.
DROP TRIGGER IF EXISTS track_flag_changes ON public.targets;
DROP TRIGGER IF EXISTS update_object_best_redshift_trigger ON public.targets;
DROP FUNCTION IF EXISTS public.log_flag_changes CASCADE;
DROP FUNCTION IF EXISTS public.update_object_best_redshift CASCADE;

DROP TRIGGER IF EXISTS update_max_snr_trigger ON public.spectra;
DROP TRIGGER IF EXISTS update_max_exposure_time_trigger ON public.spectra;


-- Object inspection: BEFORE UPDATE so version bump and updated_at land in
-- the same row write. Two triggers because PostgreSQL fires triggers in
-- alphabetical order — `bump_object_version` should run first to set
-- NEW.version, then `track_object_inspection` records the change and bumps
-- updated_at.
DROP TRIGGER IF EXISTS bump_object_version_trigger ON public.objects;
CREATE TRIGGER bump_object_version_trigger
  BEFORE UPDATE OF redshift_inspected, redshift_quality ON public.objects
  FOR EACH ROW EXECUTE FUNCTION public.bump_object_version();

DROP TRIGGER IF EXISTS track_object_inspection_trigger ON public.objects;
CREATE TRIGGER track_object_inspection_trigger
  BEFORE UPDATE OF redshift_quality ON public.objects
  FOR EACH ROW EXECUTE FUNCTION public.log_object_inspection_changes();

-- Belt-and-suspenders: the `update_objects_by_access` RLS policy has no
-- WITH CHECK clause, so this trigger enforces the column scope at the
-- row level. Must run AFTER bump_object_version so the version bump
-- from the legitimate inspection write isn't misclassified as a
-- forbidden aggregate change.
DROP TRIGGER IF EXISTS enforce_object_user_update_scope_trigger ON public.objects;
CREATE TRIGGER enforce_object_user_update_scope_trigger
  BEFORE UPDATE ON public.objects
  FOR EACH ROW EXECUTE FUNCTION public.enforce_object_user_update_scope();


-- Per-spectrum DQ flag changes
DROP TRIGGER IF EXISTS track_spectrum_dq_changes ON public.spectra;
CREATE TRIGGER track_spectrum_dq_changes
  AFTER UPDATE OF dq_flags ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.log_spectrum_dq_changes();


-- Keep spectra.updated_at fresh on every UPDATE so incremental sync can
-- pick up changes via p_updated_since regardless of which column changed.
DROP TRIGGER IF EXISTS bump_spectra_updated_at_trigger ON public.spectra;
CREATE TRIGGER bump_spectra_updated_at_trigger
  BEFORE UPDATE ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.bump_spectra_updated_at();

-- Belt-and-suspenders for update_spectra_dq_by_access — restricts non-admin
-- writes to dq_flags only. See enforce_spectra_dq_user_update_scope function.
DROP TRIGGER IF EXISTS enforce_spectra_dq_user_update_scope_trigger ON public.spectra;
CREATE TRIGGER enforce_spectra_dq_user_update_scope_trigger
  BEFORE UPDATE ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.enforce_spectra_dq_user_update_scope();


-- List membership audit (unchanged from pre-Phase-D)
DROP TRIGGER IF EXISTS track_list_member_insert ON public.object_list_members;
CREATE TRIGGER track_list_member_insert
  AFTER INSERT ON public.object_list_members
  FOR EACH ROW EXECUTE FUNCTION public.log_list_membership_change();

DROP TRIGGER IF EXISTS track_list_member_delete ON public.object_list_members;
CREATE TRIGGER track_list_member_delete
  AFTER DELETE ON public.object_list_members
  FOR EACH ROW EXECUTE FUNCTION public.log_list_membership_change();
