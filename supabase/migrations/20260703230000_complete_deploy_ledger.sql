-- Admin audit 2026-07-03, Phase 3 (docs/admin_audit_2026-07-03.md Theme B / §5):
-- complete the deploy ledger.
--
-- 1. deploy_events.field — first-class NIRCam scope column (was buried in
--    metadata->>'field'). Backfilled from the legacy metadata so all history is
--    reachable, then get_admin_deploy_events reads the column, not metadata.
-- 2. log_deploy_event gains p_field. Appending a param changes the arity, so the
--    old 7-arg overload MUST be dropped first or PostgREST sees two candidates
--    (PGRST203).
-- 3. The two lifecycle emitters (set_deployment_status NIRCam branch,
--    set_spectra_deploy_status) write the normalized metadata envelope
--    {instrument, scope, counts, flags} and set the field column.
--
-- Hand-authored (function bodies match supabase/schemas/functions.sql verbatim;
-- supabase db diff unavailable in the authoring environment). deploy_events is
-- not in seed.sql, so no seed regen.

ALTER TABLE public.deploy_events ADD COLUMN IF NOT EXISTS field text;

-- Backfill legacy NIRCam events (scope only ever lived in metadata).
UPDATE public.deploy_events
   SET field = metadata ->> 'field'
 WHERE field IS NULL AND metadata ? 'field';

COMMENT ON COLUMN public.deploy_events.field IS
  'NIRCam field scope (audit B5, Phase 3). Mirrors deployments.field; NULL for NIRSpec (observation) events. Authoritative — get_admin_deploy_events reads this, not metadata.';

CREATE INDEX IF NOT EXISTS idx_deploy_events_field
    ON public.deploy_events USING btree (field)
    WHERE field IS NOT NULL;

