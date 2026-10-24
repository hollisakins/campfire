#!/usr/bin/env bash
# Deploy one commit of CAMPFIRE to this VM: migrations first, then the web
# image, then a health gate with automatic rollback.
#
#   ops/scripts/deploy.sh <git sha>
#
# Also usable as the forced command of a deploy-only SSH key
# (authorized_keys: restrict,command="/srv/campfire/repo/ops/scripts/deploy.sh" ...),
# in which case the sha is taken from $SSH_ORIGINAL_COMMAND ("deploy <sha>").
# The GitHub workflow that calls it is step 5 of ops/README.md.
#
# Order of operations:
#   1. fetch + check out <sha> in $REPO_DIR (detached) so the migrations and
#      this script match the image being deployed
#   2. supabase db push against 127.0.0.1 (applies pending supabase/migrations)
#   3. pull ghcr image :<sha>, recreate the web container
#   4. poll /api/health; on failure recreate the previous tag and exit 1
# A migration lands a few seconds before the web that needs it (and stays if
# the web rolls back); migrations are forward-compatible by project rule.
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${OPS_DIR}/.env"
[[ -f "$ENV_FILE" ]] || { echo "missing ${ENV_FILE}" >&2; exit 1; }
# shellcheck disable=SC1090
source "${OPS_DIR}/scripts/lib.sh"; load_env "$ENV_FILE"

SHA="${1:-}"
if [[ -z "$SHA" && -n "${SSH_ORIGINAL_COMMAND:-}" ]]; then
  # "deploy <sha>" from the forced-command SSH key; nothing else is accepted.
  read -r verb arg _ <<<"$SSH_ORIGINAL_COMMAND"
  [[ "$verb" == "deploy" ]] || { echo "refused: ${SSH_ORIGINAL_COMMAND}" >&2; exit 2; }
  SHA="$arg"
fi
# Full 40-hex only: GHCR tags carry the full sha, and a short one from the CI
# key is a sign something upstream is wrong.
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "usage: deploy.sh <full 40-hex git sha>" >&2; exit 2; }

REPO_DIR="${REPO_DIR:-/srv/campfire/repo}"
WEB_PORT="${WEB_PORT:-3000}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-postgres}"
SUPABASE_CLI_VERSION="${SUPABASE_CLI_VERSION:-2.120.0}"
HEALTH_URL="http://127.0.0.1:${WEB_PORT}/api/health"
LOCK=/tmp/campfire-deploy.lock

exec 9>"$LOCK"
flock -n 9 || { echo "another deploy is running" >&2; exit 1; }

log() { printf '[deploy %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
compose() { docker compose --project-directory "$OPS_DIR" "$@"; }

previous_tag() {
  local t
  t="$(docker inspect --format '{{.Config.Image}}' campfire-web 2>/dev/null | sed 's/.*://')" || true
  [[ -n "$t" ]] && { echo "$t"; return; }
  grep -E '^WEB_TAG=' "$ENV_FILE" | head -1 | cut -d= -f2 | cut -d'#' -f1 | tr -d ' '
}

wait_healthy() {
  local deadline=$(( $(date +%s) + ${1:-90} ))
  while (( $(date +%s) < deadline )); do
    if body="$(curl -fsS -m 5 "$HEALTH_URL" 2>/dev/null)"; then
      log "healthy: ${body}"
      return 0
    fi
    sleep 3
  done
  return 1
}

persist_tag() {
  if grep -qE '^WEB_TAG=' "$ENV_FILE"; then
    sed -i -E "s|^WEB_TAG=.*|WEB_TAG=${1}|" "$ENV_FILE"
  else
    echo "WEB_TAG=${1}" >> "$ENV_FILE"
  fi
}

PREV="$(previous_tag)"
log "deploying ${SHA} (previous: ${PREV:-none})"

if [[ -n "${DEPLOY_STAGE2:-}" ]] && [[ "$(git -C "$REPO_DIR" rev-parse HEAD)" == "$SHA" ]]; then
  log "stage 2: ${SHA} checked out, continuing with its deploy.sh"
else
log "checking out ${SHA} in ${REPO_DIR}"
git -C "$REPO_DIR" fetch -q origin
# Only commits already on main deploy: the CI key cannot ship a branch.
if ! git -C "$REPO_DIR" merge-base --is-ancestor "$SHA" origin/main 2>/dev/null; then
  echo "refused: ${SHA} is not on origin/main" >&2
  exit 3
fi
git -C "$REPO_DIR" checkout -q --detach "$SHA"

# This file was just replaced underneath a running bash, which reads scripts
# incrementally: the rest of the OLD script would run against the NEW
# checkout (run 40123391014 applied migrations although the new version
# skips them). Hand over to the checked-out script for everything after the
# checkout; it skips the fetch/checkout because the sha is already in place.
if [[ -z "${DEPLOY_STAGE2:-}" ]]; then
  flock -u 9
  DEPLOY_STAGE2=1 exec "$REPO_DIR/ops/scripts/deploy.sh" "$SHA"
fi
fi

# Phase 1 (web on the hosted Supabase): migrations still land through the
# Supabase GitHub integration, and the local db may not even be running, so
# DEPLOY_MIGRATIONS=skip in .env turns this step off. Phase 2 sets it to local.
if [[ "${DEPLOY_MIGRATIONS:-local}" == "skip" ]]; then
  log "migrations: skipped (DEPLOY_MIGRATIONS=skip; the hosted project is the database)"
else
  log "applying migrations"
  # The container speaks plaintext on 127.0.0.1; the CLI insists on TLS otherwise.
  DB_URL="postgresql://postgres:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT}/${POSTGRES_DB}?sslmode=disable"
  ( cd "$REPO_DIR" && npx -y "supabase@${SUPABASE_CLI_VERSION}" db push --db-url "$DB_URL" --yes )
fi

log "pulling web image :${SHA}"
export WEB_TAG="$SHA"
compose pull -q web
compose up -d --no-deps web

if wait_healthy 90; then
  persist_tag "$SHA"
  echo "$SHA $(date -u +%FT%TZ)" >> "${OPS_DIR}/deploys.log"
  log "deployed ${SHA}"
  docker image prune -f --filter "until=168h" >/dev/null 2>&1 || true
  exit 0
fi

log "health check failed; rolling back to ${PREV:-none}"
compose logs --no-color --tail 50 web || true
if [[ -n "$PREV" && "$PREV" != "$SHA" ]]; then
  export WEB_TAG="$PREV"
  compose up -d --no-deps web
  if wait_healthy 90; then
    log "rolled back to ${PREV}"
  else
    log "ROLLBACK FAILED: web is down"
  fi
fi
exit 1
