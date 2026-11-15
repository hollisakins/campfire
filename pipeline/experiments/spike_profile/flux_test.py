"""Check predicted in-band brightness against unsaturated stars.

Every Gaia star (G < 19.5) that lands on one of the downloaded exposures,
unsaturated and at least 40 px from the edge, gets aperture photometry
(0.6" radius, background from a 1.2-1.8" annulus, divided by an encircled
energy of 0.92; good to roughly 5%). Measured AB is compared with three
predictions:
  current  the step's colour formula (G, BP-RP -> Ks, Rayleigh-Jeans beyond)
  nir      interpolation through measured 2MASS / WISE magnitudes
  locus    Gaia only: G, RP and G-X(BP-RP) relations fitted to these stars
Writes flux_test.ecsv and prints, per filter, the median and scatter of
measured - predicted.
"""
import os, warnings, numpy as np
warnings.simplefilter('ignore')
from astropy.io import fits
from astropy.table import Table, vstack
from astropy.wcs import WCS
from astropy.time import Time
from photutils.aperture import CircularAperture, CircularAnnulus, aperture_photometry, ApertureStats
from photutils.centroids import centroid_com
from campfire_pipeline.nircam import spike_mask as sm
import gaia_sed as gs

D = os.path.dirname(os.path.abspath(__file__))
STARS = {'p5398_a': (342.2440, -44.6047), 'p5398_c': (11.2508, -73.2292),
         'cos_84': (150.4299, 2.2913), 'cos_95': (149.8849, 2.0091),
         'cos_101': (150.0399, 2.3968), 'cos_102': (150.2677, 2.4212)}
lst = Table.read(D + '/download_list.ecsv')
cats = {s: gs.query(ra, dec, 0.1, cache=f'{D}/gaia_{s}.ecsv') for s, (ra, dec) in STARS.items()}
locus = gs.fit_locus(vstack(list(cats.values())))
for b, (p, s, n) in locus.items():
    print(f'locus G-{b}: scatter {s:.2f} mag, {n} stars')
rows = []
for row in lst:
    filt = str(row['filter']).upper()
    if filt not in sm.PIVOT_UM: continue
    pivot = sm.PIVOT_UM[filt]
    cat = cats[row['star']]
    path = f"{D}/cal/{row['root']}_cal.fits"
    with fits.open(path, memmap=False) as h:
        sci = h['SCI'].data.astype(float); dq = h['DQ'].data
        hdr = h['SCI'].header; year = float(Time(h[0].header['DATE-OBS']).decimalyear)
        w = WCS(hdr, relax=True)
    scale = 3600 * np.sqrt(hdr['PIXAR_SR']) * 180 / np.pi
    d = {k: np.asarray(cat[k], float) for k in ('ra', 'dec', 'pmra', 'pmdec')}
    ra, dec = sm.stars_at_epoch(d, year)
    x, y = w.all_world2pix(ra, dec, 0, quiet=True)
    ny, nx = sci.shape
    pred = dict(current=sm.band_ab_mag(np.asarray(cat['g']), np.asarray(cat['bp_rp']), pivot),
                nir=gs.predict_ab(cat, pivot, 'nir'), locus=gs.predict_ab(cat, pivot, 'locus', locus))
    for i in np.nonzero((x > 40) & (x < nx - 40) & (y > 40) & (y < ny - 40) & (cat['g'] > 12))[0]:
        hb = int(round(0.3 / scale))
        xi, yi = int(round(x[i])), int(round(y[i]))
        box = sci[yi - hb:yi + hb + 1, xi - hb:xi + hb + 1]
        # saturated core: unusable pixels in the central 3x3
        if (dq[yi - 1:yi + 2, xi - 1:xi + 2] & 1).any() or not np.isfinite(sci[yi - 1:yi + 2, xi - 1:xi + 2]).all():
            continue
        box = np.where(np.isfinite(box), box, np.nanmedian(box))
        cx, cy = centroid_com(box - np.median(box))
        px, py = xi - hb + cx, yi - hb + cy
        if not (np.isfinite(px) and abs(px - x[i]) < hb and abs(py - y[i]) < hb): continue
        ap = CircularAperture((px, py), 0.6 / scale)
        an = CircularAnnulus((px, py), 1.2 / scale, 1.8 / scale)
        img = np.where(np.isfinite(sci), sci, 0.0)
        bad = ~np.isfinite(sci)
        if aperture_photometry(bad.astype(float), ap)['aperture_sum'][0] > 0.02 * ap.area: continue
        bkg = ApertureStats(sci, an, sigma_clip=None).median
        f = (aperture_photometry(img, ap)['aperture_sum'][0] - bkg * ap.area) * 1e6 * hdr['PIXAR_SR'] / 0.92
        if not f > 0: continue
        rows.append(dict(star=row['star'], filter=filt, g=float(cat['g'][i]), bp_rp=float(cat['bp_rp'][i]),
                         has_nir=bool(np.isfinite(cat['ks'][i])), ab=-2.5 * np.log10(f / 3631.0),
                         **{k: float(v[i]) for k, v in pred.items()}))
t = Table(rows)
t.write(D + '/flux_test.ecsv', overwrite=True)
order = ['F070W', 'F090W', 'F115W', 'F150W', 'F182M', 'F200W', 'F210M', 'F277W', 'F356W', 'F444W']
print(f'\n{len(t)} measurements; measured - predicted [mag]: median (scatter)')
print(f'{"filter":7s} {"n":>4s} {"current":>14s} {"2MASS/WISE":>14s} {"n":>4s} {"Gaia locus":>14s}')
for f in order:
    w = (t['filter'] == f) & (t['ab'] < 21)
    if w.sum() < 5: continue
    def st(col, sel):
        dlt = np.asarray(t['ab'][sel] - t[col][sel]); dlt = dlt[np.isfinite(dlt)]
        return f'{np.median(dlt):+.2f} ({1.4826 * np.median(np.abs(dlt - np.median(dlt))):.2f})' if len(dlt) else '   -'
    wn = w & t['has_nir']
    print(f'{f:7s} {int(w.sum()):4d} {st("current", w):>14s} {st("nir", wn):>14s} {int(wn.sum()):4d} {st("locus", w):>14s}')
