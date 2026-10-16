SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_observation_stats (
  p_program_slugs       text[],
  p_include_unpublished boolean DEFAULT false
)
  RETURNS TABLE (
    observation           text,
    program_slug          text,
    program_name          text,
    field                 text,
    target_count          bigint,
    spectrum_count        bigint,
    total_size_bytes      bigint,
    pointings             jsonb,
    crds_context          text,
    cfpipe_version        text,
    jwst_version          text,
    reduced_at            timestamp with time zone,
    deployed_at           timestamp with time zone,
    deployed_by_username  text,
    deployed_by_full_name text,
    n_patches_since_full  integer,
    last_patch_at         timestamp with time zone
  )
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_slugs text[] := public.scoped_program_slugs(p_program_slugs);
  v_link boolean := public.is_link_account();
  v_unpublished boolean := p_include_unpublished
    AND (auth.role() = 'service_role'
         OR COALESCE(current_setting('request.jwt.claims', true), '') = ''
         OR public.is_admin());
  -- Callers whose RLS view included draft data before these readers went
  -- SECURITY DEFINER (admins; share links minted with include_drafts) keep
  -- the live aggregate; the published-only snapshot would drop their
  -- draft-only observations entirely.
  -- (P6b-2) a browsed release never reads the matview: the live path
  -- reads cat_spectra / cat_targets, which are the frozen rows then
  v_live boolean := v_unpublished OR public.is_admin() OR public.link_sees_drafts()
    OR public.dr_browse_tag() IS NOT NULL;
  -- What the live path may count, derived from the caller's authorization,
  -- never from the raw p_include_unpublished argument: a drafts link sees
  -- published + draft (policies: select_spectra_by_access /
  -- authenticated_select_deployments); an admin sees published only unless
  -- opted in. Counts and deployment provenance use the same set, so a draft
  -- re-reduction's version stamps never sit next to published-only counts
  -- (the matview's invariant, views.sql).
  v_statuses text[] := CASE
    WHEN v_unpublished THEN ARRAY['draft', 'published', 'revoked']
    WHEN public.link_sees_drafts() THEN ARRAY['published', 'draft']
    ELSE ARRAY['published'] END;
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
    FROM public.cat_targets t
    JOIN programs p ON p.slug = t.program_slug
    LEFT JOIN public.cat_spectra s ON s.target_id = t.target_id
      -- Only the statuses this caller is authorized for.
      AND s.deploy_status = ANY(v_statuses)
    WHERE t.program_slug = ANY(v_slugs)
      AND (NOT v_link OR t.observation = (SELECT public.link_observation()))
      -- Targets count on the same terms as their spectra (the snapshot's
      -- has_published_spectrum gate, generalised): a target with no spectrum
      -- in the authorized statuses is not counted unless the caller opted
      -- into unpublished data, where every target appears as before.
      AND (v_unpublished OR EXISTS (
        SELECT 1 FROM public.cat_spectra sx
        WHERE sx.target_id = t.target_id AND sx.deploy_status = ANY(v_statuses)))
    GROUP BY t.observation, t.program_slug, p.program_name, t.field
  )
  SELECT s.observation, s.program_slug, s.program_name, s.field,
    s.target_count, s.spectrum_count, s.total_size_bytes,
    o.pointings,
    COALESCE(full_dep.crds_context, frozen_dep.crds_context),
    COALESCE(full_dep.cfpipe_version, frozen_dep.cfpipe_version),
    COALESCE(full_dep.jwst_version, frozen_dep.jwst_version),
    COALESCE(full_dep.reduced_at, frozen_dep.reduced_at), full_dep.deployed_at,
    CASE WHEN v_link THEN NULL ELSE full_dep.deployed_by_username END,
    CASE WHEN v_link THEN NULL ELSE full_dep.deployed_by_full_name END,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM stats s
  LEFT JOIN observations o ON o.name = s.observation
  -- Provenance: live, the newest full deployment. Under a browsed release
  -- (P6b-2) deployments is live-only (a later deploy would label frozen
  -- spectra with its versions), so the versions come from the release's own
  -- spectra of the observation: the most common non-null value of each
  -- field, the latest reduced_at; there is no frozen deployments record, so
  -- deployed_at, the deployer and the patch counts are NULL / 0 then.
  LEFT JOIN LATERAL (
    SELECT d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
    WHERE public.dr_browse_tag() IS NULL
      AND d.observation = s.observation AND d.source_ids_filter IS NULL
      AND d.status = ANY(v_statuses)
    ORDER BY d.deployed_at DESC
    LIMIT 1
  ) full_dep ON true
  -- Gated per column with CASE, not with a WHERE on this FROM-less lateral:
  -- the planner pulls such a WHERE up into a join filter, so the four
  -- correlated subqueries ran for every observation live as well (a full
  -- spectra scan each; the 20 s admin overview of 2026-10-16). CASE only
  -- evaluates a subplan when its arm is taken.
  LEFT JOIN LATERAL (
    SELECT
      CASE WHEN public.dr_browse_tag() IS NOT NULL THEN (SELECT x.crds_context FROM public.cat_spectra x
         WHERE x.observation = s.observation AND x.deploy_status = ANY(v_statuses) AND x.crds_context IS NOT NULL
         GROUP BY x.crds_context ORDER BY count(*) DESC, x.crds_context LIMIT 1) END AS crds_context,
      CASE WHEN public.dr_browse_tag() IS NOT NULL THEN (SELECT x.cfpipe_version FROM public.cat_spectra x
         WHERE x.observation = s.observation AND x.deploy_status = ANY(v_statuses) AND x.cfpipe_version IS NOT NULL
         GROUP BY x.cfpipe_version ORDER BY count(*) DESC, x.cfpipe_version LIMIT 1) END AS cfpipe_version,
      CASE WHEN public.dr_browse_tag() IS NOT NULL THEN (SELECT x.jwst_version FROM public.cat_spectra x
         WHERE x.observation = s.observation AND x.deploy_status = ANY(v_statuses) AND x.jwst_version IS NOT NULL
         GROUP BY x.jwst_version ORDER BY count(*) DESC, x.jwst_version LIMIT 1) END AS jwst_version,
      CASE WHEN public.dr_browse_tag() IS NOT NULL THEN (SELECT max(x.reduced_at) FROM public.cat_spectra x
         WHERE x.observation = s.observation AND x.deploy_status = ANY(v_statuses)) END AS reduced_at
  ) frozen_dep ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE public.dr_browse_tag() IS NULL
      AND d.observation = s.observation
      AND d.source_ids_filter IS NOT NULL
      AND d.status = ANY(v_statuses)
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
  ) patches ON true
  ORDER BY s.observation;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_observations_overview (
  p_program_slugs       text[],
  p_include_unpublished boolean DEFAULT false
)
  RETURNS TABLE (
    observation           text,
    program_slug          text,
    program_name          text,
    field                 text,
    cycle                 integer,
    gratings              text[],
    pointing_count        integer,
    pointings             jsonb,
    target_count          bigint,
    spectrum_count        bigint,
    total_size_bytes      bigint,
    crds_context          text,
    cfpipe_version        text,
    jwst_version          text,
    reduced_at            timestamp with time zone,
    deployed_at           timestamp with time zone,
    deployed_by_username  text,
    deployed_by_full_name text,
    n_patches_since_full  integer,
    last_patch_at         timestamp with time zone
  )
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
  v_slugs text[] := public.scoped_program_slugs(p_program_slugs);
  v_link boolean := public.is_link_account();
  v_unpublished boolean := p_include_unpublished
    AND (auth.role() = 'service_role'
         OR COALESCE(current_setting('request.jwt.claims', true), '') = ''
         OR public.is_admin());
  -- Callers whose RLS view included draft data before these readers went
  -- SECURITY DEFINER (admins; share links minted with include_drafts) keep
  -- the live aggregate; the published-only snapshot would drop their
  -- draft-only observations entirely.
  -- (P6b-2) a browsed release never reads the matview: the live path
  -- reads cat_spectra / cat_targets, which are the frozen rows then
  v_live boolean := v_unpublished OR public.is_admin() OR public.link_sees_drafts()
    OR public.dr_browse_tag() IS NOT NULL;
  -- What the live path may count, derived from the caller's authorization,
  -- never from the raw p_include_unpublished argument: a drafts link sees
  -- published + draft (policies: select_spectra_by_access /
  -- authenticated_select_deployments); an admin sees published only unless
  -- opted in. Counts and deployment provenance use the same set, so a draft
  -- re-reduction's version stamps never sit next to published-only counts
  -- (the matview's invariant, views.sql).
  v_statuses text[] := CASE
    WHEN v_unpublished THEN ARRAY['draft', 'published', 'revoked']
    WHEN public.link_sees_drafts() THEN ARRAY['published', 'draft']
    ELSE ARRAY['published'] END;
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
    FROM public.cat_targets t
    LEFT JOIN public.cat_spectra s ON s.target_id = t.target_id
      -- Only the statuses this caller is authorized for.
      AND s.deploy_status = ANY(v_statuses)
    WHERE t.program_slug = ANY(v_slugs)
      AND (NOT v_link OR t.observation = (SELECT public.link_observation()))
      -- Targets count on the same terms as their spectra (see
      -- get_observation_stats).
      AND (v_unpublished OR EXISTS (
        SELECT 1 FROM public.cat_spectra sx
        WHERE sx.target_id = t.target_id AND sx.deploy_status = ANY(v_statuses)))
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
    COALESCE(full_dep.crds_context, frozen_dep.crds_context),
    COALESCE(full_dep.cfpipe_version, frozen_dep.cfpipe_version),
    COALESCE(full_dep.jwst_version, frozen_dep.jwst_version),
    COALESCE(full_dep.reduced_at, frozen_dep.reduced_at), full_dep.deployed_at,
    CASE WHEN v_link THEN NULL ELSE full_dep.deployed_by_username END,
    CASE WHEN v_link THEN NULL ELSE full_dep.deployed_by_full_name END,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM public.observations o
  JOIN public.programs p ON p.slug = o.program_slug
  LEFT JOIN stats s ON s.observation = o.name AND s.program_slug = o.program_slug
  -- Provenance as in get_observation_stats: the newest full deployment live;
  -- under a browsed release (P6b-2) the release's own spectra (deployments
  -- is live-only; no frozen record, so deployed_at / deployer / patches are
  -- NULL / 0 then).
  LEFT JOIN LATERAL (
    SELECT d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
    WHERE public.dr_browse_tag() IS NULL
      AND d.observation = o.name AND d.source_ids_filter IS NULL
      AND d.status = ANY(v_statuses)
    ORDER BY d.deployed_at DESC
    LIMIT 1
  ) full_dep ON true
  -- Gated per column with CASE, not with a WHERE on this FROM-less lateral:
  -- the planner pulls such a WHERE up into a join filter, so the four
  -- correlated subqueries ran for every observation live as well (a full
  -- spectra scan each; the 20 s admin overview of 2026-10-16). CASE only
  -- evaluates a subplan when its arm is taken.
  LEFT JOIN LATERAL (
    SELECT
      CASE WHEN public.dr_browse_tag() IS NOT NULL THEN (SELECT x.crds_context FROM public.cat_spectra x
         WHERE x.observation = o.name AND x.deploy_status = ANY(v_statuses) AND x.crds_context IS NOT NULL
         GROUP BY x.crds_context ORDER BY count(*) DESC, x.crds_context LIMIT 1) END AS crds_context,
      CASE WHEN public.dr_browse_tag() IS NOT NULL THEN (SELECT x.cfpipe_version FROM public.cat_spectra x
         WHERE x.observation = o.name AND x.deploy_status = ANY(v_statuses) AND x.cfpipe_version IS NOT NULL
         GROUP BY x.cfpipe_version ORDER BY count(*) DESC, x.cfpipe_version LIMIT 1) END AS cfpipe_version,
      CASE WHEN public.dr_browse_tag() IS NOT NULL THEN (SELECT x.jwst_version FROM public.cat_spectra x
         WHERE x.observation = o.name AND x.deploy_status = ANY(v_statuses) AND x.jwst_version IS NOT NULL
         GROUP BY x.jwst_version ORDER BY count(*) DESC, x.jwst_version LIMIT 1) END AS jwst_version,
      CASE WHEN public.dr_browse_tag() IS NOT NULL THEN (SELECT max(x.reduced_at) FROM public.cat_spectra x
         WHERE x.observation = o.name AND x.deploy_status = ANY(v_statuses)) END AS reduced_at
  ) frozen_dep ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE public.dr_browse_tag() IS NULL
      AND d.observation = o.name
      AND d.source_ids_filter IS NOT NULL
      AND d.status = ANY(v_statuses)
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
  ) patches ON true
  WHERE o.program_slug = ANY(v_slugs)
    AND (NOT v_link OR o.name = (SELECT public.link_observation()))
    -- (P6b-2) a browsed release lists only the observations it holds spectra
    -- of: observations is live-only (members with no frozen rows, and
    -- observations created after the freeze, would show with zero counts)
    AND (public.dr_browse_tag() IS NULL OR s.observation IS NOT NULL)
  ORDER BY o.program_slug, o.name;
END;
$function$;
