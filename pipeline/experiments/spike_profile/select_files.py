"""Download list: exposures x detectors with a chosen star on the detector or
within 150" of its centre. Field B of program 5398 is left out. Writes
download_list.ecsv and prints counts per star / program / filter."""
import os, numpy as np
from astropy.table import Table, vstack, unique
D = os.path.dirname(os.path.abspath(__file__))
out = []
for name in ('p5398_a', 'p5398_c', 'cos_84', 'cos_95', 'cos_101', 'cos_102'):
    t = Table.read(f'{D}/footprints_{name}.ecsv')
    t = t[np.asarray(t['on'], bool) | (t['sep'] < 150)]
    t['star'] = name
    t['root'] = [f"{a}_{str(d).lower()}" for a, d in zip(t['fileSetName'], t['detector'])]
    out.append(t['root', 'star', 'program', 'filter', 'detector', 'effexptm', 'on', 'sep'])
t = vstack(out)
print(len(t), 'rows;', len(set(t['root'])), 'distinct files')
t = unique(t, 'root')
t.write(D + '/download_list.ecsv', overwrite=True)
for s in sorted(set(t['star'])):
    w = t['star'] == s
    lw = np.array(['long' in r for r in t['root'][w]])
    print(f'{s}: {w.sum()} files ({lw.sum()} long-wavelength); programs {sorted(set(t["program"][w]))}; '
          f'filters {sorted(set(t["filter"][w]))}')
