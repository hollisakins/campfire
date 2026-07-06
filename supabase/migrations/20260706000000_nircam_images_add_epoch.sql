-- Add the mosaic `epoch` axis (subset mosaics: fields.toml [<field>.epochs.<name>]).
--
-- An epoch names a subset of a field's exposures built into its own mosaic
-- (e.g. epoch 'CW' = Cosmos Web only), selected at combine time via
-- `cfpipe nircam combine --epoch <name>` and stamped as a trailing filename
-- segment. The full-field mosaic keeps epoch = ''.
--
-- `epoch` is added to the version-free UNIQUE so a subset mosaic gets its own
-- row instead of colliding with the full-field mosaic of the same
-- (field, tile, filter, pixel_scale, extension). It is NOT NULL DEFAULT ''
-- (never NULL) because Postgres treats NULLs as distinct in a UNIQUE index,
-- which would defeat dedup of full-field mosaics. Widening the key with a
-- column that defaults to '' cannot create duplicate rows, so unlike the
-- version-retirement migration no row-collapse is needed. The seed has no
-- nircam_images rows, so no seed change is needed.

alter table "public"."nircam_images"
  add column "epoch" text not null default '';

alter table "public"."nircam_images" drop constraint "nircam_images_unique";

create unique index nircam_images_unique
  on public.nircam_images using btree (field, tile, filter, pixel_scale, extension, epoch);

alter table "public"."nircam_images"
  add constraint "nircam_images_unique" unique using index "nircam_images_unique";
