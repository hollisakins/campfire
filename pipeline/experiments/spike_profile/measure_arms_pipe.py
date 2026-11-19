"""Measure real spike arms around the calibration stars and compare with the
prediction used by the spike_mask step.

For every downloaded exposure and each of the eight arm directions of its star,
pixels within 10" of the arm axis are binned by distance from the star (log
bins, 3-300") and offset across the arm (0.1" bins); the median in each bin,
minus the level 7-10" off the axis, is the measured cross-section. Exposures
and arms are then combined per star and filter (six segment arms together, the
two strut arms together) with a median.

The prediction is the pipeline's: Gaia-predicted in-band flux x the published
model (halo removed, no widening), at the same bins.

Writes arms_<star>_<filter>.npz and arms_summary.ecsv.
usage: python measure_arms.py [STAR ...]
"""
import os, sys, warnings
import numpy as np
from astropy.io import fits
from astropy.table import Table
from scipy.stats import binned_statistic_2d
warnings.simplefilter('ignore')
from campfire_pipeline.nircam import spike_mask as sm
from campfire_pipeline.nircam.steps.spike_mask import _read_exposure

D = os.path.dirname(os.path.abspath(__file__))
STARS = {'p5398_a': (342.2440, -44.6047), 'p5398_c': (11.2508, -73.2292),
         'cos_84': (150.4299, 2.2913), 'cos_95': (149.8849, 2.0091),
         'cos_101': (150.0399, 2.3968), 'cos_102': (150.2677, 2.4212),
         'macs0647': (101.8897, 70.2229)}
