-- epic #261, N2 / D3 — retire the mosaic `version` axis.
--
-- One logical mosaic per (field, tile, filter, pixel_scale, extension). The pipeline
-- now emits a single canonical mosaic name per slot and re-combine overwrites it in
-- place, so `nircam_images.version` and its two (redundant) version-bearing unique
-- constraints are dropped and replaced by a single version-free UNIQUE. The seed has
-- no nircam_images rows, so no seed change is needed.
--
-- The generated diff also drop+recreated four unrelated views/matviews
-- (mv_filter_options, mv_programs_overview, nircam_reduction_progress,
-- spectrum_flag_summary) — a known migra limitation (it re-serializes all views on
-- any schema change). None reference nircam_images.version (nircam_reduction_progress
-- reads nircam_exposures), so their definitions are unchanged and the drop+recreate
-- is stripped.

alter table "public"."nircam_images"
  drop constraint "nircam_images_field_tile_filter_pixel_scale_version_extensi_key";

alter table "public"."nircam_images" drop constraint "nircam_images_unique";

alter table "public"."nircam_images" drop column "version";

-- Collapse pre-existing multi-version rows before the version-free UNIQUE, else the
-- index build aborts on production (older reductions kept a row per version, so a
-- slot can hold >1 row once `version` is dropped). Keep the newest (highest id) per
-- (field, tile, filter, pixel_scale, extension). No-op on a fresh DB (0 rows).
DELETE FROM public.nircam_images a
  USING public.nircam_images b
 WHERE a.field = b.field AND a.tile = b.tile AND a.filter = b.filter
   AND a.pixel_scale = b.pixel_scale AND a.extension = b.extension
   AND a.id < b.id;

create unique index nircam_images_unique
  on public.nircam_images using btree (field, tile, filter, pixel_scale, extension);

alter table "public"."nircam_images"
  add constraint "nircam_images_unique" unique using index "nircam_images_unique";
