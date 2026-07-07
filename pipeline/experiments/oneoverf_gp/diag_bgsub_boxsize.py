"""
Diagnostic: subtract_background route in the striping step (GP estimator).

For a cluster exposure (real ICL present), sweep the fit_sky background box
size and show, per box:
  (a) before        — pedestal-subtracted cal-stage input (ICL + 1/f + sources)
  (b) source mask   — SRCMASK overlaid (is the diffuse ICL masked?)
  (c) bg model      — the 2D background fit_sky removes from the *fit* copy
  (d) after         — GP-corrected output (data - horizontal - vertical)

The bg-sub is fit-only: the model (c) is subtracted from the working copy that
feeds the per-amp-row/per-column 1/f medians, NOT from the output (d). So (c)
shows how much large-scale structure each box pulls out of the 1/f fit; (d)
shows the resulting correction quality (amp steps appear if too much ICL is
left in the fit). The annotation reports the residual amp-boundary step in (d).
"""
import os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from astropy.io import fits

from campfire_pipeline.config import load_config, get_nircam_step_config
from campfire_pipeline.nircam.field import Field
from campfire_pipeline.nircam.constants import NIR_AMPS
from campfire_pipeline.nircam.skyfit import fit_sky, fit_sky_tot
from campfire_pipeline.nircam.steps.striping import (
    _build_srcmask, _resolve_gp_params, fit_residual_striping,
)

HERE = os.path.dirname(os.path.abspath(__file__))
# (box_size, filter_size) configs to sweep. filter_size must be odd.
CONFIGS = [(16, 1), (16, 3), (16, 5), (16, 7)]
ROOT = 'jw06882025001_04101_00002_nrcalong'


def amp_boundary_step(img, mask):
    bl = mask
    cp = np.array([np.nanmedian(np.where(bl[:, c], img[:, c], np.nan))
                   if bl[:, c].sum() > 30 else np.nan for c in range(img.shape[1])])
    return max(abs(np.nanmedian(cp[b - 40:b]) - np.nanmedian(cp[b:b + 40]))
               for b in (512, 1024, 1536))


def main():
    from jwst.datamodels import ImageModel, dqflags
    cfg = load_config()
    field = Field.load('rj0911_gp')
    gp_params = _resolve_gp_params(
        get_nircam_step_config('striping', cfg, field).get('gp', {}),
        ImageModel(os.path.join(HERE, 'prestriping', ROOT + '.fits')))

    m = ImageModel(os.path.join(HERE, 'prestriping', ROOT + '.fits'))
    seg = _build_srcmask(m).astype(bool)
    dq = m.dq
    data = m.data.astype(np.float64, copy=True)
    m.close()
    mask = (np.bitwise_and(dq, dqflags.pixel['DO_NOT_USE']) != 0) | seg
    blank = ~mask & np.isfinite(data)
    ped = float(fit_sky_tot(data[blank].flatten()))
    base = data - ped  # pedestal-subtracted input

    # blank for amp-step metric (exclude ref border)
    bl = blank.copy(); bl[:4] = bl[-4:] = False; bl[:, :4] = bl[:, -4:] = False

    vmin, vmax = -0.02, 0.04   # stretch that reveals the diffuse ICL
    # High-res for inspection: ~7in/panel * 200 dpi ~ 1400 px/panel.
    fig, axes = plt.subplots(len(CONFIGS), 4, figsize=(28, 7 * len(CONFIGS)))
    col_titles = ['(a) before (ped-sub)', '(b) SRCMASK overlay',
                  '(c) 2D bg model (removed from fit)', '(d) after (GP)']
    for r, (box, filt) in enumerate(CONFIGS):
        bg_in = base.copy(); bg_in[mask] = 0
        bg = fit_sky(bg_in, box_size=box, filter_size=filt)
        fitdata = base - bg
        h, v, _ = fit_residual_striping(fitdata, mask, 3,
                                        estimator='gp', gp_params=gp_params)
        after = base - h - v
        step = amp_boundary_step(after, bl)
        icl_in_bg = np.nanstd(bg[bl])  # how much large-scale the box pulled out

        panels = [base, base, bg, after]
        for c, (ax, img, title) in enumerate(zip(axes[r], panels, col_titles)):
            ax.imshow(img, origin='lower', cmap='Greys_r', vmin=vmin, vmax=vmax,
                      interpolation='nearest')
            if c == 1:  # overlay SRCMASK in red
                ov = np.zeros((*seg.shape, 4)); ov[seg] = [1, 0, 0, 0.35]
                ax.imshow(ov, origin='lower', interpolation='nearest')
            ax.set_xticks([]); ax.set_yticks([])
            if r == 0:
                ax.set_title(title, fontsize=14)
        axes[r, 0].set_ylabel(
            f'box={box} filt={filt}\n(eff~{box*filt}px)\n'
            f'ampstep={step:.2e}\nbgσ={icl_in_bg:.2e}', fontsize=13)
    fig.suptitle(f'striping bg-sub filter_size sweep at box=16 (GP) — {ROOT}',
                 fontsize=15)
    fig.tight_layout()
    out = os.path.join(HERE, 'figs', f'bgsub_filtsweep_{ROOT}.png')
    fig.savefig(out, dpi=200)
    print('saved', out)


if __name__ == '__main__':
    main()
