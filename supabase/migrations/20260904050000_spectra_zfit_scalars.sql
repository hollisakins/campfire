-- Perf T2-D2 (#508, epic #515, decision D-D): zfit scalars on spectra, and
-- the 1-D spectrum sidecar as a registrable product type.
--
-- Hand-authored: no local Docker for `supabase db diff`. Column definitions
-- and the constraint list are copied verbatim from
-- supabase/schemas/tables.sql (the source of truth).
--
-- Why. The object page's redshift-fit summary downloaded every spectrum's
-- whole zfit sidecar (p50 56 kB, p90 515 kB) to show three scalars. Deploy
-- now writes them onto the row from the summary ECSV; existing rows are
-- backfilled from the zfit JSON by scripts/backfill_zfit_scalars.py.

ALTER TABLE "public"."spectra"
  ADD COLUMN IF NOT EXISTS "chi2_min" double precision,
  ADD COLUMN IF NOT EXISTS "confidence" double precision;

-- storage_objects.product_type tracks the campfire_layout PRODUCTS registry;
-- the new `spectrum_1d_json` kind (the `_spec_1d.json` sibling deploy now
-- emits next to the full spectrum JSON) must be registrable. Same shape as
-- 20260704170000_add_nirspec_rate_product_type.sql.
ALTER TABLE "public"."storage_objects"
    DROP CONSTRAINT "storage_objects_product_type_check";

ALTER TABLE "public"."storage_objects"
    ADD CONSTRAINT "storage_objects_product_type_check" CHECK (("product_type" = ANY (ARRAY[
        'nirspec_spec'::"text", 'spectrum_json'::"text", 'spectrum_1d_json'::"text", 'zfit'::"text",
        'nirspec_spectrum_exposure'::"text", 'nirspec_rate'::"text",
        'rgb'::"text", 'sed'::"text",
        'nircam_exposure'::"text", 'nircam_exposure_preview'::"text",
        'nircam_exposure_full'::"text", 'nircam_mosaic'::"text", 'nircam_rgb'::"text",
        'nircam_expmap'::"text", 'nircam_expmap_plot'::"text",
        'nircam_mosaic_thumbnail'::"text", 'nircam_mosaic_quicklook'::"text", 'nircam_layout'::"text",
        'tile'::"text", 'photometry_pz'::"text",
        'nirspec_manual_mask'::"text", 'nirspec_stuck_shutters'::"text",
        'nirspec_bkg_override'::"text", 'nircam_mask'::"text", 'nircam_astrom_cat'::"text",
        'nircam_bad_pixel'::"text", 'nircam_flat'::"text", 'nircam_wisp'::"text"
    ]))) NOT VALID;

ALTER TABLE "public"."storage_objects"
    VALIDATE CONSTRAINT "storage_objects_product_type_check";
