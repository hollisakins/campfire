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
# shellcheck source=lib.sh
source "${OPS_DIR}/scripts/lib.sh"; load_env "${OPS_DIR}/.env"

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

# --clean --if-exists so the image's baseline auth/extension objects are
# replaced rather than colliding (a collision on auth.users would report
# users=0 as a false alarm). Restore as the superuser itself: with
# --role=postgres every statement ran as `postgres`, which cannot create in
# the image's `auth` schema ("permission denied for schema auth"), so the auth
# tables were never created and the first run reported users=0 (2026-10-25).
# Extension / event-trigger "already exists" and "must be owner" errors remain,
# so the exit status is not trusted; the row counts below are the verdict.
RESTORE_LOG="$(mktemp /tmp/restore-check.XXXXXX.log)"   # a fixed path collides between the root (systemd) and ops-user runs
docker exec "$NAME" pg_restore -U supabase_admin -d postgres --clean --if-exists --no-owner \
  "/backups/$(basename "$DUMP")" >"$RESTORE_LOG" 2>&1 || true
echo "[restore-check] pg_restore log: ${RESTORE_LOG} ($(grep -c 'error' "$RESTORE_LOG" || true) error lines)"

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
