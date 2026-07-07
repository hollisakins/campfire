#!/usr/bin/env bash
# clean_flicker_noise (cfn) matrix, full pipeline from uncal (cfn changes
# detector1, so the post-image2 prestriping snapshot cannot be reused).
#   rj0911_cfn       cfn median  + striping estimator=none  (cfn standalone)
#   rj0911_cfngp     cfn median  + striping estimator=gp    (cfn + GP cleanup)
#   rj0911_cfnfft    cfn fft     + striping estimator=none  (cfn standalone)
#   rj0911_cfnfftgp  cfn fft     + striping estimator=gp    (cfn + GP cleanup)
# Each writes to its own products/reference tree; astrom_cats symlinked to
# rj0911's so astrometry is held constant across all arms.
set -euo pipefail
FILT=f444w
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="$HERE/logs"; mkdir -p "$LOG"

for arm in rj0911_cfn rj0911_cfngp rj0911_cfnfft rj0911_cfnfftgp; do
    echo "=== running $arm ($(date +%H:%M:%S)) ==="
    /usr/bin/time -p cfpipe nircam run --field "$arm" --filters "$FILT" \
        -p 8 --all > "$LOG/${arm}.log" 2>&1
    echo "    done $arm ($(date +%H:%M:%S))"
done
echo "=== CFN MATRIX DONE $(date +%H:%M:%S) ==="
