"""
striping: 1/f striping subtraction with ``SRCMASK`` extension write.

Per-exposure step. Builds a tiered source mask, fits pedestal + (optional)
2D background + horizontal + vertical striping patterns on a flat-fielded
copy of the data, then subtracts the additive striping patterns from the
original un-flat-fielded SCI. Writes the source mask as a ``SRCMASK``
extension on the canonical file (replacing the legacy
``_rate_1fmask.fits`` sidecar) so the sky_subtraction step can read it
through a single canonical file.

Imports the numerical helpers (``fit_pedestal``, ``fit_sky``,
``collapse_image``, ``find_optimal_threshold``,
``measure_fullimage_striping``) from the legacy ``stage1`` module to avoid
duplicating ~200 lines of tested code; those helpers are pure functions
without side effects. The mask-builder is re-implemented locally (the
legacy ``masksources`` writes a sidecar file as a side effect, which we
explicitly want to avoid).
"""

import copy
import os
from datetime import datetime
from time import sleep

import numpy as np
from astropy.io import fits
from astropy.convolution import Gaussian2DKernel, Ring2DKernel, convolve_fft
from astropy.stats import biweight_location
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
    find_optimal_threshold,
    fit_pedestal,
    fit_sky,
    measure_fullimage_striping,
)


def _diagnostics_dir(canonical):
    return os.path.join(os.path.dirname(canonical), 'diagnostics')


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


def _resolve_flat(model, field, use_custom):
    crds_dict = {
        'INSTRUME': 'NIRCAM',
        'DETECTOR': model.meta.instrument.detector,
        'FILTER': model.meta.instrument.filter,
        'PUPIL': model.meta.instrument.pupil,
        'DATE-OBS': model.meta.observation.date,
        'TIME-OBS': model.meta.observation.time,
    }
    if use_custom:
        fn = crds_dict['FILTER'].upper()
        det = crds_dict['DETECTOR'].upper()
        flatfile = os.path.join(
            field.flats_dir, f'flat_nircam_{fn}_{det}_CLEAR.fits',
        )
        if os.path.exists(flatfile):
            return flatfile
        log(f"Custom flat {os.path.basename(flatfile)} not found; "
            f"falling back to CRDS")

    try:
        crds_context = os.environ['CRDS_CONTEXT']
    except KeyError:
        import crds
        crds_context = crds.get_default_context()
    import crds
    refs = crds.getreferences(crds_dict, reftypes=['flat'], context=crds_context)
    return refs.get('flat')


