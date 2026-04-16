drop materialized view if exists "public"."mv_filter_options";

drop materialized view if exists "public"."mv_programs_overview";

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.compute_object_redshift_auto(p_field text)
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE
  n INTEGER;
BEGIN
  UPDATE objects o
  SET redshift_auto = (
    SELECT s.redshift_auto
    FROM targets t
    JOIN spectra s ON s.target_id = t.target_id
    WHERE t.object_id = o.id
      AND s.redshift_auto IS NOT NULL
    ORDER BY s.signal_to_noise DESC NULLS LAST, s.id ASC
    LIMIT 1
  )
  WHERE o.field = p_field;

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$function$
;

GRANT EXECUTE ON FUNCTION public.compute_object_redshift_auto(TEXT) TO service_role;

create materialized view "public"."mv_filter_options" as  SELECT 1 AS id,
    ARRAY( SELECT DISTINCT targets.field
           FROM public.targets
          ORDER BY targets.field) AS fields,
    ARRAY( SELECT DISTINCT targets.observation
           FROM public.targets
          WHERE (targets.observation IS NOT NULL)
          ORDER BY targets.observation) AS observations,
    ARRAY( SELECT DISTINCT spectra.grating
           FROM public.spectra
          ORDER BY spectra.grating) AS gratings;


create materialized view "public"."mv_programs_overview" as  SELECT p.slug,
    p.program_name,
    p.pi_name,
    p.description,
    p.is_public,
    p.cycle,
    COALESCE(stats.target_count, (0)::bigint) AS target_count,
    COALESCE(stats.gratings, ARRAY[]::text[]) AS gratings,
    COALESCE(stats.fields, ARRAY[]::text[]) AS fields,
    COALESCE(stats.observations, ARRAY[]::text[]) AS observations,
    COALESCE(pids.jwst_pids, ARRAY[]::integer[]) AS jwst_pids
   FROM ((public.programs p
     LEFT JOIN ( SELECT t.program_slug,
            count(DISTINCT t.target_id) AS target_count,
            array_agg(DISTINCT s.grating ORDER BY s.grating) FILTER (WHERE (s.grating IS NOT NULL)) AS gratings,
            array_agg(DISTINCT t.field ORDER BY t.field) AS fields,
            array_agg(DISTINCT t.observation ORDER BY t.observation) AS observations
           FROM (public.targets t
             LEFT JOIN public.spectra s ON ((s.target_id = t.target_id)))
          GROUP BY t.program_slug) stats ON ((p.slug = stats.program_slug)))
     LEFT JOIN ( SELECT observations.program_slug,
            array_agg(DISTINCT observations.jwst_program_id ORDER BY observations.jwst_program_id) AS jwst_pids
           FROM public.observations
          GROUP BY observations.program_slug) pids ON ((p.slug = pids.program_slug)));



