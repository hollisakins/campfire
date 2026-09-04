-- Perf T2-D2 (#508, epic #515, decision D-D): zfit scalars on spectra.
--
-- Hand-authored: no local Docker for `supabase db diff`. Column definitions
-- copied verbatim from supabase/schemas/tables.sql (the source of truth).
--
-- Why. The object page's redshift-fit summary downloaded every spectrum's
-- whole zfit sidecar (p50 56 kB, p90 515 kB) to show three scalars. Deploy
-- now writes them onto the row from the summary ECSV; existing rows are
-- backfilled from the zfit JSON by scripts/backfill_zfit_scalars.py.

ALTER TABLE "public"."spectra"
  ADD COLUMN IF NOT EXISTS "chi2_min" double precision,
  ADD COLUMN IF NOT EXISTS "confidence" double precision;
