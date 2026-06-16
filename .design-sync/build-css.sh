#!/usr/bin/env bash
# Regenerate the design-sync cssEntry: compile Tailwind utilities + token vars
# (:root/.dark from app/globals.css) using the sync config (which SAFELISTS the
# full token-backed utility palette so the design agent can use any token
# utility, not just the ones the app happens to use), then prepend brand fonts.
# Output lands inside web/ because the converter bounds cfg.cssEntry to the
# package root. Run from the repo root before package-build on every sync.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO/web"
npx tailwindcss -c "$REPO/.design-sync/tailwind.sync.config.cjs" \
  -i app/globals.css -o /tmp/cf-tw-sync.css
cat "$REPO/.design-sync/css-header.css" /tmp/cf-tw-sync.css > "$REPO/web/.ds-sync-styles.css"
echo "wrote $REPO/web/.ds-sync-styles.css ($(wc -l < "$REPO/web/.ds-sync-styles.css") lines)"
