#!/usr/bin/env bash
# Keep the campfire_reader password and role settings in step with the repository
# (docs/reader-role.md, supabase/roles.sql).
#
# A holder of the public credential can run `SET TRANSACTION READ WRITE;
# ALTER ROLE campfire_reader PASSWORD ...` (Postgres lets every role change
# its own password; neither the read-only default nor the grants stop it) and
# leave the pooler unable to open new server connections. This re-applies the
# password from .env, idempotently, every few minutes (campfire-reader-heal.timer),
# so the nuisance heals itself without rotating the published credential.
# Quiet when the reader is not set up or already in step.
set -euo pipefail
cd /srv/campfire/repo/ops
[ -f .env ] || exit 0
# shellcheck source=lib.sh
source /srv/campfire/repo/ops/scripts/lib.sh; load_env /srv/campfire/repo/ops/.env   # literal parse
[ -n "${READER_PASSWORD:-}" ] || exit 0
compose="docker compose --env-file .env -f docker-compose.yml"
db_name=${POSTGRES_DB:-postgres}
# The role settings a holder may also have changed (an ordinary role can set
# its own defaults: `ALTER ROLE campfire_reader SET search_path = pg_catalog`
# breaks every later unqualified query). The canonical set, as in
# supabase/roles.sql; anything else on the role is reset.
want_cfg='{statement_timeout=30s,idle_in_transaction_session_timeout=60s,lock_timeout=5s,default_transaction_read_only=on,"search_path=reader, public"}'
have_cfg=$($compose exec -T db psql -U postgres -d "$db_name" -Atc \
  "SELECT rolconfig FROM pg_roles WHERE rolname = 'campfire_reader'" 2>/dev/null || true)
# database-specific overrides (ALTER ROLE … IN DATABASE … SET) take precedence
# over the role's settings and are not in rolconfig: the role may set its own
have_db_cfg=$($compose exec -T db psql -U postgres -d "$db_name" -Atc \
  "SELECT count(*) FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole WHERE r.rolname = 'campfire_reader' AND s.setdatabase <> 0" 2>/dev/null || echo '?')
# the stored verifier changes with the password: compare to the one the pooler was given
have=$($compose exec -T db psql -U postgres -d "$db_name" -Atc \
  "SELECT rolpassword FROM pg_authid WHERE rolname = 'campfire_reader'" 2>/dev/null || true)
want=$(cat "${CAMPFIRE_DATA_ROOT:-/srv/campfire}/pgbouncer/userlist.txt" 2>/dev/null | sed -n 's/^"campfire_reader" "\(.*\)"$/\1/p' || true)
if [ -n "$have" ] && [ "$have" = "$want" ] && [ "$have_cfg" = "$want_cfg" ] && [ "$have_db_cfg" = "0" ]; then
  exit 0
fi
$compose exec -T db psql -v ON_ERROR_STOP=1 -q -U postgres -d "$db_name" <<SQL
ALTER ROLE campfire_reader PASSWORD '${READER_PASSWORD}';
ALTER ROLE campfire_reader RESET ALL;
-- every database that carries an override, not only ours (any database name is settable)
SELECT format('ALTER ROLE campfire_reader IN DATABASE %I RESET ALL', d.datname)
  FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole JOIN pg_database d ON d.oid = s.setdatabase
 WHERE r.rolname = 'campfire_reader'
\gexec
ALTER ROLE campfire_reader SET statement_timeout = '30s';
ALTER ROLE campfire_reader SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE campfire_reader SET lock_timeout = '5s';
ALTER ROLE campfire_reader SET default_transaction_read_only = on;
ALTER ROLE campfire_reader SET search_path = reader, public;
-- settings and a changed password reach new sessions only: end the pooled ones
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'campfire_reader' AND pid <> pg_backend_pid();
SQL
# the verifier is salted anew on every ALTER: refresh the pooler's copy so the
# next comparison is quiet, and so client SCRAM keeps matching
verifier=$($compose exec -T db psql -U postgres -d "${POSTGRES_DB:-postgres}" -Atc \
  "SELECT rolpassword FROM pg_authid WHERE rolname = 'campfire_reader'")
umask 022
printf '"campfire_reader" "%s"\n' "$verifier" > "${CAMPFIRE_DATA_ROOT:-/srv/campfire}/pgbouncer/userlist.txt"
$compose --profile reader kill -s HUP pgbouncer >/dev/null 2>&1 || true
logger -t campfire-reader-heal "campfire_reader password / settings re-applied (they had been changed)"
echo "campfire_reader password / settings re-applied"
