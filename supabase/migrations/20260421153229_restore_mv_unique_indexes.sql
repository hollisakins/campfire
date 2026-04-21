-- Restore unique indexes on mv_filter_options and mv_programs_overview.
--
-- Several recent migrations (20260416191221, 20260417013428, 20260417145348)
-- dropped and recreated these materialized views without recreating their
-- unique indexes, leaving prod in a state where REFRESH MATERIALIZED VIEW
-- CONCURRENTLY fails with:
--   "cannot refresh materialized view ... concurrently"
--   "Create a unique index with no WHERE clause on one or more columns"
--
-- The deploy CLI calls refresh_filter_options() / refresh_programs_overview()
-- at the end of every deploy, both of which use CONCURRENTLY. With the
-- indexes missing, the refreshes soft-fail and the MVs go stale until
-- manually rebuilt.
--
-- Root cause is migra's known MV-tracking limitation (see CLAUDE.md). The
-- canonical index definitions live in supabase/schemas/views.sql — this
-- migration just re-applies them.

CREATE UNIQUE INDEX IF NOT EXISTS mv_filter_options_id
    ON public.mv_filter_options USING btree (id);

CREATE UNIQUE INDEX IF NOT EXISTS mv_programs_overview_slug
    ON public.mv_programs_overview USING btree (slug);
