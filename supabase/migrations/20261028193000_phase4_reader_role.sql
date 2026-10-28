SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Catalog Read Model phase 4: the campfire_reader role (docs/reader-role.md).
-- Hand-authored on purpose: a role is a cluster object pg-delta does not
-- track. supabase/roles.sql creates the same role for the local stack and the
-- shadow database; the grants and the RLS policies that name it are
-- declarative in supabase/schemas/ and land in the generated migration that
-- follows this one. Idempotent: production may already carry the role when
-- the operator pre-created it. No password is set here (the repository is
-- public): ops/scripts/reader-setup.sh sets it on the VM.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'campfire_reader') THEN
    CREATE ROLE campfire_reader LOGIN NOINHERIT NOCREATEDB NOCREATEROLE NOSUPERUSER NOREPLICATION
      CONNECTION LIMIT 20;
  END IF;
END $$;
ALTER ROLE campfire_reader SET statement_timeout = '30s';
ALTER ROLE campfire_reader SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE campfire_reader SET lock_timeout = '5s';
ALTER ROLE campfire_reader SET default_transaction_read_only = on;
ALTER ROLE campfire_reader SET search_path = public;
