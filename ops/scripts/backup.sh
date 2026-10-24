#!/usr/bin/env bash
# Nightly logical backup of the CAMPFIRE database to OSN.
#
#   ops/scripts/backup.sh            (campfire-backup.timer, 03:30 UTC)
#
# pg_dump -Fc of the catalog database plus a roles dump, written to
# $CAMPFIRE_DATA_ROOT/backups and copied with rclone to
# s3://$BACKUP_S3_BUCKET/$BACKUP_S3_PREFIX/{daily,monthly}/. Retention: 14
# daily, 12 monthly (taken on the 1st), $BACKUP_KEEP_LOCAL local files. Pings
# $HEALTHCHECK_PING_URL on success and <url>/fail on failure so a missed
# night raises an alert without the VM having to be up to send it.
#
# Restore (see README "Day 2"): pg_restore -d postgres --clean --if-exists <file>
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1090
set -a; source "${OPS_DIR}/.env"; set +a

DATA_ROOT="${CAMPFIRE_DATA_ROOT:-/srv/campfire}"
LOCAL_DIR="${DATA_ROOT}/backups"
POSTGRES_DB="${POSTGRES_DB:-postgres}"
KEEP_LOCAL="${BACKUP_KEEP_LOCAL:-3}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="campfire-${STAMP}.dump"
ROLES="campfire-${STAMP}.roles.sql"
PING="${HEALTHCHECK_PING_URL:-}"

ping() { [[ -n "$PING" ]] && curl -fsS -m 10 --retry 3 -o /dev/null "${PING}${1:-}" || true; }
fail() { echo "backup FAILED: $*" >&2; ping /fail; exit 1; }
trap 'fail "line $LINENO"' ERR

# rclone remote "osn" from env (no config file on disk).
export RCLONE_CONFIG_OSN_TYPE=s3
export RCLONE_CONFIG_OSN_PROVIDER=Ceph
export RCLONE_CONFIG_OSN_ENDPOINT="${BACKUP_S3_ENDPOINT}"
export RCLONE_CONFIG_OSN_ACCESS_KEY_ID="${BACKUP_S3_ACCESS_KEY_ID}"
export RCLONE_CONFIG_OSN_SECRET_ACCESS_KEY="${BACKUP_S3_SECRET_ACCESS_KEY}"
REMOTE="osn:${BACKUP_S3_BUCKET}/${BACKUP_S3_PREFIX:-db-backups}"

echo "[backup] dumping ${POSTGRES_DB} -> ${LOCAL_DIR}/${DUMP}"
# The db container mounts $DATA_ROOT/backups at /backups; supabase_admin is
# the image's superuser and PGPASSWORD is already in the container env.
docker exec campfire-db pg_dump -U supabase_admin -d "$POSTGRES_DB" -Fc -f "/backups/${DUMP}"
docker exec campfire-db pg_dumpall -U supabase_admin --roles-only -f "/backups/${ROLES}"
test -s "${LOCAL_DIR}/${DUMP}"
SIZE="$(stat -c %s "${LOCAL_DIR}/${DUMP}")"

echo "[backup] uploading (${SIZE} bytes)"
rclone copyto --s3-no-check-bucket "${LOCAL_DIR}/${DUMP}" "${REMOTE}/daily/${DUMP}"
rclone copyto --s3-no-check-bucket "${LOCAL_DIR}/${ROLES}" "${REMOTE}/daily/${ROLES}"
if [[ "$(date -u +%d)" == "01" ]]; then
  rclone copyto --s3-no-check-bucket "${LOCAL_DIR}/${DUMP}" "${REMOTE}/monthly/${DUMP}"
  rclone copyto --s3-no-check-bucket "${LOCAL_DIR}/${ROLES}" "${REMOTE}/monthly/${ROLES}"
fi

echo "[backup] retention"
rclone delete --min-age 14d "${REMOTE}/daily" || true
rclone delete --min-age 370d "${REMOTE}/monthly" || true
# Local: keep the newest $KEEP_LOCAL dump+roles pairs.
ls -1t "${LOCAL_DIR}"/campfire-*.dump 2>/dev/null | tail -n +"$((KEEP_LOCAL + 1))" | xargs -r rm -f
ls -1t "${LOCAL_DIR}"/campfire-*.roles.sql 2>/dev/null | tail -n +"$((KEEP_LOCAL + 1))" | xargs -r rm -f

# Marker the admin panel / Claude can read without touching OSN.
printf '%s %s %s\n' "$STAMP" "$DUMP" "$SIZE" > "${LOCAL_DIR}/last-success"
echo "[backup] ok ${DUMP} (${SIZE} bytes)"
ping
