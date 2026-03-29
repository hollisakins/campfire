-- Fix policies that excluded public programs from write/comment operations.
-- Users with can_comment should be able to inspect and comment on any object
-- they can see, including objects in public programs.

-- objects UPDATE: use accessible_program_slugs() (includes public programs)
DROP POLICY IF EXISTS "update_objects_by_access" ON objects;
CREATE POLICY "update_objects_by_access" ON objects FOR UPDATE USING (
  program_slug = ANY(public.accessible_program_slugs())
  AND public.can_comment()
);

-- comments SELECT: include public programs so comments are visible
DROP POLICY IF EXISTS "select_comments_by_access" ON comments;
CREATE POLICY "select_comments_by_access" ON comments FOR SELECT USING (
  object_id IN (
    SELECT o.id FROM objects o
    WHERE o.program_slug = ANY(public.accessible_program_slugs())
  )
);

-- comments INSERT: include public programs for users with can_comment
DROP POLICY IF EXISTS "insert_comments_by_access" ON comments;
CREATE POLICY "insert_comments_by_access" ON comments FOR INSERT WITH CHECK (
  object_id IN (
    SELECT o.id FROM objects o
    WHERE o.program_slug = ANY(public.accessible_program_slugs())
  )
  AND public.can_comment()
);

-- flag_audit_log SELECT: also include public programs for consistency
DROP POLICY IF EXISTS "select_audit_by_access" ON flag_audit_log;
CREATE POLICY "select_audit_by_access" ON flag_audit_log FOR SELECT USING (
  object_id IN (
    SELECT o.id FROM objects o
    WHERE o.program_slug = ANY(public.accessible_program_slugs())
  )
);
