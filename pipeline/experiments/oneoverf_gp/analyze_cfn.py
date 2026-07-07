"""
clean_flicker_noise (cfn) matrix analysis vs our median/GP striping.

Six arms (post-striping canonical exposures, one products tree each):
    median     rj0911_med        our production 2σ-clipped median (control)
    gp         rj0911_gp         our GP striping (the new method)
    cfn        rj0911_cfn        JWST cfn (median) standalone, no campfire 1/f
    cfngp      rj0911_cfngp      JWST cfn (median) + GP residual cleanup
    cfnfft     rj0911_cfnfft     JWST cfn (fft) standalone, no campfire 1/f
    cfnfftgp   rj0911_cfnfftgp   JWST cfn (fft) + GP residual cleanup

Three questions this answers:
  1. cfn standalone vs our GP  — does JWST's ramp-stage 1/f removal match or
     beat our amp-row GP on its own?
  2. cfn + GP (residual cleanup, nirspec-style) — does layering GP on top of
     cfn beat either alone?
  3. cfn median vs fft method.

All arms share the same uncal input + WCS + astrom_cats, so frames are on the
same detector grid and directly differenceable. Metrics are the ICL-insensitive
(high-passed) 1/f residuals from ab_metrics.

Outputs (experiments/oneoverf_gp/figs_cfn/):
    summary_cfn.md                 pooled + per-exposure metric table
    cfn_rowmedians_<root>.png      residual amp-row medians, all arms
    cfn_compare_<root>.png         matched-zscale frames, 2x3 grid
    cfn_radial_<root>.png          background radial profiles around sources
    cfn_photometry_<root>.png      aperture-flux vs median control

    conda run -n campfire python experiments/oneoverf_gp/analyze_cfn.py
"""

import glob
import os

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from astropy.visualization import ZScaleInterval

from astropy.stats import mad_std

from ab_metrics import (
    _highpass,
    aperture_photometry,
    amprow_residual_medians,
    detect_bright_sources,
    load_exposure,
    radial_profile,
    stripe_metrics,
    stripe_metrics_split,
)

ROOT = os.environ.get('CAMPFIRE_ROOT', '.')
HERE = os.path.dirname(os.path.abspath(__file__))
FIGS = os.path.join(HERE, 'figs_cfn')
os.makedirs(FIGS, exist_ok=True)

# Ordered so the table reads: our methods first, then cfn standalone, then
# cfn+GP combos. Tree names map to products/nircam/<tree>/f444w.
ARMS = [
    ('median',   'rj0911_med'),
    ('gp',       'rj0911_gp'),
    ('cfn',      'rj0911_cfn'),
    ('cfngp',    'rj0911_cfngp'),
    ('cfnfft',   'rj0911_cfnfft'),
    ('cfnfftgp', 'rj0911_cfnfftgp'),
]
ARM_DIR = {name: os.path.join(ROOT, 'products/nircam', tree, 'f444w')
           for name, tree in ARMS}
ARM_NAMES = [name for name, _ in ARMS]
COLORS = {
    'median':   'tab:blue',
    'gp':       'tab:orange',
    'cfn':      'tab:green',
    'cfngp':    'tab:red',
    'cfnfft':   '0.5',
    'cfnfftgp': 'tab:brown',
}
# fit_method="fft" is NIRSpec-only: jwst skips clean_flicker_noise for
# NRC_IMAGE, so cfnfft == a fully-uncorrected baseline and cfnfftgp == gp.
# Relabel accordingly and drop the redundant cfnfftgp from the plots.
DISPLAY = {
    'median':   'median (ours)',
    'gp':       'gp (ours)',
    'cfn':      'cfn-med alone',
    'cfngp':    'cfn-med + gp',
    'cfnfft':   'uncorrected',   # cfn-fft skipped on NIRCam -> no correction
    'cfnfftgp': '(= gp; fft skipped)',
}
# Plot order: worst (uncorrected) -> our methods -> cfn combos. Excludes the
# redundant cfnfftgp.
PLOT_ARMS = ['cfnfft', 'cfn', 'median', 'gp', 'cfngp']
# Baseline against which photometry / difference is measured.
BASE = 'median'


def lab(arm):
    return DISPLAY.get(arm, arm)


def present_arms():
    """Arms whose products tree actually has exposures on disk."""
    return [a for a in ARM_NAMES
            if glob.glob(os.path.join(ARM_DIR[a], '*long.fits'))]


def roots():
    """Exposure rootnames common to all present arms."""
    arms = present_arms()
    if not arms:
        return []
    sets = []
    for a in arms:
        fs = glob.glob(os.path.join(ARM_DIR[a], '*long.fits'))
        sets.append({os.path.basename(f).removesuffix('.fits') for f in fs})
    common = set.intersection(*sets)
    return sorted(common)


def arm_path(arm, root):
    return os.path.join(ARM_DIR[arm], f'{root}.fits')


