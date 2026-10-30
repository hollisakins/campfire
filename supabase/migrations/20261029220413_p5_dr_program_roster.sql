SET lock_timeout = '5s';
SET statement_timeout = '60s';
SET local check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.dr_program_roster (
  p_tag           text,
  p_program_slugs text[]
)
  RETURNS jsonb
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
  SELECT COALESCE(jsonb_object_agg(t.program_slug, jsonb_build_object(
           'is_public', t.is_public, 'spectra', t.n,
           'cfpipe_versions', t.versions, 'crds_contexts', t.contexts)), '{}'::jsonb)
  FROM (SELECT s.program_slug, bool_or(s.is_public) AS is_public, count(*) AS n,
               COALESCE(jsonb_agg(DISTINCT s.cfpipe_version) FILTER (WHERE s.cfpipe_version IS NOT NULL), '[]'::jsonb) AS versions,
               COALESCE(jsonb_agg(DISTINCT s.crds_context) FILTER (WHERE s.crds_context IS NOT NULL), '[]'::jsonb) AS contexts
        FROM dr_spectra s
        WHERE s.tag = p_tag
        GROUP BY s.program_slug
        HAVING bool_or(s.is_public) OR s.program_slug = ANY (COALESCE(p_program_slugs, '{}'::text[]))) t;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_program_roster"(text, text[]) FROM PUBLIC, "anon", "authenticated";

COMMENT ON FUNCTION "public"."dr_program_roster"(text, text[]) IS 'The per-program roster of a frozen release as one caller may see it (P5-3, campfire cite): programs public at the freeze plus the caller''s own among the proprietary ones, each with spectra, cfpipe_versions and crds_contexts. Computed per call; never stored on dr_releases, which every account reads whole.';

REVOKE ALL ON FUNCTION "public"."dr_program_roster"(text, text[]) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_program_roster"(text, text[]) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_program_roster"(text, text[]) TO "service_role";
