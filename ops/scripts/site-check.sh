#!/usr/bin/env bash
# Is the portal reachable from outside? Fetches the public health url THROUGH
# Cloudflare and pings $HEALTHCHECK_PING_URL_SITE on success, <url>/fail
# otherwise (campfire-site-check.timer, every 5 min).
#
# A missed ping means the VM, Docker, Caddy, the origin certificate, Cloudflare
# or DNS is broken; the loopback checks in deploy.sh cannot see the last three.
# healthchecks.io alerts when pings stop arriving, so this also covers the host
# being down.
#
# One slow request must not page: a single try that stalls before it reaches
# Caddy (2026-11-06 and 2026-11-10 — Caddy answered in 30 ms whenever the
# request arrived, the disk was fine) turned into a 5-minute "down" notice.
# Each run therefore makes up to ATTEMPTS tries, GAP seconds apart (a fresh
# connection each time), and only reports failure when every one fails; the
# worst case is ~55 s, well inside the period. Every failed try is written to
# $LOG with what failed (curl error, HTTP status, connect / TLS / first-byte
# times) and sent as the ping body, so a blip that recovered on retry is still
# visible: `tail /srv/campfire/logs/site-check.log` (the journal is not
# readable by the login user).
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
source "${OPS_DIR}/scripts/lib.sh"; load_env "${OPS_DIR}/.env"

URL="${SITE_HEALTH_URL:-https://${WEB_HOST}/api/health}"
PING="${HEALTHCHECK_PING_URL_SITE:-}"
LOG="${SITE_CHECK_LOG:-${CAMPFIRE_DATA_ROOT:-/srv/campfire}/logs/site-check.log}"
ATTEMPTS="${SITE_CHECK_ATTEMPTS:-3}"
GAP="${SITE_CHECK_GAP:-5}"
TIMEOUT="${SITE_CHECK_TIMEOUT:-15}"

# ping [suffix] [body]: a body (a diagnostics line) turns the ping into a POST
# healthchecks.io stores in the check's log; without one it is the plain GET.
ping() {
  [[ -n "$PING" ]] || return 0
  local data=()
  if [[ -n "${2:-}" ]]; then data=(--data-raw "$2"); fi
  curl -fsS -m 10 --retry 3 -o /dev/null ${data[@]+"${data[@]}"} "${PING}${1:-}" || true
}

# note TEXT: to stderr (the journal) and the log file; a missing or unwritable
# log must never fail the check.
note() {
  printf '%s [site-check] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" \
    | tee -a "$LOG" 2>/dev/null >&2 || true
}

# attempt: one try. Sets `ok` (1 = HTTP answered with "ok":true) and `detail`.
# curl's default User-Agent passes Cloudflare's browser integrity check; a
# bare script-like agent (python-urllib) is served error 1010 instead. No -f:
# a 503's body ("db":"degraded", "reason") is the most useful thing to record.
attempt() {
  local rc=0 raw stats text
  raw="$(curl -sS -m "$TIMEOUT" --connect-timeout 10 \
    -w $'\n@@ http=%{http_code} connect=%{time_connect}s tls=%{time_appconnect}s ttfb=%{time_starttransfer}s total=%{time_total}s' \
    "$URL" 2>&1)" || rc=$?
  stats="$(grep '^@@ ' <<<"$raw" | tail -n1 | cut -c4- || true)"
  text="$(grep -v '^@@ ' <<<"$raw" | head -c 200 | tr '\n' ' ' || true)"
  ok=0
  if [[ $rc -eq 0 ]] && grep -q '"ok":true' <<<"$raw"; then ok=1; fi
  detail="curl_rc=${rc} ${stats:-no-stats} ${text}"
}

ok=0 detail="" failures=""
for ((i = 1; i <= ATTEMPTS; i++)); do
  attempt
  if ((ok)); then break; fi
  note "attempt ${i}/${ATTEMPTS} FAILED: ${URL} -> ${detail}"
  failures+="attempt ${i}/${ATTEMPTS}: ${detail}"$'\n'
  if ((i < ATTEMPTS)); then sleep "$GAP"; fi
done

if ((ok)); then
  if ((i > 1)); then
    note "recovered on attempt ${i}/${ATTEMPTS} (${detail})"
    ping "" "recovered on attempt ${i}/${ATTEMPTS}"$'\n'"${failures}"
  else
    ping
  fi
else
  echo "[site-check] FAILED: ${URL} after ${ATTEMPTS} attempts" >&2
  ping /fail "${failures}"
  exit 1
fi
