-- storage_objects.stored_size_bytes: bytes as stored in the bucket for
-- transport-compressed products (gzipped mosaic FITS, PR #383); NULL = stored
-- verbatim. size_bytes stays the LOGICAL (uncompressed) size that client-side
-- dedup and the stat fast-path compare against local plain files.
--
-- NOTE: the generated diff also contained the usual spurious migra
-- drop/recreate churn for mv_filter_options, mv_programs_overview,
-- nircam_reduction_progress and spectrum_flag_summary (migra cannot track
-- (mat)views); stripped here, only the intended column kept.

alter table "public"."storage_objects" add column "stored_size_bytes" bigint;
