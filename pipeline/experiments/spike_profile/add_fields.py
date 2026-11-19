"""Append the six scratch calibration-star fields to fields.toml (once)."""
import numpy as np
from astropy.table import Table
D = '/Users/hollis/campfire-data/scratch/claude/spike_cal'
P = '/Users/hollis/campfire-data/config/fields.toml'
STARS = {'p5398_a': (342.2440, -44.6047), 'p5398_c': (11.2508, -73.2292),
         'cos_84': (150.4299, 2.2913), 'cos_95': (149.8849, 2.0091),
         'cos_101': (150.0399, 2.3968), 'cos_102': (150.2677, 2.4212)}
s = open(P).read()
assert '[spikecal_' not in s
t = Table.read(D + '/download_list.ecsv')
out = ['', '',
       '# SCRATCH (2026-11-14, safe to delete with products/nircam/spikecal_* and the',
       '# raw files listed in scratch/claude/spike_cal/download_list.ecsv): public',
       '# exposures with an isolated bright star on or within 150" of the detector,',
       '# taken through the standard process phase to measure diffraction-spike arms.',
       '# Process phase only; never combined or deployed.']
for star, (ra, dec) in STARS.items():
    w = t[t['star'] == star]
    files = ', '.join(f'"{r}*"' for r in sorted(w['root']))
    filt = ', '.join(f'"{f}"' for f in sorted({str(f).lower() for f in w['filter']}))
    out += [f'[spikecal_{star}]', f'files = [{files}]', f'filters = [{filt}]',
            f'tangent_point = [{ra}, {dec}]', '']
open(P, 'w').write(s.rstrip('\n') + '\n' + '\n'.join(out))
print('added', list(STARS))
