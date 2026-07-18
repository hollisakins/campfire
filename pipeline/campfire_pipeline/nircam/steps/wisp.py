"""
wisp: subtract a fitted wisp model from a canonical exposure file.

Per-exposure step. Only ``nrca3``, ``nrca4``, ``nrcb3``, ``nrcb4`` carry
significant wisp features in the short-wavelength channel; other detectors
get ``CFP_WISP = 'skipped (detector <name>)'`` so the status command shows
them as "ran but n/a" rather than "not yet run".

Two subtraction methods are available (``[nircam.wisp].method``):

* ``nmf`` (default) — the multi-component non-negative matrix factorization
  model of Wu et al. 2026 (JADES DR5, arXiv:2601.15958), via the ``nmfwisp``
  package. Per-detector/filter templates (a small basis of components) are fit
  to each exposure with non-negative least squares, capturing exposure-to-
  exposure morphological variation that a single scaled template cannot.
  Templates ship inside the ``nmfwisp`` wheel; coverage is irregular (not every
  filter exists for every detector), so a ``(detector, filter)`` NMF has no
  template for automatically falls back to the ``template`` method below.
* ``template`` — the legacy scaled-template subtraction. For each of four
  candidate templates (different smoothing kernels) a scale coefficient is fit
  by minimizing the median absolute deviation of ``data - c * template`` inside
  a detector-specific bbox; the template with the smallest minimum is picked and
  ``c * template`` subtracted from SCI. Templates are fetched via the checksummed
  manifest (see ``wisp_cache``).

Both methods operate in the rate frame (before ``image2``/flat/photom), mutate
SCI in place, and rewrite the same canonical file. No backup file is written —
the diagnostic PDFs are generated in-memory while both arrays are live and saved
alongside the canonical FITS in the filter's flat products directory.
"""

import copy
import functools
import os
from datetime import datetime

import numpy as np
from astropy.io import fits
from astropy.stats import median_absolute_deviation
from photutils.segmentation import detect_sources, detect_threshold
from scipy.ndimage import binary_dilation

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam.steps._flat import (
    apply_flat_with_retry,
    resolve_flat,
)


WISP_DETECTORS = {'nrca3', 'nrca4', 'nrcb3', 'nrcb4'}

# Detector-specific bbox where the wisps are most prominent — used as the
# fitting region for the legacy ``template`` method so faint sources outside
# the wisp region don't influence the variance minimization.
WISP_BBOX = {
    'nrca3': (100, 1300, 1100, 2046),
    'nrca4': (300, 1450, 0, 900),
    'nrcb3': (350, 1450, 0, 1000),
    'nrcb4': (400, 1700, 850, 2046),
}


@functools.lru_cache(maxsize=None)
def _nmf_supports(detector, filtname):
    """Does the installed ``nmfwisp`` ship a template for this pair?

    Probed by file existence rather than a hardcoded list because the bundled
    coverage is irregular *and* version-dependent: e.g. F162M is absent for all
    detectors, nrca3 lacks F090W, and the nrcb4 SW templates live only in the
    full-resolution ``nrcb4_org/`` directory (the loader prefers ``<det>_org/``
    over the 4x-downsampled ``<det>/``). We therefore check both directories.
    Returns ``False`` if ``nmfwisp`` isn't importable so the caller falls back
    to the legacy template method.
    """
    if detector.lower() not in WISP_DETECTORS:
        return False
    det = detector.lower()
    fname = f'{det}_{filtname.lower()}_wisp.fits.gz'
    try:
        import importlib.resources as ir
        base = ir.files('nmfwisp') / 'templates'
    except (ModuleNotFoundError, ImportError):
        return False
    return (base / det / fname).is_file() or (base / f'{det}_org' / fname).is_file()


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


def _calc_variance(data, template, coeff):
    """MAD^2 of (data - coeff * template), nan-safe."""
    mad = median_absolute_deviation(data - coeff * template, ignore_nan=True)
    return mad ** 2


