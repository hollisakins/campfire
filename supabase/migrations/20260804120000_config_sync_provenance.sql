-- Config sync provenance (issue #303): cloud as source of truth for the three
-- data-management config files (programs.toml / observations.toml / fields.toml).
--
-- Each registry row gains the same contract:
--   config            — the TOML section mirrored losslessly (fields already had
--                       this; observations/programs gain it so stage overrides,
--                       config_groups etc. survive the round trip)
--   config_hash       — client-computed sha256 of the canonical JSON form of the
--                       section (sorted keys); the divergence token used by
--                       `campfire config pull/push/diff`
--   config_updated_at — when the config was last pushed
--   retired_at        — soft retirement for renamed/removed definitions; sync is
--                       additive and rows are never deleted (storage_objects FKs
--                       reference observations.name, and history stays auditable)
--
-- deploy_events.action gains 'config_sync' so explicit config pushes are not
-- auditless (admin audit 2026-07-03, B3).

ALTER TABLE "public"."observations"
  ADD COLUMN IF NOT EXISTS "config" "jsonb",
  ADD COLUMN IF NOT EXISTS "config_hash" "text",
  ADD COLUMN IF NOT EXISTS "config_updated_at" timestamp with time zone,
  ADD COLUMN IF NOT EXISTS "retired_at" timestamp with time zone;

ALTER TABLE "public"."programs"
  ADD COLUMN IF NOT EXISTS "config" "jsonb",
  ADD COLUMN IF NOT EXISTS "config_hash" "text",
  ADD COLUMN IF NOT EXISTS "config_updated_at" timestamp with time zone,
  ADD COLUMN IF NOT EXISTS "retired_at" timestamp with time zone;

ALTER TABLE "public"."fields"
  ADD COLUMN IF NOT EXISTS "config_hash" "text",
  ADD COLUMN IF NOT EXISTS "config_updated_at" timestamp with time zone,
  ADD COLUMN IF NOT EXISTS "retired_at" timestamp with time zone;

ALTER TABLE "public"."deploy_events"
  DROP CONSTRAINT IF EXISTS "deploy_events_action_check";

ALTER TABLE "public"."deploy_events"
  ADD CONSTRAINT "deploy_events_action_check"
  CHECK (("action" = ANY (ARRAY['upload'::"text", 'publish'::"text", 'revoke'::"text", 'recover'::"text", 'supersede'::"text", 'delete'::"text", 'config_sync'::"text"])));

-- The log_deploy_event RPC — the only sanctioned write path to deploy_events —
-- validates the action against its own whitelist, independent of the table
-- CHECK, so it must learn 'config_sync' too. Hand-authored (migra does not
-- diff function bodies); full definition mirrored from schemas/functions.sql.
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
