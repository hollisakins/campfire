-- Surface reduction user (deployed_by) on the metadata page and derive
-- gratings from the spectra table.
--
-- - get_observations_overview: drop+recreate; add deployed_by_username and
--   deployed_by_full_name (joined via user_profiles), and derive gratings
--   from spectra when present (falling back to observations.gratings, which
--   may be empty for observations whose deploy didn't go through
--   observations.toml).
-- - get_observation_stats: same deployed_by_* additions for the program
--   detail page.
--
-- Function bodies are hand-authored (not generated via `supabase db diff`)
-- to keep the migration small; both schemas/functions.sql and this file are
-- kept in sync.


set check_function_bodies = off;


DROP FUNCTION IF EXISTS public.get_observations_overview(text[]);

CREATE OR REPLACE FUNCTION public.get_observations_overview(p_program_slugs text[])
 RETURNS TABLE(observation text, program_slug text, program_name text, field text, cycle integer, gratings text[], pointing_count integer, pointings jsonb, target_count bigint, spectrum_count bigint, total_size_bytes bigint, reduction_version text, crds_context text, cfpipe_version text, jwst_version text, reduced_at timestamp with time zone, deployed_at timestamp with time zone, deployed_by_username text, deployed_by_full_name text, n_patches_since_full integer, last_patch_at timestamp with time zone)
 LANGUAGE sql
 STABLE
AS $function$
  WITH stats AS (
    SELECT t.observation, t.program_slug,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes,
      ARRAY_AGG(DISTINCT s.grating ORDER BY s.grating)
        FILTER (WHERE s.grating IS NOT NULL) AS gratings
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
    CASE
      WHEN COALESCE(array_length(s.gratings, 1), 0) > 0 THEN s.gratings
      ELSE COALESCE(o.gratings, ARRAY[]::text[])
    END AS gratings,
    COALESCE(jsonb_array_length(o.pointings), 0) AS pointing_count,
    o.pointings,
    COALESCE(s.target_count, 0)::bigint AS target_count,
    COALESCE(s.spectrum_count, 0)::bigint AS spectrum_count,
    COALESCE(s.total_size_bytes, 0)::bigint AS total_size_bytes,
    full_dep.reduction_version, full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    full_dep.deployed_by_username, full_dep.deployed_by_full_name,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM public.observations o
  JOIN public.programs p ON p.slug = o.program_slug
  LEFT JOIN stats s ON s.observation = o.name AND s.program_slug = o.program_slug
  LEFT JOIN LATERAL (
    SELECT d.reduction_version, d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
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


DROP FUNCTION IF EXISTS public.get_observation_stats(text[]);

CREATE OR REPLACE FUNCTION public.get_observation_stats(p_program_slugs text[])
 RETURNS TABLE(observation text, program_slug text, program_name text, field text, target_count bigint, spectrum_count bigint, total_size_bytes bigint, pointings jsonb, reduction_version text, crds_context text, cfpipe_version text, jwst_version text, reduced_at timestamp with time zone, deployed_at timestamp with time zone, deployed_by_username text, deployed_by_full_name text, n_patches_since_full integer, last_patch_at timestamp with time zone)
 LANGUAGE sql
 STABLE
AS $function$
  WITH stats AS (
    SELECT t.observation, t.program_slug, p.program_name, t.field,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes
    FROM public.targets t
    JOIN public.programs p ON p.slug = t.program_slug
    LEFT JOIN public.spectra s ON s.target_id = t.target_id
    WHERE t.program_slug = ANY(p_program_slugs)
    GROUP BY t.observation, t.program_slug, p.program_name, t.field
  )
  SELECT s.observation, s.program_slug, s.program_name, s.field,
    s.target_count, s.spectrum_count, s.total_size_bytes,
    o.pointings,
    full_dep.reduction_version, full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    full_dep.deployed_by_username, full_dep.deployed_by_full_name,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM stats s
  LEFT JOIN public.observations o ON o.name = s.observation
  LEFT JOIN LATERAL (
    SELECT d.reduction_version, d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
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


GRANT EXECUTE ON FUNCTION public.get_observations_overview(text[]) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_observation_stats(text[]) TO authenticated;
