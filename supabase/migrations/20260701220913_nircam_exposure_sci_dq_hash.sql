-- epic #261, N1 — NIRCam canonical exposures on OSN.
--
-- Adds a science-only change-detection digest to the storage registry. content_hash
-- stays the authoritative whole-file sha256 (download/copy verification), but a
-- NIRCam canonical exposure re-saved by the pipeline gets fresh header timestamps
-- that shift the whole-file hash even when SCI+DQ are unchanged — so deploy compares
-- this stable sha256(SCI+DQ) to skip re-uploading a science-identical exposure (D1).
-- Additive + nullable (NULL for every existing row and every product without a
-- partial digest), so no backfill and no seed change.
--
-- The generated diff also drop+recreated four unrelated views/matviews
-- (mv_filter_options, mv_programs_overview, nircam_reduction_progress,
-- spectrum_flag_summary) — a known migra limitation (it re-serializes all views on
-- any schema change; none reference storage_objects). Stripped: those definitions
-- are unchanged, so the drop+recreate is a no-op that would needlessly churn the
-- materialized views.

alter table "public"."storage_objects" add column "sci_dq_hash" text;

alter table "public"."storage_objects"
  add constraint "storage_objects_sci_dq_hash_check"
  CHECK (((sci_dq_hash IS NULL) OR (sci_dq_hash ~ '^sha256:'::text)));
