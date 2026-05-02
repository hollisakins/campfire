-- Drop legacy objects.best_redshift / best_redshift_quality columns.
--
-- These were aggregates across member targets, frozen since the targets-side
-- trigger that maintained them was removed in Phase D. Inspection-driven
-- objects.redshift / redshift_quality have replaced them in every consumer
-- (RPCs, sync, Python client, web UI). See issue #124.

-- 1. Redefine enforce_object_user_update_scope without the dropped columns.
--    plpgsql function bodies are parsed at execution time, so the trigger
--    must be redefined before the columns disappear or it would raise on
--    the next UPDATE to objects.
CREATE OR REPLACE FUNCTION public.enforce_object_user_update_scope() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    IF auth.uid() IS NULL OR public.is_admin() THEN
        RETURN NEW;
    END IF;

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
       OR OLD.photo_z IS DISTINCT FROM NEW.photo_z
       OR OLD.photo_z_err_lo IS DISTINCT FROM NEW.photo_z_err_lo
       OR OLD.photo_z_err_hi IS DISTINCT FROM NEW.photo_z_err_hi
       OR OLD.has_photometry IS DISTINCT FROM NEW.has_photometry
       OR OLD.redshift_auto IS DISTINCT FROM NEW.redshift_auto
       OR OLD.last_data_change_at IS DISTINCT FROM NEW.last_data_change_at
       OR OLD.staleness_reason IS DISTINCT FROM NEW.staleness_reason
       OR OLD.inspected_used_auto IS DISTINCT FROM NEW.inspected_used_auto
       OR OLD.is_active IS DISTINCT FROM NEW.is_active
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
    THEN
        RAISE EXCEPTION 'Non-admin updates to objects may only change inspection fields (redshift_inspected, redshift_quality, last_inspected_at, last_inspected_by)'
            USING ERRCODE = '42501';
    END IF;

    RETURN NEW;
END;
$$;

-- 2. Drop the index on best_redshift_quality.
DROP INDEX IF EXISTS public.idx_objects_best_redshift_quality;

-- 3. Drop the columns themselves.
ALTER TABLE public.objects
    DROP COLUMN IF EXISTS best_redshift,
    DROP COLUMN IF EXISTS best_redshift_quality;
