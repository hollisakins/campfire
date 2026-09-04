-- Perf T1-5 (#501, epic #515): tighter autovacuum on the two churny tables.
--
-- Hand-authored: no local Docker for `supabase db diff`. Matches
-- supabase/schemas/tables.sql.
--
-- storage_objects sat at 14 % dead tuples (109 k) with its last autovacuum
-- nine days old; object_photometry 11 % / six weeks. A one-off
-- VACUUM (ANALYZE) on both follows outside this migration (VACUUM cannot run
-- inside a transaction block).

ALTER TABLE public.storage_objects SET (autovacuum_vacuum_scale_factor = 0.02);
ALTER TABLE public.object_photometry SET (autovacuum_vacuum_scale_factor = 0.02);
