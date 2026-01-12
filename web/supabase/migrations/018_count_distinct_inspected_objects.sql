-- Migration: Add function to count distinct objects inspected by a user
-- This function returns the count of unique objects a user has inspected
-- (based on flag_audit_log entries)

CREATE OR REPLACE FUNCTION count_distinct_inspected_objects(p_user_id UUID)
RETURNS INTEGER
LANGUAGE sql
STABLE
SECURITY DEFINER
AS $$
  SELECT COUNT(DISTINCT object_id)::INTEGER
  FROM flag_audit_log
  WHERE user_id = p_user_id;
$$;

-- Grant execute permission to authenticated users
GRANT EXECUTE ON FUNCTION count_distinct_inspected_objects(UUID) TO authenticated;

COMMENT ON FUNCTION count_distinct_inspected_objects IS
  'Returns the count of distinct objects a user has inspected (made flag changes to)';
