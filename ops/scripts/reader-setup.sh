#!/usr/bin/env bash
# Set up (or rotate) the public read-only SQL login (docs/reader-role.md).
#
#   ops/scripts/reader-setup.sh            # set the pooler's credential, apply, start the pooler
#   ops/scripts/reader-setup.sh rotate     # new password, same steps
#   ops/scripts/reader-setup.sh status     # role settings, pooler health, a test query
#
# Readers connect to the pooler without a password (auth_type trust: the data
# is public, the role's grants are the gate). The password this script sets is
# the pooler's own login to db, nothing a reader needs or is told.
#
# What it does, idempotently:
#   1. ensures the campfire_reader role exists in db (the migration chain
#      creates it; this is a belt for a cluster restored before that migration);
#   2. sets its password from READER_PASSWORD in ops/.env (generated when
#      empty, written back to .env);
#   3. writes the PgBouncer userlist with that one role and the plaintext
#      password (trust auth admits only names in the userlist, and the pooler
#      logs in to db with the password; the file grants nothing the pooler
#      does not already grant everyone), plus the stored SCRAM verifier in
#      verifier.txt, the reference scripts/reader-heal.sh compares against;
#   4. makes a self-signed certificate for the pooler when pgbouncer/reader.pem
#      is missing (the pooler refuses plaintext; libpq negotiates TLS by default);
#   5. starts or restarts the pooler (compose profile "reader") and runs one
#      query through it, the way a reader would.
# Needs: ops/.env, docker compose, the db container up. Run from the repo on
# the VM as the user that runs the stack.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
# shellcheck source=lib.sh
source "$here/scripts/lib.sh"; load_env "$here/.env"     # literal parse: never source a compose .env
root=${CAMPFIRE_DATA_ROOT:-/srv/campfire}
compose="docker compose --env-file .env -f docker-compose.yml"
psql_db() { $compose exec -T db psql -v ON_ERROR_STOP=1 -U postgres -d "${POSTGRES_DB:-postgres}" "$@"; }
mode=${1:-setup}
reader_host=${READER_HOST:-db.${WEB_HOST:-campfire-jwst.org}}
reader_port=${READER_PORT:-5432}
db_name=${POSTGRES_DB:-postgres}
# The pooler binds the VM's interface address, not 0.0.0.0: db publishes
# 127.0.0.1:$POSTGRES_PORT (deploy.sh, the operator's psql) and one host port
# cannot carry both bindings. Detected once and written to .env.
if [ -z "${READER_BIND:-}" ]; then
  READER_BIND=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") print $(i + 1)}' | head -n 1)
  [ -n "$READER_BIND" ] || { echo "cannot detect the interface address; set READER_BIND in .env"; exit 1; }
  if grep -q '^READER_BIND=' .env; then
    sed -i "s|^READER_BIND=.*|READER_BIND=${READER_BIND}|" .env
  else
    printf '\nREADER_BIND=%s\n' "$READER_BIND" >> .env
  fi
  echo "READER_BIND=${READER_BIND} written to ops/.env"
fi
# the published form: no password, no port when it is the default
port_suffix=""; [ "$reader_port" = 5432 ] || port_suffix=":${reader_port}"
conn_string="postgresql://campfire_reader@${reader_host}${port_suffix}/${db_name}"
reader_query() { psql "host=${READER_BIND} port=${reader_port} dbname=${db_name} user=campfire_reader sslmode=require" "$@"; }

if [ "$mode" = status ]; then
  psql_db -Atc "SELECT rolname, rolconfig, rolconnlimit FROM pg_roles WHERE rolname = 'campfire_reader'"
  $compose --profile reader ps pgbouncer || true
  reader_query -Atc "SELECT count(*) AS public_objects FROM objects" || echo "pooler query failed"
  echo "connection string: ${conn_string}"
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

# 3: the pooler's directory: the userlist (that one name, with the password the
# pooler logs in to db with), the heal's verifier record and the certificate.
# The container runs as its own uid, so the directory is world-traversable and
# the files world-readable: the password selects a read-only role the pooler
# hands to everyone anyway, the verifier is salted, and the key signs a
# self-signed certificate. (certs/ is 0750 for the ops user and stays untouched.)
install -d -m 0755 "$root/pgbouncer"
verifier=$(psql_db -Atc "SELECT rolpassword FROM pg_authid WHERE rolname = 'campfire_reader'")
case "$verifier" in SCRAM-SHA-256*) ;; *) echo "expected a SCRAM verifier, got: ${verifier:0:20}"; exit 1;; esac
umask 022
printf '"campfire_reader" "%s"\n' "$READER_PASSWORD" > "$root/pgbouncer/userlist.txt"
printf '%s\n' "$verifier" > "$root/pgbouncer/verifier.txt"
chmod 644 "$root/pgbouncer/userlist.txt" "$root/pgbouncer/verifier.txt"
echo "userlist and verifier record written under $root/pgbouncer/"

# 4: a certificate for the pooler (self-signed)
if [ ! -f "$root/pgbouncer/reader.pem" ] || [ ! -f "$root/pgbouncer/reader.key" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 1095 -subj "/CN=${reader_host}" \
    -addext "subjectAltName=DNS:${reader_host}" \
    -keyout "$root/pgbouncer/reader.key" -out "$root/pgbouncer/reader.pem" 2>/dev/null
  echo "self-signed certificate written for ${reader_host}"
fi
chmod 644 "$root/pgbouncer/reader.pem" "$root/pgbouncer/reader.key"

# 5: the pooler, then one query the way a reader makes it (no password)
$compose --profile reader up -d --force-recreate pgbouncer
sleep 3
reader_query -Atc "SELECT count(*) AS public_objects FROM objects"
echo "pooler up on ${READER_BIND}:${reader_port}; connection string to publish:"
echo "  ${conn_string}"
echo "Then, once, as root (the password heal, scripts/reader-heal.sh):"
echo "  install -m 0644 ${here}/systemd/campfire-reader-heal.service ${here}/systemd/campfire-reader-heal.timer /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now campfire-reader-heal.timer"
