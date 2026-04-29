"""
NIRSpec QA plotting.

All matplotlib usage in the NIRSpec pipeline is centralised here so that
(a) stage modules never import matplotlib at module level, avoiding
interactive-backend errors on headless Linux, and (b) visual style is
consistent across every diagnostic plot.
"""

import os
import warnings
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from astropy.io import fits
from astropy.visualization import ImageNormalize, ZScaleInterval

from campfire_pipeline.common.io import log, files_to_glob


# ---------------------------------------------------------------------------
# Shared style helpers
# ---------------------------------------------------------------------------

_STYLE = dict(
    dpi=300,
    fontname='monospace',
    suptitle_fontsize=8,
    grid_alpha=0.15,
    grid_linewidth=0.5,
    tick_direction='in',
)


def _style_axes(*axes):
    """Apply standard tick/grid styling to one or more axes."""
    for ax in axes:
        ax.minorticks_on()
        ax.tick_params(direction=_STYLE['tick_direction'], which='both')
        ax.grid(True, alpha=_STYLE['grid_alpha'],
                linewidth=_STYLE['grid_linewidth'], zorder=-1000)


def _zscale_norm(data_arrays, mask_arrays=None, sigma=10):
    """Compute an ImageNormalize with ZScaleInterval after MAD sigma clipping.

    Parameters
    ----------
    data_arrays : list of ndarray
        Image arrays to combine for normalization.
    mask_arrays : list of ndarray or None, optional
        Boolean good-pixel masks (True = good).  If *None*, finite pixels
        are used.
    sigma : float
        Number of MAD-sigma for outlier rejection before ZScale.

    Returns
    -------
    ImageNormalize or None
        *None* when no valid pixels remain.
    """
    pieces = []
    if mask_arrays is None:
        mask_arrays = [None] * len(data_arrays)
    for d, m in zip(data_arrays, mask_arrays):
        if m is None:
            m = np.isfinite(d)
        pieces.append(d[m])
    if not pieces:
        return None
    data_concat = np.concatenate(pieces)
    if len(data_concat) == 0:
        return None
    med = np.median(data_concat)
    mad = np.median(np.abs(data_concat - med))
    sig = mad * 1.4826
    data_concat = data_concat[np.abs(data_concat - med) < sigma * sig]
    return ImageNormalize(data_concat, interval=ZScaleInterval())


# ---------------------------------------------------------------------------
# Redshift fitting QA plot
# ---------------------------------------------------------------------------

# Rest-frame emission line positions (microns) and labels for annotation
_ZFIT_LINES = [
    (0.121567, r'Ly$\alpha$'),
    (0.154948, 'CIV'),
    (0.190873, 'CIII]'),
    (0.372742, '[OII]'),
    (0.486133, r'H$\beta$'),
    (0.500684, '[OIII]'),
    (0.656282, r'H$\alpha$'),
    (0.658346, '[NII]'),
]


def plot_zfit_results(zfit_file, spec_file=None):
    """Generate a QA plot for redshift fitting results.

    Two panels:
      Top: 1D spectrum (f_nu) with best-fit model overlay and emission line markers
      Bottom: chi-squared vs redshift with best-fit marked

    Parameters
    ----------
    zfit_file : str
        Path to the *_zfit.fits file
    spec_file : str, optional
        Path to the *_spec.fits file (for observed data). If None, inferred
        from zfit_file by replacing '_zfit' with '_spec'.
    """
    if spec_file is None:
        spec_file = zfit_file.replace('_zfit.fits', '_spec.fits')

    # Load zfit results
    hdul = fits.open(zfit_file)
    zbest = hdul[0].header['ZBEST']
    chi2_min = hdul[0].header.get('CHI2MIN', 0)
    confidence = hdul[0].header.get('ZCONF', 0)
    model_wav = hdul['MODEL'].data['wav']
    model_fnu = hdul['MODEL'].data['fnu']
    zgrid = hdul['CHI2'].data['z']
    chi2 = hdul['CHI2'].data['chi2']
    hdul.close()

    # Load observed spectrum
    from astropy import table
    tab = table.Table.read(spec_file, hdu=1)
    wav = np.asarray(tab['wave'].value, dtype='float64')
    fnu = np.asarray(tab['fnu'].value, dtype='float64')
    fnu_err = np.asarray(tab['fnu_err'].value, dtype='float64')
    valid = np.isfinite(fnu) & np.isfinite(fnu_err) & (fnu_err > 0)

    base_name = os.path.basename(zfit_file).replace('_zfit.fits', '')

    fig = plt.figure(figsize=(8, 5), constrained_layout=True, dpi=200)
    gs = gridspec.GridSpec(2, 1, height_ratios=[3, 1.5], figure=fig)
    ax_spec = fig.add_subplot(gs[0])
    ax_chi2 = fig.add_subplot(gs[1])

    # --- Top panel: spectrum + model ---
    ax_spec.step(wav[valid], fnu[valid], where='mid', color='0.4',
                 linewidth=0.7, label='Data', zorder=2)
    ax_spec.fill_between(wav[valid], (fnu - fnu_err)[valid], (fnu + fnu_err)[valid],
                         alpha=0.15, color='0.4', edgecolor='none', step='mid', zorder=1)
    ax_spec.step(model_wav, model_fnu, where='mid', color='C3',
                 linewidth=1.0, label='Model', zorder=3)

    # Emission line markers
    ymax_spec = np.nanpercentile(fnu[valid], 97) * 1.3
    for lam_rest, label in _ZFIT_LINES:
        lam_obs = lam_rest * (1 + zbest)
        if wav[valid].min() < lam_obs < wav[valid].max():
            ax_spec.axvline(lam_obs, color='C0', alpha=0.3, linewidth=0.7, zorder=0)
            ax_spec.text(lam_obs, ymax_spec * 0.95, label, fontsize=5,
                         ha='center', va='top', color='C0', alpha=0.7, rotation=90)

    ax_spec.set_xlim(wav[valid].min(), wav[valid].max())
    ax_spec.set_ylim(-0.1 * ymax_spec, ymax_spec)
    ax_spec.set_ylabel(r'$f_{\nu}$ [$\mu$Jy]')
    ax_spec.legend(loc='upper right', fontsize=7, framealpha=0.8)
    ax_spec.grid(True, alpha=0.15, linewidth=0.5, zorder=-1000)
    ax_spec.minorticks_on()
    ax_spec.tick_params(direction='in', which='both', labelbottom=False)

    # --- Bottom panel: chi2 vs z ---
    ax_chi2.plot(zgrid, chi2, color='k', linewidth=0.7)
    ax_chi2.axvline(zbest, color='C3', linewidth=1.0, alpha=0.8, linestyle='--')

    # Mark zbest
    ax_chi2.annotate(f'z = {zbest:.4f}', xy=(zbest, chi2_min),
                     xytext=(0.97, 0.92), textcoords='axes fraction',
                     fontsize=7, ha='right', va='top', color='C3',
                     arrowprops=dict(arrowstyle='->', color='C3', lw=0.8))

    # Reasonable y-limits: clip extreme chi2 outliers
    chi2_98 = np.nanpercentile(chi2, 98)
    ax_chi2.set_ylim(chi2_min * 0.95, min(chi2_98, chi2_min * 3))
    ax_chi2.set_xlim(zgrid.min(), zgrid.max())
    ax_chi2.set_xlabel('Redshift')
    ax_chi2.set_ylabel(r'$\chi^2$')
    ax_chi2.grid(True, alpha=0.15, linewidth=0.5, zorder=-1000)
    ax_chi2.minorticks_on()
    ax_chi2.tick_params(direction='in', which='both')

    fig.suptitle(f'{base_name}   z={zbest:.4f}  conf={confidence:.1f}%',
                 fontname='monospace', fontsize=8)

    plot_file = zfit_file.replace('_zfit.fits', '_zfit.pdf')
    plt.savefig(plot_file)
    plt.close()
    return plot_file


