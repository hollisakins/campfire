"""Per-exposure footprints around the chosen calibration stars.

For each star: every public NIRCam imaging exposure x detector within 6'.
Reports, per program and filter, how many have the star on the detector, how
many have it off the detector but within 150" of the detector centre, and the
exposure times. Writes footprints_<name>.ecsv.
"""
import os, numpy as np, warnings
warnings.simplefilter('ignore')
from astropy.table import Table
from astropy.coordinates import SkyCoord
import astropy.units as u
from matplotlib.path import Path
from astroquery.mast import MastMissions
D = os.path.dirname(os.path.abspath(__file__))
STARS = [('p5398_a', 342.2440, -44.6047, 8.4), ('p5398_b', 223.3729, 3.4097, 8.5),
         ('p5398_c', 11.2508, -73.2292, 9.2), ('cos_84', 150.4299, 2.2913, 8.4),
         ('cos_95', 149.8849, 2.0091, 9.5), ('cos_101', 150.0399, 2.3968, 10.1),
         ('cos_102', 150.2677, 2.4212, 10.2)]


def corners(sreg):
    return np.array(str(sreg).split()[-8:], float).reshape(4, 2)


def inside(v, ra, dec):
    x = (v[:, 0] - ra) * np.cos(np.radians(dec)); y = v[:, 1] - dec
    return bool(Path(np.c_[x, y]).contains_point((0, 0)))


MM = MastMissions(mission='jwst')
for name, ra, dec, g in STARS:
    f = f'{D}/footprints_{name}.ecsv'
    if not os.path.exists(f):
        t = MM.query_criteria(coordinates=SkyCoord(ra, dec, unit='deg'), radius=6 * u.arcmin,
                              instrume='NIRCAM', exp_type='NRC_IMAGE', productLevel='2b',
                              select_cols=['fileSetName', 'program', 'detector', 'filter', 'pupil', 'effexptm',
                                           's_region', 'subarray', 'access'], limit=20000)
        t = t[[str(a).upper() == 'PUBLIC' for a in t['access']]]
        t.write(f, overwrite=True)
    t = Table.read(f)
    V = [corners(s) for s in t['s_region']]
    on = np.array([inside(v, ra, dec) for v in V], dtype=bool)
    cen = np.array([v.mean(0) for v in V])
    sep = SkyCoord(cen[:, 0], cen[:, 1], unit='deg').separation(SkyCoord(ra, dec, unit='deg')).arcsec
    nearby = ~on & (sep < 150)
    t['on'] = on; t['sep'] = sep
    t.write(f, overwrite=True)
    print(f'\n== {name}: G={g}, {len(t)} public exposure-detector rows within 6 arcmin; '
          f'subarrays {sorted(set(t["subarray"]))} ==')
    for prog in sorted(set(t['program'])):
        for filt in sorted(set(t['filter'][t['program'] == prog])):
            w = (t['program'] == prog) & (t['filter'] == filt)
            if (w & (on | nearby)).sum() == 0: continue
            print(f'  {prog} {filt:7s} star on detector {int((w & on).sum()):3d}, off but within 150" '
                  f'{int((w & nearby).sum()):3d}, farther {int((w & ~on & ~nearby).sum()):4d}; '
                  f'exposure {np.min(t["effexptm"][w]):.0f}-{np.max(t["effexptm"][w]):.0f} s')