def _source_mask(data, nsigma=3.0, npixels=55, dilate=8):
    """Boolean mask (True = exclude): sources + non-finite pixels.

    Shared by both methods. ``detect_*`` need finite input, so detection runs
    on a NaN-zeroed copy while the returned mask still flags the original NaNs.

    ``detect_sources`` masks each source only down to its ``nsigma`` isophote;
    the fainter wings past that threshold otherwise leak into the wisp fit and
    bias the amplitude (NNLS) / scale (MAD) high where a bright source overlaps
    the wisp region. Two levers push those wings out of the fit: a lower
    ``nsigma`` detects them directly, and ``dilate`` grows the segmentation
    footprint by that many binary-dilation iterations. An nsigma x dilate sweep
    (nrcb4 F200W) showed the fitted wisp amplitude was inflated ~20% at the old
    (5.5-sigma, no-dilate) default by source flux, and converged once masking
    was adequate — nsigma is the stronger lever (dilation grows isotropically
    from bright cores and can't fully catch faint wings). Defaults nsigma=3,
    dilate=8 sit on that plateau; ``dilate=0`` disables growth.
    """
    finite = np.nan_to_num(data, nan=0.0)
    src = np.zeros(data.shape, dtype=bool)
    segm = detect_sources(finite, detect_threshold(finite, nsigma=nsigma),
                          npixels=npixels)
    if segm is not None:
        src = segm.data > 0
        if dilate:
            src = binary_dilation(src, iterations=dilate)
    return (src | ~np.isfinite(data)), (segm is not None)


def wisp_step(exposure_file, field, step_config, overwrite=False, status=None):
    """Subtract a fitted wisp model from a single canonical exposure.

    Parameters
    ----------
    exposure_file : str
        Canonical ``<rootname>.fits`` path.
    field : Field
    step_config : dict
        ``[nircam.wisp]`` block (legacy ``[nircam.stage1.remove_wisp]`` is
        equivalent in shape). ``method`` selects ``nmf`` (default) or
        ``template``; ``nmf`` transparently falls back to ``template`` for a
        ``(detector, filter)`` nmfwisp has no template for.
    overwrite : bool
        Re-run even when ``CFP_WISP`` is already set.
    status : StepStatus, optional
        Pre-scanned CFP_* status cache.
    """
    method = step_config.get('method', 'nmf')

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

    # NMF where the package ships a template; otherwise fall through to the
    # legacy template method (which itself stamps a visible skip when no
    # manifest template exists either).
    if method == 'nmf' and _nmf_supports(detector, filtname):
        _fit_nmf(exposure_file, step_config, rootname, detector, filtname)
        return

    _fit_template(exposure_file, field, step_config, rootname, detector,
                  filtname)


def _fit_nmf(exposure_file, step_config, rootname, detector, filtname):
    """Non-negative matrix factorization wisp subtraction (Wu et al. 2026).

    Fits the bundled multi-component ``nmfwisp`` templates to this exposure's
    rate-frame SCI with non-negative least squares (inverse-variance weighted,
    sources masked) and subtracts the resulting model. Unlike the template
    method there's no flat-fielded fitting copy: the NMF templates were built on
    rate-frame data, so the fit and the subtraction share the same un-flat-
    fielded frame.
    """
    import nmfwisp
    from nmfwisp import fit_wisp
    from jwst.datamodels import ImageModel

    plot = step_config.get('plot', True)
    correct_1f = step_config.get('nmf_correct_1f', False)
    nsigma = step_config.get('mask_nsigma', 3.0)
    dilate = step_config.get('mask_dilate', 8)

    log(f"Running NMF wisp subtraction on {rootname}")
    model = ImageModel(exposure_file, memmap=False)
    sci_before = model.data.copy()

    mask, found = _source_mask(model.data, nsigma=nsigma, dilate=dilate)
    if not found:
        log(f"Source detection found nothing for {rootname}; "
            "fitting NMF without a source mask")

    wisp, _wisp_e = fit_wisp(
        sci_before, model.err, mask,
        detector_name=detector, filter_name=filtname.upper(),
        correct_1f=correct_1f,
    )
    wisp = np.nan_to_num(np.asarray(wisp, dtype=np.float64), nan=0.0)
    wisp[sci_before == 0] = 0
    model.data = (sci_before - wisp).astype(model.data.dtype)
    sci_after = model.data.copy()

    ver = getattr(nmfwisp, '__version__', '?')
    from stdatamodels import util as stutil
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    model.history.append(stutil.create_history_entry(
        f'Removed wisps (NMFwisp {ver}, Wu et al. 2026, '
        f'arXiv:2601.15958) {now}'
    ))

    atomic_save(
        model, exposure_file,
        header_updates=cfp.format(CFP_WISP=f'nmf {ver}'),
    )
    model.close()
    log(f"Wisp removed (NMF {ver}): {rootname}")

    if plot:
        from campfire_pipeline.nircam.steps._plots import plot_two
        wisp_pdf = os.path.join(
            os.path.dirname(exposure_file), f'{rootname}_wisp.pdf',
        )
        plot_two(sci_after, sci_before,
                 title1='Wisp removed (NMF)', title2='Original',
                 save_file=wisp_pdf)
        log(f"Saved {os.path.basename(wisp_pdf)}")


