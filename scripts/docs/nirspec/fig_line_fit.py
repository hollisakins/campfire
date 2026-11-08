"""Emission-line fit at the inspected redshift: the spectrum, the fitted windows, and close-ups of line groups.

Top: the 1-D spectrum (flam) with the line model, the fitted windows shaded. Below: one column per chosen line group,
the spectrum, the model (Gaussians on the local linear continuum) and the continuum, each fitted line labeled with
its S/N; under each, the residual in units of the uncertainty.

    python scripts/docs/nirspec/fig_line_fit.py --root <root> --obs <obs> --grating prism_clear --source 635314 \
        --group OIII5007 --group Halpha --group HeI10830
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--grating', default='prism_clear')
p.add_argument('--source', required=True)
p.add_argument('--group', action='append', required=True,
               help='a line whose fitted complex is shown in a close-up (repeatable)')
p.add_argument('--min-snr', type=float, default=3.0, help='label lines detected above this S/N')
args = p.parse_args()
root = C.setup_root(args.root)

import os                                                             # noqa: E402

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402

LABELS = {'OII3727': '[OII]', 'OII3726': '[OII]', 'NeIII3869': '[NeIII]', 'Hgamma': 'Hγ', 'Hbeta': 'Hβ', 'OIII4959': '[OIII]',
          'OIII5007': '[OIII]', 'Halpha': 'Hα', 'NII6585': '[NII]', 'NII6550': '[NII]', 'SII6725': '[SII]',
          'SII6716': '[SII]', 'SII6731': '[SII]', 'SIII9069': '[SIII]', 'SIII9531': '[SIII]', 'HeI10830': 'HeI',
          'Pagamma': 'Paγ', 'Pabeta': 'Paβ', 'Padelta': 'Paδ', 'HeI5876': 'HeI', 'Hdelta': 'Hδ', 'Paalpha': 'Paα'}

base = os.path.join(C.workspace(root, args.obs), f'{args.obs}_{args.grating}_{args.source}')
with fits.open(base + '_spec.fits') as h:
    t = h['SPEC1D'].data
    wave, flam, err = (np.asarray(t[c], float) for c in ('wave', 'flam', 'flam_err'))
with fits.open(base + '_lines.fits') as h:
    H = h[0].header
    L = h['LINES'].data
    mw, model, cont = (np.asarray(h['MODEL'].data[c], float) for c in ('wave', 'model', 'cont'))
    cx = h['COMPLEXES'].data
z = float(H.get('ZUSED', np.nan))
model_on = np.interp(wave, mw, model, left=np.nan, right=np.nan)
cont_on = np.interp(wave, mw, cont, left=np.nan, right=np.nan)
windows = [(mw[int(c['lo_idx'])], mw[min(int(c['hi_idx']), len(mw) - 1)], c['lines'].split(',')) for c in cx]
rows = {str(r['name']).strip(): r for r in L}
# lines to label: detected, not folded into another line, not a tied doublet member, not a doublet total
shown = [r for r in L if np.isfinite(r['snr']) and r['snr'] >= args.min_snr and not (int(r['flags']) & (1 | 2))
         and str(r['component']).strip() == 'narrow']

plt = C.style()
n = len(args.group)
fig = plt.figure(figsize=(12, 7.4), constrained_layout=True)
gs = fig.add_gridspec(3, n, height_ratios=[1.15, 1.0, 0.32])
ax = fig.add_subplot(gs[0, :])
good = np.isfinite(flam)
scale = 1e-19
ax.fill_between(wave, (flam - err) / scale, (flam + err) / scale, color='#bbbbbb', lw=0, step='mid')
ax.step(wave, flam / scale, where='mid', color=C.INK, lw=0.7, label='spectrum')
ax.plot(wave, model_on / scale, color=C.PALETTE[5], lw=1.2, label='line model')
for k, name in enumerate(args.group):
    lo, hi, _ = next(w for w in windows if name in w[2])
    ax.axvspan(lo, hi, color=C.PALETTE[4], alpha=0.15, lw=0)
    ax.text(0.5 * (lo + hi), 0.97, chr(ord('a') + k), transform=ax.get_xaxis_transform(), ha='center', va='top',
            fontsize=10, color=C.PALETTE[0], fontweight='bold')
top = np.nanpercentile(flam[good], 99.7) / scale * 1.15
ax.set_ylim(min(-0.05 * top, np.nanpercentile(flam[good], 0.5) / scale), top)
ax.set_xlim(np.nanmin(wave[good]), np.nanmax(wave[good]))
ax.axhline(0, color=C.MUTED, lw=0.6)
ax.set_xlabel('observed wavelength (µm)')
ax.set_ylabel('f$_λ$ (10$^{-19}$ erg s$^{-1}$ cm$^{-2}$ Å$^{-1}$)')
ax.set_title(f'source {args.source}, inspected z = {z:.3f}', loc='left', fontsize=11)
ax.legend(loc='upper right', fontsize=8.5)

for k, name in enumerate(args.group):
    lo, hi, members = next(w for w in windows if name in w[2])
    pad = 0.15 * (hi - lo)
    s = (wave >= lo - pad) & (wave <= hi + pad)
    a = fig.add_subplot(gs[1, k])
    a.fill_between(wave[s], (flam - err)[s] / scale, (flam + err)[s] / scale, color='#bbbbbb', lw=0, step='mid')
    a.step(wave[s], flam[s] / scale, where='mid', color=C.INK, lw=0.8)
    a.plot(mw, model / scale, color=C.PALETTE[5], lw=1.4)
    a.plot(mw, cont / scale, color=C.PALETTE[0], lw=1.0, ls='--', label='continuum' if k == 0 else None)
    a.set_xlim(lo, hi)
    ymax = np.nanmax((flam + err)[s]) / scale
    a.set_ylim(min(0, np.nanmin(flam[s]) / scale) * 1.1, ymax * 1.45)
    inside = sorted((r for r in shown if lo <= r['wave_obs'] <= hi), key=lambda r: r['wave_obs'])
    last, level = -np.inf, 0
    for r in inside:
        level = level + 1 if r["wave_obs"] - last < 0.16 * (hi - lo) else 0      # stagger labels that would touch
        last = r['wave_obs']
        a.axvline(r['wave_obs'], color=C.PALETTE[0], lw=0.5, ls=':')
        a.text(r['wave_obs'], ymax * (1.42 - 0.17 * (level % 2)),
               f"{LABELS.get(str(r['name']).strip(), str(r['name']).strip())}\nS/N {r['snr']:.0f}",
               ha='center', va='top', fontsize=8.5, color=C.INK, bbox=dict(boxstyle='round,pad=0.1', fc='white', ec='none'))
    a.text(0.02, 0.97, chr(ord('a') + k), transform=a.transAxes, ha='left', va='top', fontsize=10,
           color=C.PALETTE[0], fontweight='bold')
    if k == 0:
        a.set_ylabel('f$_λ$ (10$^{-19}$ cgs)')
        a.legend(loc='center left', fontsize=8)
    a.tick_params(labelbottom=False)
    r_ax = fig.add_subplot(gs[2, k], sharex=a)
    res = (flam - model_on) / err
    r_ax.step(wave[s], res[s], where='mid', color=C.INK, lw=0.8)
    r_ax.axhline(0, color=C.MUTED, lw=0.6)
    r_ax.set_ylim(-4.5, 4.5)
    r_ax.set_xlabel('observed wavelength (µm)')
    if k == 0:
        r_ax.set_ylabel('residual (σ)')
fig.suptitle(f'{args.obs} {args.grating.split("_")[0].upper()}: emission-line fit at the inspected redshift',
             x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'line_fit.png')
