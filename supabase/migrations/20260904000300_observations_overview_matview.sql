-- Perf T1-5 (#501, epic #515): mv_observations_overview + matview-backed observation RPCs + nightly refresh.
--
-- Hand-authored: no local Docker for `supabase db diff`. Matches
-- supabase/schemas/views.sql / functions.sql.
--
-- get_observations_overview ran a 1.0–1.15 s aggregate (with a temp spill)
-- and get_observation_stats ~0.7 s on every /nirspec/metadata visit, while
-- the programs tab read its matview in 1.3 ms. The matview is refreshed at
-- deploy (refresh_observations_overview) and nightly (refresh_all_matviews
-- via pg_cron, scheduled only where the extension exists). Access: the
-- matview has no grant to authenticated; the reader RPCs are SECURITY
-- DEFINER and gate on scoped_program_slugs().

-- BEGIN mv_observations_overview
DROP MATERIALIZED VIEW IF EXISTS public.mv_observations_overview;

CREATE MATERIALIZED VIEW public.mv_observations_overview AS
WITH stats AS (
    SELECT t.observation, t.program_slug,
        COUNT(DISTINCT t.target_id) AS target_count,
        COUNT(s.id) AS spectrum_count,
        COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes,
        ARRAY_AGG(DISTINCT s.grating ORDER BY s.grating)
            FILTER (WHERE s.grating IS NOT NULL) AS gratings
    FROM public.targets t
    -- Published-only: unpublished spectra don't contribute to counts/size/gratings.
    LEFT JOIN public.spectra s ON s.target_id = t.target_id AND s.deploy_status = 'published'
    GROUP BY t.observation, t.program_slug
)
SELECT
    o.name AS observation,
    o.program_slug,
    p.program_name,
    o.field,
    p.cycle,
    -- Gratings from the deployed spectra, falling back to observations.toml's
    -- declared list for observations with no spectra yet.
    CASE
        WHEN COALESCE(array_length(s.gratings, 1), 0) > 0 THEN s.gratings
        ELSE COALESCE(o.gratings, ARRAY[]::text[])
    END AS gratings,
    COALESCE(jsonb_array_length(o.pointings), 0) AS pointing_count,
    o.pointings,
    COALESCE(s.target_count, 0)::bigint AS target_count,
    COALESCE(s.spectrum_count, 0)::bigint AS spectrum_count,
    COALESCE(s.total_size_bytes, 0)::bigint AS total_size_bytes,
    full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    full_dep.deployed_by_username, full_dep.deployed_by_full_name,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
FROM public.observations o
JOIN public.programs p ON p.slug = o.program_slug
LEFT JOIN stats s ON s.observation = o.name AND s.program_slug = o.program_slug
-- Provenance from the most recent FULL deployment (source_ids_filter IS NULL).
LEFT JOIN LATERAL (
    SELECT d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
    WHERE d.observation = o.name AND d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
) full_dep ON true
-- Patch deployments since that full one.
LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE d.observation = o.name
      AND d.source_ids_filter IS NOT NULL
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
) patches ON true
WITH DATA;

CREATE UNIQUE INDEX mv_observations_overview_observation
    ON public.mv_observations_overview (observation);

-- Default privileges in this schema hand every new relation to anon /
-- authenticated; take that back — the reader RPCs (SECURITY DEFINER) are the
-- only way in.
REVOKE ALL ON public.mv_observations_overview FROM anon, authenticated;
GRANT SELECT ON public.mv_observations_overview TO service_role;

CREATE OR REPLACE FUNCTION public.scoped_program_slugs(p_program_slugs text[])
RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT CASE
    WHEN auth.role() = 'service_role'
      OR COALESCE(current_setting('request.jwt.claims', true), '') = ''
      THEN COALESCE(p_program_slugs, '{}'::text[])
    ELSE COALESCE(ARRAY(
      SELECT unnest(COALESCE(p_program_slugs, '{}'::text[]))
      INTERSECT
      SELECT unnest(public.accessible_program_slugs())
    ), '{}'::text[])
  END;
$$;

GRANT EXECUTE ON FUNCTION public.scoped_program_slugs(text[]) TO authenticated, service_role;

