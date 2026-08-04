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
