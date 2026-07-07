#!/usr/bin/env python
"""
Calibrate the frozen ``rho`` hyperparameter for the GP 1/f striping estimator.

``rho`` (the SHOTerm length scale, in rows) is the ONLY frozen GP
hyperparameter: it is a detector readout/clocking property — independent of
filter and of flux units — so it is calibrated once per detector channel
(SW vs LW) and frozen in ``config_default.toml``. The kernel amplitude is
NOT frozen; it self-adapts per exposure inside ``gp_amprow_offsets`` (the
marginal ``mad_std`` of the clean per-amp-row medians, a deterministic robust
statistic — not a per-exposure fit). This script reports ``rho`` as the value
to paste, and the marginal amplitude only as a sanity check (it should sit
comfortably above the per-row sampling error).

Neither value is optimized per exposure in the pipeline — that would turn a
deterministic linear solve into an optimization loop and invite overfitting.

Method
------
For each exposure and each amplifier we form the per-amp-row sequence
``y_r`` (sigma-clipped background median) with sampling error ``sigma_r``
on the *clean* rows only (source-masked rows are excluded — we want the
true 1/f, not source flux). After removing the per-amp DC level and a wide
running median (high-pass, to strip large-scale sky/ICL), every
(exposure, amp) sequence is an independent realization of the same GP. We
maximize the **pooled** Gaussian-process marginal likelihood (sum of the
per-sequence celerite2 log-likelihoods with a shared kernel) over
``(log kernel_sigma, log rho)`` with ``Q = 1/sqrt(2)`` fixed, and read
``rho`` off the result. (The MLE amplitude is the high-pass residual only and
is NOT used by the pipeline; the self-adapting amplitude is the raw marginal
``mad_std``, printed here for reference.)

Usage
-----
    conda run -n campfire python scripts/calibrate_gp_striping.py \
        --field rj0911 --filter f444w

    # or point at explicit canonical exposure files
    conda run -n campfire python scripts/calibrate_gp_striping.py FILE [FILE ...]

Prints the calibrated ``rho`` per channel and the config block to paste into
``[nircam.striping.gp]``, plus the self-adapting amplitude scale for sanity.
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
    from campfire_pipeline.nircam.bkgsub import SubtractBackground

    model = ImageModel(exposure_file, memmap=False)
    channel = (getattr(model.meta.instrument, 'channel', None) or '').upper()
    channel = 'sw' if channel == 'SHORT' else 'lw'

    data = model.data.astype(np.float64)
    # Exposure-level tiered source mask (replaces the retired _build_srcmask).
    _sb = SubtractBackground(
        ring_radius_in=40, ring_width=3, tier_kernel_size=[25, 15, 5, 2],
        tier_npixels=[15, 15, 5, 2], tier_nsigma=[3, 3, 3, 3],
        tier_dilate_size=[0, 0, 0, 3])
    seg, _ = _sb.mask_from_arrays(
        model.data.astype(np.float32), model.err, model.dq)
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
        # Sanity-check amplitudes (NOT frozen — the pipeline self-adapts the
        # amplitude per exposure to this raw marginal mad_std). Report it
        # alongside the median per-row sampling error so the operator can
        # confirm the self-adapting amplitude will sit comfortably above the
        # noise (the regime where the GP trusts clean rows and only
        # interpolates at the rho scale).
        raw = np.concatenate([yr for _, _, _, yr in seqs])
        kernel_sigma = float(mad_std(raw))
        yerr_med = float(np.nanmedian(np.concatenate(
            [ye for _, _, ye, _ in seqs])))
        print(f'channel {channel.upper()}: {len(seqs)} sequences  ->  '
              f'rho={rho:.3f} (detrended MLE, converged={res.success});  '
              f'self-adapt amplitude ~{kernel_sigma:.3e} '
              f'(>> median sigma_r {yerr_med:.3e}: '
              f'{kernel_sigma / yerr_med:.0f}x)')
        block.append(f'        rho_{channel} = {rho:.4f}')

    block.append('        kernel_sigma_factor = 1.0')
    print('\nPaste into config_default.toml (rho is the only frozen value; '
          'the amplitude self-adapts per exposure):')
    print('\n'.join(block))
    return 0


if __name__ == '__main__':
    sys.exit(main())
