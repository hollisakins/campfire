#!/usr/bin/env bash
# Generate PRE-striping snapshots (detector1 -> persistence -> wisp -> image2,
# STOP before striping) for the rho cross-filter consistency check. Args =
# filters to process on the rj0911_rho clone field.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="$HERE/logs"; mkdir -p "$LOG"
FIELD=rj0911_rho

for filt in "$@"; do
    echo "=== $filt front-end ($(date +%H:%M:%S)) ==="
    for step in detector1 persistence wisp image2; do
        cfpipe nircam "$step" --field "$FIELD" --filters "$filt" -p 8 \
            >> "$LOG/rho_${filt}.log" 2>&1
        echo "    $step done"
    done
done
echo "=== RHO FRONT-END DONE $(date +%H:%M:%S) ==="