-- Arity change: drop the old 7-arg log_deploy_event before recreating with p_field.
DROP FUNCTION IF EXISTS public.log_deploy_event(text, uuid, integer, text, integer, jsonb, text);

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.log_deploy_event(
  p_action text,
  p_actor uuid DEFAULT NULL,
  p_deployment_id integer DEFAULT NULL,
  p_observation text DEFAULT NULL,
  p_field text DEFAULT NULL,
  p_affected_count integer DEFAULT NULL,
  p_metadata jsonb DEFAULT NULL,
  p_host text DEFAULT NULL
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_is_admin boolean;
  v_id uuid;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_action NOT IN ('upload', 'publish', 'revoke', 'recover', 'supersede', 'delete') THEN
    RAISE EXCEPTION 'Invalid deploy_event action: %', p_action;
  END IF;

  INSERT INTO deploy_events(actor, action, deployment_id, observation, field, affected_count, metadata, host)
  VALUES (p_actor, p_action, p_deployment_id, p_observation, p_field, p_affected_count, p_metadata, p_host)
  RETURNING id INTO v_id;
  RETURN v_id;
END;
$$;

GRANT EXECUTE ON FUNCTION public.log_deploy_event(text, uuid, integer, text, text, integer, jsonb, text) TO authenticated, service_role;

CREATE OR REPLACE FUNCTION public.get_admin_deploy_events(
  p_action text DEFAULT NULL,
  p_observation text DEFAULT NULL,
  p_field text DEFAULT NULL,
  p_sort_column text DEFAULT 'occurred_at',
  p_sort_direction text DEFAULT 'desc',
  p_page integer DEFAULT 1,
  p_page_size integer DEFAULT 50
)
RETURNS TABLE (
  id uuid,
  action text,
  observation text,
  field text,
  deployment_id integer,
  status_to text,
  affected_count integer,
  occurred_at timestamptz,
  actor uuid,
  actor_name text,
  total_count bigint
)
LANGUAGE plpgsql STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
  v_limit integer := LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
  v_offset integer := GREATEST(COALESCE(p_page, 1) - 1, 0) * LEAST(GREATEST(COALESCE(p_page_size, 50), 1), 200);
BEGIN
  IF NOT public.is_admin() THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'desc'; END IF;
  IF p_sort_column NOT IN ('occurred_at', 'action') THEN
    p_sort_column := 'occurred_at';
  END IF;

  RETURN QUERY
  SELECT e.id, e.action, e.observation,
         e.field,
         e.deployment_id, e.status_to, e.affected_count, e.occurred_at,
         e.actor,
         COALESCE(up.full_name, up.username) AS actor_name,
         count(*) OVER ()
  FROM deploy_events e
  LEFT JOIN user_profiles up ON up.user_id = e.actor
  WHERE (p_action IS NULL OR e.action = p_action)
    AND (p_observation IS NULL OR e.observation = p_observation)
    AND (p_field IS NULL OR e.field = p_field)
  ORDER BY
    CASE WHEN p_sort_column = 'occurred_at' AND p_sort_direction = 'desc' THEN e.occurred_at END DESC,
    CASE WHEN p_sort_column = 'occurred_at' AND p_sort_direction = 'asc'  THEN e.occurred_at END ASC,
    CASE WHEN p_sort_column = 'action' AND p_sort_direction = 'desc' THEN e.action END DESC,
    CASE WHEN p_sort_column = 'action' AND p_sort_direction = 'asc'  THEN e.action END ASC,
    e.occurred_at DESC
  OFFSET v_offset LIMIT v_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_admin_deploy_events TO authenticated;

CREATE OR REPLACE FUNCTION public.set_spectra_deploy_status(
  p_spectrum_db_ids integer[],
  p_to text,
  p_action text DEFAULT NULL,
  p_actor uuid DEFAULT NULL,
  p_deployment_id integer DEFAULT NULL,
  p_host text DEFAULT NULL
)
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_is_admin boolean;
  v_action text;
  v_updated int := 0;
  v_target_ids text[];
  v_recompute json;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_to NOT IN ('draft', 'published', 'revoked') THEN
    RAISE EXCEPTION 'Invalid deploy_status: %', p_to;
  END IF;

  v_action := COALESCE(p_action,
    CASE p_to WHEN 'published' THEN 'publish' WHEN 'revoked' THEN 'revoke' ELSE 'upload' END);
  IF v_action NOT IN ('upload', 'publish', 'revoke', 'recover', 'supersede', 'delete') THEN
    RAISE EXCEPTION 'Invalid action: %', v_action;
  END IF;

  IF p_spectrum_db_ids IS NULL OR array_length(p_spectrum_db_ids, 1) IS NULL THEN
    RETURN json_build_object('updated', 0, 'action', v_action);
  END IF;

  SELECT array_agg(DISTINCT s.target_id) INTO v_target_ids
  FROM spectra s WHERE s.id = ANY(p_spectrum_db_ids);

  UPDATE spectra s SET deploy_status = p_to
  WHERE s.id = ANY(p_spectrum_db_ids) AND s.deploy_status <> p_to;
  GET DIAGNOSTICS v_updated = ROW_COUNT;

  IF v_target_ids IS NOT NULL THEN
    v_recompute := public.recompute_has_published_spectrum(p_target_ids := v_target_ids);
  END IF;

  INSERT INTO deploy_events(actor, action, deployment_id, status_to, affected_count, host, metadata)
  VALUES (p_actor, v_action, p_deployment_id, p_to, v_updated, p_host,
          jsonb_build_object(
            'instrument', 'nirspec',
            'counts', jsonb_build_object(
              'succeeded', v_updated,
              'targets', COALESCE(array_length(v_target_ids, 1), 0)),
            'flags', jsonb_build_object('lifecycle', true)));

  RETURN json_build_object('updated', v_updated, 'action', v_action, 'recompute', v_recompute);
END;
$$;

GRANT EXECUTE ON FUNCTION public.set_spectra_deploy_status(integer[], text, text, uuid, integer, text) TO authenticated, service_role;

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

  RETURN json_build_object(
    'deployment_id', p_deployment_id, 'observation', v_obs,
    'status', p_to, 'spectra', v_result);
END;
$$;

GRANT EXECUTE ON FUNCTION public.set_deployment_status(integer, text, uuid, text) TO authenticated, service_role;

