-- =============================================================================
-- Post-review polish (round 3): close 🟧 items from the pre-merge audit
-- =============================================================================
--
-- #8  Widen log_object_inspection_changes + track_object_inspection_trigger so
--     pure redshift_inspected edits (with unchanged quality) still emit an
--     audit row. Previously the trigger only fired on redshift_quality
--     changes, causing count_distinct_inspected_objects to undercount.
--
-- #9  Scope bump_spectra_updated_at_trigger to user-visible columns. The old
--     unscoped BEFORE UPDATE bumped updated_at on every pipeline provenance
--     touch (crds_context, jwst_version, etc.), forcing every connected
--     client to re-sync the full spectra table after any deploy.
--
-- #14 Switch flag_audit_log subject FKs from ON DELETE CASCADE to SET NULL
--     and relax the subject-check constraint from exactly-one to at-most-one
--     so pipeline churn (delete-then-reinsert of a reprocessed spectrum,
--     object rebuild) doesn't wipe the audit trail.
--
-- #7  (Comment-only) Mark deprecated target-tier inspection columns with
--     DEPRECATED pointers so schema readers understand they are transitional.
--
-- Manually hand-edited from `supabase db diff` output to strip spurious
-- drop/recreate of mv_filter_options / mv_programs_overview /
-- spectrum_flag_summary (migra cannot diff materialized views / views) and
-- to re-introduce the COMMENT statements (migra does not track comments).
-- =============================================================================


-- ------------------------------------------------------------------
-- #14: flag_audit_log subject FKs → ON DELETE SET NULL, CHECK <= 1
-- ------------------------------------------------------------------

alter table "public"."flag_audit_log" drop constraint "flag_audit_log_object_id_fkey";
alter table "public"."flag_audit_log" drop constraint "flag_audit_log_spectrum_id_fkey";
alter table "public"."flag_audit_log" drop constraint "flag_audit_log_target_id_fkey";
alter table "public"."flag_audit_log" drop constraint "flag_audit_log_subject_check";

alter table "public"."flag_audit_log"
    add constraint "flag_audit_log_object_id_fkey"
    FOREIGN KEY (object_id) REFERENCES public.objects(id) ON DELETE SET NULL;

alter table "public"."flag_audit_log"
    add constraint "flag_audit_log_spectrum_id_fkey"
    FOREIGN KEY (spectrum_id) REFERENCES public.spectra(id) ON DELETE SET NULL;

alter table "public"."flag_audit_log"
    add constraint "flag_audit_log_target_id_fkey"
    FOREIGN KEY (target_id) REFERENCES public.targets(id) ON DELETE SET NULL;

alter table "public"."flag_audit_log"
    add constraint "flag_audit_log_subject_check"
    CHECK (
        ((target_id IS NOT NULL)::int +
         (object_id IS NOT NULL)::int +
         (spectrum_id IS NOT NULL)::int) <= 1
    );


-- ------------------------------------------------------------------
-- #8: widen object-inspection audit to redshift_inspected
-- ------------------------------------------------------------------

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.log_object_inspection_changes()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
BEGIN
    IF OLD.redshift_quality IS DISTINCT FROM NEW.redshift_quality THEN
        INSERT INTO flag_audit_log (object_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'redshift_quality', OLD.redshift_quality, NEW.redshift_quality);
    END IF;
    IF OLD.redshift_inspected IS DISTINCT FROM NEW.redshift_inspected THEN
        INSERT INTO flag_audit_log (object_id, user_id, field_name, old_value, new_value)
        VALUES (
            NEW.id, auth.uid(), 'redshift_inspected',
            (OLD.redshift_inspected * 1000000)::integer,
            (NEW.redshift_inspected * 1000000)::integer
        );
    END IF;
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$function$
;

drop trigger if exists "track_object_inspection_trigger" on "public"."objects";
CREATE TRIGGER track_object_inspection_trigger
  BEFORE UPDATE OF redshift_quality, redshift_inspected ON public.objects
  FOR EACH ROW EXECUTE FUNCTION public.log_object_inspection_changes();


-- ------------------------------------------------------------------
-- #9: scope bump_spectra_updated_at to user-visible columns
-- ------------------------------------------------------------------

drop trigger if exists "bump_spectra_updated_at_trigger" on "public"."spectra";
CREATE TRIGGER bump_spectra_updated_at_trigger
  BEFORE UPDATE OF
    dq_flags,
    redshift_auto,
    signal_to_noise,
    thumbnail_svg_fnu,
    thumbnail_svg_flambda,
    fits_path,
    file_hash
  ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.bump_spectra_updated_at();


-- ------------------------------------------------------------------
-- #7: deprecate target-tier inspection columns (comments only)
-- ------------------------------------------------------------------

COMMENT ON COLUMN "public"."targets"."redshift_auto" IS 'DEPRECATED (Phase D): per-target auto-z moved to spectra.redshift_auto + objects.redshift_auto aggregate. Still read by get_filtered_objects_paginated member_targets payload for transitional UI. Remove in Phase E.';
COMMENT ON COLUMN "public"."targets"."redshift_quality" IS 'DEPRECATED (Phase D): inspection state moved to objects.redshift_quality. No-op — no consumer reads this. Remove in Phase E.';
COMMENT ON COLUMN "public"."targets"."spectral_features" IS 'DEPRECATED (Phase D): spectral-feature flags moved to objects.spectral_features (pending Phase E feature) and/or per-spectrum comments. No-op. Remove in Phase E.';
COMMENT ON COLUMN "public"."targets"."dq_flags" IS 'DEPRECATED (Phase D): DQ flags moved to spectra.dq_flags (per-spectrum). No-op — no consumer reads this. Remove in Phase E.';
COMMENT ON COLUMN "public"."targets"."redshift_inspected" IS 'DEPRECATED (Phase D): user override moved to objects.redshift_inspected. No-op. Remove in Phase E.';
COMMENT ON COLUMN "public"."targets"."last_inspected_at" IS 'DEPRECATED (Phase D): inspection attribution moved to objects.last_inspected_at. No-op. Remove in Phase E.';
COMMENT ON COLUMN "public"."targets"."last_inspected_by" IS 'DEPRECATED (Phase D): inspection attribution moved to objects.last_inspected_by. No-op. Remove in Phase E.';
COMMENT ON COLUMN "public"."targets"."redshift" IS 'DEPRECATED (Phase D): generated column derived from targets.redshift_inspected/_auto/_quality, all of which are now no-op state. Object-level equivalent lives at objects.redshift. Remove in Phase E with its inputs.';
