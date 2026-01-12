-- Migration 022: Add RPC function for efficient program stats aggregation
-- Replaces multiple full table scans with single aggregated query

CREATE OR REPLACE FUNCTION get_program_stats()
RETURNS TABLE (
  program_id INTEGER,
  object_count BIGINT,
  user_access_count BIGINT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
AS $$
  SELECT
    p.program_id,
    COALESCE(o.cnt, 0) AS object_count,
    COALESCE(a.cnt, 0) AS user_access_count
  FROM programs p
  LEFT JOIN (
    SELECT program_id, COUNT(*) AS cnt
    FROM objects
    GROUP BY program_id
  ) o ON p.program_id = o.program_id
  LEFT JOIN (
    SELECT program_id, COUNT(*) AS cnt
    FROM user_program_access
    GROUP BY program_id
  ) a ON p.program_id = a.program_id;
$$;

-- Grant execute to authenticated users
GRANT EXECUTE ON FUNCTION get_program_stats() TO authenticated;

COMMENT ON FUNCTION get_program_stats IS
  'Returns object counts and user access counts per program using efficient GROUP BY aggregation';
