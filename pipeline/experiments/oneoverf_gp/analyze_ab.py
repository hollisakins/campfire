"""
Analyze the GP-vs-median 1/f striping A/B and emit metrics + figures.

Arms (post-striping canonical exposures, one tree each):
    before  pre-striping snapshot (experiments/oneoverf_gp/prestriping/)
    median  products/nircam/rj0911_med/f444w     (control)
    gp      products/nircam/rj0911_gp/f444w
    gp_aggr products/nircam/rj0911_gpa/f444w

Outputs (to experiments/oneoverf_gp/figs/):
    summary.md                       per-exposure + pooled metric table
    stripe_rowmedians_<root>.png     residual amp-row medians vs row, all arms
    compare_<root>.png               matched-zscale full frame, all arms
    zoom_<root>.png                  matched-zscale zoom on brightest source
    radial_<root>.png                background radial profiles around sources
    photometry.png                   aperture-flux stability vs control

Run after the three arms finish:
    conda run -n campfire python experiments/oneoverf_gp/analyze_ab.py
"""

import glob
import os

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from astropy.visualization import ZScaleInterval

from ab_metrics import (
    aperture_photometry,
    amprow_residual_medians,
    detect_bright_sources,
    load_exposure,
    radial_profile,
    stripe_metrics,
    stripe_metrics_split,
)
from campfire_pipeline.nircam.constants import NIR_AMPS

ROOT = os.environ.get('CAMPFIRE_ROOT', '.')
HERE = os.path.dirname(os.path.abspath(__file__))
FIGS = os.path.join(HERE, 'figs')
os.makedirs(FIGS, exist_ok=True)

ARMS = {
    'before': os.path.join(HERE, 'prestriping'),
    'median': os.path.join(ROOT, 'products/nircam/rj0911_med/f444w'),
    'gp':     os.path.join(ROOT, 'products/nircam/rj0911_gp/f444w'),
    'gp_aggr': os.path.join(ROOT, 'products/nircam/rj0911_gpa/f444w'),
}
COLORS = {'before': '0.6', 'median': 'tab:blue', 'gp': 'tab:orange',
          'gp_aggr': 'tab:red'}
# Arms that actually carry a corrected SCI (skip 'before' in metric tables).
CORR_ARMS = ['median', 'gp', 'gp_aggr']


def roots():
    """Exposure rootnames present in the median arm."""
    files = sorted(glob.glob(os.path.join(ARMS['median'], '*long.fits')))
    return [os.path.basename(f).removesuffix('.fits') for f in files]


def arm_path(arm, root):
    return os.path.join(ARMS[arm], f'{root}.fits')


def build_summary():
    rows = []
    for root in roots():
        for arm in CORR_ARMS:
            p = arm_path(arm, root)
            if not os.path.exists(p):
                continue
            sci, blank, seg = load_exposure(p)
            m = stripe_metrics(sci, blank)
            m.update(stripe_metrics_split(sci, blank, seg))
            m.update(root=root, arm=arm)
            rows.append(m)
    return rows


def write_summary_md(rows):
    keys = ['stripe_std', 'stripe_hf', 'stripe_std_clean', 'stripe_std_source',
            'bkg_width']
    lines = ['# GP vs median 1/f striping — A/B metrics (rj0911 f444w)\n',
             'Lower `stripe_std` / `stripe_hf` / `bkg_width` = cleaner; '
             '`psd_lowfreq` = fraction of residual row-median power at low '
             'frequency.\n',
             '## Pooled over exposures (mean ± std)\n',
             '| arm | ' + ' | '.join(keys) + ' |',
             '|' + '---|' * (len(keys) + 1)]
    for arm in CORR_ARMS:
        sub = [r for r in rows if r['arm'] == arm]
        if not sub:
            continue
        cells = []
        for k in keys:
            v = np.array([r[k] for r in sub if np.isfinite(r.get(k, np.nan))])
            cells.append(f'{v.mean():.4e} ± {v.std():.1e}' if v.size else 'n/a')
        lines.append(f'| {arm} | ' + ' | '.join(cells) + ' |')
    lines.append('\n## Per exposure\n')
    lines.append('| root | arm | ' + ' | '.join(keys) + ' |')
    lines.append('|' + '---|' * (len(keys) + 2))
    for r in rows:
        cells = [f'{r.get(k, np.nan):.3e}' for k in keys]
        lines.append(f'| {r["root"]} | {r["arm"]} | ' + ' | '.join(cells) + ' |')
    path = os.path.join(FIGS, 'summary.md')
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print('wrote', path)
    # Also echo the pooled table to stdout.
    print('\n'.join(lines[:6 + len(CORR_ARMS)]))