def _apply_flat_with_retry(model, flatfile):
    from jwst.datamodels import FlatModel
    from jwst.flatfield.flat_field import do_correction
    last_exc = None
    for delay in (0, 5, 5):
        if delay:
            sleep(delay)
        try:
            with FlatModel(flatfile) as flat:
                model, _ = do_correction(model, flat)
            return model
        except Exception as e:
            last_exc = e
    raise last_exc


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
    apply_flat = step_config.get('apply_flat', True)
    use_custom_flat = step_config.get('use_custom_flat', False)
    mask_sources = step_config.get('mask_sources', True)
    maskparam = step_config.get('maskparam', 'none')
    subtract_background = step_config.get('subtract_background', True)
    maxiters = step_config.get('maxiters', 3)
    use_bottleneck = step_config.get('use_bottleneck', True)
    do_plot = step_config.get('plot', True)

    if maskparam == 'none':
        maskparam = None

    rootname = os.path.basename(exposure_file).removesuffix('.fits')

    if not overwrite:
        already_done = (status.has(exposure_file, 'CFP_1F')
                        if status is not None
                        else cfp.has_step(exposure_file, 'CFP_1F'))
        if already_done:
            log(f"Skipping striping on {rootname}: CFP_1F already set")
            return

    log(f"Running striping on {rootname}")

    from jwst.datamodels import ImageModel, dqflags

    model = ImageModel(exposure_file)
    sci_before = model.data.copy()

    if mask_sources:
        seg = _build_srcmask(model)
    else:
        seg = np.zeros(model.data.shape, dtype=np.uint8)

    fit_model = copy.deepcopy(model)
    if apply_flat:
        flatfile = _resolve_flat(fit_model, field, use_custom_flat)
        if flatfile is None:
            log(f"Flat lookup failed for {rootname}; aborting striping")
            fit_model.close()
            model.close()
            return
        log(f"Applying flat {os.path.basename(flatfile)} for striping fit")
        fit_model = _apply_flat_with_retry(fit_model, flatfile)

    mask = np.zeros(fit_model.data.shape, dtype=bool)
    mask[fit_model.dq > 0] = True
    if mask_sources:
        mask[seg > 0] = True

    log("Measuring pedestal")
    pedestal_data = fit_model.data[~mask].flatten()
    median_image = float(np.median(pedestal_data))
    try:
        pedestal = float(fit_pedestal(pedestal_data))
    except RuntimeError:
        log("Pedestal fit failed, using median")
        pedestal = median_image
    log(f"Pedestal: {pedestal:.5e}")
    fit_model.data -= pedestal

    if subtract_background:
        try:
            log("Subtracting 2D background for fit")
            bg_input = fit_model.data.copy()
            bg_input[mask > 0] = 0
            bkgd = fit_sky(bg_input, use_bottleneck=use_bottleneck)
            fit_model.data -= bkgd
        except Exception as e:
            log(f"2D background failed for {rootname}: {e}; pedestal-only")

    full_horizontal, _ = measure_fullimage_striping(
        fit_model.data, mask, maxiters,
    )
    if maskparam is None:
        try:
            log("Auto-determining optimal maskparam")
            maskparam = float(find_optimal_threshold(
                fit_model, mask, full_horizontal, maxiters,
            ))
        except Exception:
            log(f"find_optimal_threshold failed on {rootname}")
            raise
        log(f"Optimal maskparam: {maskparam:.2f}")

    horizontal = np.zeros(fit_model.data.shape)
    ampcounts = []
    rowstart = rowstop = 0
    for amp in ('A', 'B', 'C', 'D'):
        rowstart, rowstop, colstart, colstop = NIR_AMPS[amp]['data']
        ampdata = fit_model.data[:, colstart:colstop]
        ampmask = mask[:, colstart:colstop]
        h_amp = collapse_image(
            ampdata, ampmask, dimension='y', maxiters=maxiters,
        )
        nmask = np.sum(ampmask, axis=1)
        ampcount = 0
        for i in range(ampmask.shape[0]):
            if nmask[i] > (ampmask.shape[1] * maskparam):
                horizontal[i, colstart:colstop] = full_horizontal[i]
                ampcount += 1
            elif nmask[i] > (0.95 * ampmask.shape[1]):
                horizontal[i, colstart:colstop] = full_horizontal[i]
                ampcount += 1
            else:
                horizontal[i, colstart:colstop] = h_amp[i]
        ampcounts.append(f'{amp}-{ampcount}')

    log(f"{rootname}: full-row medians used: "
        f"{', '.join(ampcounts)}/{rowstop - rowstart}")

    vertical_1d = collapse_image(
        fit_model.data - horizontal, mask, maxiters, dimension='x',
    )
    vertical = np.broadcast_to(vertical_1d, fit_model.data.shape).copy()

    fit_model.close()

    # Apply additive corrections to the original (un-flat-fielded) SCI
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
            CFP_1F=f'maskparam={maskparam:.2f}, maxiters={maxiters}',
        ),
        extra_hdus=[srcmask_hdu],
    )
    sci_after = model.data.copy()
    model.close()
    log(f"Striping removed: {rootname}")

    if do_plot:
        from campfire_pipeline.nircam.steps._plots import plot_two
        diag_dir = _diagnostics_dir(exposure_file)
        os.makedirs(diag_dir, exist_ok=True)
        striping_pdf = os.path.join(diag_dir, f'{rootname}_striping.pdf')
        plot_two(sci_after, sci_before,
                 title1='Striping removed', title2='Original',
                 save_file=striping_pdf)
        log(f"Saved {os.path.basename(striping_pdf)}")
