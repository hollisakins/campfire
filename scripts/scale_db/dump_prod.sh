#!/usr/bin/env bash
# Dump production's public-schema DATA for the local "scale DB" (P-1, PR 5).
#
# Read-only on production: one pg_dump through the session pooler. Output goes
# OUTSIDE the repository (default ~/campfire-scale-db) and must never be
# committed: it contains proprietary-program rows. The scrub step (load.sh +
# scrub.sql) removes user identities and free text after loading.
#
# Usage: scripts/scale_db/dump_prod.sh [out_dir]
#   needs: ~/.config/campfire/prod_db_url (chmod 600) or $CAMPFIRE_PROD_DB_URL
#          pg_dump >= 17 (e.g. ~/miniforge3/envs/pgvalidate/bin)
set -euo pipefail

OUT_DIR="${1:-${CAMPFIRE_SCALE_DB_DIR:-$HOME/campfire-scale-db}}"
URL_FILE="$HOME/.config/campfire/prod_db_url"
URL="${CAMPFIRE_PROD_DB_URL:-}"
if [ -z "$URL" ] && [ -r "$URL_FILE" ]; then URL="$(tr -d '[:space:]' < "$URL_FILE")"; fi
[ -n "$URL" ] || { echo "no connection string: set CAMPFIRE_PROD_DB_URL or create $URL_FILE" >&2; exit 2; }

repo_root="$(cd "$(dirname "$0")/../.." && pwd -P)"
mkdir -p "$OUT_DIR"; chmod 700 "$OUT_DIR"
case "$(cd "$OUT_DIR" && pwd -P)" in
  "$repo_root"|"$repo_root"/*) echo "refusing to write inside the repository: $OUT_DIR" >&2; exit 2 ;;
esac

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$OUT_DIR/campfire_public_data_${stamp}.dump"

# Never dumped: credentials, tokens, invites, logs with IPs, snapshot bookkeeping,
# share links (carry a password). user_profiles IS dumped and then scrubbed.
EXCLUDE=(
  api_keys refresh_tokens device_codes access_codes code_redemptions
  password_reset_log pending_invites inspection_access_requests share_links
  download_log sync_snapshots sync_deletions
)
args=()
for t in "${EXCLUDE[@]}"; do args+=(--exclude-table-data="public.$t"); done

echo "dumping public-schema data to $out"
start=$(date +%s)
PGCONNECT_TIMEOUT=30 pg_dump "$URL" \
  --data-only --schema=public --no-owner --no-privileges --no-comments \
  --format=custom --compress=zstd:3 \
  "${args[@]}" \
  --file="$out"
end=$(date +%s)
chmod 600 "$out"
shasum -a 256 "$out" > "$out.sha256"
ls -lh "$out" | awk '{print "size:", $5}'
echo "elapsed: $((end-start)) s"
echo "$out" > "$OUT_DIR/LATEST"
echo "latest -> $OUT_DIR/LATEST"
