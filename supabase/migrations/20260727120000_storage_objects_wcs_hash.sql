-- storage_objects.wcs_hash — the astrometric half of the NIRCam exposure
-- identity.
--
-- sci_dq_hash (epic #261, N1) digests only the SCI/DQ/CFMASK arrays, so it is
-- blind to a re-ALIGNMENT: `cfpipe nircam align` (and wcs_shift) rewrite an
-- exposure's WCS without touching a single science pixel. The push planner
-- compared that digest alone, classified re-aligned exposures as "unchanged",
-- and left the cloud copy carrying its pre-alignment astrometry indefinitely.
-- Deploy now requires BOTH digests to match before skipping an upload.
--
-- Existing exposure rows keep wcs_hash NULL. The planner does not stampede on
-- those: when the science digest still matches it compares the local whole-file
-- sha256 against content_hash, and a byte-for-byte match proves the cloud object
-- already carries this WCS, so the row is backfilled in place with no transfer.
--
-- Additive only — no backfill statement here, since the digest can only be
-- computed from the local FITS headers on a reducer machine.

alter table "public"."storage_objects" add column "wcs_hash" text;

alter table "public"."storage_objects"
  add constraint "storage_objects_wcs_hash_check"
  CHECK (((wcs_hash IS NULL) OR (wcs_hash ~ '^sha256:'::text)));

COMMENT ON COLUMN "public"."storage_objects"."wcs_hash" IS 'Astrometric change-detection digest: sha256 over the WCS-defining header cards of a NIRCam canonical exposure. The companion to sci_dq_hash — the align and wcs_shift steps rewrite an exposure''s WCS without touching a SCI or DQ pixel, so a science-only identity skipped re-aligned exposures and left the cloud copy on its pre-alignment astrometry. Never used for download/copy verification. NULL for whole-file-identity products, and on exposure rows written before this column existed (the push planner reconciles those against content_hash and backfills the digest without re-uploading).';

COMMENT ON COLUMN "public"."storage_objects"."sci_dq_hash" IS 'Science-only sha256(SCI+DQ) change-detection digest (epic #261, N1). Lets deploy skip re-uploading a NIRCam canonical exposure whose science is unchanged even though its whole-file content_hash shifted (pipeline re-save bumps header timestamps). Paired with wcs_hash: BOTH must match for deploy to skip an upload, since the array digest alone cannot see a re-alignment. content_hash remains the authoritative whole-file integrity token; this is never used for download/copy verification. NULL for products without a partial digest.';
