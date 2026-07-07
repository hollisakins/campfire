"""
Pipeline-faithful (kernel_sigma, rho) scan for the GP striping estimator.

Reproduces striping_step's fit input exactly on one pre-striping exposure
(flat-field copy -> pedestal -> 2D background), then sweeps the GP
hyperparameters in memory, measuring the residual amp-row-median scatter
(clean vs source-covered rows) the same way analyze_ab does. Avoids 10-min
full-pipeline re-runs per hyperparameter. The residual is measured on the
fit frame; the downstream flat re-division / sky pedestal are common-mode
across estimators, so the *ranking* matches the final cal-frame A/B.
"""
import glob
import os
import sys

import numpy as np
from astropy.stats import mad_std

from campfire_pipeline.config import load_config
from campfire_pipeline.nircam.constants import NIR_AMPS
from campfire_pipeline.nircam.field import Field
from campfire_pipeline.nircam.gp_striping import gp_amprow_offsets
from campfire_pipeline.nircam.skyfit import collapse_image, fit_pedestal, fit_sky
from campfire_pipeline.nircam.steps.striping import (
    _build_srcmask, _median_amprow_offsets,
)

HERE = os.path.dirname(os.path.abspath(__file__))


def build_fit_frame(path, field):
    """Replicate striping_step's fit input: flat, pedestal, 2D background."""
    from jwst.datamodels import ImageModel, dqflags
    from campfire_pipeline.nircam.steps._flat import (
        apply_flat_with_retry, resolve_flat,
    )
    model = ImageModel(path)
    seg = _build_srcmask(model)
    fit = model.copy()
    flatfile = resolve_flat(fit, field, False)
    fit = apply_flat_with_retry(fit, flatfile)
    mask = (np.bitwise_and(fit.dq, dqflags.pixel['DO_NOT_USE']) != 0) | (seg > 0)
    ped = float(fit_pedestal(fit.data[~mask].flatten()))
    fit.data -= ped
    bg_in = fit.data.copy(); bg_in[mask] = 0
    fit.data -= fit_sky(bg_in)
    data = fit.data.astype(np.float64).copy()
    model.close(); fit.close()
    return data, mask, seg.astype(bool)


def residual_split(data, horizontal, mask, seg, clean_f=0.2, src_f=0.4):
    blank = ~mask
    cv, sv = [], []
    for amp in ('A', 'B', 'C', 'D'):
        _, _, c0, c1 = NIR_AMPS[amp]['data']
        res = (data - horizontal)[:, c0:c1].copy()
        res[~blank[:, c0:c1]] = np.nan
        with np.errstate(invalid='ignore'):
            rm = np.nanmedian(res, axis=1)
        rm = rm - np.nanmedian(rm)
        sf = seg[:, c0:c1].mean(axis=1)
        cv.append(rm[np.isfinite(rm) & (sf < clean_f)])
        sv.append(rm[np.isfinite(rm) & (sf > src_f)])
    cv, sv = np.concatenate(cv), np.concatenate(sv)
    return float(mad_std(cv)), float(mad_std(sv))


def with_vertical(data, horizontal, mask):
    """Apply the (unchanged) vertical step so the residual matches pipeline."""
    v1d = collapse_image(data - horizontal, mask, 3, dimension='x')
    return horizontal + np.broadcast_to(v1d, data.shape)


def main():
    path = (sys.argv[1] if len(sys.argv) > 1 else
            sorted(glob.glob(os.path.join(HERE, 'prestriping', '*00004_nrcalong.fits')))[0])
    load_config()
    field = Field.load('rj0911_med')
    print(f'building fit frame from {os.path.basename(path)} ...')
    data, mask, seg = build_fit_frame(path, field)

    h_med, _ = _median_amprow_offsets(data, mask, 3, 0.1, 0.20)
    cm, sm = residual_split(data, with_vertical(data, h_med, mask), mask, seg)
    print(f'\n{"config":>26} {"clean":>11} {"source":>11}')
    print(f'{"median (control)":>26} {cm:11.4e} {sm:11.4e}')
    for ksig in (0.0057, 0.02, 0.0636, 0.2):
        for rho in (4.0, 6.0, 10.0, 20.0):
            h, _ = gp_amprow_offsets(data, mask, kernel_sigma=ksig, rho=rho)
            cg, sg = residual_split(data, with_vertical(data, h, mask), mask, seg)
            flag = '  <-- beats median both' if (cg < cm and sg < sm) else ''
            print(f'{"gp ks=%.4g rho=%g" % (ksig, rho):>26} '
                  f'{cg:11.4e} {sg:11.4e}{flag}')


if __name__ == '__main__':
    main()