def column_residual_std(sci, blank):
    """Residual *vertical* (per-column) striping scatter, high-passed.

    The cfn-standalone arms (estimator='none') get no campfire per-column
    step, so a fair comparison needs a vertical metric too: collapse blank
    pixels to a per-column median (over rows), high-pass along the column
    index to drop large-scale background, then take the mad_std. Lower =
    less residual column striping.
    """
    sub = sci.copy()
    sub[~blank] = np.nan
    with np.errstate(invalid='ignore'):
        colmed = np.nanmedian(sub, axis=0)
    hp = _highpass(colmed)
    finite = np.isfinite(hp)
    return float(mad_std(hp[finite])) if finite.sum() > 32 else np.nan


def build_summary(arms, rs):
    rows = []
    for root in rs:
        for arm in arms:
            p = arm_path(arm, root)
            if not os.path.exists(p):
                continue
            sci, blank, seg = load_exposure(p)
            m = stripe_metrics(sci, blank)
            m.update(stripe_metrics_split(sci, blank, seg))
            m['col_std'] = column_residual_std(sci, blank)
            m.update(root=root, arm=arm)
            rows.append(m)
    return rows


def write_summary_md(rows, arms):
    keys = ['stripe_std', 'stripe_hf', 'col_std', 'stripe_std_clean',
            'stripe_std_source', 'bkg_width']
    # Pooled means for the headline comparison vs gp (our method).
    pooled = {}
    for arm in arms:
        sub = [r for r in rows if r['arm'] == arm]
        pooled[arm] = {k: np.nanmean([r.get(k, np.nan) for r in sub])
                       for k in keys} if sub else {}
    ref = pooled.get('gp', {})

    lines = [
        '# clean_flicker_noise matrix vs median/GP striping (rj0911 f444w)\n',
        'Lower = cleaner. Metrics are high-passed (ICL-insensitive) 1/f '
        'residuals. Δ% columns are relative to **gp** (our method).\n',
        '> **Note:** `fit_method="fft"` is NIRSpec-only — jwst skips '
        'clean_flicker_noise for `NRC_IMAGE` (confirmed in run logs). So '
        '`cfnfft` is a fully-uncorrected baseline and `cfnfftgp` is '
        'byte-identical to `gp`. Only `fit_method="median"` is a real cfn '
        'test on NIRCam.\n',
        '## Pooled over exposures (mean), Δ% vs gp\n',
        '| arm | ' + ' | '.join(keys) + ' | Δ stripe_std vs gp |',
        '|' + '---|' * (len(keys) + 2),
    ]
    for arm in arms:
        p = pooled.get(arm, {})
        if not p:
            continue
        cells = [f'{p[k]:.4e}' if np.isfinite(p.get(k, np.nan)) else 'n/a'
                 for k in keys]
        if ref.get('stripe_std') and np.isfinite(ref['stripe_std']) and arm != 'gp':
            d = 100 * (p['stripe_std'] - ref['stripe_std']) / ref['stripe_std']
            dcell = f'{d:+.1f}%'
        else:
            dcell = '—'
        lines.append(f'| {arm} | ' + ' | '.join(cells) + f' | {dcell} |')

    lines.append('\n## Per exposure\n')
    lines.append('| root | arm | ' + ' | '.join(keys) + ' |')
    lines.append('|' + '---|' * (len(keys) + 2))
    for r in rows:
        cells = [f'{r.get(k, np.nan):.3e}' for k in keys]
        lines.append(f'| {r["root"]} | {r["arm"]} | ' + ' | '.join(cells) + ' |')

    path = os.path.join(FIGS, 'summary_cfn.md')
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print('wrote', path)
    print('\n'.join(lines[:5 + len(arms)]))


def _zscale(sci, contrast=0.15):
    return ZScaleInterval(contrast=contrast).get_limits(
        sci[np.isfinite(sci) & (sci != 0)])


def plot_rowmedians(root, arms):
    fig, axes = plt.subplots(4, 1, figsize=(11, 9), sharex=True)
    span = 0.0
    for arm in arms:
        p = arm_path(arm, root)
        if not os.path.exists(p):
            continue
        sci, blank, _ = load_exposure(p)
        rms = amprow_residual_medians(sci, blank)
        for ax, amp in zip(axes, ('A', 'B', 'C', 'D')):
            rm = rms[amp] - np.nanmedian(rms[amp])
            ax.plot(rm, color=COLORS[arm], lw=0.6, label=lab(arm), alpha=0.8)
            good = np.isfinite(rm)
            if good.any():
                span = max(span, float(np.nanpercentile(np.abs(rm[good]), 99)))
    for ax, amp in zip(axes, ('A', 'B', 'C', 'D')):
        ax.set_ylabel(f'amp {amp}\nrow median')
        ax.axhline(0, color='k', lw=0.4, ls=':')
        if span > 0:
            ax.set_ylim(-1.5 * span, 1.5 * span)
    axes[0].legend(ncol=len(arms), fontsize=7, loc='upper right')
    axes[-1].set_xlabel('row (slow axis)')
    fig.suptitle(f'Residual amp-row medians (cal stage) — {root}')
    fig.tight_layout()
    out = os.path.join(FIGS, f'cfn_rowmedians_{root}.png')
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print('wrote', out)


