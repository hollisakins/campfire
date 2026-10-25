#!/usr/bin/env bash
# Is the portal reachable from outside? Fetches the public health url THROUGH
# Cloudflare and pings $HEALTHCHECK_PING_URL_SITE on success, <url>/fail
# otherwise (campfire-site-check.timer, every 5 min).
#
# A missed ping means the VM, Docker, Caddy, the origin certificate, Cloudflare
# or DNS is broken; the loopback checks in deploy.sh cannot see the last three.
# healthchecks.io alerts when pings stop arriving, so this also covers the host
# being down.
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
source "${OPS_DIR}/scripts/lib.sh"; load_env "${OPS_DIR}/.env"

URL="${SITE_HEALTH_URL:-https://${WEB_HOST}/api/health}"
PING="${HEALTHCHECK_PING_URL_SITE:-}"

ping() { [[ -n "$PING" ]] && curl -fsS -m 10 --retry 3 -o /dev/null "${PING}${1:-}" || true; }

# curl's default User-Agent passes Cloudflare's browser integrity check; a
# bare script-like agent (python-urllib) is served error 1010 instead.
if body="$(curl -fsS -m 20 "$URL" 2>&1)" && grep -q '"ok":true' <<<"$body"; then
  ping
else
  echo "[site-check] FAILED: ${URL} -> ${body:-no response}" >&2
  ping /fail
  exit 1
fi