def _annotate_stuck_shutters(ax, n_rows, shutsta, stuck_list, stkshtrs='N/A'):
    """Draw shutter boundary lines and ordinal labels on an s2d image axis.

    Handles both pre-reprocessing (stuck shutters still in SHUTSTA as '0')
    and post-reprocessing (stuck shutters removed from metafile) geometry.

    Parameters
    ----------
    ax : matplotlib Axes
        The s2d image axis to annotate.
    n_rows : int
        Number of spatial pixels in the s2d image.
    shutsta : str
        SHUTSTA header string from the s2d file.
    stuck_list : list of int
        1-indexed shutter ordinals flagged as stuck (from TOML).
    stkshtrs : str
        STKSHTRS header from the s2d PRIMARY extension.  ``'N/A'``
        means no stuck shutters were removed (pre-reprocessing);
        any other value means the file was reprocessed with stuck
        shutters removed from the metafile.
    """
    from campfire_pipeline.nirspec.stuck_shutters import _compute_shutter_regions

    n_current = len(shutsta)
    if n_current < 1:
        return

    # Use the STKSHTRS header to distinguish pre- vs post-reprocessing.
    # The old heuristic ('0' in shutsta) fails when the source is nodded
    # onto the stuck shutter, making it appear as 'x' instead of '0'.
    reprocessed = (stkshtrs != 'N/A')

    if not reprocessed:
        # Pre-reprocessing: all original shutters still in s2d
        n_effective = n_current
        # Region 0 (bottom) = shutter N, region N-1 (top) = shutter 1
        ordinals = [n_current - k for k in range(n_current)]
    else:
        # Post-reprocessing: stuck shutters removed from metafile
        n_original = n_current + len(stuck_list)
        remaining = sorted(set(range(1, n_original + 1)) - set(stuck_list))
        if not remaining:
            return
        min_remain = min(remaining)
        max_remain = max(remaining)
        # s2d spans from min_remain to max_remain (interior stuck visible as dark bands)
        n_effective = max_remain - min_remain + 1
        ordinals = [max_remain - k for k in range(n_effective)]

    regions = _compute_shutter_regions(n_effective, n_rows)

    # Boundary lines between adjacent shutter regions
    for k in range(1, len(regions)):
        boundary = regions[k][0] - 0.5
        ax.axhline(boundary, color='gray', linestyle='--', linewidth=0.4, alpha=0.6)

    # Ordinal labels at shutter midpoints
    for k, (row_start, row_end) in enumerate(regions):
        ordinal = ordinals[k]
        y_mid = (row_start + row_end) / 2 - 0.5

        if ordinal in stuck_list:
            color = 'red'
            label = f'{ordinal}*'
            fontweight = 'bold'
        else:
            color = '0.5'
            label = str(ordinal)
            fontweight = 'normal'

        ax.text(0.98, y_mid, label,
                transform=ax.get_yaxis_transform(),
                ha='right', va='center', fontsize=5, color=color,
                fontweight=fontweight)