R_EDGES = np.logspace(np.log10(3.0), np.log10(300.0), 21)
P_EDGES = np.arange(-10.0, 10.0001, 0.1)
PC = 0.5 * (P_EDGES[1:] + P_EDGES[:-1])
RC = np.sqrt(R_EDGES[1:] * R_EDGES[:-1])
ARMS = sm.SpikeModel.ARMS
HEX = np.arange(6)
STRUT = np.array([6, 7])
todo = sys.argv[1:] or [k for k in STARS if k != 'p5398_c']
# canonical exposures from the pipeline's process phase (through the background step)
PROD = '/Users/hollis/campfire-data/products/nircam'
OUT = D + '/pipe'
os.makedirs(OUT, exist_ok=True)
lst = Table.read(D + '/download_list.ecsv')
rows = []
for star in todo:
    ra0, dec0 = STARS[star]
    g = sm.query_gaia(ra0, dec0, 5.0 / 3600.0, g_max=13.0)
    i = int(np.argmin(g['g']))
    one = {k: np.atleast_1d(v)[i:i + 1] for k, v in g.items() if k != 'truncated'}
    sub = lst[lst['star'] == star]
    if star == 'macs0647':
        # the production field's own exposures (every filter): its G = 8 star
        # is the brightest available and reaches farthest
        import glob
        from astropy.table import Table as _T
        paths = sorted(glob.glob(f'{PROD}/macs0647/f*/jw*_nrc*[0-9g].fits'))
        sub = _T(dict(root=[os.path.basename(p)[:-5] for p in paths],
                      filter=[p.split('/')[-2].upper() for p in paths]))
    for filt in sorted(set(sub['filter'])):
        if filt.upper() not in sm.PIVOT_UM:
            print(star, filt, 'no pivot wavelength; skipped'); continue
        pivot = sm.PIVOT_UM[filt.upper()]
        mag = float(sm.band_ab_mag(one['g'], one['bp_rp'], pivot)[0])
        stack = {'hex': [], 'strut': []}
        sig = []
        model = None
        for root in sub['root'][sub['filter'] == filt]:
            path = (f'{PROD}/macs0647/{filt.lower()}/{root}.fits' if star == 'macs0647'
                    else f'{PROD}/spikecal_{star}/{filt.lower()}/{root}.fits')
            if not os.path.exists(path):
                print('missing', path); continue
            m = _read_exposure(path)
            with fits.open(path, memmap=False) as h:
                sci = h['SCI'].data.astype(np.float64)
            model = sm.load_model(m['channel'], pivot)
            ny, nx = m['shape']
            yy, xx = np.mgrid[0:ny, 0:nx]
            ra, dec = m['wcs'].all_pix2world(xx.ravel(), yy.ravel(), 0)
            s_ra, s_dec = sm.stars_at_epoch(one, m['year'])
            xi, eta = sm.gnomonic(ra, dec, s_ra[0], s_dec[0])
            r = np.hypot(xi, eta)
            roll = m['roll_ref'] + (s_ra[0] - m['ra_ref']) * np.sin(np.radians(s_dec[0]))
            phi = np.degrees(np.arctan2(xi, eta)) - roll
            v = sci.ravel(); ok = m['valid'].ravel() & (r > R_EDGES[0]) & (r < R_EDGES[-1])
            sig.append(m['sigma'])
            for k, a in enumerate(ARMS):
                d = np.radians((phi - a + 180.0) % 360.0 - 180.0)
                along, perp = r * np.cos(d), r * np.sin(d)
                w = ok & (along > 0) & (np.abs(perp) < 10.0)
                if w.sum() < 2000: continue
                med = binned_statistic_2d(r[w], perp[w], v[w], 'median', [R_EDGES, P_EDGES]).statistic
                cnt = binned_statistic_2d(r[w], perp[w], v[w], 'count', [R_EDGES, P_EDGES]).statistic
                med[cnt < 30] = np.nan
                # An arm owns the wedge within 15 deg of its axis (the nearest
                # other arm is 30 deg away). The level to subtract is taken
                # 7-10" off the axis where the wedge is that wide, otherwise
                # from the outer part of the wedge; outside the wedge is blank.
                lim = RC * np.tan(np.radians(15.0))
                for j in range(len(RC)):
                    ap = np.abs(PC)
                    zone = (ap > 7.0) if lim[j] >= 10.0 else (ap > 0.6 * lim[j]) & (ap < lim[j])
                    med[j] -= np.nanmedian(med[j, zone]) if zone.any() else np.nan
                    med[j, ap > lim[j]] = np.nan
                stack['hex' if k < 6 else 'strut'].append(med)
        if model is None: continue
        lam = pivot / model.wave
        amp = float(sm.star_amplitude(np.array([mag]), model.pixarea0)[0]) / lam ** 2
        out = dict(rc=RC, pc=PC, mag=mag, sigma=np.median(sig))
        for cls, arm in (('hex', 0.0), ('strut', 90.0)):
            if not stack[cls]: continue
            dat = np.nanmedian(np.array(stack[cls]), axis=0)
            a = np.radians(arm)
            x = RC[:, None] * np.sin(a) + PC[None, :] * np.cos(a)
            y = RC[:, None] * np.cos(a) - PC[None, :] * np.sin(a)
            mod = amp * model.sample(x / lam, y / lam, 1.0)
            out[cls + '_data'] = dat; out[cls + '_model'] = mod
            for j in range(len(RC)):
                dj, mj = dat[j], mod[j]
                if np.isfinite(dj).sum() < 10: continue
                core = np.abs(PC) < 1.0
                pk_d, pk_m = np.nanmax(dj[core]), float(mj.max())
                wd = 0.1 * np.sum(dj > 0.5 * pk_d) if pk_d > 0 else np.nan
                wm = 0.1 * np.sum(mj > 0.5 * pk_m) if pk_m > 0 else np.nan
                rows.append(dict(star=star, g=round(float(one['g'][0]), 1), filter=filt, arms=cls,
                                 r=round(float(RC[j]), 1), sigma=float(np.median(sig)),
                                 peak_data=float(pk_d), peak_model=pk_m,
                                 width_data=float(wd), width_model=float(wm),
                                 n=len(stack[cls])))
        np.savez(f'{OUT}/arms_{star}_{filt}.npz', **out)
        print(star, filt, f'AB={mag:.1f}', {k: len(v) for k, v in stack.items()}, flush=True)
t = Table(rows)
name = OUT + '/arms_summary.ecsv' if not sys.argv[1:] else OUT + f'/arms_summary_{"_".join(todo)}.ecsv'
t.write(name, overwrite=True)
print(name, len(t))
