"""Finding stuck-closed shutters automatically (master background, step 1).

Before calibration, the count-rate frame of every exposure is fitted as a sum of one
background model per commanded-open shutter: the exposure's background spectrum, seen
through that shutter (its position, bar shadow and the detector rows its light reaches),
each with its own amplitude a. An open shutter comes out near a = 1, a stuck-closed one
near 0, whether or not other slitlets' light falls on the same pixels; a shutter is called
stuck when the median a over the nods and both detectors is below 0.3.

One row per example: the data around one slitlet, the model with every shutter open, the
model with each shutter at its fitted amplitude, and the brightness across the slitlet
(averaged over a stretch of columns) for all three.

    python scripts/docs/nirspec/fig_stuck_shutters.py --root <root> --obs ember_cosmos_p1 \
        --example jw07076001001_03101_00001_nrs1:q4r58c92 \
        --example jw07076001001_05101_00001_nrs1:q4r117c24
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--example', action='append', required=True,
               help="'<exposure>:q<Q>r<ROW>c<COL>', a stuck shutter to show (repeat for more rows)")
p.add_argument('--columns', type=int, default=150, help='columns averaged in the profile across the slitlet')
args = p.parse_args()
root = C.setup_root(args.root)

import os                                                             # noqa: E402
import re                                                             # noqa: E402
import warnings                                                       # noqa: E402

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402
from astropy.table import Table                                       # noqa: E402

from campfire_pipeline.nirspec import barshadow_calibration as B      # noqa: E402
from campfire_pipeline.nirspec import master_bkg as M                 # noqa: E402
from campfire_pipeline.nirspec import master_bkg_rate as R            # noqa: E402
from campfire_pipeline.nirspec import shutter_fit as SF               # noqa: E402
from campfire_pipeline.nirspec.coverage import rate_geometry          # noqa: E402

warnings.filterwarnings('ignore', category=RuntimeWarning)
plt = C.style()
ws = C.workspace(root, args.obs)
cache_dir = os.path.join(root, 'cache', 'coverage')


def models(exposure):
    """Data, the template matrix, the parameters of its columns and the fitted amplitudes."""
    rpath = C.rate_file(ws, exposure)
    h = fits.getheader(rpath)
    key = (str(h['GRATING']).strip().upper(), str(h['FILTER']).strip().upper())
    geo = rate_geometry(rpath, cache_dir, R.cutoffs_for(*key), None)
    sky, qs = R.read_rate_sky(rpath)
    _, bs_path = M._barshadow_reference(key[0], rpath)
    info, _ = SF._shutter_info(rpath)
    A, params, _ = SF.build_templates(geo, sky, qs, B.load_table(bs_path), info)
    with fits.open(rpath) as f:
        rate = np.asarray(f['SCI'].data, float)
        amp = Table(f['SHUTAMP'].data)
    a_of = {(int(r['q']), int(r['row']), int(r['col'])): float(r['a']) for r in amp}
    return rate, A.tocsc(), params, a_of, f"{key[0]}/{key[1]}"


fig = plt.figure(figsize=(14, 3.6 * len(args.example) + 0.6), constrained_layout=True)
gs = fig.add_gridspec(len(args.example), 4, width_ratios=[1, 1, 1, 0.55])
for r, ex in enumerate(args.example):
    exposure, shutter = ex.split(':')
    q, row, col = map(int, re.match(r'q(\d)r(\d+)c(\d+)', shutter).groups())
    rate, A, params, a_of, grating = models(exposure)
    NY, NX = rate.shape
    target = [i for i, pp in enumerate(params) if (pp['q'], pp['row'], pp['col']) == (q, row, col)]
    if not target:
        raise SystemExit(f'{shutter} has no background model in {exposure}')
    slit = params[target[0]]['slitlet_id']
    members = [i for i, pp in enumerate(params) if pp['kind'] == 'msa' and pp['slitlet_id'] == slit]
    pix = {i: A[:, i].nonzero()[0] for i in members}
    allp = np.concatenate(list(pix.values()))
    ys, xs = np.unravel_index(allp, (NY, NX))
    y0, y1 = max(ys.min() - 8, 0), min(ys.max() + 9, NY)
    x0, x1 = xs.min(), xs.max() + 1
    # every shutter whose light reaches this region, at a = 1 and at its fitted a
    box = np.zeros((NY, NX), bool)
    box[y0:y1, x0:x1] = True
    near = np.flatnonzero(np.asarray(A[box.ravel()].sum(axis=0)).ravel() != 0)
    a_fit = np.array([a_of.get((params[i]['q'], params[i]['row'], params[i]['col']), 1.0)
                      if params[i]['kind'] == 'msa' else 1.0 for i in near])
    open_model = np.asarray(A[:, near] @ np.ones(near.size)).reshape(NY, NX)
    fit_model = np.asarray(A[:, near] @ a_fit).reshape(NY, NX)
    sl = (slice(y0, y1), slice(x0, x1))
    ext = [x0 - 0.5, x1 - 0.5, y0 - 0.5, y1 - 0.5]
    norm = C.asinh_norm(rate[sl])
    a_t = a_of.get((q, row, col), np.nan)
    a_txt = f'{a_t:.2f}'.replace('-0.00', '0.00')
    for k, (img, title) in enumerate([(rate, 'Data (count rate)'), (open_model, 'Model: every shutter open'),
                                      (fit_model, 'Model: fitted amplitudes')]):
        ax = fig.add_subplot(gs[r, k])
        ax.imshow(img[sl], extent=ext, cmap='Greys', norm=norm, aspect='auto')
        for i in members:
            yy, xx = np.unravel_index(pix[i], (NY, NX))
            stuck = i in target
            C.draw_trace(ax, yy, xx, C.PALETTE[5] if stuck else C.PALETTE[0], lw=1.4 if stuck else 1.0)
        ax.set_xlim(ext[0], ext[1])
        ax.set_ylim(ext[2], ext[3])
        if r == 0:
            ax.set_title(title)
        if k == 0:
            ax.set_ylabel('detector row')
            C.label_panel(ax, f'{grating}: shutter q{q} r{row} c{col}, a = {a_txt}')
        else:
            ax.set_yticklabels([])
        if r == len(args.example) - 1:
            ax.set_xlabel('detector column')
    # profile across the slitlet over the stretch of columns where the stuck shutter's own
    # background model is brightest
    own = np.asarray(A[:, target[0]].todense()).reshape(NY, NX)
    colsum = np.nansum(own[sl], axis=0)
    c0 = int(np.argmax(np.convolve(colsum, np.ones(args.columns), 'valid')))
    cs = slice(x0 + c0, x0 + c0 + args.columns)
    rows = np.arange(y0, y1)
    ax = fig.add_subplot(gs[r, 3])
    ax.axvline(0, color='#999999', lw=0.8)
    C.profile_step(ax, np.nanmean(rate[y0:y1, cs], axis=1), rows, '#555555', vertical=True, label='data')
    C.profile_step(ax, np.nanmean(open_model[y0:y1, cs], axis=1), rows, C.PALETTE[0], vertical=True,
                   fill_alpha=0, ls='--', label='every shutter open')
    C.profile_step(ax, np.nanmean(fit_model[y0:y1, cs], axis=1), rows, C.PALETTE[5], vertical=True,
                   fill_alpha=0, label='fitted amplitudes')
    yy, _ = np.unravel_index(pix[target[0]], (NY, NX))
    ax.axhspan(yy.min() - 0.5, yy.max() + 0.5, color=C.PALETTE[5], alpha=0.08, lw=0)
    ax.set_ylim(ext[2], ext[3])
    ax.set_yticklabels([])
    if r == 0:
        ax.set_title('Across the slitlet')
        ax.legend(loc='upper right', fontsize=8.5)
    ax.set_xlabel('count rate')
    ax.text(0.97, 0.02, f'columns {cs.start}-{cs.stop - 1}', transform=ax.transAxes, ha='right', va='bottom',
            fontsize=8.5, color=C.MUTED)
fig.suptitle(f'{args.obs}: closed shutter outlined in orange, the other shutters of its slitlet in blue',
             x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'stuck_shutters.png')
