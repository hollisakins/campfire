-- Hardening of the matview-backed observation readers (follow-up to
-- 20260904000300 / PR #520: review findings that landed as it merged).
--
-- Hand-authored: no local Docker for `supabase db diff`. Matches
-- supabase/schemas/functions.sql.
--
-- 1. scoped_program_slugs() failed OPEN for the anon key: PostgREST sets
--    request.jwt.claims for anon too, and accessible_program_slugs() still
--    returns the public programs with auth.uid() NULL, so the SECURITY
--    DEFINER readers served public-program observation data to unauthenticated
--    callers. Now: no auth.uid() => empty scope; and both readers plus the
--    helper are revoked from PUBLIC / anon.
-- 2. refresh_observations_overview() / refresh_all_matviews() were callable by
--    anon (default privileges grant every new function). Revoked.
-- 3. Admins and include_drafts share links go back to the live aggregate:
--    the published-only snapshot dropped their draft-only observations.
--    The live path now carries the share-link observation conjunct too.
-- 4. Share links no longer receive the deployer's username / full name,
--    which user_profiles RLS hid from them before the readers went DEFINER.

CREATE OR REPLACE FUNCTION public.scoped_program_slugs(p_program_slugs text[])
RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT CASE
    WHEN auth.role() = 'service_role'
      OR COALESCE(current_setting('request.jwt.claims', true), '') = ''
      THEN COALESCE(p_program_slugs, '{}'::text[])
    WHEN auth.uid() IS NULL THEN '{}'::text[]
    ELSE COALESCE(ARRAY(
      SELECT unnest(COALESCE(p_program_slugs, '{}'::text[]))
      INTERSECT
      SELECT unnest(public.accessible_program_slugs())
    ), '{}'::text[])
  END;
$$;

REVOKE ALL ON FUNCTION public.scoped_program_slugs(text[]) FROM PUBLIC, anon;
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
  v_link boolean := public.is_link_account();
  v_unpublished boolean := p_include_unpublished
    AND (auth.role() = 'service_role'
         OR COALESCE(current_setting('request.jwt.claims', true), '') = ''
         OR public.is_admin());
  -- Callers whose RLS view included draft data before these readers went
  -- SECURITY DEFINER (admins; share links minted with include_drafts) keep
  -- the live aggregate, where p_include_unpublished decides what is
  -- counted; the published-only snapshot would drop their draft-only
  -- observations entirely.
  v_live boolean := v_unpublished OR public.is_admin() OR public.link_sees_drafts();
BEGIN
  IF NOT v_live THEN
    RETURN QUERY
    SELECT mv.observation, mv.program_slug, mv.program_name, mv.field,
      mv.target_count, mv.spectrum_count, mv.total_size_bytes,
      mv.pointings,
      mv.crds_context, mv.cfpipe_version, mv.jwst_version,
      mv.reduced_at, mv.deployed_at,
      -- Deployer identity stays hidden from share links, as user_profiles
      -- RLS hid it before the reader went SECURITY DEFINER.
      CASE WHEN v_link THEN NULL ELSE mv.deployed_by_username END,
      CASE WHEN v_link THEN NULL ELSE mv.deployed_by_full_name END,
      mv.n_patches_since_full, mv.last_patch_at
    FROM public.mv_observations_overview mv
    WHERE mv.program_slug = ANY(v_slugs)
      -- Share links see only the observation they were minted for (mirrors
      -- accessible_observations_select; the matview has no RLS to do it).
      AND (NOT v_link OR mv.observation = (SELECT public.link_observation()))
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
      AND (NOT v_link OR t.observation = (SELECT public.link_observation()))
    GROUP BY t.observation, t.program_slug, p.program_name, t.field
  )
  SELECT s.observation, s.program_slug, s.program_name, s.field,
    s.target_count, s.spectrum_count, s.total_size_bytes,
    o.pointings,
    full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    CASE WHEN v_link THEN NULL ELSE full_dep.deployed_by_username END,
    CASE WHEN v_link THEN NULL ELSE full_dep.deployed_by_full_name END,
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

REVOKE ALL ON FUNCTION public.get_observation_stats(text[], boolean) FROM PUBLIC, anon;
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
  v_link boolean := public.is_link_account();
  v_unpublished boolean := p_include_unpublished
    AND (auth.role() = 'service_role'
         OR COALESCE(current_setting('request.jwt.claims', true), '') = ''
         OR public.is_admin());
  -- Callers whose RLS view included draft data before these readers went
  -- SECURITY DEFINER (admins; share links minted with include_drafts) keep
  -- the live aggregate, where p_include_unpublished decides what is
  -- counted; the published-only snapshot would drop their draft-only
  -- observations entirely.
  v_live boolean := v_unpublished OR public.is_admin() OR public.link_sees_drafts();
BEGIN
  IF NOT v_live THEN
    RETURN QUERY
    SELECT mv.observation, mv.program_slug, mv.program_name, mv.field,
      mv.cycle, mv.gratings, mv.pointing_count, mv.pointings,
      mv.target_count, mv.spectrum_count, mv.total_size_bytes,
      mv.crds_context, mv.cfpipe_version, mv.jwst_version,
      mv.reduced_at, mv.deployed_at,
      -- Deployer identity stays hidden from share links, as user_profiles
      -- RLS hid it before the reader went SECURITY DEFINER.
      CASE WHEN v_link THEN NULL ELSE mv.deployed_by_username END,
      CASE WHEN v_link THEN NULL ELSE mv.deployed_by_full_name END,
      mv.n_patches_since_full, mv.last_patch_at
    FROM public.mv_observations_overview mv
    WHERE mv.program_slug = ANY(v_slugs)
      -- Share links see only the observation they were minted for (mirrors
      -- accessible_observations_select; the matview has no RLS to do it).
      AND (NOT v_link OR mv.observation = (SELECT public.link_observation()))
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
      AND (NOT v_link OR t.observation = (SELECT public.link_observation()))
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
    CASE WHEN v_link THEN NULL ELSE full_dep.deployed_by_username END,
    CASE WHEN v_link THEN NULL ELSE full_dep.deployed_by_full_name END,
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
    AND (NOT v_link OR o.name = (SELECT public.link_observation()))
  ORDER BY o.program_slug, o.name;
END;
$$;

REVOKE ALL ON FUNCTION public.get_observations_overview(text[], boolean) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.get_observations_overview TO authenticated;

REVOKE ALL ON FUNCTION public.refresh_observations_overview() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.refresh_observations_overview TO authenticated;

REVOKE ALL ON FUNCTION public.refresh_all_matviews() FROM PUBLIC, anon, authenticated;
