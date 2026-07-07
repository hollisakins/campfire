drop materialized view if exists "public"."mv_filter_options";

drop materialized view if exists "public"."mv_programs_overview";

drop view if exists "public"."nircam_reduction_progress";

drop view if exists "public"."spectrum_flag_summary";

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.filter_accessible_storage_keys(p_keys text[], p_program_slugs text[], p_include_unpublished boolean DEFAULT false)
 RETURNS TABLE(storage_key text)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT so.storage_key
  FROM storage_objects so
  WHERE so.storage_key = ANY(p_keys)
    AND so.status = 'active'
    AND (
      p_include_unpublished
      OR (so.spectrum_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM spectra s
            JOIN targets t ON t.target_id = s.target_id
            WHERE s.spectrum_id = so.spectrum_id
              AND s.deploy_status = 'published'
              AND t.program_slug = ANY(p_program_slugs)))
      OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM deployments d
            JOIN observations o ON o.name = d.observation
            WHERE d.id = so.deployment_id
              AND d.status = 'published'
              AND o.program_slug = ANY(p_program_slugs)))
    );
$function$
;

CREATE OR REPLACE FUNCTION public.get_storage_objects_for_sync(p_program_slugs text[], p_updated_since timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0, p_include_counts boolean DEFAULT true, p_include_unpublished boolean DEFAULT false)
 RETURNS TABLE(objects jsonb, total_count bigint, total_accessible_count bigint)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
 SET statement_timeout TO '120s'
