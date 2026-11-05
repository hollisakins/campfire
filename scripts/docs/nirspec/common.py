"""Shared pieces of the NIRSpec documentation figures.

Every figure script takes the CAMPFIRE root and the observation on the command line, so
the same figure can be redrawn from another reduction or another observation:

    python scripts/docs/nirspec/<figure>.py --root <CAMPFIRE_ROOT> --obs <obs> [figure options]

``setup_root`` must run before anything from ``campfire_pipeline`` is imported: it points
``CAMPFIRE_ROOT`` and ``CRDS_PATH`` at the given root, so a figure never reads or writes
another root's cache. Figures meant for the docs pages are written to
``web/public/docs/nirspec/``; validation figures for the pull request to
``docs/figures/pipeline-v2/``.
"""
import argparse
import glob
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
OUT_DIRS = {'docs': os.path.join(REPO, 'web', 'public', 'docs', 'nirspec'),
            'pr': os.path.join(REPO, 'docs', 'figures', 'pipeline-v2')}

# Okabe-Ito: distinguishable with the common forms of color blindness, in a fixed order.
PALETTE = ['#0072B2', '#E69F00', '#009E73', '#CC79A7', '#56B4E9', '#D55E00', '#F0E442', '#000000']
# Wavelength colors, blue to red, the same in every figure that compares wavelengths: 1, 3, 5 µm (or the
# nearest the figure uses). Kept apart from the colors that mark kinds of source or of model.
WAVE_COLORS = ['#0072B2', '#009E73', '#D55E00']
INK = '#222222'
MUTED = '#777777'


def parser(description, obs=True):
    """Argument parser with the options every figure takes (``obs`` False for a figure of
    calibration files only)."""
    p = argparse.ArgumentParser(description=description)
    p.add_argument('--root', required=True, help='CAMPFIRE root holding the reduction (an isolated root is fine)')
    if obs:
        p.add_argument('--obs', required=True, help='observation name, as in config/observations.toml')
    p.add_argument('--kind', choices=sorted(OUT_DIRS), default='docs', help='where the figure is written')
    p.add_argument('--out', default=None, help='output file name (default: the script name, .png)')
    return p


def setup_root(root):
    """Point the pipeline at ``root`` (its config, products and CRDS cache) and make the
    repository's packages importable when the script is run from a checkout."""
    root = os.path.abspath(os.path.expanduser(root))
    os.environ['CAMPFIRE_ROOT'] = root
    os.environ['CRDS_PATH'] = os.path.join(root, 'cache', 'crds')
    for sub in ('pipeline', 'layout', 'python'):
        path = os.path.join(REPO, sub)
        if path not in sys.path:
            sys.path.insert(0, path)
    return root


def observation(obs_name):
    """(config, Observation) for ``obs_name`` in the current root."""
    from campfire_pipeline.nirspec import cli
    cfg, obs, _ = cli._setup(None, obs_name)
    return cfg, obs


def workspace(root, obs_name):
    return os.path.join(root, 'products', 'nirspec', obs_name)


def rate_file(ws, exposure):
    """``<exposure>_rate.fits``, exposure like 'jw07076001001_05101_00001_nrs1'."""
    return os.path.join(ws, f'{exposure}_rate.fits')


def mbkg_file(ws, exposure):
    return os.path.join(ws, f'{exposure}_mbkg.fits')


def canonical(ws, exposure, source_id):
    """The calibrated cutout of one source in one exposure (stage 2a), or None."""
    p = os.path.join(ws, f'{exposure}_{source_id}.fits')
    return p if os.path.exists(p) else None


def spec_file(ws, obs_name, grating_tag, source_id):
    """``<obs>_<grating>_<sid>_spec.fits``, grating_tag like 'prism_clear' or 'g395m_f290lp'."""
    return os.path.join(ws, f'{obs_name}_{grating_tag}_{source_id}_spec.fits')


def exposures(ws, pattern='jw*_rate.fits'):
    """Exposure names of the rate files in a workspace."""
    return sorted(os.path.basename(p)[:-len('_rate.fits')] for p in glob.glob(os.path.join(ws, pattern)))


def stuck_keys(obs):
    """{root: stuck-closed physical shutters} of the observation."""
    from campfire_pipeline.nirspec import profile_contam as CT
    return CT.stuck_keys_by_root(obs)


def trace_map(ws, exposure, stuck):
    """The exposure's trace map, read from its rate file with the stuck-closed shutters
    of its MSA configuration left out."""
    from campfire_pipeline.nirspec import trace_overlap as TO
    return TO.trace_map_from_rate(rate_file(ws, exposure), stuck.get(exposure[:19], set()))


