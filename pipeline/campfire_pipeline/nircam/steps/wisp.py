"""
wisp: subtract a fitted wisp template from a canonical exposure file.

Per-exposure step. Only ``nrca3``, ``nrca4``, ``nrcb3``, ``nrcb4`` carry
significant wisp features in the short-wavelength channel; other detectors
get ``CFP_WISP = 'skipped (detector <name>)'`` so the status command shows
them as "ran but n/a" rather than "not yet run".

For each of four candidate templates (different smoothing kernels) the step
fits a scale coefficient by minimizing the median absolute deviation of
``data - c * template`` inside a detector-specific bbox, picks the template
with the smallest minimum, and subtracts ``c * template`` from SCI.

No backup file is written — the diagnostic PDFs (one for the fit residuals,
one for before/after) are generated in-memory while both arrays are live and
saved to ``exposures/<filter>/diagnostics/``. The pre-mutation SCI snapshot
also makes the source-detection-on-flat-fielded-copy idiom from the legacy
implementation work without re-reading from disk.
"""

import copy
import os
from datetime import datetime
from time import sleep

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


def _diagnostics_dir(canonical):
    return os.path.join(os.path.dirname(canonical), 'diagnostics')


def _resolve_flat(model, field, use_custom):
    """Pick the flat reference: custom (if requested + present) else CRDS."""
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
    """Apply flat in-memory, retrying once or twice on transient CRDS races."""
    from jwst.datamodels import FlatModel
    from jwst.flatfield.flat_field import do_correction

    last_exc = None
    for delay in (0, 3, 10):
        if delay:
            sleep(delay)
        try:
            with FlatModel(flatfile) as flat:
                model, _ = do_correction(model, flat)
            return model
        except Exception as e:  # CRDS races, IOErrors, etc.
            last_exc = e
    raise last_exc