def plot_stage2a_results(files, plot_suffix='nods', stuck_shutters=None):
    """
    Plot s2d cutouts for visual inspection of a single source.
    Groups by root, combining multiple exp_groups (subpixel dither groups)
    into one plot with labeled rows.
    """

    assert len(np.unique(files['source_id']))==1, "Can't plot multiple sources at the same time!"
    source_id = files['source_id'][0]
    workspace_dir = os.path.dirname(files['path'][0])

    for root in np.unique(files['root']):
        root_files = files[files['root'] == root]
        detectors = sorted(np.unique(root_files['detector']))
        has_both = 'nrs1' in detectors and 'nrs2' in detectors
        exp_groups = sorted(np.unique(root_files['exp_group']))
        multi_eg = len(exp_groups) > 1

        # Build ordered list of (label, nrs1_s2d, nrs2_s2d) rows,
        # sorted by exp_group then nod
        plot_rows = []
        for eg_idx, eg in enumerate(exp_groups):
            eg_files = root_files[root_files['exp_group'] == eg]
            for nod in sorted(np.unique(eg_files['nod'])):
                nod_files = eg_files[eg_files['nod'] == nod]
                nrs1_s2d = nrs2_s2d = None
                for f in nod_files:
                    s2d = f['path'].replace('_cal', '_s2d')
                    if os.path.exists(s2d):
                        if f['detector'] == 'nrs1':
                            nrs1_s2d = s2d
                        else:
                            nrs2_s2d = s2d
                label = f"d{eg_idx+1}:{nod}" if multi_eg else nod
                plot_rows.append((label, nrs1_s2d, nrs2_s2d))

        Nnods = len(plot_rows)
        if Nnods == 0:
            continue

        # Look up stuck shutters for this root/source
        stuck_list = []
        shutsta = ''
        stkshtrs = 'N/A'
        if stuck_shutters:
            stuck_list = stuck_shutters.get(root, {}).get(int(source_id), [])
        if stuck_list:
            for _, n1, n2 in plot_rows:
                s2d = n1 or n2
                if s2d:
                    shutsta = fits.getheader(s2d, ext=1).get('SHUTSTA', '')
                    stkshtrs = fits.getheader(s2d, ext=0).get('STKSHTRS', 'N/A')
                    break

        # Shared ZScale normalization across all nods,
        # masking DQ>0 pixels and >10-sigma outliers
        data_all = []
        for _, n1, n2 in plot_rows:
            for s2d_file in [n1, n2]:
                if s2d_file:
                    d = fits.getdata(s2d_file, ext=1)
                    try:
                        dq = fits.getdata(s2d_file, extname='DQ')
                        good = np.isfinite(d) & (dq == 0)
                    except KeyError:
                        good = np.isfinite(d)
                    data_all.append(d[good])
        if not data_all:
            continue
        data_concat = np.concatenate(data_all)
        med = np.median(data_concat)
        mad = np.median(np.abs(data_concat - med))
        sigma = mad * 1.4826  # MAD -> Gaussian sigma
        data_concat = data_concat[np.abs(data_concat - med) < 10 * sigma]
        norm = ImageNormalize(data_concat, interval=ZScaleInterval())

        title = files_to_glob(list(root_files['name']))
        log(f'Plotting {root}_{source_id}')

        if has_both:
            # Determine width ratios from first available shapes
            nrs1_shape = nrs2_shape = None
            for _, n1, n2 in plot_rows:
                if n1 and nrs1_shape is None:
                    nrs1_shape = np.shape(fits.getdata(n1, ext=1))
                if n2 and nrs2_shape is None:
                    nrs2_shape = np.shape(fits.getdata(n2, ext=1))
                if nrs1_shape and nrs2_shape:
                    break

            nrs1_ratio = nrs1_shape[1]/(nrs1_shape[1]+nrs2_shape[1]) * 6
            nrs2_ratio = 6 - nrs1_ratio

            fig, ax = plt.subplots(Nnods, 4,
                figsize=(7*1.5, Nnods*1.5),
                width_ratios=[nrs1_ratio, 0.5, nrs2_ratio, 0.5],
                constrained_layout=True)
            if Nnods == 1:
                ax = ax.reshape(1, -1)

            fig.suptitle(title, fontname='monospace', fontsize=8)

            for i, (label, nrs1_s2d, nrs2_s2d) in enumerate(plot_rows):
                if nrs1_s2d:
                    nrs1 = fits.getdata(nrs1_s2d, ext=1)
                    ax[i,0].imshow(nrs1, norm=norm, origin='lower', aspect='auto', interpolation='nearest')
                    if stuck_list and shutsta:
                        _annotate_stuck_shutters(ax[i,0], nrs1.shape[0], shutsta, stuck_list, stkshtrs)
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore')
                        prof = np.nanmedian(nrs1, axis=1)
                    ax[i,1].step(prof, np.arange(nrs1.shape[0])-0.5, where='pre', linewidth=1, color='k')

                if nrs2_s2d:
                    nrs2 = fits.getdata(nrs2_s2d, ext=1)
                    ax[i,2].imshow(nrs2, norm=norm, origin='lower', aspect='auto', interpolation='nearest')
                    if stuck_list and shutsta:
                        _annotate_stuck_shutters(ax[i,2], nrs2.shape[0], shutsta, stuck_list, stkshtrs)
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore')
                        prof = np.nanmedian(nrs2, axis=1)
                    ax[i,3].step(prof, np.arange(nrs2.shape[0])-0.5, where='pre', linewidth=1, color='k')

                ax[i,1].tick_params(labelleft=False)
                ax[i,2].tick_params(labelleft=False)
                ax[i,3].tick_params(labelleft=False)
                ax[i,1].set_ylim(*ax[i,0].get_ylim())
                ax[i,2].set_ylim(*ax[i,0].get_ylim())
                ax[i,3].set_ylim(*ax[i,0].get_ylim())
                if i==0:
                    ax[i,0].set_title('nrs1', fontname='monospace')
                    ax[i,2].set_title('nrs2', fontname='monospace')
                ax[i,3].set_ylabel(label, fontname='monospace')
                ax[i,3].yaxis.set_label_position("right")

            for i in range(Nnods-1):
                for j in range(4):
                    ax[i,j].tick_params(labelbottom=False)

            for col in [1, 3]:
                xmins = [ax[i,col].get_xlim()[0] for i in range(Nnods)]
                xmaxs = [ax[i,col].get_xlim()[1] for i in range(Nnods)]
                for i in range(Nnods):
                    ax[i,col].set_xlim(min(xmins), max(xmaxs))

            plt.savefig(os.path.join(workspace_dir, f'{root}_{source_id}_{plot_suffix}.pdf'), dpi=300)
            plt.close()

        else:
            det = detectors[0]

            fig, ax = plt.subplots(Nnods, 2,
                figsize=(7*1.5, Nnods*1.5),
                width_ratios=[6, 1],
                constrained_layout=True)
            if Nnods == 1:
                ax = ax.reshape(1, -1)

            fig.suptitle(title, fontname='monospace', fontsize=8)

            for i, (label, nrs1_s2d, nrs2_s2d) in enumerate(plot_rows):
                s2d_file = nrs1_s2d if det == 'nrs1' else nrs2_s2d
                if s2d_file:
                    data = fits.getdata(s2d_file, ext=1)
                    ax[i,0].imshow(data, norm=norm, origin='lower', aspect='auto', interpolation='nearest')
                    if stuck_list and shutsta:
                        _annotate_stuck_shutters(ax[i,0], data.shape[0], shutsta, stuck_list, stkshtrs)
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore')
                        prof = np.nanmedian(data, axis=1)
                    ax[i,1].step(prof, np.arange(data.shape[0])-0.5, where='pre', linewidth=1, color='k')

                ax[i,1].tick_params(labelleft=False)
                ax[i,1].set_ylim(*ax[i,0].get_ylim())
                ax[i,1].set_ylabel(label, fontname='monospace')
                ax[i,1].yaxis.set_label_position("right")

            for i in range(Nnods-1):
                ax[i,0].tick_params(labelbottom=False)
                ax[i,1].tick_params(labelbottom=False)

            xmins = [ax[i,1].get_xlim()[0] for i in range(Nnods)]
            xmaxs = [ax[i,1].get_xlim()[1] for i in range(Nnods)]
            for i in range(Nnods):
                ax[i,1].set_xlim(min(xmins), max(xmaxs))

            plt.savefig(os.path.join(workspace_dir, f'{root}_{source_id}_{plot_suffix}.pdf'), dpi=300)
            plt.close()


