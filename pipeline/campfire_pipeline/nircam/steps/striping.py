"""
striping: 1/f striping subtraction with ``SRCMASK`` extension write.

Per-exposure step. Runs **after** ``image2`` on flat-fielded, flux-calibrated
cal-stage data (JADES-style ordering). Builds a tiered source mask, fits
pedestal + (optional) 2D background + horizontal + vertical striping patterns
directly in the cal frame, then subtracts the additive striping patterns from
the SCI. Writes the source mask as a ``SRCMASK`` extension on the canonical
file (replacing the legacy ``_rate_1fmask.fits`` sidecar) so the sky-subtraction
and diag_striping steps can read it through a single canonical file.

The per-amp-row offset estimator is selectable via ``estimator`` (default
``'lowclip'`` — an asymmetric low-side clip robust to leaked source wings;
``'baseline'`` — the legacy median + asymmetry-triggered full-row fallback).

Imports the numerical helpers (``fit_sky``, ``fit_sky_tot``, ``collapse_image``,
``measure_fullimage_striping``) from ``skyfit``; those are pure, scale-free
functions. The mask-builder is re-implemented locally (the legacy
``masksources`` writes a sidecar file as a side effect, which we avoid).
"""

import os
import warnings
from datetime import datetime

import numpy as np
from astropy.io import fits
from astropy.convolution import Gaussian2DKernel, Ring2DKernel, convolve_fft
from astropy.stats import biweight_location, sigma_clipped_stats
from photutils.segmentation import (
    SegmentationImage,
    detect_sources,
    detect_threshold,
)
from scipy.ndimage import binary_dilation, median_filter

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam.constants import NIR_AMPS
from campfire_pipeline.nircam.skyfit import (
    collapse_image,
    fit_sky,
    fit_sky_tot,
    measure_fullimage_striping,
)


def _build_srcmask(model):
    """Tiered source mask from a JWST ImageModel; returns ``uint8`` array.

    Equivalent to ``stage1.masksources`` but in-memory only — the legacy
    function additionally writes ``_1fmask.fits`` as a side effect, which
    the canonical-exposure layout replaces with a SRCMASK extension on the
    canonical file.
    """
    from jwst.datamodels import dqflags

    sci = model.data
    err = model.err
    dq = model.dq

    bp = np.bitwise_and(dq, dqflags.pixel['DO_NOT_USE'])
    bpmask = np.logical_not(bp == 0)

    sci_nan = np.choose(np.isnan(sci), (sci, err))
    rmb = biweight_location(sci_nan, c=6., ignore_nan=True)
    sci_filled = np.choose(np.isnan(sci), (sci, rmb))

    ring = Ring2DKernel(40, 3)
    filtered = median_filter(sci_filled, footprint=ring.array)

    log('masksources: tier 1')
    cd = convolve_fft(sci_filled - filtered, Gaussian2DKernel(25))
    seg1 = detect_sources(cd, detect_threshold(cd, nsigma=3.0),
                          npixels=15, mask=bpmask)
    mask1 = SegmentationImage.make_source_mask(seg1)
    temp = np.zeros(sci.shape)
    temp[mask1] = 1
    sources = np.logical_not(temp == 0)
    source_wings = binary_dilation(sources, Gaussian2DKernel(3))
    temp[source_wings] = 1
    mask1 = np.logical_not(temp == 0)

    log('masksources: tier 2')
    cd = convolve_fft(sci_filled - filtered, Gaussian2DKernel(10))
    seg2 = detect_sources(cd, detect_threshold(cd, nsigma=3.0),
                          npixels=10, mask=mask1)
    mask2 = SegmentationImage.make_source_mask(seg2) | mask1

    log('masksources: tier 3')
    cd = convolve_fft(sci_filled - filtered, Gaussian2DKernel(5))
    seg3 = detect_sources(cd, detect_threshold(cd, nsigma=3.0),
                          npixels=5, mask=mask2)
    mask3 = SegmentationImage.make_source_mask(seg3) | mask2

    log('masksources: tier 4')
    cd = convolve_fft(sci_filled - filtered, Gaussian2DKernel(2))
    seg4 = detect_sources(cd, detect_threshold(cd, nsigma=3.0),
                          npixels=3, mask=mask3)
    mask4 = SegmentationImage.make_source_mask(seg4)
    finalmask = mask4 | mask3

    out = np.zeros(finalmask.shape, dtype=np.uint8)
    out[finalmask] = 1
    return out


