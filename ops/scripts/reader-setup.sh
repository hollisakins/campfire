#!/usr/bin/env bash
# Set up (or rotate) the public read-only SQL login (docs/reader-role.md).
#
#   ops/scripts/reader-setup.sh            # generate a password, apply, start the pooler
#   ops/scripts/reader-setup.sh rotate     # new password, same steps
#   ops/scripts/reader-setup.sh status     # role settings, pooler health, a test query
#
# What it does, idempotently:
#   1. ensures the campfire_reader role exists in db (the migration chain
#      creates it; this is a belt for a cluster restored before that migration);
#   2. sets its password from READER_PASSWORD in ops/.env (generated when
#      empty, written back to .env);
#   3. writes the PgBouncer SCRAM userlist with that one role, from the
#      verifier Postgres stores (pg_authid), so the pooler never sees the
#      plaintext and no other role can authenticate at it;
#   4. makes a self-signed certificate for the pooler when certs/reader.pem is
#      missing (clients use sslmode=require; see the doc for verify-full);
#   5. starts or restarts the pooler (compose profile "reader") and runs one
#      query through it.
# Needs: ops/.env, docker compose, the db container up. Run from the repo on
# the VM as the user that runs the stack.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
[ -f .env ] || { echo "ops/.env missing"; exit 1; }
set -a; . ./.env; set +a
root=${CAMPFIRE_DATA_ROOT:-/srv/campfire}
compose="docker compose --env-file .env -f docker-compose.yml"
psql_db() { $compose exec -T db psql -v ON_ERROR_STOP=1 -U postgres -d "${POSTGRES_DB:-postgres}" "$@"; }
mode=${1:-setup}
reader_host=${READER_HOST:-db.${WEB_HOST:-campfire-jwst.org}}
reader_port=${READER_PORT:-6432}
db_name=${POSTGRES_DB:-postgres}

if [ "$mode" = status ]; then
  psql_db -Atc "SELECT rolname, rolconfig, rolconnlimit FROM pg_roles WHERE rolname = 'campfire_reader'"
  $compose --profile reader ps pgbouncer || true
  PGPASSWORD="$READER_PASSWORD" psql "host=127.0.0.1 port=${reader_port} dbname=${db_name} user=campfire_reader sslmode=require" \
    -Atc "SELECT count(*) AS public_objects FROM objects" || echo "pooler query failed"
  exit 0
fi

if [ "$mode" = rotate ] || [ -z "${READER_PASSWORD:-}" ]; then
  READER_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-28)
  if grep -q '^READER_PASSWORD=' .env; then
    sed -i "s|^READER_PASSWORD=.*|READER_PASSWORD=${READER_PASSWORD}|" .env
  else
    printf '\nREADER_PASSWORD=%s\n' "$READER_PASSWORD" >> .env
  fi
  echo "READER_PASSWORD written to ops/.env"
fi

# 1 + 2: role and password (the migration chain created the role; ALTER is idempotent)
psql_db -q <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'campfire_reader') THEN
    CREATE ROLE campfire_reader LOGIN NOINHERIT NOCREATEDB NOCREATEROLE NOSUPERUSER NOREPLICATION CONNECTION LIMIT 20;
  END IF;
END \$\$;
ALTER ROLE campfire_reader PASSWORD '${READER_PASSWORD}';
SQL
echo "campfire_reader password set"

# 3: the pooler's userlist from the stored SCRAM verifier (never the plaintext)
mkdir -p "$root/pgbouncer"
verifier=$(psql_db -Atc "SELECT rolpassword FROM pg_authid WHERE rolname = 'campfire_reader'")
case "$verifier" in SCRAM-SHA-256*) ;; *) echo "expected a SCRAM verifier, got: ${verifier:0:20}"; exit 1;; esac
umask 077
printf '"campfire_reader" "%s"\n' "$verifier" > "$root/pgbouncer/userlist.txt"
echo "userlist written: $root/pgbouncer/userlist.txt"

# 4: a certificate for the pooler (self-signed; sslmode=require)
mkdir -p "$root/certs"
if [ ! -f "$root/certs/reader.pem" ] || [ ! -f "$root/certs/reader.key" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 1095 -subj "/CN=${reader_host}" \
    -addext "subjectAltName=DNS:${reader_host}" \
    -keyout "$root/certs/reader.key" -out "$root/certs/reader.pem" 2>/dev/null
  echo "self-signed certificate written for ${reader_host} (sslmode=require)"
fi
# PgBouncer runs as its own uid inside the container: both files must be readable there
chmod 644 "$root/certs/reader.pem" "$root/certs/reader.key"
chmod 644 "$root/pgbouncer/userlist.txt"

# 5: the pooler
$compose --profile reader up -d --force-recreate pgbouncer
sleep 3
PGPASSWORD="$READER_PASSWORD" psql "host=127.0.0.1 port=${reader_port} dbname=${db_name} user=campfire_reader sslmode=require" \
  -Atc "SELECT count(*) AS public_objects FROM objects"
echo "pooler up on ${reader_port}; connection string:"
echo "  postgresql://campfire_reader:${READER_PASSWORD}@${reader_host}:${reader_port}/${db_name}?sslmode=require"