# ---------------------------------------------------------------------------
# Background subtraction diagnostic (from stage1)
# ---------------------------------------------------------------------------

def plot_bkg_subtraction(rate_file, image_data, mask, *,
                         pictureframe_model=None, pedestal_model=None,
                         bkg2d_model=None, col_model=None, row_model=None):
    """Multi-column diagnostic showing background subtraction stages.

    Top row: each background component.  Bottom row: cumulative residual
    after subtracting each component in sequence.

    Parameters
    ----------
    rate_file : str
        Path to the rate file (used to derive the output PDF path).
    image_data : ndarray
        Raw rate image (2D).
    mask : ndarray
        Boolean slit mask (True = valid pixel).
    pictureframe_model, pedestal_model, bkg2d_model : ndarray or None
        Optional background component arrays.
    col_model, row_model : ndarray or None
        Column and row 1/f noise models.
    """
    norm = ImageNormalize(image_data[mask], interval=ZScaleInterval())

    columns = []

    # Column 0: raw + mask
    columns.append({
        'top_title': 'Raw rate file',
        'top_data': image_data,
        'top_norm': norm,
        'bottom_title': 'Slit mask (white=valid)',
        'bottom_data': mask,
        'bottom_norm': None,
        'bottom_cmap': 'gray',
        'bottom_vmin': 0,
        'bottom_vmax': 1,
    })

    bkg_cumulative = np.zeros_like(image_data)

    if pictureframe_model is not None:
        bkg_cumulative = bkg_cumulative + pictureframe_model
        columns.append({
            'top_title': 'Picture frame',
            'top_data': pictureframe_model,
            'top_norm': norm,
            'bottom_title': 'Raw - picture frame',
            'bottom_data': image_data - bkg_cumulative,
            'bottom_norm': norm,
        })

    if pedestal_model is not None:
        bkg_cumulative = bkg_cumulative + pedestal_model
        columns.append({
            'top_title': 'Pedestal quarters',
            'top_data': pedestal_model,
            'top_norm': norm,
            'bottom_title': 'Raw - picture frame - pedestal',
            'bottom_data': image_data - bkg_cumulative,
            'bottom_norm': norm,
        })

    if bkg2d_model is not None:
        bkg_cumulative = bkg_cumulative + bkg2d_model
        bottom_title = 'Raw'
        if pictureframe_model is not None:
            bottom_title += ' - picture frame'
        if pedestal_model is not None:
            bottom_title += ' - pedestal'
        bottom_title += ' - 2D'
        columns.append({
            'top_title': '2D background',
            'top_data': bkg2d_model,
            'top_norm': norm,
            'bottom_title': bottom_title,
            'bottom_data': image_data - bkg_cumulative,
            'bottom_norm': norm,
        })

    if col_model is not None:
        bkg_cumulative = bkg_cumulative + col_model
        bottom_title = 'Raw'
        if pictureframe_model is not None:
            bottom_title += ' - picture frame'
        if pedestal_model is not None:
            bottom_title += ' - pedestal'
        if bkg2d_model is not None:
            bottom_title += ' - 2D'
        bottom_title += ' - col'
        columns.append({
            'top_title': 'Column 1/f',
            'top_data': np.zeros_like(image_data) + col_model,
            'top_norm': norm,
            'bottom_title': bottom_title,
            'bottom_data': image_data - bkg_cumulative,
            'bottom_norm': norm,
        })

    if row_model is not None:
        bkg_cumulative = bkg_cumulative + row_model
        bottom_title = 'Raw'
        if pictureframe_model is not None:
            bottom_title += ' - picture frame'
        if pedestal_model is not None:
            bottom_title += ' - pedestal'
        if bkg2d_model is not None:
            bottom_title += ' - 2D'
        if col_model is not None:
            bottom_title += ' - col'
        bottom_title += ' - row'
        columns.append({
            'top_title': 'Row 1/f',
            'top_data': np.zeros_like(image_data) + row_model,
            'top_norm': norm,
            'bottom_title': bottom_title,
            'bottom_data': image_data - bkg_cumulative,
            'bottom_norm': norm,
        })

    n_cols = len(columns)
    fig, ax = plt.subplots(2, n_cols, figsize=(4 * n_cols, 8),
                           sharex=True, sharey=True)
    if n_cols == 1:
        ax = ax.reshape(2, 1)

    for i, col in enumerate(columns):
        ax[0, i].imshow(col['top_data'], norm=col['top_norm'])
        ax[0, i].set_title(col['top_title'])

        if col.get('bottom_cmap') is not None:
            ax[1, i].imshow(col['bottom_data'],
                            cmap=col['bottom_cmap'],
                            vmin=col.get('bottom_vmin'),
                            vmax=col.get('bottom_vmax'))
        else:
            ax[1, i].imshow(col['bottom_data'],
                            norm=col.get('bottom_norm', norm))
        ax[1, i].set_title(col['bottom_title'])

    plt.tight_layout()
    plot_file = rate_file.replace('_rate.fits', '_bkg.pdf')
    log(f'Saving to {plot_file}')
    plt.savefig(plot_file, dpi=_STYLE['dpi'])
    plt.close()