def plot_rowmedians(root):
    """Residual per-amp-row medians vs row for each corrected arm (per amp).

    Corrected arms are all cal-stage (matched units); 'before' is rate-stage
    (different units) so it is excluded here.
    """
    fig, axes = plt.subplots(4, 1, figsize=(11, 9), sharex=True)
    span = 0.0
    for arm in CORR_ARMS:
        p = arm_path(arm, root)
        if not os.path.exists(p):
            continue
        sci, blank, _ = load_exposure(p)
        rms = amprow_residual_medians(sci, blank)
        for ax, amp in zip(axes, ('A', 'B', 'C', 'D')):
            rm = rms[amp] - np.nanmedian(rms[amp])
            ax.plot(rm, color=COLORS[arm], lw=0.6, label=arm, alpha=0.85)
            good = np.isfinite(rm)
            if good.any():
                span = max(span, float(np.nanpercentile(np.abs(rm[good]), 99)))
    for ax, amp in zip(axes, ('A', 'B', 'C', 'D')):
        ax.set_ylabel(f'amp {amp}\nrow median')
        ax.axhline(0, color='k', lw=0.4, ls=':')
        if span > 0:
            ax.set_ylim(-1.5 * span, 1.5 * span)
    axes[0].legend(ncol=3, fontsize=8, loc='upper right')
    axes[-1].set_xlabel('row (slow axis)')
    fig.suptitle(f'Residual amp-row medians (cal stage) — {root}')
    fig.tight_layout()
    out = os.path.join(FIGS, f'stripe_rowmedians_{root}.png')
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print('wrote', out)


def _zscale(sci, contrast=0.15):
    return ZScaleInterval(contrast=contrast).get_limits(
        sci[np.isfinite(sci) & (sci != 0)])


def plot_compare(root, zoom=None, tag='compare'):
    """Comparison panels: corrected arms share one zscale (matched units).

    'before' (rate-stage, different units) is shown first on its OWN zscale,
    labelled, purely as raw-stripe context — it is not part of the matched
    comparison among median/gp/gp_aggr.
    """
    arms = ([a for a in ['before'] if os.path.exists(arm_path(a, root))]
            + [a for a in CORR_ARMS if os.path.exists(arm_path(a, root))])
    # Matched stretch for the corrected (cal-stage) arms, from the control.
    smed, _, _ = load_exposure(arm_path('median', root))
    if zoom is not None:
        y0, y1, x0, x1 = zoom
        smed = smed[y0:y1, x0:x1]
    vmin, vmax = _zscale(smed)
    fig, axes = plt.subplots(1, len(arms), figsize=(4.2 * len(arms), 4.4))
    if len(arms) == 1:
        axes = [axes]
    for ax, arm in zip(axes, arms):
        sci, _, _ = load_exposure(arm_path(arm, root))
        if zoom is not None:
            sci = sci[y0:y1, x0:x1]
        if arm == 'before':
            lo, hi = _zscale(sci)
            ax.set_title('before (rate, own scale)', fontsize=9)
        else:
            lo, hi = vmin, vmax
            ax.set_title(arm)
        ax.imshow(sci, origin='lower', cmap='Greys_r', vmin=lo, vmax=hi)
        ax.set_xticks([]); ax.set_yticks([])
    fig.suptitle(f'{tag} — {root}  '
                 f'(corrected arms matched zscale [{vmin:.4g}, {vmax:.4g}])')
    fig.tight_layout()
    out = os.path.join(FIGS, f'{tag}_{root}.png')
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print('wrote', out)


def plot_difference(root, zoom=None, tag='difference'):
    """Per-exposure (gp − median) and (gp_aggr − median) difference maps.

    The cal-stage frames share the detector pixel grid (same uncal input),
    so the difference is the difference in the *applied 1/f correction*.
    Where the median fell back to the full-row value, its correction is the
    wrong (cross-amp) quantity over the source's amp-rows; that shows up as
    a structured amp-boundary + slow-axis "box" in the difference, while
    clean rows differ only by the GP's smoothing. Diverging cmap, symmetric
    robust limits.
    """
    med, _, _ = load_exposure(arm_path('median', root))
    panels = [('median (control)', med, 'grey')]
    for arm in ('gp', 'gp_aggr'):
        p = arm_path(arm, root)
        if os.path.exists(p):
            sci, _, _ = load_exposure(p)
            panels.append((f'{arm} − median', sci - med, 'div'))
    if zoom is not None:
        y0, y1, x0, x1 = zoom
        panels = [(t, a[y0:y1, x0:x1], k) for t, a, k in panels]
    gvmin, gvmax = _zscale(panels[0][1])
    # Symmetric limit for the difference panels from their robust scatter.
    diffs = np.concatenate([a[np.isfinite(a)].ravel() for _, a, k in panels
                            if k == 'div'])
    dlim = 3.0 * float(np.nanstd(diffs)) if diffs.size else 1.0
    fig, axes = plt.subplots(1, len(panels), figsize=(4.3 * len(panels), 4.5))
    for ax, (title, arr, kind) in zip(axes, panels):
        if kind == 'div':
            im = ax.imshow(arr, origin='lower', cmap='RdBu_r',
                           vmin=-dlim, vmax=dlim)
            fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        else:
            ax.imshow(arr, origin='lower', cmap='Greys_r',
                      vmin=gvmin, vmax=gvmax)
        ax.set_title(title, fontsize=9)
        ax.set_xticks([]); ax.set_yticks([])
    fig.suptitle(f'{tag} — {root}  (diff limit ±{dlim:.3g})')
    fig.tight_layout()
    out = os.path.join(FIGS, f'{tag}_{root}.png')
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print('wrote', out)


