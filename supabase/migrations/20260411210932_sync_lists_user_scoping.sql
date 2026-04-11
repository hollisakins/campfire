drop function if exists "public"."get_lists_for_sync"();

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_lists_for_sync(p_user_id uuid DEFAULT NULL::uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE
AS $function$
BEGIN
  RETURN COALESCE(
    (SELECT jsonb_agg(jsonb_build_object(
      'id', ol.id,
      'slug', ol.slug,
      'name', ol.name,
      'description', ol.description,
      'visibility', ol.visibility,
      'is_system', ol.is_system,
      'created_by', ol.created_by,
      'created_at', ol.created_at,
      'updated_at', ol.updated_at,
      'member_count', (SELECT COUNT(*) FROM object_list_members olm WHERE olm.list_id = ol.id)
    ) ORDER BY ol.is_system DESC, ol.name)
    FROM object_lists ol
    WHERE ol.created_by = p_user_id
       OR ol.visibility IN ('public_read', 'public_edit')),
    '[]'::jsonb
  );
END;
$function$
;

GRANT EXECUTE ON FUNCTION public.get_lists_for_sync(UUID) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_lists_for_sync(UUID) TO service_role;