def style():
    """One look for every figure: white background, readable type, a frame on all four
    sides with inward ticks on every side, minor ticks on."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        'figure.facecolor': 'white', 'axes.facecolor': 'white', 'savefig.facecolor': 'white',
        'font.size': 11, 'axes.titlesize': 12, 'axes.labelsize': 11, 'legend.fontsize': 10,
        'xtick.labelsize': 9.5, 'ytick.labelsize': 9.5,
        'axes.edgecolor': '#444444', 'axes.linewidth': 0.9, 'axes.labelcolor': INK, 'text.color': INK,
        'xtick.color': '#333333', 'ytick.color': '#333333',
        'axes.spines.top': True, 'axes.spines.right': True,
        'xtick.top': True, 'xtick.bottom': True, 'ytick.left': True, 'ytick.right': True,
        'xtick.direction': 'in', 'ytick.direction': 'in',
        'xtick.minor.visible': True, 'ytick.minor.visible': True,
        'xtick.major.size': 4.5, 'ytick.major.size': 4.5, 'xtick.minor.size': 2.5, 'ytick.minor.size': 2.5,
        'axes.titlelocation': 'left', 'axes.titleweight': 'bold',
        'legend.frameon': False, 'lines.linewidth': 1.6,
        'image.interpolation': 'nearest', 'image.origin': 'lower',
    })
    return plt


def smooth_edges(iy, ix, deg=2, min_cols=20):
    """Smooth lower and upper edges of a trace footprint given its pixels (rows ``iy``,
    columns ``ix``): per column the lowest and highest lit row, each fitted with a
    polynomial in column. Returns (columns, lower, upper) on a fine grid over the columns
    the footprint covers, in pixel coordinates (edges at the pixel borders), or None."""
    import numpy as np
    iy, ix = np.asarray(iy), np.asarray(ix)
    cols = np.unique(ix)
    if cols.size < min_cols:
        return None
    lo = np.array([iy[ix == c].min() for c in cols], float) - 0.5
    hi = np.array([iy[ix == c].max() for c in cols], float) + 0.5
    x = np.linspace(cols.min() - 0.5, cols.max() + 0.5, 400)
    t = (cols - cols.mean()) / max(np.ptp(cols), 1)
    tx = (x - cols.mean()) / max(np.ptp(cols), 1)
    d = min(deg, cols.size - 1)
    return x, np.polyval(np.polyfit(t, lo, d), tx), np.polyval(np.polyfit(t, hi, d), tx)


def draw_trace(ax, iy, ix, color, fill_alpha=0.0, lw=1.5, label=None):
    """Draw a trace footprint as two smooth edge lines (optionally filled between)."""
    e = smooth_edges(iy, ix)
    if e is None:
        return None
    x, lo, hi = e
    if fill_alpha > 0:
        ax.fill_between(x, lo, hi, color=color, alpha=fill_alpha, lw=0)
    ax.plot(x, lo, color=color, lw=lw, label=label)
    ax.plot(x, hi, color=color, lw=lw)
    return e


def profile_step(ax, values, positions, color, label=None, vertical=False, fill_alpha=0.25, lw=1.5, ls='-'):
    """A profile drawn as steps, filled to zero. ``positions`` are the bin centers (pixel
    rows); with ``vertical`` the profile runs up the y axis (value on x)."""
    import numpy as np
    v = np.asarray(values, float)
    pos = np.asarray(positions, float)
    edges = np.r_[pos[0] - 0.5, 0.5 * (pos[1:] + pos[:-1]), pos[-1] + 0.5]
    orient = 'horizontal' if vertical else 'vertical'
    if fill_alpha > 0:
        ax.stairs(np.nan_to_num(v), edges, baseline=0, fill=True, color=color, alpha=fill_alpha,
                  orientation=orient, lw=0)
    ax.stairs(v, edges, baseline=None, color=color, lw=lw, ls=ls, orientation=orient, label=label)


def save(fig, args, default_name):
    """Write the figure to its output folder and print the path."""
    out_dir = OUT_DIRS[args.kind]
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, args.out or default_name)
    fig.savefig(path, dpi=150, bbox_inches='tight')
    print(path)
    return path


def asinh_limits(img, lo=1.0, hi=99.5):
    """Display limits for a data image: percentiles of its finite pixels."""
    import numpy as np
    v = np.asarray(img, float)
    v = v[np.isfinite(v)]
    return (float(np.percentile(v, lo)), float(np.percentile(v, hi))) if v.size else (0.0, 1.0)


def asinh_norm(img, lo=1.0, hi=99.5, soft=0.05):
    """An asinh stretch, so faint background and bright traces both stay visible."""
    from matplotlib.colors import AsinhNorm
    vmin, vmax = asinh_limits(img, lo, hi)
    return AsinhNorm(linear_width=max(soft * (vmax - vmin), 1e-12), vmin=vmin, vmax=vmax)


def label_panel(ax, text):
    """A short label in the top-left corner of an image panel."""
    ax.text(0.01, 0.97, text, transform=ax.transAxes, va='top', ha='left', fontsize=10.5, color=INK,
            bbox=dict(boxstyle='round,pad=0.25', fc='white', ec='none', alpha=0.85))
