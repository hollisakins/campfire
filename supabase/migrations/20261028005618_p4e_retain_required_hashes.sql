SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.retain_required_hashes (
  p_hashes text[]
)
  RETURNS text[]
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
BEGIN
  PERFORM dr_require_admin();
  IF p_hashes IS NULL OR cardinality(p_hashes) = 0 THEN
    RETURN '{}'::text[];
  END IF;
  IF cardinality(p_hashes) > 2000 THEN
    RAISE EXCEPTION 'retain_required_hashes: at most 2000 hashes per call (got %)', cardinality(p_hashes);
  END IF;
  RETURN COALESCE((
    SELECT array_agg(DISTINCT m.content_hash ORDER BY m.content_hash)
    FROM dr_manifest m
    WHERE m.content_hash = ANY (p_hashes)
      -- the explicit list matches idx_dr_manifest_unarchived_hash's predicate
      AND m.byte_state IN ('pending', 'failed')
  ), '{}'::text[]);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."retain_required_hashes"(text[]) FROM PUBLIC, "anon";

CREATE INDEX idx_dr_manifest_unarchived_hash ON public.dr_manifest USING btree (content_hash)
  WHERE (byte_state = ANY (ARRAY['pending'::text, 'failed'::text]));

COMMENT ON FUNCTION "public"."retain_required_hashes"(text[]) IS 'Retain-on-supersede (P4e, D-P2-4): of the given content hashes, those a data release has frozen (dr_manifest) and its byte pass has not archived yet (byte_state pending or failed). A deploy copies a live object with such a hash into the archive before overwriting it; every other overwrite needs no copy. Admin or service_role.';

GRANT EXECUTE ON FUNCTION "public"."retain_required_hashes"(text[]) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."retain_required_hashes"(text[]) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."retain_required_hashes"(text[]) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."retain_required_hashes"(text[]) TO "service_role";
