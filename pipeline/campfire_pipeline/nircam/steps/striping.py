"""
striping: 1/f striping subtraction with ``SRCMASK`` extension write.

Per-exposure step. Runs **after** ``image2``, on flat-fielded, flux-
calibrated cal-stage data. Builds a tiered source mask, fits pedestal +
(optional) 2D background + horizontal + vertical striping patterns on a
pedestal-subtracted working copy of the cal-stage SCI, then subtracts the
additive striping patterns from the SCI **in that same cal frame**.

Fitting and applying in the cal frame (rather than the legacy "fit on a
flat-fielded copy, subtract from the un-flat rate SCI" round-trip) is exact:
the old path removed the per-amp-row offset in flat units but subtracted it
from un-flat data that image2 then re-divided by the per-amp-structured
flat, leaving a coherent per-amp DC step (``N/g·(1−1/g)``) at the amplifier
boundaries. Subtracting in the cal frame eliminates that leak.

Writes the source mask as a ``SRCMASK`` extension on the canonical file
(replacing the legacy ``_rate_1fmask.fits`` sidecar) so the sky_subtraction
step can read it through a single canonical file.

Imports the numerical helpers (``fit_sky_tot``, ``fit_sky``,
``collapse_image``, ``measure_fullimage_striping``) from ``skyfit``; those
helpers are pure functions without side effects. The mask-builder is
re-implemented locally (the legacy ``masksources`` writes a sidecar file
as a side effect, which we explicitly want to avoid).
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


def _build_srcmask(model, extra_dilation=0):
    """Tiered source mask from a JWST ImageModel; returns ``uint8`` array.

    Equivalent to ``stage1.masksources`` but in-memory only — the legacy
    function additionally writes ``_1fmask.fits`` as a side effect, which
    the canonical-exposure layout replaces with a SRCMASK extension on the
    canonical file.

    ``extra_dilation`` grows the final mask by that many 1-px binary
    dilations (3×3 connectivity). Used by the aggressive-masking variant
    of the GP estimator, whose error budget is asymmetric: over-masking
    only inflates the per-amp-row ``sigma_r`` (the GP interpolates across),
    whereas under-masking leaks source wings into the median and the GP
    faithfully fits — then oversubtracts — the biased offset.
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

    if extra_dilation > 0:
        finalmask = binary_dilation(finalmask, iterations=int(extra_dilation))

    out = np.zeros(finalmask.shape, dtype=np.uint8)
    out[finalmask] = 1
    return out


def _median_amprow_offsets(data, mask, maxiters, asymmetry_threshold,
                           nmask_prefilter):
    """Per-amp per-row offset via 2σ-clipped median + full-row fallback.

    The production estimator. Returns ``(horizontal, ampcounts)`` where
    ``ampcounts`` reports the number of rows per amp that fell back to the
    full-image row median. See ``fit_residual_striping`` for the parameter
    semantics.
    """
    full_horizontal, _ = measure_fullimage_striping(data, mask, maxiters)

    horizontal = np.zeros(data.shape)
    ampcounts = []
    for amp in ('A', 'B', 'C', 'D'):
        _, _, colstart, colstop = NIR_AMPS[amp]['data']
        ampdata = data[:, colstart:colstop]
        ampmask = mask[:, colstart:colstop]
        # Replaces a separate `collapse_image(...)` call: returns the same
        # per-row median in `h_amp`, plus the mean and std needed for the
        # asymmetry test below — all from one sigma-clip pass.
        # Fully-masked rows produce all-NaN slices; the asymmetry guard
        # below treats them as contaminated, so the inner RuntimeWarnings
        # are noise.
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
        # Non-finite (e.g. all-masked rows where std is 0/nan) → treat as
        # contaminated so the fallback path is taken.
        asymmetry = np.where(np.isfinite(asymmetry), asymmetry, np.inf)

        nmask = np.sum(ampmask, axis=1)
        ampcount = 0
        for i in range(ampmask.shape[0]):
            # Per-amp median in clean rows (where the source mask says
            # nothing's there); asymmetry test only in rows where the
            # source mask is meaningful AND there are still enough
            # unmasked pixels to estimate from.
            nmask_frac_i = nmask[i] / ampmask.shape[1]
            if nmask_frac_i > 0.95:
                # Too few unmasked pixels for any reliable per-amp
                # estimate — fall back regardless of asymmetry.
                horizontal[i, colstart:colstop] = full_horizontal[i]
                ampcount += 1
            elif (nmask_frac_i > nmask_prefilter
                  and asymmetry[i] > asymmetry_threshold):
                # Source mask is meaningful here AND the post-clip
                # distribution is still asymmetric: contamination biased
                # the per-amp median.
                horizontal[i, colstart:colstop] = full_horizontal[i]
                ampcount += 1
            else:
                horizontal[i, colstart:colstop] = h_amp[i]
        ampcounts.append(f'{amp}-{ampcount}')
    return horizontal, ampcounts


