#!/usr/bin/env bash
# Nightly sync-catalog snapshot (replaces the Vercel cron in web/vercel.json).
#
#   ops/scripts/cron-sync-snapshot.sh    (campfire-sync-snapshot.timer, 05:00 UTC)
#
# Calls the route on the web container's loopback port, never through
# Cloudflare: the route runs up to 300 s and the proxy cuts origins off at
# 100 s. Pings $HEALTHCHECK_PING_URL_SNAPSHOT on success, <url>/fail otherwise.
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1090
set -a; source "${OPS_DIR}/.env"; set +a

WEB_PORT="${WEB_PORT:-3000}"
PING="${HEALTHCHECK_PING_URL_SNAPSHOT:-}"
[[ -n "${CRON_SECRET:-}" ]] || { echo "CRON_SECRET unset" >&2; exit 1; }

ping() { [[ -n "$PING" ]] && curl -fsS -m 10 --retry 3 -o /dev/null "${PING}${1:-}" || true; }

if out="$(curl -fsS -m 900 -H "Authorization: Bearer ${CRON_SECRET}" \
      "http://127.0.0.1:${WEB_PORT}/api/cron/sync-snapshot")"; then
  echo "[sync-snapshot] ${out}"
  ping
else
  echo "[sync-snapshot] FAILED" >&2
  ping /fail
  exit 1
fi
