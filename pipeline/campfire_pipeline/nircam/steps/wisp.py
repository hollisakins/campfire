"""
wisp: subtract a fitted wisp template from a canonical exposure file.

Per-exposure step. Runs **after** ``image2`` on flat-fielded, flux-calibrated
cal-stage data (JADES-style ordering). Only ``nrca3``, ``nrca4``, ``nrcb3``,
``nrcb4`` carry significant wisp features in the short-wavelength channel; other
detectors get ``CFP_WISP = 'skipped (detector <name>)'`` so the status command
shows them as "ran but n/a" rather than "not yet run".

For each of four candidate templates (different smoothing kernels) the step
fits a scale coefficient by minimizing the median absolute deviation of
``data - c * template`` inside a detector-specific bbox, picks the template
with the smallest minimum, and subtracts ``c * template`` from SCI.

Because only the template's *shape* matters (a free per-exposure coefficient
absorbs its overall normalization), the fit is frame-agnostic: a robust
least-squares prefactor anchors the coefficient grid to the data's units, so
the search is valid whether the templates are stored in rate (DN/s) or
cal (MJy/sr) units. No flat is applied here — image2 already did.

No backup file is written — the diagnostic PDFs (one for the fit residuals,
one for before/after) are generated in-memory while both arrays are live and
saved alongside the canonical FITS in the filter's flat products directory.
"""

import functools
import os
from datetime import datetime

import numpy as np
from astropy.io import fits
from astropy.stats import median_absolute_deviation
from photutils.segmentation import detect_sources, detect_threshold

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp


WISP_DETECTORS = {'nrca3', 'nrca4', 'nrcb3', 'nrcb4'}

# Detector-specific bbox where the wisps are most prominent — used as the
# fitting region so faint sources outside the wisp region don't influence
# the variance minimization.
WISP_BBOX = {
    'nrca3': (100, 1300, 1100, 2046),
    'nrca4': (300, 1450, 0, 900),
    'nrcb3': (350, 1450, 0, 1000),
    'nrcb4': (400, 1700, 850, 2046),
}


def _calc_variance(data, template, coeff):
    """MAD^2 of (data - coeff * template), nan-safe."""
    mad = median_absolute_deviation(data - coeff * template, ignore_nan=True)
    return mad ** 2


def _ls_scale(data_region, template_region):
    """Robust least-squares scale anchoring the coefficient grid to the data.

    Returns ``c0 = <d, t> / <t, t>`` over finite, unmasked (non-zero) pixels —
    a units-agnostic anchor for the wisp coefficient. The MAD grid search then
    refines around it. Falls back to 1.0 when undefined. The template is a
    near-zero-mean residual, so the sky pedestal contributes little to the
    inner product.
    """
    d = data_region.ravel()
    t = template_region.ravel()
    good = np.isfinite(d) & np.isfinite(t) & (d != 0) & (t != 0)
    denom = float(np.sum(t[good] * t[good]))
    if denom <= 0:
        return 1.0
    c0 = float(np.sum(d[good] * t[good]) / denom)
    return c0 if (np.isfinite(c0) and c0 > 0) else 1.0


@functools.lru_cache(maxsize=8)
def _load_template(path):
    """Read a wisp template once per worker, NaN-cleaned, read-only.

    The same ~16MB template applies to every exposure of a given
    (detector, filter), and each exposure's fit loop touches all four
    candidates plus a fifth read of the winner — over NFS that's pure
    re-read churn. The NaN replacement is template-intrinsic so it lives
    in the cache; the exposure-specific ``sci == 0`` masking happens on a
    copy at the call sites. The array is marked read-only as a guard
    against accidental in-place mutation of the shared cache entry.
    """
    data = fits.getdata(path, memmap=False)
    data[np.isnan(data)] = 0
    data.flags.writeable = False
    return data