def fit_residual_striping(
    data,
    mask,
    maxiters,
    asymmetry_threshold=0.1,
    nmask_prefilter=0.20,
    estimator='median',
    gp_params=None,
):
    """Fit per-amp per-row horizontal + per-column vertical 1/f striping.

    Pure function. Operates on a flat-fielded (and ideally pedestal-/
    background-subtracted) frame. Returns 2D additive correction arrays
    that should be subtracted from the un-flat-fielded SCI.

    The per-amp-row **horizontal** estimate is dispatched on ``estimator``;
    the per-column **vertical** estimate (measured on ``data - horizontal``)
    is identical for both and is intentionally left untouched.

    Parameters
    ----------
    data : (H, W) ndarray
        Input frame (flat-fielded, pedestal/background pre-subtracted).
    mask : (H, W) bool ndarray
        True where pixels are masked (DQ flagged or source).
    maxiters : int
        Sigma-clipping iterations.
    asymmetry_threshold, nmask_prefilter : float
        ``estimator="median"`` only. Asymmetry test threshold and the
        mask-fraction below which it is skipped (see
        ``_median_amprow_offsets``). Clean / successfully clipped rows give
        asymmetry ≈ 0; heavy bright-source contamination gives ≳ 0.3. The
        prefilter avoids false positives from the asymmetry statistic's own
        sample-noise floor (~0.07 for N ≈ 508) in clean rows.
    estimator : {'median', 'gp'}
        ``'median'`` (default): the production 2σ-clipped median with
        full-row fallback. ``'gp'``: a 1-D Gaussian-Process smoother fit
        along the slow axis per amplifier (see
        ``campfire_pipeline.nircam.gp_striping``), which interpolates the
        offset across masked source rows *within the same amplifier* rather
        than substituting the full-row median.
    gp_params : dict, optional
        Required when ``estimator='gp'``. Keys: ``kernel_sigma`` and
        ``rho`` (frozen SHOTerm hyperparameters; required), plus optional
        ``q`` (default ``1/sqrt(2)``), ``sigma_clip`` (default 2.0), and
        ``weak_frac`` (default 0.5).

    Returns
    -------
    horizontal : (H, W) ndarray
        Per-amp, per-row striping pattern.
    vertical : (H, W) ndarray
        Per-column striping pattern (1D x-collapse broadcast to 2D).
    ampcounts : list[str]
        Per-amp diagnostic strings. For ``'median'``: ``'A-N'`` ... where
        ``N`` is the number of rows that fell back to the full-image
        median. For ``'gp'``: ``'A:anchors/weak/maxσ'`` per amp.
    """
    if estimator == 'gp':
        from campfire_pipeline.nircam.gp_striping import gp_amprow_offsets
        params = gp_params or {}
        if 'kernel_sigma' not in params or 'rho' not in params:
            raise ValueError(
                "estimator='gp' requires gp_params with 'kernel_sigma' "
                "and 'rho'")
        horizontal, ampcounts = gp_amprow_offsets(
            data, mask,
            kernel_sigma=params['kernel_sigma'],
            rho=params['rho'],
            q=params.get('q', 1.0 / np.sqrt(2.0)),
            sigma_clip_sigma=params.get('sigma_clip', 2.0),
            maxiters=maxiters,
            weak_frac=params.get('weak_frac', 0.5),
        )
    elif estimator == 'median':
        horizontal, ampcounts = _median_amprow_offsets(
            data, mask, maxiters, asymmetry_threshold, nmask_prefilter)
    else:
        raise ValueError(
            f"unknown estimator {estimator!r} (expected 'median' or 'gp')")

    vertical_1d = collapse_image(
        data - horizontal, mask, maxiters, dimension='x',
    )
    vertical = np.broadcast_to(vertical_1d, data.shape).copy()

    return horizontal, vertical, ampcounts


