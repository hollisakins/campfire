-- Migration 013: Add materialized view for filter options caching
-- This replaces the RPC function approach with a cached view that's refreshed after deployments

-- Drop the RPC function we created in migration 012 (no longer needed)
DROP FUNCTION IF EXISTS get_distinct_filter_options(INTEGER[]);

-- Create materialized view with all filter options
-- Note: This caches ALL fields/observations; access control happens in application layer
CREATE MATERIALIZED VIEW mv_filter_options AS
SELECT
  ARRAY(SELECT DISTINCT field FROM objects ORDER BY field) AS fields,
  ARRAY(SELECT DISTINCT observation FROM objects WHERE observation IS NOT NULL ORDER BY observation) AS observations;

-- Create unique index (required for REFRESH MATERIALIZED VIEW CONCURRENTLY)
CREATE UNIQUE INDEX mv_filter_options_single_row ON mv_filter_options ((1));

-- Create refresh function callable from deploy script
CREATE OR REPLACE FUNCTION refresh_filter_options()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY mv_filter_options;
END;
$$;

-- Grant execute to authenticated users (for admin/deploy refresh)
GRANT EXECUTE ON FUNCTION refresh_filter_options() TO authenticated;

-- Grant select on materialized view to authenticated users
GRANT SELECT ON mv_filter_options TO authenticated;

-- Add comments for documentation
COMMENT ON MATERIALIZED VIEW mv_filter_options IS
  'Cached distinct filter options (fields, observations). Refresh after data deployments using refresh_filter_options()';

COMMENT ON FUNCTION refresh_filter_options IS
  'Refreshes the mv_filter_options materialized view. Call after deploying new data.';
