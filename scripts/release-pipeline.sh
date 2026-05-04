#!/usr/bin/env bash
# release-pipeline.sh — tag a new campfire-pipeline release.
#
# Usage:
#   scripts/release-pipeline.sh <X.Y.Z>           # local commit + tag only
#   scripts/release-pipeline.sh <X.Y.Z> --push    # also push to origin
#
# Pre-conditions enforced:
#   - on `main`, working tree clean, up-to-date with origin/main
#   - tag `pipeline-v<X.Y.Z>` does not yet exist
#   - `pipeline/CHANGELOG.md` has a non-empty `## Unreleased` section
#
# Effects:
#   - rewrites `pipeline/CHANGELOG.md`: `## Unreleased` -> `## vX.Y.Z — YYYY-MM-DD`,
#     with a fresh empty `## Unreleased` block above it
#   - commits as `release(pipeline): vX.Y.Z`
#   - creates an annotated tag `pipeline-v<X.Y.Z>` whose body is the changelog
#   - with --push, pushes both commit and tag to origin

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHANGELOG="${REPO_ROOT}/pipeline/CHANGELOG.md"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

usage() {
  cat >&2 <<EOF
Usage: $0 <X.Y.Z> [--push]

Examples:
  $0 0.4.0           # local tag only
  $0 0.4.0 --push    # local tag + push to origin
EOF
  exit 1
}

[[ $# -ge 1 && $# -le 2 ]] || usage
NEW_VERSION="$1"
PUSH=false
[[ ${2:-} == "--push" ]] && PUSH=true

[[ "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || err "version must be X.Y.Z (got: '$NEW_VERSION')"

TAG="pipeline-v${NEW_VERSION}"

cd "$REPO_ROOT"

# --- pre-flight --------------------------------------------------------------

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
[[ "$CURRENT_BRANCH" == "main" ]] \
  || err "must be on main (currently: $CURRENT_BRANCH)"

git diff --quiet HEAD -- \
  || err "working tree has uncommitted changes"

info "Fetching origin/main..."
git fetch --quiet origin main
LOCAL=$(git rev-parse main)
REMOTE=$(git rev-parse origin/main)
[[ "$LOCAL" == "$REMOTE" ]] \
  || err "local main ($LOCAL) differs from origin/main ($REMOTE); pull or push first"

if git rev-parse "$TAG" >/dev/null 2>&1; then
  err "tag $TAG already exists"
fi

[[ -f "$CHANGELOG" ]] || err "$CHANGELOG not found"

# Extract the Unreleased section content (everything between `## Unreleased`
# and the next `## ` heading).
UNRELEASED=$(awk '
  /^## Unreleased[[:space:]]*$/ { in_section=1; next }
  /^## / && in_section { exit }
  in_section { print }
' "$CHANGELOG")

if ! printf '%s\n' "$UNRELEASED" | grep -qE '^[[:space:]]*-[[:space:]]+\S'; then
  err "Unreleased section in $CHANGELOG has no entries"
fi

info "Pre-flight checks passed."
echo
echo "Releasing pipeline-v${NEW_VERSION}"
echo "Changelog entries:"
printf '%s\n' "$UNRELEASED" | sed 's/^/    /'
echo

# --- rewrite changelog -------------------------------------------------------

TODAY=$(date -u +%Y-%m-%d)
TMP=$(mktemp)
awk -v ver="${NEW_VERSION}" -v date="${TODAY}" '
  BEGIN { swapped = 0 }
  /^## Unreleased[[:space:]]*$/ && !swapped {
    print "## Unreleased"
    print ""
    print "## v" ver " — " date
    swapped = 1
    next
  }
  { print }
' "$CHANGELOG" > "$TMP"
mv "$TMP" "$CHANGELOG"

info "Updated $CHANGELOG"

# --- commit + tag ------------------------------------------------------------

git add "$CHANGELOG"
git commit --quiet -m "release(pipeline): v${NEW_VERSION}"

TAG_BODY=$(printf 'pipeline v%s\n\n%s\n' "$NEW_VERSION" "$UNRELEASED")
git tag -a "$TAG" -m "$TAG_BODY"

info "Created commit $(git rev-parse --short HEAD) and tag $TAG"

# --- push --------------------------------------------------------------------

if $PUSH; then
  info "Pushing to origin..."
  git push origin main
  git push origin "$TAG"
  echo
  info "Released pipeline-v${NEW_VERSION}."
  echo
  echo "Install with:"
  echo "  pip install \"git+https://github.com/hollisakins/campfire.git@${TAG}#subdirectory=pipeline\""
else
  echo
  echo "Tag created locally only. To publish:"
  echo "  git push origin main && git push origin $TAG"
  echo "Or re-run with --push."
fi