# ---------------------------------------------------------------------------
# Extraction profile comparison (from stage3)
# ---------------------------------------------------------------------------

def plot_extraction_profiles(out_path, collapsed, profiles, x1d_start, x1d_stop, cen):
    """4-panel plot comparing extraction profiles against the collapsed spatial profile.

    Parameters
    ----------
    out_path : str
        Output PDF path.
    collapsed : ndarray
        1D collapsed spatial profile (median across wavelength).
    profiles : dict
        ``{label: profile_array}`` for each extraction method.
    x1d_start, x1d_stop : float
        Extraction aperture bounds (pixels).
    cen : float
        Extraction centre pixel.
    """
    n = len(profiles)
    fig, axes = plt.subplots(1, n, figsize=(2.5 * n, 2))
    if n == 1:
        axes = [axes]
    for ax, (label, prof) in zip(axes, profiles.items()):
        ax.stairs(collapsed / np.nanmax(collapsed),
                  np.arange(len(collapsed) + 1), color='k', zorder=1000)
        ax.set_ylim(*ax.get_ylim())
        ax.stairs(prof / np.nanmax(prof),
                  np.arange(len(collapsed) + 1), color='tab:red')
        ax.stairs(prof / np.nanmax(prof),
                  np.arange(len(collapsed) + 1), color='tab:red',
                  fill=True, alpha=0.2)
        ax.axhline(0, color='0.3', linewidth=0.5, linestyle='--')
        ax.axvline(x1d_start, linewidth=0.5, color='b', linestyle=':')
        ax.axvline(x1d_stop, linewidth=0.5, color='b', linestyle=':')
        ax.axvline(cen, linewidth=0.5, color='b', linestyle=':')
        ax.set_title(label)

    plt.savefig(out_path, dpi=_STYLE['dpi'])
    plt.close()


# ---------------------------------------------------------------------------
# Spectrum QA plot (from stage3)
# ---------------------------------------------------------------------------

