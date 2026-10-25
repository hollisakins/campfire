-- Activity feed rows carry subject_kind ('target' | 'object' | 'spectrum') so the
-- admin activity page can link each row to a route that resolves; object-parented comments join the feed.

SET lock_timeout = '5s'; SET statement_timeout = '60s';

SET local check_function_bodies = off;

DROP FUNCTION "public"."get_activity_feed"(boolean, boolean, uuid[], boolean, text[], integer, integer);

CREATE OR REPLACE FUNCTION public.get_activity_feed (
  p_include_comments    boolean DEFAULT true,
  p_include_inspections boolean DEFAULT true,
  p_user_ids            uuid[]  DEFAULT NULL::uuid[],
  p_include_system      boolean DEFAULT false,
  p_field_names         text[]  DEFAULT NULL::text[],
  p_page                integer DEFAULT 1,
  p_page_size           integer DEFAULT 50
)
  RETURNS TABLE (
    id                    text,
    type                  text,
    target_db_id          integer,
    target_display_id     text,
    user_id               uuid,
    ts                    timestamp without time zone,
    content               text,
    edited_at             timestamp without time zone,
    field_name            text,
    old_value             integer,
    new_value             integer,
    user_full_name        text,
    user_is_group_account boolean,
    subject_kind          text,
    total_count           bigint
  )
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_has_user_filter boolean :=
    (p_user_ids IS NOT NULL AND array_length(p_user_ids, 1) > 0) OR p_include_system;
  v_limit integer := LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 100);
  v_offset integer := GREATEST(COALESCE(p_page, 1) - 1, 0) * LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 100);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  RETURN QUERY
  WITH feed AS (
    SELECT
      'comment-' || c.id AS id,
      'comment'::text AS type,
      COALESCE(c.target_id, c.object_id, 0) AS target_db_id,
      COALESCE(t.target_id, oc.object_id, '') AS target_display_id,
      c.user_id,
      c.created_at AS ts,
      c.content,
      c.edited_at,
      NULL::text AS field_name,
      NULL::integer AS old_value,
      NULL::integer AS new_value,
      CASE WHEN c.object_id IS NOT NULL THEN 'object'
           WHEN c.target_id IS NOT NULL THEN 'target' END AS subject_kind
    FROM comments c
    LEFT JOIN targets t ON t.id = c.target_id
    LEFT JOIN objects oc ON oc.id = c.object_id
    WHERE p_include_comments
      AND NOT c.is_deleted
      AND (c.target_id IS NOT NULL OR c.object_id IS NOT NULL)
      AND (NOT v_has_user_filter
           OR c.user_id = ANY(COALESCE(p_user_ids, '{}'::uuid[])))

    UNION ALL

    SELECT
      'audit-' || f.id,
      'inspection'::text,
      COALESCE(f.target_id, f.object_id, f.spectrum_id, 0),
      COALESCE(
        t.target_id,
        o.object_id,
        CASE WHEN s.id IS NOT NULL THEN s.target_id || '/' || s.grating END,
        ''
      ),
      f.user_id,
      f.changed_at,
      NULL::text,
      NULL::timestamp without time zone,
      f.field_name,
      f.old_value,
      f.new_value,
      CASE WHEN f.target_id IS NOT NULL THEN 'target'
           WHEN f.object_id IS NOT NULL THEN 'object'
           WHEN f.spectrum_id IS NOT NULL THEN 'spectrum' END
    FROM flag_audit_log f
    LEFT JOIN targets t ON t.id = f.target_id
    LEFT JOIN objects o ON o.id = f.object_id
    LEFT JOIN spectra s ON s.id = f.spectrum_id
    WHERE p_include_inspections
      AND (p_field_names IS NULL OR f.field_name = ANY(p_field_names))
      AND (NOT v_has_user_filter
           OR f.user_id = ANY(COALESCE(p_user_ids, '{}'::uuid[]))
           OR (p_include_system AND f.user_id IS NULL))
  )
  SELECT
    feed.id,
    feed.type,
    feed.target_db_id,
    feed.target_display_id,
    feed.user_id,
    feed.ts,
    feed.content,
    feed.edited_at,
    feed.field_name,
    feed.old_value,
    feed.new_value,
    up.full_name,
    COALESCE(up.is_group_account, false),
    feed.subject_kind,
    count(*) OVER ()
  FROM feed
  LEFT JOIN user_profiles up ON up.user_id = feed.user_id
  ORDER BY feed.ts DESC, feed.id DESC
  OFFSET v_offset
  LIMIT v_limit;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."get_activity_feed"(boolean, boolean, uuid[], boolean, text[], integer, integer) FROM PUBLIC, "anon";

GRANT EXECUTE ON FUNCTION "public"."get_activity_feed"(boolean, boolean, uuid[], boolean, text[], integer, integer) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."get_activity_feed"(boolean, boolean, uuid[], boolean, text[], integer, integer) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."get_activity_feed"(boolean, boolean, uuid[], boolean, text[], integer, integer) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."get_activity_feed"(boolean, boolean, uuid[], boolean, text[], integer, integer) TO "service_role";
