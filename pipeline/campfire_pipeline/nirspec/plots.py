"""
NIRSpec stage-specific QA plotting.
"""

import os
import warnings
import numpy as np
import matplotlib
matplotlib.use('pdf')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from astropy.io import fits
from astropy.visualization import ImageNormalize, ZScaleInterval

from campfire_pipeline.common.io import log, files_to_glob


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