CREATE OR REPLACE FUNCTION public.get_observation_stats(p_program_slugs text[], p_include_unpublished boolean DEFAULT false)
RETURNS TABLE(
  observation text, program_slug text, program_name text, field text,
  target_count bigint, spectrum_count bigint, total_size_bytes bigint,
  pointings jsonb,
  crds_context text, cfpipe_version text, jwst_version text,
  reduced_at timestamptz, deployed_at timestamptz,
  deployed_by_username text, deployed_by_full_name text,
  n_patches_since_full integer, last_patch_at timestamptz
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_slugs text[] := public.scoped_program_slugs(p_program_slugs);
  v_unpublished boolean := p_include_unpublished
    AND (auth.role() = 'service_role'
         OR COALESCE(current_setting('request.jwt.claims', true), '') = ''
         OR public.is_admin());
BEGIN
  IF NOT v_unpublished THEN
    RETURN QUERY
    SELECT mv.observation, mv.program_slug, mv.program_name, mv.field,
      mv.target_count, mv.spectrum_count, mv.total_size_bytes,
      mv.pointings,
      mv.crds_context, mv.cfpipe_version, mv.jwst_version,
      mv.reduced_at, mv.deployed_at,
      mv.deployed_by_username, mv.deployed_by_full_name,
      mv.n_patches_since_full, mv.last_patch_at
    FROM public.mv_observations_overview mv
    WHERE mv.program_slug = ANY(v_slugs)
      -- The live aggregate only yields observations that have targets.
      AND mv.target_count > 0
    ORDER BY mv.observation;
    RETURN;
  END IF;

  -- Live aggregate (unpublished-inclusive). Aggregate stats first, then LEFT
  -- JOIN observations once for the JSONB payload; provenance from the most
  -- recent FULL deployment, patches counted since it.
  RETURN QUERY
  WITH stats AS (
    SELECT t.observation, t.program_slug, p.program_name, t.field,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes
    FROM targets t
    JOIN programs p ON p.slug = t.program_slug
    LEFT JOIN spectra s ON s.target_id = t.target_id
      -- B1: unpublished spectra don't contribute to counts/size (targets still appear).
      AND (p_include_unpublished OR s.deploy_status = 'published')
    WHERE t.program_slug = ANY(v_slugs)
    GROUP BY t.observation, t.program_slug, p.program_name, t.field
  )
  SELECT s.observation, s.program_slug, s.program_name, s.field,
    s.target_count, s.spectrum_count, s.total_size_bytes,
    o.pointings,
    full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    full_dep.deployed_by_username, full_dep.deployed_by_full_name,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM stats s
  LEFT JOIN observations o ON o.name = s.observation
  LEFT JOIN LATERAL (
    SELECT d.crds_context, d.cfpipe_version, d.jwst_version,
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
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_observation_stats TO authenticated;

CREATE OR REPLACE FUNCTION public.get_observations_overview(p_program_slugs text[], p_include_unpublished boolean DEFAULT false)
RETURNS TABLE(
  observation text, program_slug text, program_name text, field text,
  cycle integer, gratings text[], pointing_count integer, pointings jsonb,
  target_count bigint, spectrum_count bigint, total_size_bytes bigint,
  crds_context text, cfpipe_version text, jwst_version text,
  reduced_at timestamptz, deployed_at timestamptz,
  deployed_by_username text, deployed_by_full_name text,
  n_patches_since_full integer, last_patch_at timestamptz
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_slugs text[] := public.scoped_program_slugs(p_program_slugs);
  v_unpublished boolean := p_include_unpublished
    AND (auth.role() = 'service_role'
         OR COALESCE(current_setting('request.jwt.claims', true), '') = ''
         OR public.is_admin());
BEGIN
  IF NOT v_unpublished THEN
    RETURN QUERY
    SELECT mv.observation, mv.program_slug, mv.program_name, mv.field,
      mv.cycle, mv.gratings, mv.pointing_count, mv.pointings,
      mv.target_count, mv.spectrum_count, mv.total_size_bytes,
      mv.crds_context, mv.cfpipe_version, mv.jwst_version,
      mv.reduced_at, mv.deployed_at,
      mv.deployed_by_username, mv.deployed_by_full_name,
      mv.n_patches_since_full, mv.last_patch_at
    FROM public.mv_observations_overview mv
    WHERE mv.program_slug = ANY(v_slugs)
    ORDER BY mv.program_slug, mv.observation;
    RETURN;
  END IF;

  RETURN QUERY
  WITH stats AS (
    SELECT t.observation, t.program_slug,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes,
      ARRAY_AGG(DISTINCT s.grating ORDER BY s.grating)
        FILTER (WHERE s.grating IS NOT NULL) AS gratings
    FROM public.targets t
    LEFT JOIN public.spectra s ON s.target_id = t.target_id
      -- B1: unpublished spectra don't contribute to counts/size/gratings.
      AND (p_include_unpublished OR s.deploy_status = 'published')
    WHERE t.program_slug = ANY(v_slugs)
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
    full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    full_dep.deployed_by_username, full_dep.deployed_by_full_name,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM public.observations o
  JOIN public.programs p ON p.slug = o.program_slug
  LEFT JOIN stats s ON s.observation = o.name AND s.program_slug = o.program_slug
  LEFT JOIN LATERAL (
    SELECT d.crds_context, d.cfpipe_version, d.jwst_version,
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
  WHERE o.program_slug = ANY(v_slugs)
  ORDER BY o.program_slug, o.name;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_observations_overview TO authenticated;

CREATE OR REPLACE FUNCTION public.refresh_observations_overview()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_observations_overview;
END;
$$;

GRANT EXECUTE ON FUNCTION public.refresh_observations_overview TO authenticated;

CREATE OR REPLACE FUNCTION public.refresh_all_matviews()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_filter_options;
  REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_programs_overview;
  REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_observations_overview;
END;
$$;

DO $cron$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
    PERFORM cron.schedule('nightly-refresh-matviews', '30 4 * * *', 'SELECT public.refresh_all_matviews()');
  END IF;
END
$cron$;
