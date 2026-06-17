#!/usr/bin/env python
"""
Calibrate frozen SHOTerm hyperparameters for the GP 1/f striping estimator.

The GP estimator (``[nircam.striping].estimator = "gp"``) must NOT optimize
its hyperparameters per exposure — that turns a deterministic linear solve
into an optimization loop and invites overfitting. Instead we calibrate
``kernel_sigma`` (amplitude) and ``rho`` (length scale, in rows) ONCE on a
representative set of clean exposures and freeze the result in
``config_default.toml`` (split by detector channel, SW vs LW, because the
1/f amplitude and correlation length differ).

Method
------
For each exposure and each amplifier we form the per-amp-row sequence
``y_r`` (sigma-clipped background median) with sampling error ``sigma_r``
on the *clean* rows only (source-masked rows are excluded — we want the
true 1/f, not source flux). After removing the per-amp DC level, every
(exposure, amp) sequence is an independent realization of the same GP. We
maximize the **pooled** Gaussian-process marginal likelihood (sum of the
per-sequence celerite2 log-likelihoods with a shared kernel) over
``(log kernel_sigma, log rho)`` with ``Q = 1/sqrt(2)`` fixed.

Usage
-----
    conda run -n campfire python scripts/calibrate_gp_striping.py \
        --field rj0911 --filter f444w

    # or point at explicit canonical exposure files
    conda run -n campfire python scripts/calibrate_gp_striping.py FILE [FILE ...]

Prints the calibrated ``kernel_sigma`` / ``rho`` for the channel(s) found and
the config block to paste into ``[nircam.striping.gp]``.
"""

import argparse
import glob
import os
import sys

import numpy as np
from astropy.stats import mad_std
from scipy.optimize import minimize

from campfire_pipeline.nircam.constants import NIR_AMPS
from campfire_pipeline.nircam.gp_striping import (
    _MEDIAN_SE_FACTOR,
    _REF_BORDER,
    _amprow_statistics,
)

Q_FIXED = 1.0 / np.sqrt(2.0)


def _exposure_sequences(exposure_file, sigma_clip_sigma=2.0, maxiters=3,
                        max_mask_frac=0.30, detrend_scale=151):
    """Per-amp (rows, y_centered, yerr) sequences from one canonical exposure.

    Builds the source mask the same way the striping step does, subtracts a
    global pedestal, then for each amp keeps only *clean* rows (mask fraction
    below ``max_mask_frac``) and **high-pass detrends** the per-amp-row
    medians (subtract a ``detrend_scale``-row running median) before
    returning them. Detrending is essential: without it, a single SHOTerm
    fit absorbs large-scale sky/background structure into a long ``rho`` and
    the resulting GP over-smooths the genuine high-frequency row-to-row 1/f
    (verified — raw fit gives rho~60, detrended ~10, and the rho-scan on
    real data minimizes residual striping near the detrended value). The
    estimator removes large scales via its 2D-background step, so the kernel
    should describe only the residual 1/f. Returns ``(channel, seqs)`` where
    ``seqs`` is a list of ``(rows, y, yerr)`` arrays.
    """
    from scipy.ndimage import median_filter
    from jwst.datamodels import ImageModel, dqflags
    from campfire_pipeline.nircam.steps.striping import _build_srcmask

    model = ImageModel(exposure_file, memmap=False)
    channel = (getattr(model.meta.instrument, 'channel', None) or '').upper()
    channel = 'sw' if channel == 'SHORT' else 'lw'

    data = model.data.astype(np.float64)
    seg = _build_srcmask(model)
    mask = (np.bitwise_and(model.dq, dqflags.pixel['DO_NOT_USE']) != 0)
    mask[seg > 0] = True
    model.close()

    # Remove a global pedestal so the per-amp DC subtraction below is small.
    finite = np.isfinite(data) & ~mask
    data = data - np.median(data[finite])

    n_rows = data.shape[0]
    sci = slice(_REF_BORDER, n_rows - _REF_BORDER)
    rows_sci = np.arange(n_rows)[sci]

    seqs = []
    for amp in ('A', 'B', 'C', 'D'):
        _, _, c0, c1 = NIR_AMPS[amp]['data']
        y_r, s_hat, n_r = _amprow_statistics(
            data[sci, c0:c1], mask[sci, c0:c1], sigma_clip_sigma, maxiters)
        width = c1 - c0
        # Keep only rows whose source-mask fraction is below max_mask_frac,
        # i.e. enough surviving pixels to measure the true 1/f cleanly.
        clean = (n_r >= (1.0 - max_mask_frac) * width) \
            & np.isfinite(y_r) & np.isfinite(s_hat) & (s_hat > 0)
        if clean.sum() < 50:
            continue
        # High-pass detrend: interpolate y_r over masked gaps, subtract a
        # wide running median (large-scale sky/background), keep clean rows.
        # The detrended sequence is used to fit RHO (the genuine 1/f
        # correlation length). The raw (un-detrended) centered sequence
        # gives KERNEL_SIGMA (the marginal amplitude) — see main() for why
        # the two come from different quantities.
        idx = np.arange(y_r.size)
        y_full = np.interp(idx, idx[clean], y_r[clean])
        trend = median_filter(y_full, size=detrend_scale, mode='nearest')
        y_hp = (y_full - trend)[clean]
        y_raw = y_r[clean] - np.median(y_r[clean])
        yerr = _MEDIAN_SE_FACTOR * s_hat[clean] / np.sqrt(n_r[clean])
        seqs.append((rows_sci[clean].astype(np.float64), y_hp, yerr, y_raw))
    return channel, seqs