def plot_compare(root, arms, zoom=None, tag='cfn_compare'):
    """Matched-zscale frames (stretch from the median control)."""
    smed, _, _ = load_exposure(arm_path(BASE, root))
    if zoom is not None:
        y0, y1, x0, x1 = zoom
        smed = smed[y0:y1, x0:x1]
    vmin, vmax = _zscale(smed)
    n = len(arms)
    ncol = 3
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(4.4 * ncol, 4.6 * nrow),
                             squeeze=False)
    for i in range(nrow * ncol):
        ax = axes[i // ncol][i % ncol]
        if i >= n:
            ax.axis('off')
            continue
        arm = arms[i]
        sci, _, _ = load_exposure(arm_path(arm, root))
        if zoom is not None:
            sci = sci[y0:y1, x0:x1]
        ax.imshow(sci, origin='lower', cmap='Greys_r', vmin=vmin, vmax=vmax)
        ax.set_title(lab(arm))
        ax.set_xticks([]); ax.set_yticks([])
    fig.suptitle(f'{tag} — {root}  (matched zscale [{vmin:.4g}, {vmax:.4g}])')
    fig.tight_layout()
    out = os.path.join(FIGS, f'{tag}_{root}.png')
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print('wrote', out)


def plot_radial(root, arms, sources):
    n = min(len(sources), 6)
    if n == 0:
        return
    ncol = 3
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(4 * ncol, 3 * nrow),
                             squeeze=False)
    loaded = {arm: load_exposure(arm_path(arm, root))
              for arm in arms if os.path.exists(arm_path(arm, root))}
    for i in range(n):
        ax = axes[i // ncol][i % ncol]
        yc, xc, flux = sources[i]
        for arm, (sci, blank, _) in loaded.items():
            r, prof = radial_profile(sci, blank, yc, xc)
            ax.plot(r, prof, color=COLORS[arm], label=lab(arm), lw=1.1)
        ax.axhline(0, color='k', lw=0.4, ls=':')
        ax.set_title(f'src ({int(xc)},{int(yc)}) f={flux:.0f}', fontsize=8)
        ax.set_xlabel('r [px]'); ax.set_ylabel('bkg median')
    axes[0][0].legend(fontsize=7)
    fig.suptitle(f'Background radial profiles around bright sources — {root}\n'
                 '(negative trough = oversubtraction)')
    fig.tight_layout()
    out = os.path.join(FIGS, f'cfn_radial_{root}.png')
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print('wrote', out)


def plot_photometry(root, arms, sources):
    if not sources:
        return
    fluxes = {}
    for arm in arms:
        p = arm_path(arm, root)
        if os.path.exists(p):
            sci, _, _ = load_exposure(p)
            fluxes[arm] = aperture_photometry(sci, sources)
    if BASE not in fluxes:
        return
    base = fluxes[BASE]
    fig, ax = plt.subplots(figsize=(7, 5))
    for arm in arms:
        if arm == BASE or arm not in fluxes:
            continue
        frac = (fluxes[arm] - base) / np.where(base != 0, base, np.nan)
        ax.plot(base, frac * 100, 'o', color=COLORS[arm], label=lab(arm), alpha=0.8)
    ax.axhline(0, color='k', lw=0.5)
    ax.set_xscale('symlog')
    ax.set_xlabel(f'{BASE} aperture flux')
    ax.set_ylabel(f'Δflux vs {BASE} [%]')
    ax.set_title(f'Photometric conservation — {root}')
    ax.legend(fontsize=8)
    fig.tight_layout()
    out = os.path.join(FIGS, f'cfn_photometry_{root}.png')
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print('wrote', out)


def main():
    arms = present_arms()
    rs = roots()
    print('present arms:', arms)
    print('common exposures:', rs)
    if not rs:
        print('no common exposures yet; arms still running?')
        return
    rows = build_summary(arms, rs)
    write_summary_md(rows, arms)

    # Curated arm set for the figures (drops the redundant cfnfftgp == gp;
    # cfnfft shown as the uncorrected baseline).
    plot_arms = [a for a in PLOT_ARMS if a in arms]

    rep, best_sources = rs[0], []
    for root in rs:
        sci, blank, _ = load_exposure(arm_path(BASE, root))
        srcs = detect_bright_sources(sci, blank)
        if srcs and (not best_sources or srcs[0][2] > best_sources[0][2]):
            best_sources, rep = srcs, root
    print(f'representative exposure: {rep} ({len(best_sources)} bright sources)')

    plot_rowmedians(rep, plot_arms)
    plot_compare(rep, plot_arms, tag='cfn_compare')
    if best_sources:
        yc, xc, _ = best_sources[0]
        half = 220
        h, w = load_exposure(arm_path(BASE, rep))[0].shape
        zoom = (max(int(yc) - half, 0), min(int(yc) + half, h),
                max(int(xc) - half, 0), min(int(xc) + half, w))
        plot_compare(rep, plot_arms, zoom=zoom, tag='cfn_zoom')
        plot_radial(rep, plot_arms, best_sources)
        plot_photometry(rep, plot_arms, best_sources)


if __name__ == '__main__':
    main()