def _amp_row_lowclip(vals, min_pixels=8):
    """Asymmetric low-side-clipped location of a 1-D amp-row sample.

    ``vals`` is a row slice with NaN at masked pixels. Rejects the high
    (source-wing) tail at 2σ harder than the low tail at 4σ, iterating on a
    robust MAD scale, so leaked source flux that biases a plain median is
    rejected while the negative noise tail (real 1/f excursions) is kept.
    Returns NaN when fewer than ``min_pixels`` finite values survive — the
    caller then falls back to the full-image row median.

    This is the cal-frame analogue validated in
    ``experiments/oneoverf_noise/oneoverf_experiments.ipynb``, where it
    recovered the per-amp-row 1/f on source-contaminated rows ~20% better
    than the legacy full-row-median fallback while matching it on clean rows.
    """
    v = vals[np.isfinite(vals)]
    if v.size < min_pixels:
        return np.nan
    m = float(np.median(v))
    for _ in range(5):
        s = 1.4826 * np.median(np.abs(v - m))
        if s == 0:
            break
        keep = (v < m + 2.0 * s) & (v > m - 4.0 * s)
        n_keep = int(keep.sum())
        if n_keep < min_pixels or n_keep == v.size:
            break
        v = v[keep]
        m = float(np.median(v))
    return m


