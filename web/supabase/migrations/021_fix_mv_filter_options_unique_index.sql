-- Migration 021: Fix mv_filter_options unique index for concurrent refresh
-- Migration 020 regressed by removing the id column that migration 014 added
-- PostgreSQL requires a unique index on actual columns (not constant expressions) for CONCURRENTLY

-- Drop and recreate the materialized view with proper id column
DROP MATERIALIZED VIEW IF EXISTS mv_filter_options;

CREATE MATERIALIZED VIEW mv_filter_options AS
SELECT
  1 AS id,
  ARRAY(SELECT DISTINCT field FROM objects ORDER BY field) AS fields,
  ARRAY(SELECT DISTINCT observation FROM objects WHERE observation IS NOT NULL ORDER BY observation) AS observations,
  ARRAY(SELECT DISTINCT grating FROM spectra ORDER BY grating) AS gratings;

-- Create unique index on actual column (required for REFRESH MATERIALIZED VIEW CONCURRENTLY)
CREATE UNIQUE INDEX mv_filter_options_id ON mv_filter_options (id);

-- Grant select on materialized view to authenticated users
GRANT SELECT ON mv_filter_options TO authenticated;

COMMENT ON MATERIALIZED VIEW mv_filter_options IS
  'Cached distinct filter options (fields, observations, gratings). Refresh after data deployments using refresh_filter_options()';
