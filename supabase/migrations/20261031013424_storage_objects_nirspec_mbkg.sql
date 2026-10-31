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
    'nirspec_bkg_override'::text, 'nircam_mask'::text, 'nircam_astrom_cat'::text, 'nircam_bad_pixel'::text, 'nircam_flat'::text, 'nircam_wisp'::text])));