def fit_residual_striping(
    data,
    mask,
    maxiters,
    asymmetry_threshold=0.1,
    nmask_prefilter=0.20,
    estimator='lowclip',
    min_pixels=8,
):
    """Fit per-amp per-row horizontal + per-column vertical 1/f striping.

    Pure function. Operates on a (pedestal-/background-subtracted) frame and
    returns 2D additive correction arrays to subtract from the SCI. Frame-
    agnostic: post-image2 the input is cal-stage data and the corrections are
    applied in the cal frame.

    Parameters
    ----------
    data, mask, maxiters
        Input frame, mask (True = DQ/source), sigma-clip iterations.
    estimator : {'lowclip', 'baseline'}
        ``'lowclip'`` (default): estimate each amp-row offset from its
        surviving pixels with an asymmetric low-side clip
        (``_amp_row_lowclip``), keeping the local estimate even on
        source-contaminated rows instead of discarding it; fall back to the
        full-image row median only when too few pixels survive
        (``> 0.95`` masked or ``< min_pixels`` finite). ``'baseline'``: the
        legacy per-amp 2σ-clipped median with an asymmetry-triggered
        full-row-median fallback (parameters ``asymmetry_threshold`` /
        ``nmask_prefilter`` apply to this path only).
    asymmetry_threshold, nmask_prefilter : float
        Used by ``estimator='baseline'`` only. ``asymmetry_threshold`` is the
        ``|mean − median| / std`` post-clip threshold above which a row falls
        back to the full-row median; ``nmask_prefilter`` gates that test to
        rows whose source-mask fraction exceeds it (the asymmetry statistic
        has a ~0.07 sample-noise floor on clean rows).
    min_pixels : int
        ``estimator='lowclip'`` only: minimum surviving pixels for a local
        estimate before falling back to the full-row median.

    Returns
    -------
    horizontal : (H, W) ndarray
        Per-amp, per-row striping pattern.
    vertical : (H, W) ndarray
        Per-column striping pattern (1D x-collapse broadcast to 2D).
    ampcounts : list[str]
        Per-amp diagnostic strings ``'A-N'`` ... ``'D-N'`` where ``N`` is
        the number of rows that fell back to the full-image median.
    """
    if estimator not in ('lowclip', 'baseline'):
        raise ValueError(f"unknown estimator {estimator!r}")

    full_horizontal, _ = measure_fullimage_striping(data, mask, maxiters)

    horizontal = np.zeros(data.shape)
    ampcounts = []
    for amp in ('A', 'B', 'C', 'D'):
        _, _, colstart, colstop = NIR_AMPS[amp]['data']
        ampdata = data[:, colstart:colstop]
        ampmask = mask[:, colstart:colstop]
        width = ampmask.shape[1]
        nmask = np.sum(ampmask, axis=1)
        ampcount = 0

        if estimator == 'lowclip':
            vals_all = np.where(ampmask, np.nan, ampdata)
            for i in range(ampmask.shape[0]):
                if nmask[i] / width > 0.95:
                    est = np.nan
                else:
                    est = _amp_row_lowclip(vals_all[i], min_pixels)
                if not np.isfinite(est):
                    fb = full_horizontal[i]
                    horizontal[i, colstart:colstop] = fb if np.isfinite(fb) else 0.0
                    ampcount += 1
                else:
                    horizontal[i, colstart:colstop] = est
            ampcounts.append(f'{amp}-{ampcount}')
            continue

        # estimator == 'baseline': legacy per-amp median + asymmetry fallback.
        # Replaces a separate `collapse_image(...)` call: returns the per-row
        # median in `h_amp` plus the mean/std for the asymmetry test below —
        # all from one sigma-clip pass. Fully-masked rows give all-NaN slices;
        # the asymmetry guard treats them as contaminated, so the inner
        # RuntimeWarnings are noise.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', category=RuntimeWarning,
                message='Mean of empty slice',
            )
            warnings.filterwarnings(
                'ignore', category=RuntimeWarning,
                message='All-NaN slice encountered',
            )
            warnings.filterwarnings(
                'ignore', category=RuntimeWarning,
                message='Degrees of freedom <= 0',
            )
            mean_amp, h_amp, std_amp = sigma_clipped_stats(
                ampdata, mask=ampmask, sigma=2.0,
                cenfunc=np.nanmedian, stdfunc=np.nanstd,
                axis=1, maxiters=maxiters,
            )
        with np.errstate(invalid='ignore', divide='ignore'):
            asymmetry = np.abs(mean_amp - h_amp) / std_amp
        asymmetry = np.where(np.isfinite(asymmetry), asymmetry, np.inf)

        for i in range(ampmask.shape[0]):
            nmask_frac_i = nmask[i] / width
            if nmask_frac_i > 0.95:
                horizontal[i, colstart:colstop] = full_horizontal[i]
                ampcount += 1
            elif (nmask_frac_i > nmask_prefilter
                  and asymmetry[i] > asymmetry_threshold):
                horizontal[i, colstart:colstop] = full_horizontal[i]
                ampcount += 1
            else:
                horizontal[i, colstart:colstop] = h_amp[i]
        ampcounts.append(f'{amp}-{ampcount}')

    vertical_1d = collapse_image(
        data - horizontal, mask, maxiters, dimension='x',
    )
    vertical = np.broadcast_to(vertical_1d, data.shape).copy()

    return horizontal, vertical, ampcounts