def _fit_template(exposure_file, field, step_config, rootname, detector,
                  filtname):
    """Legacy scaled-template wisp subtraction.

    Also the automatic fallback for a ``(detector, filter)`` NMF has no template
    for (e.g. F140M). Stamps a visible ``skipped (no template)`` when no manifest
    template exists either — a mosaic must never silently look wisp-subtracted.
    """
    plot = step_config.get('plot', True)
    apply_flat = step_config.get('apply_flat', True)
    use_custom_flat = step_config.get('use_custom_flat', False)

    from campfire_pipeline.nircam import wisp_cache

    template_files = wisp_cache.required_templates(detector, filtname)
    short_names = ['Masked', 'Masked + smoothed 1x1',
                   'Masked + smoothed 2x2', 'Masked + smoothed 3x3']
    if not template_files:
        # No wisp template characterized for this (detector, filter) in the
        # shipped manifest. This is a legitimate, *visible* n/a — stamp it so a
        # mosaic can never look wisp-subtracted when no template exists, unlike
        # the old silent disk-check skip.
        log(f"No wisp template in manifest for {detector}/{filtname}; "
            f"stamping skipped for {rootname}")
        from jwst.datamodels import ImageModel
        with ImageModel(exposure_file, memmap=False) as m:
            atomic_save(
                m, exposure_file,
                header_updates=cfp.format(CFP_WISP='skipped (no template)'),
            )
        return

    # Resolve each template to an absolute path (fetch cache -> legacy dir),
    # fetching any that are missing. Preflight (orchestrate._prefetch_wisp_templates)
    # normally warms these before the parallel fan-out; this is the
    # defense-in-depth path for single-step / ad-hoc runs. A manifest-listed
    # template that still can't be resolved is fatal — 'enabled + missing' must
    # never be a silent skip.
    missing = [n for n in template_files
               if wisp_cache.resolve(n, field.wisp_dir) is None]
    if missing:
        wisp_cache.ensure(missing, legacy_dir=field.wisp_dir)
    template_paths = {}
    for n in template_files:
        p = wisp_cache.resolve(n, field.wisp_dir)
        if p is None:
            raise wisp_cache.WispTemplateError(
                f"wisp template {n} for {detector}/{filtname} is listed in the "
                "manifest but could not be found or fetched")
        template_paths[n] = p

    log(f"Running wisp subtraction on {rootname}")

    from jwst.datamodels import ImageModel

    model = ImageModel(exposure_file, memmap=False)
    sci_before = model.data.copy()

    # Deep-copy and flat-field for fitting only; the actual subtraction goes
    # back onto the un-flat-fielded ``model`` so we don't permanently apply
    # the flat to the canonical file.
    fit_model = copy.deepcopy(model)
    if apply_flat:
        flatfile = resolve_flat(fit_model, field, use_custom_flat)
        if flatfile is None:
            log(f"Flat lookup failed for {rootname}; skipping")
            fit_model.close()
            model.close()
            return
        log(f"Using flat {os.path.basename(flatfile)} for fit")
        fit_model = apply_flat_with_retry(fit_model, flatfile)

    fit_data = fit_model.data
    mask, found = _source_mask(
        fit_data,
        nsigma=step_config.get('mask_nsigma', 3.0),
        dilate=step_config.get('mask_dilate', 8),
    )
    if not found:
        log(f"Source detection found nothing for {rootname}; skipping")
        fit_model.close()
        model.close()
        return

    # NaN (not 0) the masked pixels: _calc_variance takes MAD with
    # ignore_nan=True, so NaNs drop out of the objective entirely. Zeroing only
    # the data left the template nonzero there, turning every masked source
    # pixel into a spurious ``-c * template`` residual that biased the scale
    # downward — an effect the dilated/lower-sigma mask would amplify.
    masked = fit_data.copy()
    masked[mask] = np.nan
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
        wisp = _load_template(template_paths[tname]).copy()
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
        fit_pdf = os.path.join(
            os.path.dirname(exposure_file), f'{rootname}_wisp_fit.pdf',
        )
        fig_fit.savefig(fit_pdf)
        plt.close(fig_fit)
        log(f"Saved {os.path.basename(fit_pdf)}")

    # Subtract from the original (un-flat-fielded) data. Cache hit: the fit
    # loop above already loaded this template.
    wisp_final = _load_template(template_paths[template_name]).copy()
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
        wisp_pdf = os.path.join(
            os.path.dirname(exposure_file), f'{rootname}_wisp.pdf',
        )
        plot_two(sci_after, sci_before,
                 title1='Wisp removed', title2='Original',
                 save_file=wisp_pdf)
        log(f"Saved {os.path.basename(wisp_pdf)}")