def plot_spectrum_qa(out_path, wave, fnu, fnu_err, flam, flam_err,
                     sci_2d, err_2d, profile_ypos, profile_opt, cen,
                     product_name, subtitle=None):
    """3-row diagnostic: 2D S/N image, f_nu spectrum, f_lambda spectrum.

    Parameters
    ----------
    out_path : str
        Output PDF path.
    wave : ndarray
        Wavelength array (microns).
    fnu, fnu_err : ndarray
        Flux density and error in micro-Jansky.
    flam, flam_err : ndarray
        Flux density and error in erg/s/cm2/Angstrom.
    sci_2d, err_2d : ndarray
        2D science and error arrays from the s2d file.
    profile_ypos : ndarray
        Spatial pixel positions for the extraction profile.
    profile_opt : ndarray
        Optimal extraction profile.
    cen : float
        Extraction centre pixel.
    product_name : str
        Product name for the suptitle.
    subtitle : str, optional
        Extra text appended to suptitle (e.g. ``"(1D combine)"``).
    """
    from astropy.stats import sigma_clipped_stats
    from astropy.utils.exceptions import AstropyWarning

    fig = plt.figure(figsize=(8, 6), constrained_layout=True, dpi=_STYLE['dpi'])
    gs = gridspec.GridSpec(nrows=3, ncols=2, width_ratios=[9, 1],
                           height_ratios=[1, 2.5, 2.5], figure=fig)

    ax_2d = fig.add_subplot(gs[0, 0])
    ax_1d_fnu = fig.add_subplot(gs[1, 0])
    ax_1d_flam = fig.add_subplot(gs[2, 0])
    ax_prof = fig.add_subplot(gs[0, 1])

    # 2D S/N
    nsci = sci_2d / err_2d
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=AstropyWarning)
        std = sigma_clipped_stats(nsci, sigma=3)[2]
    snr_2d = nsci / std

    vmin, vmax = -3, 8
    cmap = plt.colormaps['viridis']
    cmap.set_bad('0.8')

    ax_2d.pcolormesh(wave, profile_ypos - cen, snr_2d,
                     vmin=vmin, vmax=vmax, cmap=cmap, rasterized=True)
    ax_2d.set_ylabel('$y$ [pix]')
    ax_2d.set_ylim(-10, 10)
    ax_2d.minorticks_on()
    ax_2d.tick_params(direction='in', which='both', axis='y')

    # f_nu
    ax_1d_fnu.step(wave, fnu, where='mid', color='k', linewidth=1)
    ax_1d_fnu.fill_between(wave, fnu - fnu_err, fnu + fnu_err,
                           alpha=0.15, facecolor='k', edgecolor='none', step='mid')
    ax_1d_fnu.set_ylabel(r'$f_{\nu}$ [$\mu$Jy]')

    # f_lambda
    ax_1d_flam.step(wave, flam, where='mid', color='k', linewidth=1)
    ax_1d_flam.fill_between(wave, flam - flam_err, flam + flam_err,
                            alpha=0.15, facecolor='k', edgecolor='none', step='mid')
    ax_1d_flam.set_ylabel(r'$f_{\lambda}$ [erg s$^{-1}$ cm$^{-2}$ Å$^{-1}$]')
    ax_1d_flam.set_xlabel(r'Observed Wavelength [$\mu$m]')

    _style_axes(ax_1d_fnu, ax_1d_flam)

    # Spatial profile
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        p = np.nanmedian(sci_2d, axis=1)
        p /= np.nanmax(p[profile_opt != 0])
    ax_prof.step(p, profile_ypos - cen, where='post', color='k')
    ax_prof.fill_betweenx(profile_ypos - cen, np.zeros_like(profile_ypos),
                          profile_opt / np.nanmax(profile_opt),
                          facecolor='r', alpha=0.3, edgecolor='none', step='pre')
    ax_prof.axvline(0, color='k', linewidth=1, zorder=-1000, alpha=0.2)
    ax_prof.minorticks_on()
    ax_prof.set_xlim(-0.3, 1.2)
    ax_prof.set_ylim(-10, 10)
    ax_prof.tick_params(labelbottom=False, bottom=False, labelleft=False,
                        direction='in', which='both')

    # Axis limits
    xmin, xmax = wave.min(), wave.max()
    ax_2d.set_xlim(xmin, xmax)
    ax_1d_fnu.set_xlim(xmin, xmax)
    ax_1d_flam.set_xlim(xmin, xmax)

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        ymax = np.nanpercentile(fnu + fnu_err, 97) * 1.2
        if np.isfinite(ymax):
            ax_1d_fnu.set_ylim(-0.1 * ymax, ymax)
        ymax = np.nanpercentile(flam + flam_err, 97) * 1.2
        if np.isfinite(ymax):
            ax_1d_flam.set_ylim(-0.1 * ymax, ymax)

    title = product_name + '_spec'
    if subtitle:
        title += f' {subtitle}'
    fig.suptitle(title, fontname=_STYLE['fontname'],
                 fontsize=_STYLE['suptitle_fontsize'])

    plt.savefig(out_path)
    plt.close()


# ---------------------------------------------------------------------------
# Stuck shutter diagnostic (from stuck_shutters)
# ---------------------------------------------------------------------------

