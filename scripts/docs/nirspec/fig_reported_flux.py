"""What the reported flux means: the source's light inside a circle 0.8" across, not the light that got into the shutter.

A shutter is 0.20" by 0.46" on the sky, so it catches only part of a galaxy, less the bigger the galaxy is. The
profile fit models the whole source (its width and position, through the optics model), so it can report the light
of the fitted source inside a fixed circle 0.8" across (1.5 shutter lengths): all of a point source, and for a
galaxy the light a 0.8" aperture would measure in an image. Left: a galaxy, the open shutter, and the circle.
Right: each source's spectrum over its imaging photometry (averaged over the NIRCam bands its spectrum covers),
against the source's fitted size: the optimal extraction (the light in the shutter) and the reported flux (the
light in the 0.8" circle). The photometry is a total (Kron) flux, so for the biggest galaxies the circle holds less
than it.

    python scripts/docs/nirspec/fig_reported_flux.py --root <root> --obs <obs> [--grating prism_clear]
        [--catalog-db <campfire.db>] [--field cosmos]
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--grating', default='prism_clear')
p.add_argument('--catalog-db', default='~/campfire-data/meta/campfire.db',
               help='the local catalog mirror (read only) with the photometry')
p.add_argument('--field', default='cosmos')
p.add_argument('--bands', nargs='+', default=['f150w', 'f200w', 'f277w', 'f356w', 'f444w'])
p.add_argument('--min-snr', type=float, default=10.0)
p.add_argument('--aperture-json', default=None,
               help="the catalog's aperture fluxes per source and band ({'diam': [...], 'sources': {sid: {band: "
                    "{'aper': [...], 'auto': ...}}}}): compare with the 0.8\" aperture flux (linear in diameter) "
                    'instead of the total (Kron) flux')
args = p.parse_args()
root = C.setup_root(args.root)

import glob                                                           # noqa: E402
import json                                                           # noqa: E402
import os                                                             # noqa: E402
import sqlite3                                                        # noqa: E402

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402
from matplotlib.patches import Circle, Rectangle                      # noqa: E402

SLIT = 0.46                                   # arcsec per slit-frame unit (the open height)
OPEN_W, OPEN_H, PITCH_Y = 0.20, 0.46, 0.53    # open shutter on the sky [arcsec]


def photometry():
    db = sqlite3.connect(f'file:{os.path.expanduser(args.catalog_db)}?mode=ro', uri=True)
    grating = args.grating.split('_')[0].upper()
    rows = db.execute("select s.target_id, p.photometry from spectra s join object_photometry p "
                      "on p.object_id = s.object_id where s.observation = ? and s.grating = ? and p.field = ?",
                      (args.obs, grating, args.field))
    return {int(t.rsplit('_', 1)[1]): json.loads(ph)['bands'] for t, ph in rows}


APER = None
if args.aperture_json:
    with open(os.path.expanduser(args.aperture_json)) as fh:
        APER = json.load(fh)


def reference_flux(sid, b, pb):
    """The photometry the spectrum is compared with: the catalog's 0.8" aperture flux when an aperture file is
    given (None when it has no entry), else the total flux."""
    if APER is None:
        return pb['flux']
    rec = APER['sources'].get(str(sid), {}).get(b)
    if not rec:
        return None
    return float(np.interp(0.8, APER['diam'], rec['aper']))


def ratio(lam, f, e, bands, sid):
    out = []
    for b in args.bands:
        pb = bands.get(b)
        if not pb or not np.isfinite(pb.get('flux', np.nan)) or pb['flux'] <= 0 or \
                pb['flux'] / pb['flux_err'] < args.min_snr:
            continue
        ref = reference_flux(sid, b, pb)
        if ref is None or not np.isfinite(ref) or ref <= 0:
            continue
        s = (lam >= pb['wav_min']) & (lam <= pb['wav_max']) & np.isfinite(f)
        if s.sum() < 5 or lam[s].min() > pb['wav_min'] + 0.05 or lam[s].max() < pb['wav_max'] - 0.05:
            continue
        if np.mean(f[s]) / (np.sqrt(np.nansum(e[s] ** 2)) / s.sum()) < args.min_snr:
            continue
        out.append(np.mean(f[s]) / ref)
    return float(np.median(out)) if out else np.nan


phot = photometry()
rows = []
for path in sorted(glob.glob(os.path.join(C.workspace(root, args.obs), f'{args.obs}_{args.grating}_*_spec.fits'))):
    sid = int(os.path.basename(path).split('_')[-2])
    if sid not in phot:
        continue
    with fits.open(path) as h:
        H, t = h[0].header, h['SPEC1D'].data
        if H.get('PFSHAPE') != 'own':                       # the fitted width is the source's own
            continue
        lam = np.asarray(t['wave'], float)
        rep = ratio(lam, np.asarray(t['fnu'], float), np.asarray(t['fnu_err'], float), phot[sid], sid)
        opt = ratio(lam, np.asarray(t['fnu_opt'], float), np.asarray(t['fnu_opt_err'], float), phot[sid], sid)
        w2 = max(H['PFWIDTH'] + H['PFDWIDTH'] * np.log2(2.0 / H['PFPIVOT']), 0.003) * SLIT
    if np.isfinite(rep) and np.isfinite(opt) and rep > 0 and opt > 0:
        rows.append((2.355 * w2, opt, rep))
fwhm, opt, rep = (np.array(c) for c in zip(*rows))
print(f'{len(fwhm)} sources')

plt = C.style()
fig, (a, b) = plt.subplots(1, 2, figsize=(12.5, 4.8), constrained_layout=True,
                           gridspec_kw=dict(width_ratios=[1, 1.5]))
# a galaxy (FWHM 0.4"), the open shutter and its neighbors along the slit, and the 0.8" circle
g = np.linspace(-0.75, 0.75, 301)
X, Y = np.meshgrid(g, g)
s = 0.4 / 2.355
a.imshow(np.exp(-0.5 * (X ** 2 + Y ** 2) / s ** 2), extent=[g[0], g[-1], g[0], g[-1]], cmap='Greys', origin='lower')
for k in (-1, 0, 1):
    a.add_patch(Rectangle((-OPEN_W / 2, k * PITCH_Y - OPEN_H / 2), OPEN_W, OPEN_H, fill=False,
                          ec=C.PALETTE[0] if k == 0 else '#8fb8d8', lw=2.0 if k == 0 else 1.0, ls='-' if k == 0 else '--'))
a.add_patch(Circle((0, 0), 0.4, fill=False, ec=C.PALETTE[5], lw=2.0))
a.text(0.0, -0.58, 'the circle the reported\nflux refers to (0.8")', ha='center', fontsize=9, color=C.PALETTE[5])
a.text(0.13, 0.05, 'the open\nshutter', ha='left', fontsize=9, color=C.PALETTE[0])
a.set_xlim(g[0], g[-1])
a.set_ylim(g[0], g[-1])
a.set_xlabel('along the dispersion (arcsec)')
a.set_ylabel('along the slit (arcsec)')
a.set_title('A galaxy (FWHM 0.4") and the shutter')

bins = np.quantile(fwhm, np.linspace(0, 1, 7))
for v, color, label in ((opt, '#9a9a9a', 'optimal extraction: the light in the shutter (fnu_opt)'),
                        (rep, C.PALETTE[0], 'reported flux: the light in the 0.8" circle (fnu)')):
    b.scatter(fwhm, v, s=12, color=color, alpha=0.45, lw=0)
    lv = np.log10(v)
    mids, med, err = [], [], []
    for lo, hi in zip(bins[:-1], bins[1:]):
        sel = (fwhm >= lo) & (fwhm <= hi)
        mids.append(np.median(fwhm[sel]))
        med.append(10 ** np.median(lv[sel]))
        err.append(1.2533 * np.std(lv[sel]) / np.sqrt(sel.sum()) * med[-1] * np.log(10))
    b.errorbar(mids, med, yerr=err, color=color, lw=2.2, marker='o', ms=5, capsize=2, label=label)
b.axhline(1.0, color=C.INK, lw=0.8)
b.set_xscale('log')
b.set_yscale('log')
b.set_ylim(0.2, 2.5)
from matplotlib.ticker import FixedLocator, FormatStrFormatter, NullLocator   # noqa: E402
for axis, ticks in ((b.xaxis, [0.03, 0.05, 0.1, 0.2, 0.5, 1.0]), (b.yaxis, [0.2, 0.3, 0.5, 1.0, 2.0])):
    axis.set_major_locator(FixedLocator(ticks))
    axis.set_major_formatter(FormatStrFormatter('%g'))
    axis.set_minor_locator(NullLocator())
b.set_xlabel('fitted source size at 2 µm (FWHM of the fitted Gaussian, arcsec)')
b.set_ylabel('spectrum / imaging photometry in a 0.8" aperture' if APER else 'spectrum / total imaging photometry')
b.set_title(f'{len(fwhm)} galaxies with their own fitted shape')
b.legend(loc='lower left', fontsize=8.5)
fig.suptitle(f'{args.obs} {args.grating.split("_")[0].upper()}: what the reported flux means (photometry: '
             f'{args.field.upper()} catalog, bands {", ".join(x.upper() for x in args.bands)})', x=0.01,
             ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'reported_flux.png')
