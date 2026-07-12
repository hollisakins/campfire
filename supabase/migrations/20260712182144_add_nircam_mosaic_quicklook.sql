-- Add 'nircam_mosaic_quicklook' to the storage_objects product-type CHECK:
-- the larger rendition of the mosaic thumbnail pair (long side ~4k px, for
-- the NIRCam page's click-to-enlarge popup; `_thumb.png` stays the small
-- table rendition). Named "quicklook" because `_preview.png`/`_full.png` in
-- the same directory already mean the per-exposure triage PNGs.
--
-- NOTE: the generated diff also contained the usual spurious migra
-- drop/recreate churn for mv_filter_options, mv_programs_overview,
-- nircam_reduction_progress and spectrum_flag_summary (migra cannot track
-- (mat)views); stripped here, only the intended CHECK change kept.

alter table "public"."storage_objects" drop constraint "storage_objects_product_type_check";

alter table "public"."storage_objects" add constraint "storage_objects_product_type_check" CHECK ((product_type = ANY (ARRAY['nirspec_spec'::text, 'spectrum_json'::text, 'zfit'::text, 'nirspec_spectrum_exposure'::text, 'nirspec_rate'::text, 'rgb'::text, 'sed'::text, 'nircam_exposure'::text, 'nircam_exposure_preview'::text, 'nircam_exposure_full'::text, 'nircam_mosaic'::text, 'nircam_rgb'::text, 'nircam_expmap'::text, 'nircam_expmap_plot'::text, 'nircam_mosaic_thumbnail'::text, 'nircam_mosaic_quicklook'::text, 'nircam_layout'::text, 'tile'::text, 'photometry_pz'::text, 'nirspec_manual_mask'::text, 'nirspec_stuck_shutters'::text, 'nirspec_bkg_override'::text, 'nircam_mask'::text, 'nircam_astrom_cat'::text, 'nircam_bad_pixel'::text, 'nircam_flat'::text, 'nircam_wisp'::text]))) not valid;

alter table "public"."storage_objects" validate constraint "storage_objects_product_type_check";