def wisp_step(exposure_file, field, step_config, overwrite=False):
    """Subtract a fitted wisp template from a single canonical exposure.

    Parameters
    ----------
    exposure_file : str
        Canonical ``<rootname>.fits`` path.
    field : Field
    step_config : dict
        ``[nircam.wisp]`` block (legacy ``[nircam.stage1.remove_wisp]`` is
        equivalent in shape).
    overwrite : bool
        Re-run even when ``CFP_WISP`` is already set.
    """
    plot = step_config.get('plot', True)
    apply_flat = step_config.get('apply_flat', True)
    use_custom_flat = step_config.get('use_custom_flat', False)

    rootname = os.path.basename(exposure_file).removesuffix('.fits')
    filtname = exposure_file.split('/')[-2]
    detector = rootname.split('_')[3]

    if not overwrite and cfp.has_step(exposure_file, 'CFP_WISP'):
        log(f"Skipping wisp on {rootname}: CFP_WISP already set")
        return

    if detector not in WISP_DETECTORS:
        log(f"Skipping wisp on {rootname}: detector {detector} has no wisps")
        from jwst.datamodels import ImageModel
        with ImageModel(exposure_file) as m:
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

    model = ImageModel(exposure_file)
    sci_before = model.data.copy()

    # Deep-copy and flat-field for fitting only; the actual subtraction goes
    # back onto the un-flat-fielded ``model`` so we don't permanently apply
    # the flat to the canonical file.
    fit_model = copy.deepcopy(model)
    if apply_flat:
        flatfile = _resolve_flat(fit_model, field, use_custom_flat)
        if flatfile is None:
            log(f"Flat lookup failed for {rootname}; skipping")
            fit_model.close()
            model.close()
            return
        log(f"Using flat {os.path.basename(flatfile)} for fit")
        fit_model = _apply_flat_with_retry(fit_model, flatfile)

    fit_data = fit_model.data
    mask = np.zeros(fit_data.shape, dtype=bool)
    mask[np.isnan(fit_data)] = True
    threshold = detect_threshold(fit_data, nsigma=5.5)
    segm = detect_sources(fit_data, threshold, npixels=55)
    if segm is None:
        log(f"Source detection found nothing for {rootname}; skipping")
        fit_model.close()
        model.close()
        return
    mask[segm.data > 0] = True

    masked = fit_data.copy()
    masked[mask] = 0
    x1, x2, y1, y2 = WISP_BBOX[detector]
    im_seg = masked[y1:y2, x1:x2]

    if plot:
        import matplotlib.pyplot as plt
        fig_fit, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8),
                                           tight_layout=True)

    coeffs = np.arange(0.01, 1.5, 0.01)
    min_x = np.zeros(len(template_files))
    min_y = np.zeros(len(template_files))
    for i, (tname, sname) in enumerate(zip(template_files, short_names)):
        wisp = fits.getdata(os.path.join(field.wisp_dir, tname))
        wisp[np.isnan(wisp)] = 0
        wisp[sci_before == 0] = 0
        seg_w = wisp[y1:y2, x1:x2]

        var_mad = np.array([_calc_variance(im_seg, seg_w, c) for c in coeffs])
        var_pred = np.poly1d(np.polyfit(coeffs, var_mad, deg=2))(coeffs)

        m = int(np.argmin(var_mad))
        min_x[i] = coeffs[m]
        min_y[i] = var_mad[m]
        log(f"{tname}: fit coefficient = {min_x[i]:.2f}")

        if plot:
            ax1.plot(coeffs, var_pred * 1e4, f'C{i}', lw=1.5, label=sname)
            ax1.plot(coeffs, var_mad * 1e4, f'C{i}o', lw=1.5)
            ax2.plot(coeffs, (var_mad - var_pred) * 1e6, f'C{i}', lw=1)
            for ax in (ax1, ax2):
                ax.axvline(min_x[i], color=f'C{i}', ls=':', lw=0.5)

    fit_model.close()

    pick = int(np.argmin(min_y))
    minval = float(min_x[pick])
    template_name = template_files[pick]
    log(f"Best template: {template_name}, scale = {minval:.2f}")

    if plot:
        ax1.set_ylabel(r'var (from MAD, 10$^{-4}$)')
        ax1.legend()
        ax2.set_xlabel('coefficient')
        ax2.set_ylabel(r'residuals (10$^{-6}$)')
        diag_dir = _diagnostics_dir(exposure_file)
        os.makedirs(diag_dir, exist_ok=True)
        fit_pdf = os.path.join(diag_dir, f'{rootname}_wisp_fit.pdf')
        fig_fit.savefig(fit_pdf)
        plt.close(fig_fit)
        log(f"Saved {os.path.basename(fit_pdf)}")

    # Subtract from the original (un-flat-fielded) data
    wisp_final = fits.getdata(os.path.join(field.wisp_dir, template_name))
    wisp_final[np.isnan(wisp_final)] = 0
    wisp_final[sci_before == 0] = 0
    model.data = sci_before - minval * wisp_final
    sci_after = model.data.copy()

    # Preserve the legacy HISTORY entry alongside the structured CFP_WISP key
    from stdatamodels import util as stutil
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    model.history.append(stutil.create_history_entry(
        f'Removed wisps ({template_name}, scale = {minval:.2f}) {now}'
    ))

    atomic_save(
        model, exposure_file,
        header_updates=cfp.format(
            CFP_WISP=f'{template_name}, {minval:.2f}'
        ),
    )
    model.close()
    log(f"Wisp removed: {rootname}")

    if plot:
        from campfire_pipeline.nircam.steps._plots import plot_two
        diag_dir = _diagnostics_dir(exposure_file)
        os.makedirs(diag_dir, exist_ok=True)
        wisp_pdf = os.path.join(diag_dir, f'{rootname}_wisp.pdf')
        plot_two(sci_after, sci_before,
                 title1='Wisp removed', title2='Original',
                 save_file=wisp_pdf)
        log(f"Saved {os.path.basename(wisp_pdf)}")
