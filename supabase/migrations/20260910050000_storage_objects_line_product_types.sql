-- Hand-authored (no Docker on the authoring machine, so no `supabase db diff`);
-- the CHECK is copied from supabase/schemas/tables.sql, which remains the
-- source of truth. Same form as 20260712182144_add_nircam_mosaic_quicklook.sql.
--
-- The storage_objects product-type CHECK tracks the campfire_layout PRODUCTS
-- registry (every entry with a non-null bucket) and was missed by the
-- emission-line PRs: `nirspec_lines` (_lines.fits, cfpipe nirspec linefit,
-- #551) and `nirspec_redshifts` (reference/nirspec/<obs>/redshifts.toml, the
-- inspected-redshift materialization, #551) were registered in the layout
-- without an entry here, and `nirspec_lines_json` (the _lines.json sidecar the
-- spectrum plot's "Lines" overlay fetches) joins them now. Without this, a
-- deploy carrying line fits uploads the bytes and then fails the registry
-- upsert with a 23514 check violation, so nothing in that batch is registered
-- and the sidecar resolver reports the products absent. A guard test
-- (python/tests/test_registry.py) now diffs the registry against this CHECK.

alter table "public"."storage_objects" drop constraint "storage_objects_product_type_check";

alter table "public"."storage_objects" add constraint "storage_objects_product_type_check" CHECK ((product_type = ANY (ARRAY['nirspec_spec'::text, 'spectrum_json'::text, 'spectrum_1d_json'::text, 'zfit'::text, 'nirspec_lines'::text, 'nirspec_lines_json'::text, 'nirspec_redshifts'::text, 'nirspec_spectrum_exposure'::text, 'nirspec_rate'::text, 'rgb'::text, 'sed'::text, 'nircam_exposure'::text, 'nircam_exposure_preview'::text, 'nircam_exposure_full'::text, 'nircam_mosaic'::text, 'nircam_rgb'::text, 'nircam_expmap'::text, 'nircam_expmap_plot'::text, 'nircam_mosaic_thumbnail'::text, 'nircam_mosaic_quicklook'::text, 'nircam_layout'::text, 'tile'::text, 'photometry_pz'::text, 'nirspec_manual_mask'::text, 'nirspec_stuck_shutters'::text, 'nirspec_bkg_override'::text, 'nircam_mask'::text, 'nircam_astrom_cat'::text, 'nircam_bad_pixel'::text, 'nircam_flat'::text, 'nircam_wisp'::text]))) not valid;

alter table "public"."storage_objects" validate constraint "storage_objects_product_type_check";
