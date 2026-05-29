-- Guard: every materialized view in the public schema must have a unique index.
--
-- REFRESH MATERIALIZED VIEW CONCURRENTLY requires a unique index on the view.
-- migra (the engine behind `supabase db diff`) does not track materialized-view
-- indexes, so any migra-generated migration that drops + recreates an MV will
-- silently strip its unique index, breaking concurrent refresh in production.
-- This has regressed twice already (restored by 20260421153229 and
-- 20260529120000). This assertion catches the next recurrence at PR time.
--
-- Run locally:
--   eval "$(supabase status -o env | grep '^DB_URL=')"
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/check_mv_unique_indexes.sql
DO $$
DECLARE
  missing text;
BEGIN
  SELECT string_agg(mv.matviewname, ', ' ORDER BY mv.matviewname)
  INTO missing
  FROM pg_matviews mv
  JOIN pg_class c
    ON c.relname = mv.matviewname
   AND c.relnamespace = 'public'::regnamespace
  WHERE mv.schemaname = 'public'
    AND NOT EXISTS (
      SELECT 1 FROM pg_index i
      WHERE i.indrelid = c.oid
        AND i.indisunique
    );

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION
      'Materialized view(s) lack a unique index (REFRESH ... CONCURRENTLY will fail): %', missing
      USING HINT = 'Add CREATE UNIQUE INDEX in supabase/schemas/views.sql AND a migration to restore it.';
  END IF;

  RAISE NOTICE 'OK: all public materialized views have a unique index.';
END $$;