def _add_shutter_overlays(ax_img, ax_prof, n_rows, n_shutters,
                          stuck_shutters_list, data, var_rnoise=None):
    """Add shutter boundary lines and stuck-shutter highlights.

    Spatial axis is inverted: region 0 (bottom) = shutter N,
    region N-1 (top) = shutter 1.
    """
    from campfire_pipeline.nirspec.stuck_shutters import _compute_shutter_regions

    regions = _compute_shutter_regions(n_shutters, n_rows)

    for k in range(1, n_shutters):
        boundary = regions[k][0] - 0.5
        ax_img.axhline(boundary, color='gray', linestyle='--',
                       linewidth=0.5, alpha=0.7)
        ax_prof.axhline(boundary, color='gray', linestyle='--',
                        linewidth=0.5, alpha=0.7)

    for s in stuck_shutters_list:
        k = n_shutters - s
        row_start, row_end = regions[k]
        y_lo = row_start - 0.5
        y_hi = row_end - 0.5
        ax_img.axhspan(y_lo, y_hi, color='red', alpha=0.15)
        ax_prof.axhspan(y_lo, y_hi, color='red', alpha=0.15)
        y_mid = (y_lo + y_hi) / 2
        ax_prof.text(0.95, y_mid, f'STUCK (s{s})',
                     transform=ax_prof.get_yaxis_transform(),
                     ha='right', va='center', fontsize=5, color='red',
                     fontweight='bold')

    if var_rnoise is not None:
        good_mask = (np.isfinite(data) & np.isfinite(var_rnoise)
                     & (var_rnoise > 0))
        for k, (row_start, row_end) in enumerate(regions):
            region_data = data[row_start:row_end, :]
            region_var = var_rnoise[row_start:row_end, :]
            region_good = good_mask[row_start:row_end, :]
            d = region_data[region_good]
            rn_sigma = np.sqrt(region_var[region_good])
            if len(d) > 0:
                low_frac = np.sum(d < 2 * rn_sigma) / len(d)
            else:
                low_frac = 1.0
            shutter_ordinal = n_shutters - k

            y_mid = (row_start + row_end) / 2 - 0.5
            ax_img.text(0.02, y_mid,
                        f's{shutter_ordinal} lf={low_frac:.2f}',
                        transform=ax_img.get_yaxis_transform(),
                        fontsize=4, color='white', va='center',
                        fontweight='bold',
                        bbox=dict(boxstyle='round,pad=0.2',
                                  facecolor='black', alpha=0.5))


