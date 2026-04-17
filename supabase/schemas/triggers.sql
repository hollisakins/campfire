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


-- 4. log_list_membership_change
--    Logs additions and removals from object lists into list_audit_log.
DROP FUNCTION IF EXISTS public.log_list_membership_change CASCADE;

CREATE OR REPLACE FUNCTION public.log_list_membership_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO list_audit_log (list_id, object_id, user_id, action, ra, dec)
        VALUES (NEW.list_id, NEW.object_id, auth.uid(), 'add', NEW.ra, NEW.dec);
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO list_audit_log (list_id, object_id, user_id, action, ra, dec)
        VALUES (OLD.list_id, OLD.object_id, auth.uid(), 'remove', OLD.ra, OLD.dec);
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


-- Per-spectrum DQ flag changes
DROP TRIGGER IF EXISTS track_spectrum_dq_changes ON public.spectra;
CREATE TRIGGER track_spectrum_dq_changes
  AFTER UPDATE OF dq_flags ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.log_spectrum_dq_changes();


-- List membership audit (unchanged from pre-Phase-D)
DROP TRIGGER IF EXISTS track_list_member_insert ON public.object_list_members;
CREATE TRIGGER track_list_member_insert
  AFTER INSERT ON public.object_list_members
  FOR EACH ROW EXECUTE FUNCTION public.log_list_membership_change();

DROP TRIGGER IF EXISTS track_list_member_delete ON public.object_list_members;
CREATE TRIGGER track_list_member_delete
  AFTER DELETE ON public.object_list_members
  FOR EACH ROW EXECUTE FUNCTION public.log_list_membership_change();
