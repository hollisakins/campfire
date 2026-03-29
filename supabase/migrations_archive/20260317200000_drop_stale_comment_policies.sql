-- Drop stale permissive comment policies from the original schema.
-- These were not dropped in the multi_pid migration (20260316), causing them
-- to OR with the new access-based policies and effectively bypassing
-- program-level access control on comments.

DROP POLICY IF EXISTS "Allow authenticated users to read comments" ON comments;
DROP POLICY IF EXISTS "Allow authenticated users to insert comments" ON comments;