def _resolve_gp_params(gp_cfg, model):
    """Resolve frozen GP hyperparameters for this exposure's channel.

    Hyperparameters are calibrated offline (see
    ``scripts/calibrate_gp_striping.py``) and split by detector channel
    (SW vs LW) because the 1/f amplitude and correlation length differ.
    Config keys: ``kernel_sigma_sw`` / ``rho_sw`` / ``kernel_sigma_lw`` /
    ``rho_lw`` (per-channel), or channel-agnostic ``kernel_sigma`` / ``rho``
    (used as a fallback for whichever channel-specific key is absent).
    Shared optional keys: ``q`` (default ``1/sqrt(2)``), ``sigma_clip``
    (default 2.0), ``weak_frac`` (default 0.5).
    """
    channel = (getattr(model.meta.instrument, 'channel', None) or '').upper()
    suffix = 'sw' if channel == 'SHORT' else 'lw'

    def pick(name):
        val = gp_cfg.get(f'{name}_{suffix}', gp_cfg.get(name))
        if val is None:
            raise ValueError(
                f"estimator='gp' needs [nircam.striping.gp].{name}_{suffix} "
                f"(or {name}); run scripts/calibrate_gp_striping.py and set "
                f"the frozen value in config")
        return float(val)

    return {
        'kernel_sigma': pick('kernel_sigma'),
        'rho': pick('rho'),
        'q': float(gp_cfg.get('q', 1.0 / np.sqrt(2.0))),
        'sigma_clip': float(gp_cfg.get('sigma_clip', 2.0)),
        'weak_frac': float(gp_cfg.get('weak_frac', 0.5)),
    }


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
    # 2D-background (fit-only detrend) box / filter sizes. The effective
    # smoothing scale is ~box*filter; smaller pulls the field's large-scale
    # structure (cluster ICL) out of the 1/f fit more completely, but too
    # small starts absorbing the 1/f itself. 32/3 calibrated on rj0911 f444w.
    subtract_background_box = step_config.get('subtract_background_box', 32)
    subtract_background_filter = step_config.get('subtract_background_filter', 3)
    maxiters = step_config.get('maxiters', 3)
    use_bottleneck = step_config.get('use_bottleneck', True)
    do_plot = step_config.get('plot', True)
    estimator = step_config.get('estimator', 'median')
    # Aggressive masking pairs with the GP estimator: grow the source mask
    # and fold in additional DQ classes so leaked source/CR flux cannot bias
    # the per-amp-row median that the GP then interpolates. Over-masking is
    # cheap for the GP (large sigma_r, interpolated across); under-masking
    # is not (oversubtracted troughs around sources).
    mask_aggressive = step_config.get('mask_aggressive', False)
    mask_extra_dilation = step_config.get('mask_extra_dilation', 4)
    gp_cfg = step_config.get('gp', {})

    rootname = os.path.basename(exposure_file).removesuffix('.fits')

    if cfp.should_skip(exposure_file, 'CFP_1F', rootname,
                       'striping', status, overwrite):
        return

    log(f"Running striping on {rootname}")

    from jwst.datamodels import ImageModel, dqflags

    model = ImageModel(exposure_file, memmap=False)
    sci_before = model.data.copy()

    if mask_sources:
        seg = _build_srcmask(
            model, extra_dilation=mask_extra_dilation if mask_aggressive else 0)
    else:
        seg = np.zeros(model.data.shape, dtype=np.uint8)

    # Runs after image2, so model.data is already flat-fielded and flux-
    # calibrated. Fit and apply the 1/f correction directly in the cal frame
    # (no flat-fielded copy, no apply-to-rate round-trip): the fit operates on
    # a pedestal-/background-subtracted working copy ``fitdata``; the
    # corrections are subtracted from the SCI itself further down.
    #
    # Only DO_NOT_USE pixels are unusable for fitting — JUMP_DET and other
    # informational bits flag pixels that have already been corrected and
    # are still fine for sky/striping estimation. (Some exposures, e.g.
    # bright-target MSATA pointings on MEDIUM8/NGROUPS=9, get JUMP_DET set
    # on >97% of pixels; treating dq>0 as bad masks the entire frame.)
    dq_bits = dqflags.pixel['DO_NOT_USE']
    if mask_aggressive:
        # Fold CR/jump/saturation/persistence-flagged pixels into the fit
        # mask too. These carry residual signal (snowball halos, bleeds,
        # persistence trails) that would otherwise bias the per-amp-row
        # median; the GP tolerates the extra masking via inflated sigma_r.
        for bit in ('JUMP_DET', 'SATURATED', 'PERSISTENCE'):
            dq_bits |= dqflags.pixel[bit]
    mask = np.bitwise_and(model.dq, dq_bits) != 0
    if mask_sources:
        mask[seg > 0] = True

    if estimator == 'none':
        # cfn-only reference arm: JWST's clean_flicker_noise already removed
        # the 1/f at the ramp stage, so campfire applies no further striping.
        # We still build + write SRCMASK and run the rest of the pipeline so
        # the frame is directly comparable to the median/gp arms (same
        # downstream steps, same blank-pixel mask for QA).
        horizontal = np.zeros(model.data.shape)
        vertical = np.zeros(model.data.shape)
        ampcounts = []
        log(f"{rootname}: estimator=none (no campfire 1/f; SRCMASK only)")
        cfp_value = 'estimator=none (no campfire 1/f, cfn-only reference)'
    else:
        fitdata = model.data.astype(np.float64, copy=True)

        log("Measuring pedestal")
        pedestal_data = fitdata[~mask & np.isfinite(fitdata)].flatten()
        median_image = float(np.median(pedestal_data)) if pedestal_data.size else 0.0
        try:
            # Scale-free Gaussian sky-peak fit (cal-frame units); the rate-tuned
            # fit_pedestal with its hard-coded -1..1.5 histogram cannot be used here.
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
                bkgd = fit_sky(bg_input, use_bottleneck=use_bottleneck,
                               box_size=subtract_background_box,
                               filter_size=subtract_background_filter)
                fitdata -= bkgd
            except Exception as e:
                log(f"2D background failed for {rootname}: {e}; pedestal-only")

        ASYMMETRY_THRESHOLD = 0.1
        NMASK_PREFILTER = 0.20
        if estimator == 'gp':
            gp_params = _resolve_gp_params(gp_cfg, model)
            horizontal, vertical, ampcounts = fit_residual_striping(
                fitdata, mask, maxiters,
                estimator='gp', gp_params=gp_params,
            )
            log(f"{rootname}: GP striping (sigma={gp_params['kernel_sigma']:.3e}, "
                f"rho={gp_params['rho']:.1f}) "
                f"[amp:anchors/weak/maxσ] {', '.join(ampcounts)}")
            cfp_value = (
                f'estimator=gp, kernel_sigma={gp_params["kernel_sigma"]:.4e}, '
                f'rho={gp_params["rho"]:.2f}, q={gp_params["q"]:.4f}, '
                f'aggr_mask={int(mask_aggressive)}, maxiters={maxiters} (cal-frame)'
            )
        else:
            horizontal, vertical, ampcounts = fit_residual_striping(
                fitdata, mask, maxiters,
                asymmetry_threshold=ASYMMETRY_THRESHOLD,
                nmask_prefilter=NMASK_PREFILTER,
            )
            log(f"{rootname}: full-row medians used: "
                f"{', '.join(ampcounts)}/{fitdata.shape[0]}")
            cfp_value = (
                f'estimator=median, asymmetry={ASYMMETRY_THRESHOLD}, '
                f'nmask_prefilter={NMASK_PREFILTER}, maxiters={maxiters} (cal-frame)'
            )

    # Apply additive corrections to the (cal-stage) SCI — same frame the
    # patterns were fit in, so no flat re-division leak.
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
        header_updates=cfp.format(CFP_1F=cfp_value),
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
