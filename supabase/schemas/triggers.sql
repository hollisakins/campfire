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

-- 1. log_flag_changes
--    Logs changes to redshift_quality, spectral_features, dq_flags
--    into the flag_audit_log table and bumps updated_at.
DROP FUNCTION IF EXISTS public.log_flag_changes CASCADE;

CREATE OR REPLACE FUNCTION public.log_flag_changes() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    IF OLD.redshift_quality IS DISTINCT FROM NEW.redshift_quality THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'redshift_quality', OLD.redshift_quality, NEW.redshift_quality);
    END IF;
    IF OLD.spectral_features IS DISTINCT FROM NEW.spectral_features THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'spectral_features', OLD.spectral_features, NEW.spectral_features);
    END IF;
    IF OLD.dq_flags IS DISTINCT FROM NEW.dq_flags THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'dq_flags', OLD.dq_flags, NEW.dq_flags);
    END IF;
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

-- 2. update_object_best_redshift
--    Recomputes objects.best_redshift and best_redshift_quality from the
--    highest-quality, highest-SNR member target when redshift-related
--    columns change on a target.
DROP FUNCTION IF EXISTS public.update_object_best_redshift CASCADE;

CREATE OR REPLACE FUNCTION public.update_object_best_redshift() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    IF NEW.object_id IS NULL THEN RETURN NEW; END IF;

    UPDATE objects SET
        best_redshift = sub.redshift,
        best_redshift_quality = sub.redshift_quality
    FROM (
        SELECT redshift::double precision, redshift_quality
        FROM targets
        WHERE object_id = NEW.object_id
          AND redshift IS NOT NULL
        ORDER BY redshift_quality DESC NULLS LAST,
                 max_snr DESC NULLS LAST
        LIMIT 1
    ) sub
    WHERE objects.id = NEW.object_id;

    -- Handle case where no member has a redshift (all Impossible)
    IF NOT FOUND THEN
        UPDATE objects SET
            best_redshift = NULL,
            best_redshift_quality = (
                SELECT MAX(redshift_quality) FROM targets WHERE object_id = NEW.object_id
            )
        WHERE objects.id = NEW.object_id;
    END IF;

    RETURN NEW;
END;
$$;


-- ============================================================
-- TRIGGERS
-- ============================================================

DROP TRIGGER IF EXISTS track_flag_changes ON public.targets;
CREATE TRIGGER track_flag_changes
  BEFORE UPDATE ON public.targets
  FOR EACH ROW EXECUTE FUNCTION public.log_flag_changes();

DROP TRIGGER IF EXISTS update_max_snr_trigger ON public.spectra;
DROP TRIGGER IF EXISTS update_max_exposure_time_trigger ON public.spectra;

DROP TRIGGER IF EXISTS update_object_best_redshift_trigger ON public.targets;
CREATE TRIGGER update_object_best_redshift_trigger
  AFTER UPDATE OF redshift_quality, redshift_inspected ON public.targets
  FOR EACH ROW
  WHEN (NEW.object_id IS NOT NULL)
  EXECUTE FUNCTION public.update_object_best_redshift();


-- 5. log_list_membership_change
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

DROP TRIGGER IF EXISTS track_list_member_insert ON public.object_list_members;
CREATE TRIGGER track_list_member_insert
  AFTER INSERT ON public.object_list_members
  FOR EACH ROW EXECUTE FUNCTION public.log_list_membership_change();

DROP TRIGGER IF EXISTS track_list_member_delete ON public.object_list_members;
CREATE TRIGGER track_list_member_delete
  AFTER DELETE ON public.object_list_members
  FOR EACH ROW EXECUTE FUNCTION public.log_list_membership_change();