def wisp_step(exposure_file, field, step_config, overwrite=False, status=None):
    """Subtract a fitted wisp template from a single canonical exposure.

    Parameters
    ----------
    exposure_file : str
        Canonical ``<rootname>.fits`` path (post-image2; cal-stage data).
    field : Field
    step_config : dict
        ``[nircam.wisp]`` block.
    overwrite : bool
        Re-run even when ``CFP_WISP`` is already set.
    status : StepStatus, optional
        Pre-scanned CFP_* status cache.
    """
    plot = step_config.get('plot', True)

    rootname = os.path.basename(exposure_file).removesuffix('.fits')
    filtname = exposure_file.split('/')[-2]
    detector = rootname.split('_')[3]

    if cfp.should_skip(exposure_file, 'CFP_WISP', rootname,
                       'wisp', status, overwrite):
        return

    if detector not in WISP_DETECTORS:
        log(f"Skipping wisp on {rootname}: detector {detector} has no wisps")
        from jwst.datamodels import ImageModel
        with ImageModel(exposure_file, memmap=False) as m:
            atomic_save(
                m, exposure_file,
                header_updates=cfp.format(
                    CFP_WISP=f'skipped (detector {detector})'
                ),
            )
        return

    template_files = [
        f'WISP_{detector.upper()}_{filtname.upper()}_CLEAR_masked.fits',
        f'WISP_{detector.upper()}_{filtname.upper()}_CLEAR_masked_smoothed_1x1.fits',
        f'WISP_{detector.upper()}_{filtname.upper()}_CLEAR_masked_smoothed_2x2.fits',
        f'WISP_{detector.upper()}_{filtname.upper()}_CLEAR_masked_smoothed_3x3.fits',
    ]
    short_names = ['Masked', 'Masked + smoothed 1x1',
                   'Masked + smoothed 3x3', 'Masked + smoothed 5x5']
    if not os.path.exists(os.path.join(field.wisp_dir, template_files[0])):
        log(f"Wisp templates for {detector}/{filtname} not in "
            f"{field.wisp_dir}; skipping {rootname}")
        return

    log(f"Running wisp subtraction on {rootname}")

    from jwst.datamodels import ImageModel

    model = ImageModel(exposure_file, memmap=False)
    sci_before = model.data.copy()

    # Fit directly in the cal frame — image2 already applied the flat and
    # photometric calibration, so no flat-fielded copy is needed.
    data = model.data
    mask = np.zeros(data.shape, dtype=bool)
    mask[np.isnan(data)] = True
    threshold = detect_threshold(data, nsigma=5.5)
    segm = detect_sources(data, threshold, npixels=55)
    if segm is None:
        log(f"Source detection found nothing for {rootname}; skipping")
        model.close()
        return
    mask[segm.data > 0] = True

    masked = data.copy()
    masked[mask] = 0
    x1, x2, y1, y2 = WISP_BBOX[detector]
    im_seg = masked[y1:y2, x1:x2]

    if plot:
        import matplotlib.pyplot as plt
        fig_fit, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8),
                                           tight_layout=True)

    min_x = np.zeros(len(template_files))
    min_y = np.zeros(len(template_files))
    for i, (tname, sname) in enumerate(zip(template_files, short_names)):
        wisp = _load_template(os.path.join(field.wisp_dir, tname)).copy()
        wisp[sci_before == 0] = 0
        seg_w = wisp[y1:y2, x1:x2]

        # Anchor the coefficient grid to the data units (frame-agnostic), then
        # search 0 .. 2x the least-squares scale for the MAD-robust minimum.
        c0 = _ls_scale(im_seg, seg_w)
        coeffs = c0 * np.linspace(0.0, 2.0, 200)

        var_mad = np.array([_calc_variance(im_seg, seg_w, c) for c in coeffs])

        m = int(np.argmin(var_mad))
        min_x[i] = coeffs[m]
        min_y[i] = var_mad[m]
        log(f"{tname}: fit coefficient = {min_x[i]:.3g} (c0={c0:.3g})")

        if plot:
            try:
                var_pred = np.poly1d(np.polyfit(coeffs, var_mad, deg=2))(coeffs)
            except (np.linalg.LinAlgError, ValueError):
                var_pred = var_mad
            ax1.plot(coeffs, var_pred * 1e4, f'C{i}', lw=1.5, label=sname)
            ax1.plot(coeffs, var_mad * 1e4, f'C{i}o', lw=1.5)
            ax2.plot(coeffs, (var_mad - var_pred) * 1e6, f'C{i}', lw=1)
            ax1.axvline(min_x[i], color=f'C{i}', ls=':', lw=0.5)

    pick = int(np.argmin(min_y))
    minval = float(min_x[pick])
    template_name = template_files[pick]
    log(f"Best template: {template_name}, scale = {minval:.3g}")

    if plot:
        ax1.set_ylabel(r'var (from MAD, 10$^{-4}$)')
        ax1.legend()
        ax2.set_xlabel('coefficient')
        ax2.set_ylabel(r'residuals (10$^{-6}$)')
        fit_pdf = os.path.join(
            os.path.dirname(exposure_file), f'{rootname}_wisp_fit.pdf',
        )
        fig_fit.savefig(fit_pdf)
        plt.close(fig_fit)
        log(f"Saved {os.path.basename(fit_pdf)}")

    # Subtract from the cal-stage data. Cache hit: the fit loop above already
    # loaded this template.
    wisp_final = _load_template(
        os.path.join(field.wisp_dir, template_name)).copy()
    wisp_final[sci_before == 0] = 0
    model.data = sci_before - minval * wisp_final
    sci_after = model.data.copy()

    # Preserve the legacy HISTORY entry alongside the structured CFP_WISP key
    from stdatamodels import util as stutil
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    model.history.append(stutil.create_history_entry(
        f'Removed wisps ({template_name}, scale = {minval:.3g}) {now}'
    ))

    atomic_save(
        model, exposure_file,
        header_updates=cfp.format(
            CFP_WISP=f'{template_name}, {minval:.3g}'
        ),
    )
    model.close()
    log(f"Wisp removed: {rootname}")

    if plot:
        from campfire_pipeline.nircam.steps._plots import plot_two
        wisp_pdf = os.path.join(
            os.path.dirname(exposure_file), f'{rootname}_wisp.pdf',
        )
        plot_two(sci_after, sci_before,
                 title1='Wisp removed', title2='Original',
                 save_file=wisp_pdf)
        log(f"Saved {os.path.basename(wisp_pdf)}")
