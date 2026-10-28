#!/usr/bin/env bash
# Keep the campfire_reader password in step with ops/.env (docs/reader-role.md).
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
set -a; . ./.env; set +a
[ -n "${READER_PASSWORD:-}" ] || exit 0
compose="docker compose --env-file .env -f docker-compose.yml"
# the stored verifier changes with the password: compare to the one the pooler was given
have=$($compose exec -T db psql -U postgres -d "${POSTGRES_DB:-postgres}" -Atc \
  "SELECT rolpassword FROM pg_authid WHERE rolname = 'campfire_reader'" 2>/dev/null || true)
want=$(cat "${CAMPFIRE_DATA_ROOT:-/srv/campfire}/pgbouncer/userlist.txt" 2>/dev/null | sed -n 's/^"campfire_reader" "\(.*\)"$/\1/p' || true)
if [ -n "$have" ] && [ "$have" = "$want" ]; then
  exit 0
fi
$compose exec -T db psql -v ON_ERROR_STOP=1 -q -U postgres -d "${POSTGRES_DB:-postgres}" <<SQL
ALTER ROLE campfire_reader PASSWORD '${READER_PASSWORD}';
SQL
# the verifier is salted anew on every ALTER: refresh the pooler's copy so the
# next comparison is quiet, and so client SCRAM keeps matching
verifier=$($compose exec -T db psql -U postgres -d "${POSTGRES_DB:-postgres}" -Atc \
  "SELECT rolpassword FROM pg_authid WHERE rolname = 'campfire_reader'")
umask 022
printf '"campfire_reader" "%s"\n' "$verifier" > "${CAMPFIRE_DATA_ROOT:-/srv/campfire}/pgbouncer/userlist.txt"
$compose --profile reader kill -s HUP pgbouncer >/dev/null 2>&1 || true
logger -t campfire-reader-heal "campfire_reader password re-applied (it had been changed)"
echo "campfire_reader password re-applied"
