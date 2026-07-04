-- Add 'nirspec_rate' to the storage_objects.product_type CHECK (P1, NIRSpec review
-- loop, design §3.1/§5). The stage-1 detector rate file ('<root>_<nrsN>_rate.fits')
-- is now a registered cloud-backed layout product deployed to OSN, so the registry
-- must accept its product_type. Purely additive enum widening: no existing row is
-- invalidated, no column/table changes, so seed.sql needs no regen.
--
-- Mirrors the drop + re-add (NOT VALID) + validate shape used when the constraint
-- was first authored (20260628221623_add_storage_objects_registry.sql). Keep the
-- ARRAY membership textually identical to supabase/schemas/tables.sql (the source
-- of truth) or the preview-branch db diff check goes red.

alter table "public"."storage_objects"
    drop constraint "storage_objects_product_type_check";

alter table "public"."storage_objects"
    add constraint "storage_objects_product_type_check" CHECK ((product_type = ANY (ARRAY['nirspec_spec'::text, 'spectrum_json'::text, 'zfit'::text, 'nirspec_spectrum_exposure'::text, 'nirspec_rate'::text, 'rgb'::text, 'sed'::text, 'nircam_exposure'::text, 'nircam_exposure_preview'::text, 'nircam_exposure_full'::text, 'nircam_mosaic'::text, 'nircam_rgb'::text, 'nircam_expmap'::text, 'tile'::text, 'photometry_pz'::text, 'nirspec_manual_mask'::text, 'nirspec_stuck_shutters'::text, 'nirspec_bkg_override'::text, 'nircam_mask'::text, 'nircam_astrom_cat'::text, 'nircam_bad_pixel'::text, 'nircam_flat'::text, 'nircam_wisp'::text]))) not valid;

alter table "public"."storage_objects"
    validate constraint "storage_objects_product_type_check";
