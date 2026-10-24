#!/usr/bin/env bash
# Prove the newest backup restores: load it into a throwaway Postgres
# container and count rows in spectra and objects.
#
#   ops/scripts/restore-check.sh      (campfire-restore-check.timer, Sundays 06:00 UTC)
#
# Uses the same supabase/postgres image as the stack so every role and
# extension the dump references already exists. Pings
# $HEALTHCHECK_PING_URL_RESTORE on success, <url>/fail otherwise.
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1090
set -a; source "${OPS_DIR}/.env"; set +a

DATA_ROOT="${CAMPFIRE_DATA_ROOT:-/srv/campfire}"
LOCAL_DIR="${DATA_ROOT}/backups"
NAME=campfire-restore-check
PING="${HEALTHCHECK_PING_URL_RESTORE:-}"
MIN_SPECTRA="${RESTORE_CHECK_MIN_SPECTRA:-1000}"

ping() { [[ -n "$PING" ]] && curl -fsS -m 10 --retry 3 -o /dev/null "${PING}${1:-}" || true; }
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
fail() { echo "restore-check FAILED: $*" >&2; cleanup; ping /fail; exit 1; }
trap 'fail "line $LINENO"' ERR

DUMP="$(ls -1t "${LOCAL_DIR}"/campfire-*.dump 2>/dev/null | head -1)"
[[ -n "$DUMP" ]] || fail "no dump in ${LOCAL_DIR}"
IMAGE="$(docker inspect --format '{{.Config.Image}}' campfire-db)"
echo "[restore-check] ${DUMP} into ${IMAGE}"

cleanup
docker run -d --name "$NAME" --rm \
  -e POSTGRES_PASSWORD=restorecheck -e POSTGRES_HOST=/var/run/postgresql \
  -v "${LOCAL_DIR}:/backups:ro" \
  "$IMAGE" postgres -c config_file=/etc/postgresql/postgresql.conf -c log_min_messages=fatal >/dev/null

for _ in $(seq 1 60); do
  docker exec "$NAME" pg_isready -U postgres -h localhost >/dev/null 2>&1 && break
  sleep 2
done
docker exec "$NAME" pg_isready -U postgres -h localhost >/dev/null

# Pre-existing extensions/schemas in the image produce "already exists"
# errors; those are expected, so the exit status is not trusted. The row
# counts below are the verdict.
docker exec "$NAME" pg_restore -U supabase_admin -d postgres --no-owner --role=postgres \
  "/backups/$(basename "$DUMP")" >/tmp/restore-check.log 2>&1 || true

SPECTRA="$(docker exec "$NAME" psql -U supabase_admin -d postgres -Atc 'select count(*) from public.spectra')"
OBJECTS="$(docker exec "$NAME" psql -U supabase_admin -d postgres -Atc 'select count(*) from public.objects')"
USERS="$(docker exec "$NAME" psql -U supabase_admin -d postgres -Atc 'select count(*) from auth.users')"
echo "[restore-check] spectra=${SPECTRA} objects=${OBJECTS} users=${USERS}"
cleanup

(( SPECTRA >= MIN_SPECTRA )) || fail "only ${SPECTRA} spectra restored (min ${MIN_SPECTRA})"
(( OBJECTS > 0 )) || fail "no objects restored"
(( USERS > 0 )) || fail "no auth.users restored"

printf '%s %s spectra=%s objects=%s users=%s\n' "$(date -u +%FT%TZ)" "$(basename "$DUMP")" "$SPECTRA" "$OBJECTS" "$USERS" \
  > "${LOCAL_DIR}/last-restore-check"
echo "[restore-check] ok"
ping
