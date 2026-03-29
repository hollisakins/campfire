drop policy "insert_comments_by_access" on "public"."comments";

drop policy "select_comments_by_access" on "public"."comments";

drop policy "insert_audit_by_access" on "public"."flag_audit_log";

drop policy "select_audit_by_access" on "public"."flag_audit_log";

drop policy "Users can view own reset logs" on "public"."password_reset_log";

drop policy "select_spectra_by_access" on "public"."spectra";

drop materialized view if exists "public"."mv_programs_overview";

drop view if exists "public"."target_flag_summary";

drop view if exists "public"."targets_with_flags";

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.count_distinct_inspected_objects(p_user_id uuid)
 RETURNS integer
 LANGUAGE sql
 STABLE SECURITY DEFINER
AS $function$
  SELECT COUNT(DISTINCT target_id)::INTEGER
  FROM flag_audit_log
  WHERE user_id = p_user_id;
$function$
;

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

CREATE UNIQUE INDEX mv_programs_overview_slug ON public.mv_programs_overview USING btree (slug);

CREATE OR REPLACE FUNCTION public.refresh_filter_options()
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY mv_filter_options;
END;
$function$
;

create or replace view "public"."target_flag_summary" as  SELECT t.id,
    t.target_id,
    array_agg(DISTINCT fd.label) FILTER (WHERE ((fd.category = 'spectral_features'::text) AND ((t.spectral_features & fd.value) > 0))) AS spectral_features_labels,
    array_agg(DISTINCT fd.label) FILTER (WHERE ((fd.category = 'object_flags'::text) AND ((t.object_flags & fd.value) > 0))) AS object_flags_labels,
    array_agg(DISTINCT fd.label) FILTER (WHERE ((fd.category = 'dq_flags'::text) AND ((t.dq_flags & fd.value) > 0))) AS dq_flags_labels
   FROM (public.targets t
     CROSS JOIN public.flag_definitions fd)
  GROUP BY t.id, t.target_id;


create or replace view "public"."targets_with_flags" as  SELECT t.id,
    t.target_id,
    t.program_slug,
    t.field,
    t.ra,
    t."dec",
    t.redshift_auto AS redshift,
    t.redshift_quality,
    t.spectral_features,
    t.object_flags,
    t.dq_flags,
    t.created_at,
    t.updated_at,
    rq.label AS redshift_quality_label,
    rq.icon AS redshift_quality_icon,
    rq.color AS redshift_quality_color
   FROM (public.targets t
     LEFT JOIN public.flag_definitions rq ON (((rq.category = 'redshift_quality'::text) AND (rq.value = t.redshift_quality))));



  create policy "insert_comments_by_access"
  on "public"."comments"
  as permissive
  for insert
  to public
with check (((target_id IN ( SELECT t.id
   FROM public.targets t
  WHERE (t.program_slug = ANY (public.accessible_program_slugs())))) AND public.can_comment()));



  create policy "select_comments_by_access"
  on "public"."comments"
  as permissive
  for select
  to public
using ((target_id IN ( SELECT t.id
   FROM public.targets t
  WHERE (t.program_slug = ANY (public.accessible_program_slugs())))));



  create policy "insert_audit_by_access"
  on "public"."flag_audit_log"
  as permissive
  for insert
  to authenticated
with check ((target_id IN ( SELECT t.id
   FROM public.targets t
  WHERE (t.program_slug = ANY (public.accessible_program_slugs())))));



  create policy "select_audit_by_access"
  on "public"."flag_audit_log"
  as permissive
  for select
  to public
using ((target_id IN ( SELECT t.id
   FROM public.targets t
  WHERE (t.program_slug = ANY (public.accessible_program_slugs())))));



  create policy "Users can view own reset logs"
  on "public"."password_reset_log"
  as permissive
  for select
  to public
using ((user_id = auth.uid()));



  create policy "select_spectra_by_access"
  on "public"."spectra"
  as permissive
  for select
  to public
using ((target_id IN ( SELECT t.target_id
   FROM public.targets t
  WHERE (t.program_slug = ANY (public.accessible_program_slugs())))));



