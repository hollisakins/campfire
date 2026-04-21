-- Fix #1 from the pre-merge audit: DQ write RLS + audit attribution race.
--
-- Before this change, inspectors writing per-spectrum dq_flags had no RLS
-- policy and therefore had to go through a service-role client in the API
-- layer, which made auth.uid() NULL during the audit-log trigger. The route
-- then patched the audit row's user_id post-hoc via
-- ".order('id').limit(1)", which attributes concurrent DQ edits on
-- different spectra to the wrong user.
--
-- New RLS: `update_spectra_dq_by_access` lets can_comment users update
-- spectra whose parent target is in an accessible program. The column
-- scope is restricted to dq_flags by the belt-and-suspenders trigger
-- `enforce_spectra_dq_user_update_scope`, mirroring the
-- `enforce_object_user_update_scope` pattern.
--
-- With this in place, the route can use the user's JWT for the UPDATE,
-- auth.uid() in the audit trigger is the real user, and the post-hoc
-- patch is unnecessary.

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.enforce_spectra_dq_user_update_scope()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
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
$function$
;

DROP POLICY IF EXISTS "update_spectra_dq_by_access" ON "public"."spectra";
CREATE POLICY "update_spectra_dq_by_access"
  ON "public"."spectra"
  AS permissive
  FOR UPDATE
  TO authenticated
  USING (
    public.can_comment()
    AND target_id IN (
      SELECT t.target_id FROM public.targets t
      WHERE t.program_slug = ANY(public.accessible_program_slugs())
    )
  )
  WITH CHECK (
    public.can_comment()
    AND target_id IN (
      SELECT t.target_id FROM public.targets t
      WHERE t.program_slug = ANY(public.accessible_program_slugs())
    )
  );

DROP TRIGGER IF EXISTS enforce_spectra_dq_user_update_scope_trigger ON public.spectra;
CREATE TRIGGER enforce_spectra_dq_user_update_scope_trigger
  BEFORE UPDATE ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.enforce_spectra_dq_user_update_scope();
