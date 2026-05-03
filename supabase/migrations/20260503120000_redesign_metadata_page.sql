-- Redesign /nirspec/programs into /nirspec/metadata.
--
-- Surface per-observation reduction provenance and database-wide scope so the
-- metadata page can show "last full reduction" + "+N patches since" without
-- the user drilling into program detail. Patch deployments
-- (deployments.source_ids_filter IS NOT NULL) never override the canonical
-- observation-level provenance — they only contribute to the patch count.
--
-- Function bodies + index were generated via `supabase db diff`; the
-- mv_programs_overview drop+recreate (section 1) is hand-authored because
-- migra does not track materialized view diffs (per CLAUDE.md schema
-- workflow notes).


-- =============================================================================
-- 1. Materialized view: mv_programs_overview (manual — migra exemption)
-- =============================================================================

drop materialized view if exists "public"."mv_programs_overview";

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
             LEFT JOIN public.spectra s ON ((s.target_id = t.target_id)))
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

CREATE UNIQUE INDEX mv_programs_overview_slug ON public.mv_programs_overview (slug);

GRANT SELECT ON public.mv_programs_overview TO authenticated;


-- =============================================================================
-- 2. Indexes
-- =============================================================================

CREATE INDEX idx_deployments_full_obs_recent ON public.deployments USING btree (observation, deployed_at DESC) WHERE (source_ids_filter IS NULL);


-- =============================================================================
-- 3. Functions
-- =============================================================================

drop function if exists "public"."get_observation_stats"(p_program_slugs text[]);

drop function if exists "public"."get_programs_overview"();

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_database_overview()
 RETURNS TABLE(n_programs bigint, n_observations bigint, n_pointings bigint, n_targets bigint, n_spectra bigint, total_size_bytes bigint, latest_deployed_at timestamp with time zone, latest_reduction_version text)
 LANGUAGE sql
 STABLE
AS $function$
  WITH latest AS (
    SELECT d.deployed_at, d.reduction_version
    FROM public.deployments d
    WHERE d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
  )
  SELECT
    (SELECT COUNT(*)::bigint FROM public.programs) AS n_programs,
    (SELECT COUNT(*)::bigint FROM public.observations) AS n_observations,
    (SELECT COALESCE(SUM(jsonb_array_length(pointings)), 0)::bigint
       FROM public.observations
       WHERE pointings IS NOT NULL) AS n_pointings,
    (SELECT COUNT(*)::bigint FROM public.targets) AS n_targets,
    (SELECT COUNT(*)::bigint FROM public.spectra) AS n_spectra,
    (SELECT COALESCE(SUM(file_size), 0)::bigint FROM public.spectra) AS total_size_bytes,
    (SELECT deployed_at FROM latest) AS latest_deployed_at,
    (SELECT reduction_version FROM latest) AS latest_reduction_version;
$function$
;

CREATE OR REPLACE FUNCTION public.get_observations_overview(p_program_slugs text[])
 RETURNS TABLE(observation text, program_slug text, program_name text, field text, cycle integer, gratings text[], pointing_count integer, pointings jsonb, target_count bigint, spectrum_count bigint, total_size_bytes bigint, reduction_version text, crds_context text, cfpipe_version text, jwst_version text, reduced_at timestamp with time zone, deployed_at timestamp with time zone, n_patches_since_full integer, last_patch_at timestamp with time zone)
 LANGUAGE sql
 STABLE
AS $function$
  WITH stats AS (
    SELECT t.observation, t.program_slug,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes
    FROM public.targets t
    LEFT JOIN public.spectra s ON s.target_id = t.target_id
    WHERE t.program_slug = ANY(p_program_slugs)
    GROUP BY t.observation, t.program_slug
  )
  SELECT
    o.name AS observation,
    o.program_slug,
    p.program_name,
    o.field,
    p.cycle,
    COALESCE(o.gratings, ARRAY[]::text[]) AS gratings,
    COALESCE(jsonb_array_length(o.pointings), 0) AS pointing_count,
    o.pointings,
    COALESCE(s.target_count, 0)::bigint AS target_count,
    COALESCE(s.spectrum_count, 0)::bigint AS spectrum_count,
    COALESCE(s.total_size_bytes, 0)::bigint AS total_size_bytes,
    full_dep.reduction_version, full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM public.observations o
  JOIN public.programs p ON p.slug = o.program_slug
  LEFT JOIN stats s ON s.observation = o.name AND s.program_slug = o.program_slug
  LEFT JOIN LATERAL (
    SELECT d.reduction_version, d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at
    FROM public.deployments d
    WHERE d.observation = o.name AND d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
  ) full_dep ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE d.observation = o.name
      AND d.source_ids_filter IS NOT NULL
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
  ) patches ON true
  WHERE o.program_slug = ANY(p_program_slugs)
  ORDER BY o.program_slug, o.name;
$function$
;

CREATE OR REPLACE FUNCTION public.get_observation_stats(p_program_slugs text[])
 RETURNS TABLE(observation text, program_slug text, program_name text, field text, target_count bigint, spectrum_count bigint, total_size_bytes bigint, pointings jsonb, reduction_version text, crds_context text, cfpipe_version text, jwst_version text, reduced_at timestamp with time zone, deployed_at timestamp with time zone, n_patches_since_full integer, last_patch_at timestamp with time zone)
 LANGUAGE sql
 STABLE
AS $function$
  WITH stats AS (
    SELECT t.observation, t.program_slug, p.program_name, t.field,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes
    FROM targets t
    JOIN programs p ON p.slug = t.program_slug
    LEFT JOIN spectra s ON s.target_id = t.target_id
    WHERE t.program_slug = ANY(p_program_slugs)
    GROUP BY t.observation, t.program_slug, p.program_name, t.field
  )
  SELECT s.observation, s.program_slug, s.program_name, s.field,
    s.target_count, s.spectrum_count, s.total_size_bytes,
    o.pointings,
    full_dep.reduction_version, full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM stats s
  LEFT JOIN observations o ON o.name = s.observation
  LEFT JOIN LATERAL (
    SELECT d.reduction_version, d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at
    FROM public.deployments d
    WHERE d.observation = s.observation AND d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
  ) full_dep ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE d.observation = s.observation
      AND d.source_ids_filter IS NOT NULL
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
  ) patches ON true
  ORDER BY s.observation;
$function$
;

CREATE OR REPLACE FUNCTION public.get_programs_overview()
 RETURNS TABLE(slug text, program_name text, pi_name text, description text, is_public boolean, cycle integer, target_count bigint, gratings text[], fields text[], observations text[], jwst_pids integer[], n_observations bigint, last_reduced_at timestamp with time zone)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT mv.slug, mv.program_name, mv.pi_name, mv.description, mv.is_public, mv.cycle,
    mv.target_count, mv.gratings, mv.fields, mv.observations, mv.jwst_pids,
    mv.n_observations, mv.last_reduced_at
  FROM public.mv_programs_overview mv ORDER BY mv.program_name;
$function$
;

GRANT EXECUTE ON FUNCTION public.get_database_overview TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_observations_overview(text[]) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_observation_stats(text[]) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_programs_overview() TO authenticated;
