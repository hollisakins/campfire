-- Migration 020: Add gratings to mv_filter_options materialized view
-- This eliminates the full table scan on spectra for distinct gratings

-- Drop and recreate the materialized view with gratings included
DROP MATERIALIZED VIEW IF EXISTS mv_filter_options;

CREATE MATERIALIZED VIEW mv_filter_options AS
SELECT
  ARRAY(SELECT DISTINCT field FROM objects ORDER BY field) AS fields,
  ARRAY(SELECT DISTINCT observation FROM objects WHERE observation IS NOT NULL ORDER BY observation) AS observations,
  ARRAY(SELECT DISTINCT grating FROM spectra ORDER BY grating) AS gratings;

-- Recreate unique index (required for REFRESH MATERIALIZED VIEW CONCURRENTLY)
CREATE UNIQUE INDEX mv_filter_options_single_row ON mv_filter_options ((1));

-- Grant select on materialized view to authenticated users
GRANT SELECT ON mv_filter_options TO authenticated;

-- Update the refresh function to include a comment about gratings
COMMENT ON MATERIALIZED VIEW mv_filter_options IS
  'Cached distinct filter options (fields, observations, gratings). Refresh after data deployments using refresh_filter_options()';