def _neg_log_like(theta, seqs):
    """Pooled negative GP log marginal likelihood at ``theta=(logσ, logρ)``."""
    import celerite2
    from celerite2 import terms

    kernel_sigma, rho = np.exp(theta)
    kernel = terms.SHOTerm(sigma=kernel_sigma, rho=rho, Q=Q_FIXED)
    total = 0.0
    for rows, y, yerr, _ in seqs:
        gp = celerite2.GaussianProcess(kernel, mean=0.0)
        try:
            gp.compute(rows, yerr=yerr)
            total += gp.log_likelihood(y)
        except Exception:
            return 1e30
    return -total


def calibrate(seqs, sigma0, rho0):
    """Maximize the pooled marginal likelihood; return (kernel_sigma, rho)."""
    theta0 = np.log([sigma0, rho0])
    res = minimize(_neg_log_like, theta0, args=(seqs,), method='Nelder-Mead',
                   options={'xatol': 1e-3, 'fatol': 1e-2, 'maxiter': 400})
    sigma, rho = np.exp(res.x)
    return float(sigma), float(rho), res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('files', nargs='*', help='canonical exposure FITS files')
    ap.add_argument('--field', help='field name (with --filter)')
    ap.add_argument('--filter', dest='filt', help='filter name (with --field)')
    ap.add_argument('--root', default=os.environ.get('CAMPFIRE_ROOT', '.'),
                    help='CAMPFIRE_ROOT (default $CAMPFIRE_ROOT)')
    args = ap.parse_args(argv)

    files = list(args.files)
    if args.field and args.filt:
        pat = os.path.join(args.root, 'products', 'nircam', args.field,
                           args.filt, '*long.fits')
        pat_sw = os.path.join(args.root, 'products', 'nircam', args.field,
                              args.filt, '*nrc[ab][1-4].fits')
        files += sorted(glob.glob(pat)) + sorted(glob.glob(pat_sw))
    if not files:
        ap.error('no exposures: pass files or --field/--filter')

    by_channel = {'sw': [], 'lw': []}
    for f in files:
        channel, seqs = _exposure_sequences(f)
        by_channel[channel].extend(seqs)
        print(f'  {os.path.basename(f)}: channel={channel}, '
              f'{len(seqs)} clean amp sequences')

    print()
    block = ['    [nircam.striping.gp]']
    for channel in ('lw', 'sw'):
        seqs = by_channel[channel]
        if not seqs:
            continue
        # rho: detrended-MLE correlation length of the high-frequency 1/f.
        sigma0 = float(np.sqrt(np.mean(np.concatenate([y for _, y, _, _ in seqs]) ** 2)))
        _, rho, res = calibrate(seqs, max(sigma0, 1e-4), 8.0)
        # kernel_sigma: the *raw* marginal amplitude (mad_std of the
        # un-detrended, per-amp-centered row medians). It must be >> the
        # per-row sampling error so the GP trusts each clean row's data
        # (posterior ~ per-row median there) and only smooths/interpolates
        # at the rho scale. The detrended-MLE amplitude is the high-pass
        # residual only — far too small, and using it makes the GP
        # over-regularize and lose to the plain per-row median (verified
        # with experiments/oneoverf_gp/scan_fitframe.py).
        raw = np.concatenate([yr for _, _, _, yr in seqs])
        kernel_sigma = float(mad_std(raw))
        print(f'channel {channel.upper()}: {len(seqs)} sequences  ->  '
              f'kernel_sigma={kernel_sigma:.5e} (raw marginal)  '
              f'rho={rho:.3f} (detrended MLE, converged={res.success})')
        block.append(f'        kernel_sigma_{channel} = {kernel_sigma:.6e}')
        block.append(f'        rho_{channel} = {rho:.4f}')

    print('\nPaste into config_default.toml:')
    print('\n'.join(block))
    return 0


if __name__ == '__main__':
    sys.exit(main())
