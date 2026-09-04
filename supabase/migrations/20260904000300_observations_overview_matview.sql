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
    -- ...and draft-only targets don't count at all (what targets RLS enforced
    -- for the old invoker query; this view is built by the owner).
    WHERE t.has_published_spectrum
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
-- Provenance from the most recent PUBLISHED full deployment (source_ids_filter
-- IS NULL). Published-only like the rest of the snapshot: a draft re-reduction
-- must not surface its version stamps here before it is published.
LEFT JOIN LATERAL (
    SELECT d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
    WHERE d.observation = o.name AND d.source_ids_filter IS NULL
      AND d.status = 'published'
    ORDER BY d.deployed_at DESC
    LIMIT 1
) full_dep ON true
-- Published patch deployments since that full one.
LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE d.observation = o.name
      AND d.source_ids_filter IS NOT NULL
      AND d.status = 'published'
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
      -- Share links see only the observation they were minted for (mirrors
      -- accessible_observations_select; the matview has no RLS to do it).
      AND ((SELECT NOT public.is_link_account()) OR mv.observation = (SELECT public.link_observation()))
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
      -- Share links see only the observation they were minted for (mirrors
      -- accessible_observations_select; the matview has no RLS to do it).
      AND ((SELECT NOT public.is_link_account()) OR mv.observation = (SELECT public.link_observation()))
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

-- Publish / revoke refresh the published-only snapshots in the same
-- transaction (the web action and `campfire deploy publish|revoke` both go
-- through this RPC).
CREATE OR REPLACE FUNCTION public.set_deployment_status(
  p_deployment_id integer,
  p_to text,
  p_actor uuid DEFAULT NULL,
  p_host text DEFAULT NULL
)
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_is_admin boolean;
  v_obs text;
  v_field text;
  v_action text;
  v_spectrum_ids integer[];
  v_n_images integer;
  v_result json;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_to NOT IN ('draft', 'published', 'revoked') THEN
    RAISE EXCEPTION 'Invalid status: %', p_to;
  END IF;

  SELECT observation, field INTO v_obs, v_field FROM deployments WHERE id = p_deployment_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Deployment % not found', p_deployment_id;
  END IF;

  -- NIRCam field-scoped deployment (epic #261, N1/N2): exposure/mosaic FITS
  -- visibility rides deployment.status via the storage_objects gate; the public
  -- mosaic index (nircam_images) carries its own deploy_status, flipped here to
  -- match (mirrors how the observation path flips spectra.deploy_status).
  IF v_field IS NOT NULL THEN
    v_action := CASE WHEN p_to = 'revoked' THEN 'revoke'
                     WHEN p_to = 'draft' THEN 'upload' ELSE 'publish' END;
    UPDATE deployments SET
      status = p_to,
      published_at = CASE WHEN p_to = 'published' THEN now() ELSE published_at END,
      revoked_at = CASE WHEN p_to = 'revoked' THEN now() ELSE revoked_at END
    WHERE id = p_deployment_id;
    WITH flipped AS (
      UPDATE nircam_images SET deploy_status = p_to
      WHERE deployment_id = p_deployment_id AND deploy_status <> p_to
      RETURNING 1)
    SELECT count(*) INTO v_n_images FROM flipped;
    INSERT INTO deploy_events (actor, action, deployment_id, field, status_to, host, affected_count, metadata)
      VALUES (p_actor, v_action, p_deployment_id, v_field, p_to, p_host, v_n_images,
              jsonb_build_object(
                'instrument', 'nircam',
                'scope', jsonb_build_object('field', v_field),
                'counts', jsonb_build_object('succeeded', v_n_images),
                'flags', jsonb_build_object('lifecycle', true)));
    RETURN json_build_object(
      'deployment_id', p_deployment_id, 'field', v_field, 'status', p_to,
      'nircam_images', json_build_object('updated', v_n_images, 'action', v_action));
  END IF;

  -- Which current statuses transition to p_to:
  --   p_to='published'  -> draft (first publish) OR revoked (recover) become visible
  --   p_to='revoked'    -> published spectra are hidden
  --   p_to='draft'    -> published spectra go back to draft
  -- The prior version matched only 'draft' for the published case, so recovering
  -- a REVOKED deployment flipped the deployment row but left its spectra revoked
  -- and hidden ("0 updated", silently inconsistent) — #233 review.
  SELECT array_agg(s.id) INTO v_spectrum_ids
  FROM spectra s JOIN targets t ON s.target_id = t.target_id
  WHERE t.observation = v_obs
    AND s.deploy_status = ANY (
      CASE p_to WHEN 'published' THEN ARRAY['draft', 'revoked']
                WHEN 'revoked'   THEN ARRAY['published']
                ELSE                  ARRAY['published'] END);

  -- Audit label: publishing previously-revoked spectra is a 'recover', not a
  -- first 'publish'. Computed before the transition (spectra still hold old status).
  v_action := CASE
    WHEN p_to = 'revoked' THEN 'revoke'
    WHEN p_to = 'draft' THEN 'upload'
    WHEN EXISTS (SELECT 1 FROM spectra s JOIN targets t ON s.target_id = t.target_id
                 WHERE t.observation = v_obs AND s.deploy_status = 'revoked')
      THEN 'recover'
    ELSE 'publish'
  END;

  UPDATE deployments SET
    status = p_to,
    published_at = CASE WHEN p_to = 'published' THEN now() ELSE published_at END,
    revoked_at = CASE WHEN p_to = 'revoked' THEN now() ELSE revoked_at END
  WHERE id = p_deployment_id;

  IF v_spectrum_ids IS NOT NULL THEN
    v_result := public.set_spectra_deploy_status(
      p_spectrum_db_ids := v_spectrum_ids, p_to := p_to, p_action := v_action,
      p_actor := p_actor, p_deployment_id := p_deployment_id, p_host := p_host);
  ELSE
    v_result := json_build_object('updated', 0, 'action', v_action);
  END IF;

  -- The published-only snapshots (mv_observations_overview, and the two
  -- older matviews, which also filter on deploy_status) must follow a
  -- publish / revoke immediately, not at the nightly refresh (perf T1-5 /
  -- #501). Same transaction, so the refresh sees the flipped rows.
  PERFORM public.refresh_all_matviews();

  RETURN json_build_object(
    'deployment_id', p_deployment_id, 'observation', v_obs,
    'status', p_to, 'spectra', v_result);
END;
$$;

GRANT EXECUTE ON FUNCTION public.set_deployment_status(integer, text, uuid, text) TO authenticated, service_role;
