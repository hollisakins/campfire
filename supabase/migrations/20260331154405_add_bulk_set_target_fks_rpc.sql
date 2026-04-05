set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.bulk_set_target_object_fks(p_pairs jsonb, p_updated_at timestamp with time zone DEFAULT now())
 RETURNS void
 LANGUAGE plpgsql
AS $function$
BEGIN
  UPDATE targets t SET
    object_id = (pair->>'object_id')::integer,
    updated_at = p_updated_at
  FROM jsonb_array_elements(p_pairs) AS pair
  WHERE t.id = (pair->>'target_id')::integer;
END;
$function$
;

GRANT EXECUTE ON FUNCTION public.bulk_set_target_object_fks(JSONB, TIMESTAMPTZ) TO service_role;
