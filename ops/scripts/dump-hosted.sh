#!/usr/bin/env bash
# Dump the hosted Supabase project for the Phase 2 cutover (ops/README.md,
# "Database cutover checklist", step 2), plus the reference material
# restore-hosted.sh verifies against.
#
#   ops/scripts/dump-hosted.sh "$OLD_DB_URL" <out dir>
#
# OLD_DB_URL: the hosted connection string. From the VM only the IPv4 session
# pooler resolves (aws-1-<region>.pooler.supabase.com:5432, user
# postgres.<ref>); the direct db.<ref>.supabase.co host is IPv6-only.
#
# Writes into <out dir>:
#   roles.sql               cluster roles + role settings (statement_timeout,
#                           pgrst.db_pre_request on authenticator)
#   schema.sql              everything but auth/storage DDL and supabase_migrations
#   migrations_history.sql  supabase_migrations.schema_migrations (the CLI dump skips it)
#   data.sql                all rows, COPY format (auth.* and public.*; the empty
#                           storage.* blocks are dropped at restore time)
#   cron_jobs.sql           cron.schedule() calls re-creating the pg_cron jobs
#                           (cron.job is not dumped)
#   reference.txt           row counts the restore must reproduce
#   fingerprint-hosted.txt  ops/sql/fingerprint.sql run on the hosted database
# Rehearsal 2026-10-24: 70 s for 2.2 GB (1.2 GB data.sql).
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
source "${OPS_DIR}/scripts/lib.sh"; load_env "${OPS_DIR}/.env"
SUPABASE_CLI_VERSION="${SUPABASE_CLI_VERSION:-2.120.0}"

OLD="${1:-}"; OUT="${2:-}"
[[ -n "$OLD" && -n "$OUT" ]] || { echo "usage: dump-hosted.sh <hosted db url> <out dir>" >&2; exit 2; }
mkdir -p "$OUT"; cd "$OUT"
command -v pg_dump >/dev/null || { echo "pg_dump missing (postgresql-client-17)" >&2; exit 1; }

T0=$(date +%s); step() { printf '[dump %4ss] %s\n' "$(( $(date +%s) - T0 ))" "$*"; }
cli() { npx -y "supabase@${SUPABASE_CLI_VERSION}" "$@"; }

step "roles";   cli db dump --db-url "$OLD" -f roles.sql --role-only
step "schema";  cli db dump --db-url "$OLD" -f schema.sql
step "history"; pg_dump "$OLD" --schema=supabase_migrations --no-owner --no-privileges -f migrations_history.sql
step "data";    cli db dump --db-url "$OLD" -f data.sql --data-only --use-copy
step "cron jobs, reference counts, fingerprint"
psql "$OLD" -Atc "select format('select cron.schedule(%L, %L, %L);', jobname, schedule, command) from cron.job where active order by jobid" > cron_jobs.sql
psql "$OLD" -At -F'|' <<'SQL' > reference.txt
select 'users', count(*) from auth.users
union all select 'spectra', count(*) from public.spectra
union all select 'objects', count(*) from public.objects
union all select 'storage_objects', count(*) from public.storage_objects
union all select 'migrations', count(*) from supabase_migrations.schema_migrations
union all select 'auth_migrations_max', max(version)::bigint from auth.schema_migrations
SQL
psql "$OLD" -At -F'|' -f "${OPS_DIR}/sql/fingerprint.sql" > fingerprint-hosted.txt

grep -q 'pgrst.db_pre_request' roles.sql || echo "WARNING: roles.sql lacks the authenticator pgrst.db_pre_request setting" >&2
step "done"; ls -la "$OUT"; cat reference.txt
