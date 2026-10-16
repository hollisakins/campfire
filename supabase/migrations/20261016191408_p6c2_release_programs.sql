SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.cat_program_slugs()
  RETURNS text[]
  LANGUAGE sql
  STABLE
  SET search_path TO 'public'
  AS $function$
  SELECT COALESCE(ARRAY(
    SELECT DISTINCT s.program_slug FROM public.cat_spectra s
    WHERE s.deploy_status = 'published'
    ORDER BY s.program_slug), '{}'::text[]);
$function$;

REVOKE ALL ON FUNCTION "public"."cat_program_slugs"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.get_programs_overview()
  RETURNS TABLE (
    slug            text,
    program_name    text,
    pi_name         text,
    description     text,
    is_public       boolean,
    cycle           integer,
    target_count    bigint,
    gratings        text[],
    fields          text[],
    observations    text[],
    jwst_pids       integer[],
    n_observations  bigint,
    last_reduced_at timestamp with time zone
  )
  LANGUAGE sql
  STABLE
  SET search_path TO 'public'
  AS $function$
  SELECT mv.slug, mv.program_name, mv.pi_name, mv.description, mv.is_public, mv.cycle,
    mv.target_count, mv.gratings, mv.fields, mv.observations, mv.jwst_pids,
    mv.n_observations, mv.last_reduced_at
  FROM public.mv_programs_overview mv
  WHERE public.dr_browse_tag() IS NULL
  UNION ALL
  SELECT p.slug, p.program_name, p.pi_name, p.description, p.is_public, p.cycle,
    st.target_count,
    COALESCE(st.gratings, ARRAY[]::text[]),
    COALESCE(st.fields, ARRAY[]::text[]),
    COALESCE(st.observations, ARRAY[]::text[]),
    COALESCE(pids.jwst_pids, ARRAY[]::integer[]),
    COALESCE(pids.n_observations, 0)::bigint,
    st.last_reduced_at
  FROM public.programs p
  JOIN (
    SELECT t.program_slug,
      COUNT(DISTINCT t.target_id)::bigint AS target_count,
      ARRAY_AGG(DISTINCT s.grating ORDER BY s.grating) FILTER (WHERE s.grating IS NOT NULL) AS gratings,
      ARRAY_AGG(DISTINCT t.field ORDER BY t.field) AS fields,
      ARRAY_AGG(DISTINCT t.observation ORDER BY t.observation) AS observations,
      MAX(s.reduced_at) AS last_reduced_at
    FROM public.cat_targets t
    LEFT JOIN public.cat_spectra s ON s.target_id = t.target_id AND s.deploy_status = 'published'
    GROUP BY t.program_slug
  ) st ON st.program_slug = p.slug
  LEFT JOIN (
    SELECT o.program_slug,
      ARRAY_AGG(DISTINCT o.jwst_program_id ORDER BY o.jwst_program_id) AS jwst_pids,
      COUNT(*)::bigint AS n_observations
    FROM public.observations o
    WHERE o.name IN (SELECT t2.observation FROM public.cat_targets t2)
    GROUP BY o.program_slug
  ) pids ON pids.program_slug = p.slug
  WHERE public.dr_browse_tag() IS NOT NULL
  ORDER BY program_name;
$function$;

GRANT EXECUTE ON FUNCTION "public"."cat_program_slugs"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."cat_program_slugs"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."cat_program_slugs"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."cat_program_slugs"() TO "service_role";
