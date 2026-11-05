"""How a redshift is fitted: the spectrum against the best template combination, and the fit's quality at
every trial redshift.

At each redshift on a grid the spectrum is fitted as a non-negative sum of templates (stellar populations,
emission lines, broad lines, blackbodies); the redshift where the fit is best is the pipeline's redshift. One
row per source: left, the spectrum (fnu) and the best-fit model, with the main emission lines marked at the
fitted redshift; right, chi-squared against redshift, relative to its minimum.

    python scripts/docs/nirspec/fig_redshift_fit.py --root <root> --obs <obs> --grating prism_clear \
        --source 635292 --source 635314
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--grating', default='prism_clear')
p.add_argument('--source', action='append', required=True)
args = p.parse_args()
root = C.setup_root(args.root)

import os                                                             # noqa: E402

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402

LINES = {'Lyα': 1215.67, 'CIII]': 1908.7, '[OII]': 3728.5, 'Hβ': 4862.7, '[OIII]': 5008.2, 'Hα': 6564.6,
         '[SIII]': 9533.2, 'HeI': 10833.3, 'Paβ': 12821.6, 'Paα': 18756.1}

ws = C.workspace(root, args.obs)
plt = C.style()
n = len(args.source)
fig = plt.figure(figsize=(14, 3.4 * n), constrained_layout=True)
gs = fig.add_gridspec(n, 2, width_ratios=[2.6, 1])
for k, sid in enumerate(args.source):
    base = os.path.join(ws, f'{args.obs}_{args.grating}_{sid}')
    with fits.open(base + '_spec.fits') as h:
        t = h['SPEC1D'].data
        wave, fnu, err = (np.asarray(t[c], float) for c in ('wave', 'fnu', 'fnu_err'))
    with fits.open(base + '_zfit.fits') as h:
        z = float(h[0].header['ZBEST'])
        mw, mf = np.asarray(h['MODEL'].data['wav'], float), np.asarray(h['MODEL'].data['fnu'], float)
        zg, chi2 = np.asarray(h['CHI2'].data['z'], float), np.asarray(h['CHI2'].data['chi2'], float)
    ax = fig.add_subplot(gs[k, 0])
    good = np.isfinite(fnu)
    ax.fill_between(wave, fnu - err, fnu + err, color='#bbbbbb', lw=0, step='mid')
    ax.step(wave, fnu, where='mid', color=C.INK, lw=0.8, label='spectrum (fnu)')
    ax.plot(mw, mf, color=C.PALETTE[5], lw=1.4, label='best-fit templates')
    top = np.nanpercentile(fnu[good], 99.5) * 1.25
    lo, hi = np.nanmin(wave[good]), np.nanmax(wave[good])
    for name, rest in LINES.items():
        lam = rest * (1 + z) / 1e4
        if lo < lam < hi:
            ax.axvline(lam, color=C.PALETTE[0], lw=0.6, ls=':')
            ax.text(lam, top * 0.97, name, rotation=90, ha='right', va='top', fontsize=8, color=C.PALETTE[0])
    ax.set_xlim(lo, hi)
    ax.set_ylim(min(-0.05 * top, np.nanpercentile(fnu[good], 0.5)), top)
    ax.axhline(0, color=C.MUTED, lw=0.6)
    ax.set_ylabel('flux density (µJy)')
    ax.set_title(f'source {sid}: z = {z:.3f}', loc='left', fontsize=11)
    if k == n - 1:
        ax.set_xlabel('wavelength (µm)')
    if k == 0:
        ax.legend(loc='lower right', fontsize=8.5)
    ax = fig.add_subplot(gs[k, 1])
    o = np.argsort(zg)
    ax.plot(zg[o], chi2[o] - np.nanmin(chi2), color=C.INK, lw=0.9)
    ax.axvline(z, color=C.PALETTE[5], lw=1.0)
    ax.set_yscale('symlog', linthresh=10)
    ax.set_ylim(0, None)
    ax.set_ylabel('χ² - minimum')
    ax.set_title('fit quality at every redshift', loc='left', fontsize=11)
    if k == n - 1:
        ax.set_xlabel('redshift')
fig.suptitle(f'{args.obs} {args.grating.split("_")[0].upper()}: template redshift fits', x=0.01, ha='left',
             fontsize=10, color=C.MUTED)
C.save(fig, args, 'redshift_fit.png')
