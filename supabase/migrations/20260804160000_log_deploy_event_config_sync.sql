-- The log_deploy_event RPC — the only sanctioned write path to deploy_events —
-- validates the action against its own whitelist, independent of the
-- deploy_events_action_check table constraint widened in 20260804120000. It
-- must learn 'config_sync' too, or every `campfire config push` audit write
-- raises and is swallowed as a warning (no row lands, defeating the point).
--
-- Separate migration rather than an edit to 20260804120000: Supabase preview
-- branches only push NEW migration files, so an edited already-applied file
-- would silently never reach environments that ran the original.
-- Hand-authored (migra does not diff function bodies); full definition
-- mirrored from schemas/functions.sql.
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

  IF p_action NOT IN ('upload', 'publish', 'revoke', 'recover', 'supersede', 'delete', 'config_sync') THEN
    RAISE EXCEPTION 'Invalid deploy_event action: %', p_action;
  END IF;

  INSERT INTO deploy_events(actor, action, deployment_id, observation, field, affected_count, metadata, host)
  VALUES (p_actor, p_action, p_deployment_id, p_observation, p_field, p_affected_count, p_metadata, p_host)
  RETURNING id INTO v_id;
  RETURN v_id;
END;
$$;
