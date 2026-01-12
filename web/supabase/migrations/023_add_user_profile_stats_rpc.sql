-- Migration 023: Add RPC function for efficient user profile stats
-- Combines multiple sequential queries into a single database round-trip

CREATE OR REPLACE FUNCTION get_user_profile_stats(p_user_id UUID)
RETURNS JSON
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
AS $$
DECLARE
  result JSON;
  objects_inspected BIGINT;
  comments_posted BIGINT;
  last_comment_at TIMESTAMPTZ;
  last_inspection_at TIMESTAMPTZ;
  last_activity TIMESTAMPTZ;
BEGIN
  -- Get distinct objects inspected count
  SELECT COUNT(DISTINCT object_id) INTO objects_inspected
  FROM flag_audit_log
  WHERE user_id = p_user_id;

  -- Get comments count
  SELECT COUNT(*) INTO comments_posted
  FROM comments
  WHERE user_id = p_user_id AND is_deleted = false;

  -- Get last comment timestamp
  SELECT created_at INTO last_comment_at
  FROM comments
  WHERE user_id = p_user_id
  ORDER BY created_at DESC
  LIMIT 1;

  -- Get last inspection timestamp
  SELECT changed_at INTO last_inspection_at
  FROM flag_audit_log
  WHERE user_id = p_user_id
  ORDER BY changed_at DESC
  LIMIT 1;

  -- Determine most recent activity
  last_activity := GREATEST(
    COALESCE(last_comment_at, '1970-01-01'::timestamptz),
    COALESCE(last_inspection_at, '1970-01-01'::timestamptz)
  );
  IF last_activity = '1970-01-01'::timestamptz THEN
    last_activity := NULL;
  END IF;

  -- Build result JSON
  result := json_build_object(
    'objects_inspected', COALESCE(objects_inspected, 0),
    'comments_posted', COALESCE(comments_posted, 0),
    'last_activity', last_activity
  );

  RETURN result;
END;
$$;

-- Grant execute to authenticated users
GRANT EXECUTE ON FUNCTION get_user_profile_stats(UUID) TO authenticated;

COMMENT ON FUNCTION get_user_profile_stats IS
  'Returns aggregated user stats (objects inspected, comments posted, last activity) in a single call';
