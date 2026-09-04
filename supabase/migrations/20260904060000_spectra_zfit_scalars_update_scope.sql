-- Perf T2-D2 (#508): the zfit scalars added by 20260904050000 are
-- deploy-owned. enforce_spectra_dq_user_update_scope is a denylist of the
-- columns a non-admin inspector (update_spectra_dq_by_access) may not
-- change, so the two new columns were writable through PostgREST by anyone
-- allowed to set DQ flags. Add them.
--
-- Hand-authored: no local Docker for `supabase db diff`. The function body
-- is copied verbatim from supabase/schemas/triggers.sql (the source of
-- truth). CREATE OR REPLACE keeps the existing trigger binding.

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
       -- zfit scalars on the row (perf T2-D2, #508): deploy / backfill only.
       OR OLD.chi2_min IS DISTINCT FROM NEW.chi2_min
       OR OLD.confidence IS DISTINCT FROM NEW.confidence
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
