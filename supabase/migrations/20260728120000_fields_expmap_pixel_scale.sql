-- fields.expmap_pixel_scale_arcsec — the pixel scale of a field's exposure-map
-- grid, so the NIRCam product table can render a Scale for expmap rows instead
-- of an em-dash. Deploy-owned: written from <field>_layout.json alongside
-- coverage_area_* (see python/campfire/deploy/fields.py).
--
-- One value per field rather than per object: expmap builds a single shared
-- auto-WCS across every filter in an invocation, so a field's expmap FITS are
-- all on the same grid.

ALTER TABLE "public"."fields"
  ADD COLUMN IF NOT EXISTS "expmap_pixel_scale_arcsec" double precision;

-- Backfill already-deployed fields. Every expmap in the registry predates this
-- column and was built with the cfpipe default (`cfpipe nircam expmap
-- --pixel-scale`, default 0.5 since the command was introduced — no release
-- ever shipped another value and nothing in the repo passes the flag). Scoped
-- to fields that actually have an active expmap so a field with none stays
-- NULL and picks up its real value on its next deploy.
UPDATE "public"."fields" f
   SET "expmap_pixel_scale_arcsec" = 0.5
 WHERE f."expmap_pixel_scale_arcsec" IS NULL
   AND EXISTS (
     SELECT 1 FROM "public"."storage_objects" so
      WHERE so."product_type" = 'nircam_expmap'
        AND so."status" = 'active'
        AND so."field" = f."name"
   );
