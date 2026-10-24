-- Service-role passwords from POSTGRES_PASSWORD, applied by the image's
-- migrate.sh on first init (ON_ERROR_STOP=1, before the image's own 53 schema
-- migrations). Guarded: the upstream file also alters supabase_functions_admin,
-- which only webhooks.sql (edge functions, not in this stack) creates, and one
-- failing statement here aborts the whole init — the cluster then comes up
-- with auth.uid() owned by postgres (GoTrue cannot migrate), no vault/graphql
-- schemas, and a healthy-looking but unusable database (rehearsal 2026-10-24).
\set pgpass `echo "$POSTGRES_PASSWORD"`

-- psql does not interpolate :'pgpass' inside a dollar-quoted DO body, so the
-- guard is a query whose rows \gexec runs as statements.
SELECT format('ALTER USER %I WITH PASSWORD %L', rolname, :'pgpass')
  FROM pg_roles
 WHERE rolname IN ('authenticator', 'pgbouncer', 'supabase_auth_admin',
                   'supabase_functions_admin', 'supabase_storage_admin')
\gexec