def plot_radial(root, sources):
    """Background radial profiles around bright sources, all arms overlaid."""
    n = min(len(sources), 6)
    if n == 0:
        return
    ncol = 3
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(4 * ncol, 3 * nrow),
                             squeeze=False)
    loaded = {arm: load_exposure(arm_path(arm, root))
              for arm in CORR_ARMS if os.path.exists(arm_path(arm, root))}
    for i in range(n):
        ax = axes[i // ncol][i % ncol]
        yc, xc, flux = sources[i]
        for arm, (sci, blank, _) in loaded.items():
            r, prof = radial_profile(sci, blank, yc, xc)
            ax.plot(r, prof, color=COLORS[arm], label=arm, lw=1.2)
        ax.axhline(0, color='k', lw=0.4, ls=':')
        ax.set_title(f'src ({int(xc)},{int(yc)}) f={flux:.0f}', fontsize=8)
        ax.set_xlabel('r [px]'); ax.set_ylabel('bkg median')
    axes[0][0].legend(fontsize=8)
    fig.suptitle(f'Background radial profiles around bright sources — {root}\n'
                 '(negative trough = oversubtraction)')
    fig.tight_layout()
    out = os.path.join(FIGS, f'radial_{root}.png')
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print('wrote', out)


def plot_photometry(root, sources):
    """Aperture flux of each arm vs the control, to check conservation."""
    if not sources:
        return
    fluxes = {}
    for arm in CORR_ARMS:
        p = arm_path(arm, root)
        if os.path.exists(p):
            sci, _, _ = load_exposure(p)
            fluxes[arm] = aperture_photometry(sci, sources)
    if 'median' not in fluxes:
        return
    base = fluxes['median']
    fig, ax = plt.subplots(figsize=(6, 5))
    for arm in ('gp', 'gp_aggr'):
        if arm not in fluxes:
            continue
        frac = (fluxes[arm] - base) / np.where(base != 0, base, np.nan)
        ax.plot(base, frac * 100, 'o', color=COLORS[arm], label=arm, alpha=0.8)
    ax.axhline(0, color='k', lw=0.5)
    ax.set_xscale('symlog')
    ax.set_xlabel('control aperture flux')
    ax.set_ylabel('Δflux vs control [%]')
    ax.set_title(f'Photometric conservation — {root}')
    ax.legend()
    fig.tight_layout()
    out = os.path.join(FIGS, f'photometry_{root}.png')
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print('wrote', out)


def main():
    rs = roots()
    print('exposures:', rs)
    rows = build_summary()
    write_summary_md(rows)

    # Pick the exposure with the brightest detected source for the visuals.
    rep = rs[0]
    best_sources = []
    best_root = rep
    for root in rs:
        sci, blank, _ = load_exposure(arm_path('median', root))
        srcs = detect_bright_sources(sci, blank)
        if srcs and (not best_sources or srcs[0][2] > best_sources[0][2]):
            best_sources, best_root = srcs, root
    print(f'representative exposure: {best_root} '
          f'({len(best_sources)} bright sources)')

    plot_rowmedians(best_root)
    plot_compare(best_root, tag='compare')
    plot_difference(best_root, tag='difference')
    if best_sources:
        yc, xc, _ = best_sources[0]
        half = 220
        h, w = load_exposure(arm_path('median', best_root))[0].shape
        zoom = (max(int(yc) - half, 0), min(int(yc) + half, h),
                max(int(xc) - half, 0), min(int(xc) + half, w))
        plot_compare(best_root, zoom=zoom, tag='zoom')
        plot_difference(best_root, zoom=zoom, tag='difference_zoom')
        plot_radial(best_root, best_sources)
        plot_photometry(best_root, best_sources)


if __name__ == '__main__':
    main()
