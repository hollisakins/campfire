"""
NIRSpec stage-specific QA plotting.
"""

import os
import warnings
import numpy as np
import matplotlib.pyplot as plt
from astropy.io import fits
from astropy.visualization import ImageNormalize, ZScaleInterval

from campfire_pipeline.common.io import log, files_to_glob


def plot_stage2a_results(files, plot_suffix='nods'):
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
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore')
                        prof = np.nanmedian(nrs1, axis=1)
                    ax[i,1].step(prof, np.arange(nrs1.shape[0])-0.5, where='pre', linewidth=1, color='k')

                if nrs2_s2d:
                    nrs2 = fits.getdata(nrs2_s2d, ext=1)
                    ax[i,2].imshow(nrs2, norm=norm, origin='lower', aspect='auto', interpolation='nearest')
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
