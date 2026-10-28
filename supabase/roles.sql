-- Custom roles for the local stack and the shadow database (Supabase CLI:
-- "Include custom roles from supabase/roles.sql", applied before the
-- migration chain). Roles are cluster objects pg-delta does not track, so
-- production gets the same role from the hand-authored migration
-- 20261028193000_phase4_reader_role.sql, and the grants and policies that name
-- it are declarative in supabase/schemas/ as usual. Keep the two in step.
--
-- campfire_reader (Catalog Read Model phase 4, docs/reader-role.md): a
-- read-only Postgres login for public data. SELECT on the catalog tables and
-- the published data releases under its own RLS policies, nothing else: no
-- functions, no sequences, no other schema. No password here: the operator
-- sets one on the VM (ops/scripts/reader-setup.sh); locally the role has no
-- password and is reached with SET ROLE.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'campfire_reader') THEN
    CREATE ROLE campfire_reader LOGIN NOINHERIT NOCREATEDB NOCREATEROLE NOSUPERUSER NOREPLICATION
      CONNECTION LIMIT 20;
  END IF;
END $$;
-- The migration chain runs as postgres and alters this role (its settings).
-- Postgres 16+ lets a role alter only the roles it holds ADMIN on: the
-- creator gets it automatically, so when this file ran as a superuser (the
-- shadow database, the local stack) postgres needs the grant; when postgres
-- ran it (a psql -f apply), it is the creator and the grant would be refused
-- ("cannot be granted back to your own grantor").
DO $$
BEGIN
  IF current_user <> 'postgres' THEN
    GRANT campfire_reader TO postgres WITH ADMIN OPTION;
  END IF;
END $$;
ALTER ROLE campfire_reader SET statement_timeout = '30s';
ALTER ROLE campfire_reader SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE campfire_reader SET lock_timeout = '5s';
ALTER ROLE campfire_reader SET default_transaction_read_only = on;
ALTER ROLE campfire_reader SET search_path = public;
