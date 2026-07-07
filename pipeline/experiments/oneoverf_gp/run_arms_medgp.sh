#!/usr/bin/env bash
# Re-run the median + gp arms from the cal-stage pre-striping snapshot
# (cal-frame ordering, bg-sub on, box=32/filter=3). gp_aggr skipped.
set -euo pipefail
ROOT="${CAMPFIRE_ROOT:?}"
FILT=f444w
HERE="$(cd "$(dirname "$0")" && pwd)"
SNAP="$HERE/prestriping"
LOG="$HERE/logs"; mkdir -p "$LOG"

for arm in rj0911_med rj0911_gp; do
    dest="$ROOT/products/nircam/$arm/$FILT"; mkdir -p "$dest"
    cp "$SNAP"/*long.fits "$dest"/
    echo "=== running $arm ($(date +%H:%M:%S)) ==="
    /usr/bin/time -p cfpipe nircam run --field "$arm" --filters "$FILT" \
        -p 8 --all > "$LOG/${arm}.log" 2>&1
    echo "    done $arm"
done
echo "=== MED+GP ARMS DONE $(date +%H:%M:%S) ==="