AS $function$
BEGIN
  RETURN QUERY
  WITH scoped AS (
    SELECT so.*
    FROM storage_objects so
    WHERE so.status = 'active'
      AND (
        p_include_unpublished
        OR (so.spectrum_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM spectra s
              JOIN targets t ON t.target_id = s.target_id
              WHERE s.spectrum_id = so.spectrum_id
                AND s.deploy_status = 'published'
                AND t.program_slug = ANY(p_program_slugs)))
        OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM deployments d
              JOIN observations o ON o.name = d.observation
              WHERE d.id = so.deployment_id
                AND d.status = 'published'
                AND o.program_slug = ANY(p_program_slugs)))
      )
  ),
  matched AS MATERIALIZED (
    SELECT * FROM scoped
    WHERE (p_updated_since IS NULL OR scoped.updated_at > p_updated_since)
    ORDER BY scoped.storage_key
    LIMIT p_limit OFFSET p_offset
  ),
  total AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
      AND (p_updated_since IS NULL OR scoped.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'backend', m.backend,
        'bucket', m.bucket,
        'storage_key', m.storage_key,
        'content_hash', m.content_hash,
        'size_bytes', m.size_bytes,
        'content_type', m.content_type,
        'product_type', m.product_type,
        'instrument', m.instrument,
        'status', m.status,
        'observation', m.observation,
        'field', m.field,
        'spectrum_id', m.spectrum_id,
        'exposure_ref', m.exposure_ref,
        'deployment_id', m.deployment_id,
        'cfpipe_version', m.cfpipe_version,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$function$
;

-- NOTE (#228): can_inspect()/handle_new_user() re-emitted by migra (pre-existing
-- SET search_path drift, tracked in #228). Stripped to keep this migration single-purpose.

create materialized view "public"."mv_filter_options" as  SELECT 1 AS id,
    ARRAY( SELECT DISTINCT targets.field
           FROM public.targets
          WHERE targets.has_published_spectrum
          ORDER BY targets.field) AS fields,
    ARRAY( SELECT DISTINCT targets.observation
           FROM public.targets
          WHERE ((targets.observation IS NOT NULL) AND targets.has_published_spectrum)
          ORDER BY targets.observation) AS observations,
    ARRAY( SELECT DISTINCT spectra.grating
           FROM public.spectra
          WHERE (spectra.deploy_status = 'published'::text)
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
    COALESCE(pids.jwst_pids, ARRAY[]::integer[]) AS jwst_pids,
    COALESCE(pids.n_observations, (0)::bigint) AS n_observations,
    last_red.last_reduced_at
   FROM (((public.programs p
     LEFT JOIN ( SELECT t.program_slug,
            count(DISTINCT t.target_id) AS target_count,
            array_agg(DISTINCT s.grating ORDER BY s.grating) FILTER (WHERE (s.grating IS NOT NULL)) AS gratings,
            array_agg(DISTINCT t.field ORDER BY t.field) AS fields,
            array_agg(DISTINCT t.observation ORDER BY t.observation) AS observations
           FROM (public.targets t
             LEFT JOIN public.spectra s ON (((s.target_id = t.target_id) AND (s.deploy_status = 'published'::text))))
          GROUP BY t.program_slug) stats ON ((p.slug = stats.program_slug)))
     LEFT JOIN ( SELECT observations.program_slug,
            array_agg(DISTINCT observations.jwst_program_id ORDER BY observations.jwst_program_id) AS jwst_pids,
            count(*) AS n_observations
           FROM public.observations
          GROUP BY observations.program_slug) pids ON ((p.slug = pids.program_slug)))
     LEFT JOIN ( SELECT o.program_slug,
            max(d.reduced_at) AS last_reduced_at
           FROM (public.observations o
             JOIN public.deployments d ON ((d.observation = o.name)))
          WHERE (d.source_ids_filter IS NULL)
          GROUP BY o.program_slug) last_red ON ((p.slug = last_red.program_slug)));


-- Restore matview unique indexes migra drops (REFRESH CONCURRENTLY needs them).
CREATE UNIQUE INDEX mv_filter_options_id ON public.mv_filter_options USING btree (id);
CREATE UNIQUE INDEX mv_programs_overview_slug ON public.mv_programs_overview USING btree (slug);

-- security_invoker restored (migra drops it on recreate).
create or replace view "public"."nircam_reduction_progress" with (security_invoker = true) as  SELECT field,
    filter,
    count(*) AS total,
    count(*) FILTER (WHERE (stage = 'uncal'::text)) AS at_uncal,
    count(*) FILTER (WHERE (stage = 'detector1'::text)) AS at_detector1,
    count(*) FILTER (WHERE (stage = 'persistence'::text)) AS at_persistence,
    count(*) FILTER (WHERE (stage = 'wisp'::text)) AS at_wisp,
    count(*) FILTER (WHERE (stage = 'striping'::text)) AS at_striping,
    count(*) FILTER (WHERE (stage = 'image2'::text)) AS at_image2,
    count(*) FILTER (WHERE (stage = 'edge'::text)) AS at_edge,
    count(*) FILTER (WHERE (stage = 'sky'::text)) AS at_sky,
    count(*) FILTER (WHERE (stage = 'diag_striping'::text)) AS at_diag_striping,
    count(*) FILTER (WHERE (stage = 'variance'::text)) AS at_variance,
    count(*) FILTER (WHERE (stage = 'wcs_shift'::text)) AS at_wcs_shift,
    count(*) FILTER (WHERE (stage = 'preview'::text)) AS at_preview,
    count(*) FILTER (WHERE (stage = 'jhat'::text)) AS at_jhat,
    count(*) FILTER (WHERE (stage = 'apply_mask'::text)) AS at_apply_mask,
    count(*) FILTER (WHERE (stage = 'bad_pixel'::text)) AS at_bad_pixel,
    count(*) FILTER (WHERE (stage = 'outlier'::text)) AS at_outlier,
    count(*) FILTER (WHERE (review_status = 'pending'::text)) AS pending_review,
    count(*) FILTER (WHERE (review_status = 'approved'::text)) AS approved,
    count(*) FILTER (WHERE (review_status = 'excluded'::text)) AS excluded,
    count(*) FILTER (WHERE (masking = 'needed'::text)) AS needs_masking,
    count(*) FILTER (WHERE (correction = 'needed'::text)) AS needs_correction
   FROM public.nircam_exposures
  GROUP BY field, filter;


-- security_invoker restored (migra drops it on recreate).
create or replace view "public"."spectrum_flag_summary" with (security_invoker = true) as  SELECT s.id,
    s.target_id,
    s.grating,
    array_agg(DISTINCT fd.label) FILTER (WHERE ((fd.category = 'dq_flags'::text) AND ((s.dq_flags & fd.value) > 0))) AS dq_flags_labels
   FROM (public.spectra s
     CROSS JOIN public.flag_definitions fd)
  WHERE ((s.deploy_status = 'published'::text) OR public.is_admin())
  GROUP BY s.id, s.target_id, s.grating;



  create policy "select_storage_objects_by_access"
  on "public"."storage_objects"
  as permissive
  for select
  to authenticated
using (((status = 'active'::text) AND (((spectrum_id IS NOT NULL) AND (EXISTS ( SELECT 1
   FROM (public.spectra s
     JOIN public.targets t ON ((t.target_id = s.target_id)))
  WHERE ((s.spectrum_id = storage_objects.spectrum_id) AND (s.deploy_status = 'published'::text) AND (t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])))))) OR ((spectrum_id IS NULL) AND (deployment_id IS NOT NULL) AND (EXISTS ( SELECT 1
   FROM (public.deployments d
     JOIN public.observations o ON ((o.name = d.observation)))
  WHERE ((d.id = storage_objects.deployment_id) AND (d.status = 'published'::text) AND (o.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])))))))));



