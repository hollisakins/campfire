"""Rank the bright stars found by find_candidates.py.

For each star: the public level-3 NIRCam image rows whose centre is within 150",
the filters, the range of exposure time, programs, and how isolated it is
(distance to the nearest other G < 12.5 star).
"""
import os, numpy as np, warnings
warnings.simplefilter('ignore')
from astropy.table import Table
from astropy.coordinates import SkyCoord, search_around_sky
import astropy.units as u
D = os.path.dirname(os.path.abspath(__file__))
t = Table.read(D + '/mast_nircam_l3.ecsv')
m = Table.read(D + '/gaia_matches.ecsv')
ct = SkyCoord(t['s_ra'], t['s_dec'], unit='deg')
cm = SkyCoord(m['ra'], m['dec'], unit='deg')
i1, i2, sep, _ = search_around_sky(cm, cm, 600 * u.arcsec)
near = np.full(len(m), 600.0)
for a, b, s in zip(i1, i2, sep.arcsec):
    if a != b: near[a] = min(near[a], s)
im, it, sep, _ = search_around_sky(cm, ct, 150 * u.arcsec)
rows = []
for k in range(len(m)):
    w = it[im == k]
    if not len(w): continue
    sub = t[w]
    filt = sorted({f for f in sub['filters'] if ';' not in str(f)} | {str(f).split(';')[0] for f in sub['filters']})
    wide = [f for f in filt if f.endswith('W')]
    rows.append(dict(ra=round(float(m['ra'][k]), 4), dec=round(float(m['dec'][k]), 4),
                     g=round(float(m['g'][k]), 1), bp_rp=round(float(m['bp_rp'][k]), 2) if np.isfinite(m['bp_rp'][k]) else -9,
                     near=int(near[k]), nfilt=len(filt), nwide=len(wide),
                     tmin=int(np.min(sub['t_exptime'])), tmax=int(np.max(sub['t_exptime'])),
                     progs=','.join(sorted({str(p) for p in sub['proposal_id']})),
                     intent=','.join(sorted({str(x)[:3] for x in sub['intentType']})),
                     target=str(sub['target_name'][0])[:22], filters=' '.join(filt)))
r = Table(rows)
r.write(D + '/candidates.ecsv', overwrite=True)
print(len(r), 'stars with imaging')
for lo, hi in ((0, 8), (8, 9.5), (9.5, 11), (11, 12.5)):
    s = r[(r['g'] >= lo) & (r['g'] < hi) & (r['near'] > 60)]
    s.sort(['nwide', 'tmax'], reverse=True)
    print(f'\n== G {lo}-{hi}: {len(s)} isolated (>60" from another G<12.5 star); top 12 ==')
    s['ra', 'dec', 'g', 'bp_rp', 'near', 'nwide', 'nfilt', 'tmin', 'tmax', 'progs', 'intent', 'target'][:12].pprint(max_width=220, max_lines=-1)