def plot_stuck_shutter_diagnostics(files, source_id, root, workspace_dir,
                                   n_shutters, stuck_shutters_list, stage_config):
    """Generate a diagnostic QA plot for stuck shutter detection.

    Layout mirrors the nods.pdf plots with shutter boundaries and
    stuck shutter highlights overlaid.

    Parameters
    ----------
    files : Table
        File table for this source (from discover_files + group_files).
    source_id : int
    root : str
    workspace_dir : str
        Output directory (plots go into ``{workspace_dir}/stuck_shutters/``).
    n_shutters : int
    stuck_shutters_list : list of int
        1-indexed shutter numbers flagged as stuck.
    stage_config : dict
        For retrieving threshold values to display.
    """
    root_files = files[(files['root'] == root) & (files['source_id'] == source_id)]
    detectors = sorted(np.unique(root_files['detector']))
    has_both = 'nrs1' in detectors and 'nrs2' in detectors
    exp_groups = sorted(np.unique(root_files['exp_group']))
    multi_eg = len(exp_groups) > 1

    plot_rows = []
    for eg_idx, eg in enumerate(exp_groups):
        eg_files = root_files[root_files['exp_group'] == eg]
        for nod in sorted(np.unique(eg_files['nod'])):
            nod_files = eg_files[eg_files['nod'] == nod]
            nrs1_s2d = nrs2_s2d = None
            for f in nod_files:
                s2d = f['path'].replace('_cal.fits', '_s2d.fits')
                if os.path.exists(s2d):
                    if f['detector'] == 'nrs1':
                        nrs1_s2d = s2d
                    else:
                        nrs2_s2d = s2d
            label = f"d{eg_idx+1}:{nod}" if multi_eg else nod
            plot_rows.append((label, nrs1_s2d, nrs2_s2d))

    n_nods = len(plot_rows)
    if n_nods == 0:
        return

    # Shared ZScale normalization
    data_arrays = []
    mask_arrays = []
    for _, n1, n2 in plot_rows:
        for s2d_file in [n1, n2]:
            if s2d_file:
                d = fits.getdata(s2d_file, ext=1)
                try:
                    dq = fits.getdata(s2d_file, extname='DQ')
                    good = np.isfinite(d) & (dq == 0)
                except KeyError:
                    good = np.isfinite(d)
                data_arrays.append(d)
                mask_arrays.append(good)
    norm = _zscale_norm(data_arrays, mask_arrays)
    if norm is None:
        return

    grating = root_files['grating'][0].upper() if len(root_files) > 0 else 'PRISM'
    if grating == 'PRISM':
        low_frac_thresh = stage_config.get('stuck_shutter_low_frac_threshold', 0.5)
    else:
        low_frac_thresh = stage_config.get(
            'stuck_shutter_low_frac_threshold_grating', 0.7)
    stuck_str = ', '.join(str(s) for s in stuck_shutters_list)
    title = (f'{root} | {source_id} | {grating} | Stuck shutters: [{stuck_str}]\n'
             f'low_frac > {low_frac_thresh}')

    out_dir = os.path.join(workspace_dir, 'stuck_shutters')
    os.makedirs(out_dir, exist_ok=True)

    if has_both:
        nrs1_shape = nrs2_shape = None
        for _, n1, n2 in plot_rows:
            if n1 and nrs1_shape is None:
                nrs1_shape = np.shape(fits.getdata(n1, ext=1))
            if n2 and nrs2_shape is None:
                nrs2_shape = np.shape(fits.getdata(n2, ext=1))
            if nrs1_shape and nrs2_shape:
                break

        nrs1_ratio = nrs1_shape[1] / (nrs1_shape[1] + nrs2_shape[1]) * 6
        nrs2_ratio = 6 - nrs1_ratio

        fig, ax = plt.subplots(n_nods, 4,
            figsize=(7 * 1.5, n_nods * 1.5),
            width_ratios=[nrs1_ratio, 0.5, nrs2_ratio, 0.5],
            constrained_layout=True)
        if n_nods == 1:
            ax = ax.reshape(1, -1)

        fig.suptitle(title, fontname=_STYLE['fontname'],
                     fontsize=_STYLE['suptitle_fontsize'] - 1)

        for i, (label, nrs1_s2d, nrs2_s2d) in enumerate(plot_rows):
            if nrs1_s2d:
                nrs1 = fits.getdata(nrs1_s2d, ext=1)
                try:
                    nrs1_vrn = fits.getdata(nrs1_s2d, extname='VAR_RNOISE')
                except KeyError:
                    nrs1_vrn = None
                ax[i, 0].imshow(nrs1, norm=norm, origin='lower', aspect='auto',
                                interpolation='nearest')
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    prof = np.nanmedian(nrs1, axis=1)
                ax[i, 1].step(prof, np.arange(nrs1.shape[0]) - 0.5, where='pre',
                              linewidth=1, color='k')
                _add_shutter_overlays(ax[i, 0], ax[i, 1], nrs1.shape[0],
                                      n_shutters, stuck_shutters_list, nrs1,
                                      var_rnoise=nrs1_vrn)

            if nrs2_s2d:
                nrs2 = fits.getdata(nrs2_s2d, ext=1)
                try:
                    nrs2_vrn = fits.getdata(nrs2_s2d, extname='VAR_RNOISE')
                except KeyError:
                    nrs2_vrn = None
                ax[i, 2].imshow(nrs2, norm=norm, origin='lower', aspect='auto',
                                interpolation='nearest')
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    prof = np.nanmedian(nrs2, axis=1)
                ax[i, 3].step(prof, np.arange(nrs2.shape[0]) - 0.5, where='pre',
                              linewidth=1, color='k')
                _add_shutter_overlays(ax[i, 2], ax[i, 3], nrs2.shape[0],
                                      n_shutters, stuck_shutters_list, nrs2,
                                      var_rnoise=nrs2_vrn)

            ax[i, 1].tick_params(labelleft=False)
            ax[i, 2].tick_params(labelleft=False)
            ax[i, 3].tick_params(labelleft=False)
            ax[i, 1].set_ylim(*ax[i, 0].get_ylim())
            ax[i, 2].set_ylim(*ax[i, 0].get_ylim())
            ax[i, 3].set_ylim(*ax[i, 0].get_ylim())
            if i == 0:
                ax[i, 0].set_title('nrs1', fontname=_STYLE['fontname'])
                ax[i, 2].set_title('nrs2', fontname=_STYLE['fontname'])
            ax[i, 3].set_ylabel(label, fontname=_STYLE['fontname'])
            ax[i, 3].yaxis.set_label_position("right")

        for i in range(n_nods - 1):
            for j in range(4):
                ax[i, j].tick_params(labelbottom=False)

        for col in [1, 3]:
            xmins = [ax[i, col].get_xlim()[0] for i in range(n_nods)]
            xmaxs = [ax[i, col].get_xlim()[1] for i in range(n_nods)]
            for i in range(n_nods):
                ax[i, col].set_xlim(min(xmins), max(xmaxs))

    else:
        det = detectors[0]

        fig, ax = plt.subplots(n_nods, 2,
            figsize=(7 * 1.5, n_nods * 1.5),
            width_ratios=[6, 1],
            constrained_layout=True)
        if n_nods == 1:
            ax = ax.reshape(1, -1)

        fig.suptitle(title, fontname=_STYLE['fontname'],
                     fontsize=_STYLE['suptitle_fontsize'] - 1)

        for i, (label, nrs1_s2d, nrs2_s2d) in enumerate(plot_rows):
            s2d_file = nrs1_s2d if det == 'nrs1' else nrs2_s2d
            if s2d_file:
                data = fits.getdata(s2d_file, ext=1)
                try:
                    vrn = fits.getdata(s2d_file, extname='VAR_RNOISE')
                except KeyError:
                    vrn = None
                ax[i, 0].imshow(data, norm=norm, origin='lower', aspect='auto',
                                interpolation='nearest')
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    prof = np.nanmedian(data, axis=1)
                ax[i, 1].step(prof, np.arange(data.shape[0]) - 0.5, where='pre',
                              linewidth=1, color='k')
                _add_shutter_overlays(ax[i, 0], ax[i, 1], data.shape[0],
                                      n_shutters, stuck_shutters_list, data,
                                      var_rnoise=vrn)

            ax[i, 1].tick_params(labelleft=False)
            ax[i, 1].set_ylim(*ax[i, 0].get_ylim())
            ax[i, 1].set_ylabel(label, fontname=_STYLE['fontname'])
            ax[i, 1].yaxis.set_label_position("right")

        for i in range(n_nods - 1):
            ax[i, 0].tick_params(labelbottom=False)
            ax[i, 1].tick_params(labelbottom=False)

        xmins = [ax[i, 1].get_xlim()[0] for i in range(n_nods)]
        xmaxs = [ax[i, 1].get_xlim()[1] for i in range(n_nods)]
        for i in range(n_nods):
            ax[i, 1].set_xlim(min(xmins), max(xmaxs))

    out_path = os.path.join(out_dir, f'{root}_{source_id}_stuck_diagnostic.pdf')
    plt.savefig(out_path, dpi=_STYLE['dpi'])
    plt.close()
    log(f'Saved stuck shutter diagnostic plot: {out_path}')
