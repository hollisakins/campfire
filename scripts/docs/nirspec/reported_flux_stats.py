"""Numbers behind reported_flux.png: fnu (and fnu_opt) over the 0.8" aperture flux, same selection as the figure
(SHAPE own, photometry S/N >= 10, spectrum S/N >= 10 in band, band fully covered, aperture entry present).
Read only: the products under <root> and the catalog mirror (mode=ro).

    python scripts/docs/nirspec/reported_flux_stats.py <root> <aperture json>
"""
import glob
import json
import os
import sqlite3
import sys

import numpy as np
from astropy.io import fits

ROOT, OBS, GRATING = sys.argv[1], 'ember_cosmos_p1', 'prism_clear'
APER = json.load(open(sys.argv[2]))
BANDS = ['f150w', 'f200w', 'f277w', 'f356w', 'f444w']
MIN_SNR = 10.0
ws = glob.glob(os.path.join(ROOT, 'products', 'nirspec', '**', OBS), recursive=True)
ws = [w for w in ws if glob.glob(os.path.join(w, f'{OBS}_{GRATING}_*_spec.fits'))][0]

db = sqlite3.connect(f'file:{os.path.expanduser("~/campfire-data/meta/campfire.db")}?mode=ro', uri=True)
rows = db.execute("select s.target_id, p.photometry from spectra s join object_photometry p on p.object_id = s.object_id "
                  "where s.observation = ? and s.grating = 'PRISM' and p.field = 'cosmos'", (OBS,))
phot = {int(t.rsplit('_', 1)[1]): json.loads(ph)['bands'] for t, ph in rows}


def per_band(lam, f, e, bands, sid):
    out = {}
    for b in BANDS:
        pb = bands.get(b)
        if not pb or not np.isfinite(pb.get('flux', np.nan)) or pb['flux'] <= 0 or pb['flux'] / pb['flux_err'] < MIN_SNR:
            continue
        rec = APER['sources'].get(str(sid), {}).get(b)
        if not rec:
            continue
        ref = float(np.interp(0.8, APER['diam'], rec['aper']))
        if not np.isfinite(ref) or ref <= 0:
            continue
        s = (lam >= pb['wav_min']) & (lam <= pb['wav_max']) & np.isfinite(f)
        if s.sum() < 5 or lam[s].min() > pb['wav_min'] + 0.05 or lam[s].max() < pb['wav_max'] - 0.05:
            continue
        if np.mean(f[s]) / (np.sqrt(np.nansum(e[s] ** 2)) / s.sum()) < MIN_SNR:
            continue
        out[b] = np.mean(f[s]) / ref
    return out


rep_src, opt_src, bands_rep = [], [], {b: [] for b in BANDS}
for path in sorted(glob.glob(os.path.join(ws, f'{OBS}_{GRATING}_*_spec.fits'))):
    sid = int(os.path.basename(path).split('_')[-2])
    if sid not in phot:
        continue
    with fits.open(path) as h:
        if h[0].header.get('SHAPE') != 'own':
            continue
        t = h['SPEC1D'].data
        lam = np.asarray(t['wave'], float)
        r = per_band(lam, np.asarray(t['fnu'], float), np.asarray(t['fnu_err'], float), phot[sid], sid)
        o = per_band(lam, np.asarray(t['fnu_opt'], float), np.asarray(t['fnu_opt_err'], float), phot[sid], sid)
    if not r or not o:
        continue
    rr, oo = float(np.median(list(r.values()))), float(np.median(list(o.values())))
    if rr > 0 and oo > 0:
        rep_src.append(rr)
        opt_src.append(oo)
        for b, v in r.items():
            if v > 0:
                bands_rep[b].append(v)


def summary(v):
    v = np.asarray(v)
    lv = np.log10(v)
    p16, p50, p84 = np.percentile(v, [16, 50, 84])
    mad = 1.4826 * np.median(np.abs(lv - np.median(lv)))
    return f'median {p50:.3f}, 16-84% {p16:.3f}-{p84:.3f}, scatter {mad:.3f} dex (NMAD), N={len(v)}'


print('sources:', len(rep_src))
print('fnu / aper0.8     :', summary(rep_src))
print('fnu_opt / aper0.8 :', summary(opt_src))
for b in BANDS:
    if bands_rep[b]:
        print(f'  {b} fnu/aper0.8 :', summary(bands_rep[b]))
