SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Hand-authored (the local stack was not started on the authoring machine, so
-- no `supabase db schema declarative sync`); the CHECK is copied from
-- supabase/schemas/tables.sql, which remains the source of truth. Same form as
-- 20260910050000_storage_objects_line_product_types.sql. Run the `database`
-- check before merging: it has not been run against this file.
--
-- The storage_objects product-type CHECK tracks the campfire_layout PRODUCTS
-- registry (every entry with a non-null bucket). The HST imaging instruments
-- add ten kinds there: for each of acswfc and wfc3ir, the canonical exposure,
-- its two review PNGs, the mosaic, and the hand-drawn mask
-- (layout/campfire_layout/products.py). Nothing uploads them yet; without
-- this the first deploy that does would upload the bytes and then fail the
-- registry upsert with a 23514 check violation, and the guard test
-- python/tests/test_registry.py fails as soon as the kinds exist.
--
-- NOT VALID then VALIDATE: the new constraint admits every row the old one
-- did, and validating separately takes only a SHARE UPDATE EXCLUSIVE lock for
-- the scan.

alter table "public"."storage_objects" drop constraint "storage_objects_product_type_check";

alter table "public"."storage_objects" add constraint "storage_objects_product_type_check" CHECK ((product_type = ANY (ARRAY['nirspec_spec'::text, 'spectrum_json'::text, 'spectrum_1d_json'::text, 'zfit'::text, 'nirspec_lines'::text, 'nirspec_lines_json'::text, 'nirspec_redshifts'::text, 'nirspec_spectrum_exposure'::text, 'nirspec_rate'::text, 'rgb'::text, 'sed'::text, 'nircam_exposure'::text, 'nircam_exposure_preview'::text, 'nircam_exposure_full'::text, 'nircam_mosaic'::text, 'nircam_rgb'::text, 'nircam_expmap'::text, 'nircam_expmap_plot'::text, 'nircam_mosaic_thumbnail'::text, 'nircam_mosaic_quicklook'::text, 'nircam_layout'::text, 'tile'::text, 'photometry_pz'::text, 'nirspec_manual_mask'::text, 'nirspec_stuck_shutters'::text, 'nirspec_bkg_override'::text, 'nircam_mask'::text, 'nircam_astrom_cat'::text, 'nircam_bad_pixel'::text, 'nircam_flat'::text, 'nircam_wisp'::text, 'acswfc_exposure'::text, 'acswfc_exposure_preview'::text, 'acswfc_exposure_full'::text, 'acswfc_mosaic'::text, 'acswfc_mask'::text, 'wfc3ir_exposure'::text, 'wfc3ir_exposure_preview'::text, 'wfc3ir_exposure_full'::text, 'wfc3ir_mosaic'::text, 'wfc3ir_mask'::text]))) not valid;

alter table "public"."storage_objects" validate constraint "storage_objects_product_type_check";
