-- Migration 014: Fix materialized view concurrent refresh
-- The unique index must be on actual columns, not constant expressions

-- Drop the existing materialized view and its index
DROP MATERIALIZED VIEW IF EXISTS mv_filter_options;

-- Recreate with an actual id column that can be uniquely indexed
CREATE MATERIALIZED VIEW mv_filter_options AS
SELECT
  1 AS id,
  ARRAY(SELECT DISTINCT field FROM objects ORDER BY field) AS fields,
  ARRAY(SELECT DISTINCT observation FROM objects WHERE observation IS NOT NULL ORDER BY observation) AS observations;

-- Create unique index on the id column (required for CONCURRENTLY refresh)
CREATE UNIQUE INDEX mv_filter_options_id ON mv_filter_options (id);

-- Grant select on materialized view to authenticated users
GRANT SELECT ON mv_filter_options TO authenticated;

-- Add comment for documentation
COMMENT ON MATERIALIZED VIEW mv_filter_options IS
  'Cached distinct filter options (fields, observations). Refresh after data deployments using refresh_filter_options()';