def striping_step(exposure_file, field, step_config, overwrite=False,
                  status=None):
    """Subtract 1/f striping from a single canonical exposure.

    Parameters
    ----------
    exposure_file : str
        Canonical ``<rootname>.fits`` path.
    field : Field
    step_config : dict
        ``[nircam.striping]`` (legacy ``[nircam.stage1.remove_striping]``).
    overwrite : bool
    status : StepStatus, optional
        Pre-scanned CFP_* status cache.
    """
    mask_sources = step_config.get('mask_sources', True)
    subtract_background = step_config.get('subtract_background', False)
    maxiters = step_config.get('maxiters', 3)
    use_bottleneck = step_config.get('use_bottleneck', True)
    estimator = step_config.get('estimator', 'lowclip')
    do_plot = step_config.get('plot', True)

    rootname = os.path.basename(exposure_file).removesuffix('.fits')

    if cfp.should_skip(exposure_file, 'CFP_1F', rootname,
                       'striping', status, overwrite):
        return

    log(f"Running striping on {rootname} (estimator={estimator})")

    from jwst.datamodels import ImageModel, dqflags

    model = ImageModel(exposure_file, memmap=False)
    sci_before = model.data.copy()

    if mask_sources:
        seg = _build_srcmask(model)
    else:
        seg = np.zeros(model.data.shape, dtype=np.uint8)

    # Runs after image2, so model.data is already flat-fielded and flux-
    # calibrated — fit and apply the 1/f correction directly in the cal frame
    # (no flat-fielded copy, no apply-to-rate round-trip). The fit operates on
    # a pedestal-/background-subtracted working copy; the corrections are
    # subtracted from the SCI itself.
    #
    # Only DO_NOT_USE pixels are unusable for fitting — JUMP_DET and other
    # informational bits flag pixels that have already been corrected and are
    # still fine for sky/striping estimation. (Some exposures, e.g. bright-
    # target MSATA pointings on MEDIUM8/NGROUPS=9, get JUMP_DET set on >97% of
    # pixels; treating dq>0 as bad masks the entire frame.)
    mask = np.bitwise_and(model.dq, dqflags.pixel['DO_NOT_USE']) != 0
    if mask_sources:
        mask[seg > 0] = True

    fitdata = model.data.astype(np.float64, copy=True)

    log("Measuring pedestal")
    pedestal_data = fitdata[~mask & np.isfinite(fitdata)].flatten()
    median_image = float(np.median(pedestal_data)) if pedestal_data.size else 0.0
    try:
        # Scale-free Gaussian sky-peak fit (cal-frame units); the rate-tuned
        # fit_pedestal with its hard-coded -1..1.5 histogram range cannot be
        # used here.
        pedestal = float(fit_sky_tot(pedestal_data))
    except (RuntimeError, ValueError, TypeError):
        log("Pedestal fit failed, using median")
        pedestal = median_image
    log(f"Pedestal: {pedestal:.5e}")
    fitdata -= pedestal

    if subtract_background:
        try:
            log("Subtracting 2D background for fit")
            bg_input = fitdata.copy()
            bg_input[mask > 0] = 0
            bkgd = fit_sky(bg_input, use_bottleneck=use_bottleneck)
            fitdata -= bkgd
        except Exception as e:
            log(f"2D background failed for {rootname}: {e}; pedestal-only")

    ASYMMETRY_THRESHOLD = 0.1
    NMASK_PREFILTER = 0.20
    horizontal, vertical, ampcounts = fit_residual_striping(
        fitdata, mask, maxiters,
        asymmetry_threshold=ASYMMETRY_THRESHOLD,
        nmask_prefilter=NMASK_PREFILTER,
        estimator=estimator,
    )
    log(f"{rootname}: full-row medians used: "
        f"{', '.join(ampcounts)}/{fitdata.shape[0]}")

    # Apply additive corrections to the (cal-stage) SCI
    outsci = sci_before - horizontal - vertical
    outsci[sci_before == 0] = 0
    wnan = np.isnan(outsci)
    outsci[wnan] = 0
    bpflag = dqflags.pixel['DO_NOT_USE']
    model.dq[wnan] = np.bitwise_or(model.dq[wnan], bpflag)
    model.data = outsci

    # Preserve legacy HISTORY card alongside the structured CFP_1F key
    from stdatamodels import util as stutil
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    model.history.append(stutil.create_history_entry(
        f'Removed horizontal,vertical striping; {now}',
        software={
            'name': 'remstriping.py',
            'author': 'Micaela Bagley',
            'version': '1.0',
            'homepage': 'ceers.github.io',
        },
    ))

    srcmask_hdu = fits.ImageHDU(seg, name='SRCMASK')
    atomic_save(
        model, exposure_file,
        header_updates=cfp.format(
            CFP_1F=(
                f'estimator={estimator}, maxiters={maxiters}, '
                f'bkg={int(bool(subtract_background))} (cal-frame)'
            ),
        ),
        extra_hdus=[srcmask_hdu],
    )
    sci_after = model.data.copy()
    model.close()
    log(f"Striping removed: {rootname}")

    if do_plot:
        from campfire_pipeline.nircam.steps._plots import plot_two
        striping_pdf = os.path.join(
            os.path.dirname(exposure_file), f'{rootname}_striping.pdf',
        )
        plot_two(sci_after, sci_before,
                 title1='Striping removed', title2='Original',
                 save_file=striping_pdf)
        log(f"Saved {os.path.basename(striping_pdf)}")
