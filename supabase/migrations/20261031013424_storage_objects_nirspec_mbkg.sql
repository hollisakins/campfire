SET lock_timeout = '5s';
SET statement_timeout = '60s';

ALTER TABLE "public"."storage_objects"
  DROP CONSTRAINT "storage_objects_product_type_check";

ALTER TABLE "public"."storage_objects"
  ADD CONSTRAINT "storage_objects_product_type_check"
    CHECK
    ((product_type = ANY (ARRAY['nirspec_spec'::text, 'spectrum_json'::text, 'spectrum_1d_json'::text, 'zfit'::text, 'nirspec_lines'::text, 'nirspec_lines_json'::text,
    'nirspec_redshifts'::text,
    'nirspec_spectrum_exposure'::text,
    'nirspec_rate'::text,
    'nirspec_mbkg'::text,
    'rgb'::text,
    'sed'::text,
    'nircam_exposure'::text,
    'nircam_exposure_preview'::text,
    'nircam_exposure_full'::text,
    'nircam_mosaic'::text,
    'nircam_rgb'::text,
    'nircam_expmap'::text,
    'nircam_expmap_plot'::text,
    'nircam_mosaic_thumbnail'::text,
    'nircam_mosaic_quicklook'::text,
    'nircam_layout'::text,
    'tile'::text,
    'photometry_pz'::text,
    'nirspec_manual_mask'::text,
    'nirspec_stuck_shutters'::text,
    'nirspec_bkg_override'::text, 'nircam_mask'::text, 'nircam_astrom_cat'::text, 'nircam_bad_pixel'::text, 'nircam_flat'::text, 'nircam_wisp'::text]))) NOT VALID;

-- Added NOT VALID and validated separately, the shape main's storage_objects
-- migrations use (within one migration's transaction the table stays locked until
-- commit either way; the scan is one pass over product_type).
ALTER TABLE "public"."storage_objects" VALIDATE CONSTRAINT "storage_objects_product_type_check";
