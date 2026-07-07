"""
Diagnostic: rho-sensitivity of the GP estimator + amp-to-amp offset scale.

Runs the GP per-amp-row estimator directly on a pre-striping snapshot frame
(rate stage; flat ~ 1, so a faithful stand-in for the estimator input) for a
range of length scales rho, and reports the residual amp-row-median scatter
split into clean vs source-covered rows. Also reports the amp-to-amp DC
offset spread — the quantity the median's full-row fallback gets wrong, and
the GP's whole reason to exist. If that spread is small (LW), the fallback
is nearly right and the GP has little to gain.
"""
import glob
import os
import sys

import numpy as np
from astropy.stats import mad_std

from campfire_pipeline.nircam.constants import NIR_AMPS
from campfire_pipeline.nircam.gp_striping import gp_amprow_offsets
from campfire_pipeline.nircam.steps.striping import (
    _build_srcmask, _median_amprow_offsets,
)

HERE = os.path.dirname(os.path.abspath(__file__))


def _residual_split(data, horizontal, blank, seg, clean_frac=0.2, src_frac=0.4):
    clean, source = [], []
    for amp in ('A', 'B', 'C', 'D'):
        _, _, c0, c1 = NIR_AMPS[amp]['data']
        res = (data - horizontal)[:, c0:c1].copy()
        res[~blank[:, c0:c1]] = np.nan
        with np.errstate(invalid='ignore'):
            rm = np.nanmedian(res, axis=1)
        rm = rm - np.nanmedian(rm)
        sf = seg[:, c0:c1].mean(axis=1)
        clean.append(rm[np.isfinite(rm) & (sf < clean_frac)])
        source.append(rm[np.isfinite(rm) & (sf > src_frac)])
    cv, sv = np.concatenate(clean), np.concatenate(source)
    return float(mad_std(cv)), float(mad_std(sv)), cv.size, sv.size


def main():
    path = (sys.argv[1] if len(sys.argv) > 1 else
            sorted(glob.glob(os.path.join(HERE, 'prestriping', '*00004_nrcalong.fits')))[0])
    from jwst.datamodels import ImageModel, dqflags
    m = ImageModel(path, memmap=False)
    data = m.data.astype(np.float64)
    seg = _build_srcmask(m).astype(bool)
    mask = (np.bitwise_and(m.dq, dqflags.pixel['DO_NOT_USE']) != 0) | seg
    m.close()
    data = data - np.median(data[np.isfinite(data) & ~mask])  # pedestal

    blank = np.isfinite(data) & ~mask
    print(f'frame: {os.path.basename(path)}')

    # Amp-to-amp DC offset spread (clean rows only).
    dcs = []
    for amp in ('A', 'B', 'C', 'D'):
        _, _, c0, c1 = NIR_AMPS[amp]['data']
        sub = data[:, c0:c1][blank[:, c0:c1]]
        dcs.append(np.median(sub))
    dcs = np.array(dcs)
    sky = mad_std(data[blank])
    ptp = float(np.ptp(dcs))
    print(f'amp DC offsets (A,B,C,D) = {np.round(dcs, 5)}  '
          f'peak-to-peak = {ptp:.5e}  (per-pixel sky scatter = {sky:.5e})')
    print(f'amp-to-amp offset / sky-noise = {ptp / sky:.2f}')

    # Median estimator reference.
    h_med, _ = _median_amprow_offsets(data, mask, 3, 0.1, 0.20)
    cm, sm, nc, ns = _residual_split(data, h_med, blank, seg)
    print(f'\n{"estimator":>16} {"clean":>11} {"source":>11}')
    print(f'{"median":>16} {cm:11.4e} {sm:11.4e}   (n_clean={nc}, n_source={ns})')
    for rho in (5.0, 10.0, 20.0, 40.0, 62.3, 120.0):
        h_gp, _ = gp_amprow_offsets(data, mask, kernel_sigma=0.0636, rho=rho)
        cg, sg, _, _ = _residual_split(data, h_gp, blank, seg)
        print(f'{"gp rho=%.0f" % rho:>16} {cg:11.4e} {sg:11.4e}')


if __name__ == '__main__':
    main()
