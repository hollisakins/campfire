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
  The source mask is iterated (``mask_iterations``): each pass re-detects on
  the wisp-subtracted frame, so a bright wisp cannot mask its own fit region.
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

# ---------------------------------------------------------------------------
# TEMPORARY: nmfwisp filter-name case shim.
#
# nmfwisp <= 1.1.4 cannot load ANY bundled template on a case-sensitive
# filesystem. ``estimate_wisp_standard`` uppercases ``filter_name`` for its
# validation list and then hands that same uppercased string to
# ``load_wisp_templates``, which uses it verbatim to build the filename:
#
#     nrcb4 + 'F200W'  ->  templates/nrcb4_org/nrcb4_F200W_wisp.fits.gz
#     shipped file     ->  templates/nrcb4_org/nrcb4_f200w_wisp.fits.gz
#
# The lookup misses, ``load_wisp_templates`` returns None for wmask instead of
# raising, and the None surfaces three frames later as an opaque numpy error
# from ``np.any((mask, None), axis=0)``. macOS's case-insensitive filesystem
# hides this entirely, which is likely why it shipped.
#
# Workaround: a cache-resident tree of uppercase-named symlinks pointing at the
# real lowercase templates, passed to ``fit_wisp(wisp_path=...)``.
#
# This is SELF-REMOVING: _nmfwisp_case_shim() probes the installed nmfwisp and
# returns None when the upstream lookup works, so once a fixed nmfwisp is
# installed the shim stops being built or used with no code change. Delete this
# block (and the two call sites it feeds) when the fixed release is pinned.
# ---------------------------------------------------------------------------

# A (detector, filter) pair the package is known to ship, used only to probe
# whether upstream's lookup resolves.
_SHIM_PROBE = ('nrcb4', 'F115W')


def _upstream_lookup_works():
    """True when the installed nmfwisp resolves a template from an UPPERCASE
    filter name (i.e. the case bug is fixed and no shim is needed)."""
    try:
        from nmfwisp.nmfwisp import load_wisp_templates
    except (ModuleNotFoundError, ImportError):
        return True          # nothing to shim; caller falls back anyway
    det, filt = _SHIM_PROBE
    try:
        _tmpl, _err, wmask, _hsr = load_wisp_templates(None, det, filt)
    except Exception:
        # A fixed version may raise FileNotFoundError on a genuine miss; that is
        # not the case bug, and a shim would not help.
        return True
    return wmask is not None and np.size(wmask) > 0


@functools.lru_cache(maxsize=1)
def _nmfwisp_case_shim():
    """Path to the uppercase-symlink shim tree, or None if not needed.

    Built once per host under the wisps cache. Construction is race-safe: each
    process builds into a private directory and atomically renames it into
    place, discarding its copy if another process got there first.
    """
    if _upstream_lookup_works():
        return None
    try:
        import importlib.resources as ir
        import nmfwisp  # noqa: F401
        src = ir.files('nmfwisp') / 'templates'
    except (ModuleNotFoundError, ImportError):
        return None

    from campfire_pipeline.nircam import wisp_cache
    final = os.path.join(wisp_cache.cache_dir(), '_nmfwisp_case_shim')
    if os.path.isdir(final):
        return final

    staging = f'{final}.{os.getpid()}'
    n = 0
    for det_dir in sorted(os.listdir(str(src))):
        sub = os.path.join(str(src), det_dir)
        if not os.path.isdir(sub):
            continue
        os.makedirs(os.path.join(staging, det_dir), exist_ok=True)
        for fn in sorted(os.listdir(sub)):
            # <det>_<filt>_wisp.fits[.gz] -> uppercase only the filter token.
            parts = fn.split('_')
            if len(parts) < 3:
                continue
            parts[1] = parts[1].upper()
            link = os.path.join(staging, det_dir, '_'.join(parts))
            try:
                os.symlink(os.path.join(sub, fn), link)
                n += 1
            except FileExistsError:
                pass
    try:
        os.rename(staging, final)
        log(f'nmfwisp case shim: built {n} uppercase symlinks at {final}')
    except OSError:
        # Another process won the race; drop ours and use theirs.
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
    return final if os.path.isdir(final) else None

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


def _fit_region_mask(tmpl, hsnr, region):
    """Pixels the NMF amplitude solve scores, before source masking.

    ``'hsnr'`` is the template's own ``MASK_hSNR`` extension; ``'tNN'`` is
    pixels above NN% of the summed template's peak.
    """
    tmpl = np.asarray(tmpl, dtype=np.float64)
    if tmpl.ndim == 2:
        tmpl = tmpl[None]
    if region == 'hsnr':
        return np.asarray(hsnr, bool)
    if region.startswith('t') and region[1:].isdigit():
        tot = tmpl.sum(0)
        return tot > (int(region[1:]) / 100.0) * float(np.nanmax(tot))
    raise ValueError(f'unknown nmf_fit_region {region!r}')


def _nmf_amplitudes(sci, err, srcmask, tmpl, wmask, hsnr,
                    region='hsnr', sigma='ivar'):
    """Solve for the NMF template amplitudes.

    Returns ``(W, model, sky, region_used)``. ``region_used`` is the region the
    solve actually scored — ``region``, or ``'hsnr(fallback)'`` when the
    requested one was too starved to fit, or **None** when even hSNR was, in
    which case ``W`` and ``model`` are zeros and the caller must not treat the
    exposure as subtracted. Reporting it is what keeps ``CFP_WISP`` honest: the
    header would otherwise claim the configured region on an exposure that
    silently fell back to the legacy one.

    campfire's own reproduction of the amplitude solve, so the two things that
    actually set the answer — the *fit region* and the *pixel weighting* — are
    reachable. ``nmfwisp`` stays the template provider; nothing private is
    imported. Defaults reproduce ``nmfwisp.estimate_wisp_standard`` exactly.

    ``region``
        ``'hsnr'``  the template's ``MASK_hSNR`` extension (nmfwisp behaviour).
        ``'tNN'``   pixels where the summed template exceeds NN% of its peak.

        ``MASK_hSNR`` is misnamed: on nrcb4 F200W it is ~511k pixels of which
        ~90% sit below 20% of the template peak. A mirrored-model null test
        (project the data onto a left-right flipped copy of the model, where no
        wisp is) returns a_null of 1.7-70 in those bins — as large as or larger
        than the signal, i.e. they measure large-scale background, not wisp.
        Being ~200x more numerous they set the fit, dragging the filament
        amplitude to ~0.5x and letting NNLS zero the components that carry it.

    ``sigma``
        ``'ivar'``  weight by the ERR array (nmfwisp behaviour).
        ``'flat'``  uniform weight.

        ERR carries a Poisson term computed from the *measured* signal, so
        ``spearman(err, data) = +0.63..+0.70`` on every wisp detector: any pixel
        with a positive excess — the wisp, or an unmasked source wing — gets a
        larger err and is down-weighted, biasing the amplitude low. With the
        region held fixed this is worth -25% on nrcb3 and -14% on nrca4. It is
        independent of the region and pushes the same way (under-subtraction).
    """
    from scipy.optimize import nnls

    tmpl = np.asarray(tmpl, dtype=np.float64)
    if tmpl.ndim == 2:
        tmpl = tmpl[None]

    # nmfwisp.process_data: refpix border + non-finite are hard-masked, the sky
    # is one median over everything that is neither source nor wisp.
    bad = ~np.isfinite(sci) | ~np.isfinite(err) | (err <= 0)
    bad[:4] = bad[-4:] = True
    bad[:, :4] = bad[:, -4:] = True
    mask = np.asarray(srcmask, bool) | bad
    ref = ~(mask | np.asarray(wmask, bool))
    sky = float(np.nanmedian(sci[ref])) if ref.any() else 0.0

    reg = _fit_region_mask(tmpl, hsnr, region)

    fit = reg & ~mask
    ncomp = tmpl.shape[0]
    used = region
    if fit.sum() < 50 * ncomp:
        log(f'NMF fit region has only {int(fit.sum())} usable pixels for '
            f'{ncomp} component(s); falling back to the full hSNR region')
        fit = np.asarray(hsnr, bool) & ~mask
        used = 'hsnr(fallback)'
        if fit.sum() < 50 * ncomp:
            return np.zeros(ncomp), np.zeros_like(sci, dtype=np.float64), sky, None

    if sigma == 'flat':
        sig = np.ones(int(fit.sum()), dtype=np.float64)
    elif sigma == 'ivar':
        sig = err[fit].astype(np.float64)
    else:
        raise ValueError(f'unknown nmf_fit_sigma {sigma!r}')

    A = (tmpl[:, fit] / sig[None, :]).T
    b = (sci[fit] - sky) / sig
    ok = np.isfinite(b) & np.all(np.isfinite(A), axis=1)
    if ok.sum() < 50 * ncomp:
        return np.zeros(ncomp), np.zeros_like(sci, dtype=np.float64), sky, None

    W, _ = nnls(A[ok], b[ok])
    return W, np.einsum('i,imn->mn', W, tmpl), sky, used


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
    from jwst.datamodels import ImageModel

    plot = step_config.get('plot', True)
    correct_1f = step_config.get('nmf_correct_1f', False)
    # 'hsnr'/'ivar' reproduce nmfwisp exactly; the region default is t50 (see
    # config_default.toml for why). Both land verbatim in CFP_WISP below.
    fit_region_name = step_config.get('nmf_fit_region', 't50')
    fit_sigma = step_config.get('nmf_fit_sigma', 'ivar')
    mask_nsigma = step_config.get('mask_nsigma', 3.0)
    mask_dilate = step_config.get('mask_dilate', 8)
    mask_passes = max(int(step_config.get('mask_iterations', 5)), 1)

    log(f"Running NMF wisp subtraction on {rootname}")
    model = ImageModel(exposure_file, memmap=False)
    sci_before = model.data.copy()

    # wisp_path is the case shim when the installed nmfwisp needs it, else None
    # (upstream default). See _nmfwisp_case_shim.
    shim = _nmfwisp_case_shim()

    mask, found = _source_mask(model.data, nsigma=mask_nsigma,
                               dilate=mask_dilate)
    if not found:
        log(f"Source detection found nothing for {rootname}; "
            "fitting NMF without a source mask")

    if correct_1f:
        # The 1/f path lives entirely inside nmfwisp; no campfire equivalent.
        # fit_wisp always runs nmfwisp's own hSNR/ivar solve, so the configured
        # region/sigma are NOT honoured here — record what actually ran, not
        # what was asked for, or the header would claim a calibration this
        # exposure never received.
        from nmfwisp import fit_wisp
        wisp, _wisp_e = fit_wisp(
            sci_before, model.err, mask,
            wisp_path=shim,
            detector_name=detector, filter_name=filtname.upper(),
            correct_1f=True,
        )
        W = None
        if (fit_region_name, fit_sigma) != ('hsnr', 'ivar'):
            log(f'nmf_correct_1f=True delegates to nmfwisp, which always fits '
                f'hsnr/ivar; the configured {fit_region_name}/{fit_sigma} is '
                f'not applied and is not recorded for {rootname}')
        region_used, sigma_used = 'hsnr', 'ivar'
        n_passes = 1          # nmfwisp owns the solve; no mask iteration here
    else:
        from nmfwisp.nmfwisp import load_wisp_templates
        tmpl, _terr, wmask, hsnr = load_wisp_templates(
            shim, detector, filtname.upper())
        W, wisp, _sky, region_used = _nmf_amplitudes(
            sci_before, model.err, mask, tmpl, wmask, hsnr,
            region=fit_region_name, sigma=fit_sigma)
        sigma_used = fit_sigma

        # Re-detect sources on the wisp-SUBTRACTED frame and re-fit, until the
        # model stops moving. The first-pass mask is built on a frame that
        # still contains the wisp, so ``detect_sources`` sees the wisp itself
        # as a source and masks the very pixels the amplitude solve scores.
        # The bias scales with wisp brightness and is self-defeating at the
        # bright end: on A2744/F200W nrcb4 the wisp core runs 7.6 sigma above
        # sky against a 3-sigma detection threshold, so 97.6% of t50 is
        # detected, dilation takes the rest, and ``t50 & ~mask`` collapses to
        # ZERO usable pixels -- the solve then falls back to the whole hSNR
        # region, which is ~90% background and answers with a spurious broad
        # component that digs a -0.006..-0.014 MJy/sr bowl over the wisp
        # footprint. That is exactly the failure the t50 default exists to
        # avoid, re-entered through the fallback.
        #
        # Subtracting even a bad first model drops the wisp below the
        # detection threshold, so pass 2 recovers the region and the fit
        # converges in 2-4 passes. It is seed-independent: starting from the
        # hSNR fallback and from a t30 fit land within 2% of each other.
        # ``mask_iterations = 1`` restores the single-pass behaviour.
        n_passes = 1
        for _ in range(mask_passes - 1):
            if region_used is None:
                break                      # nothing subtracted; mask can't move
            # nan_to_num for the same reason the final subtraction does it: a
            # NaN in the model would turn that pixel non-finite and _source_mask
            # would mask it, quietly shrinking the fit region pass by pass.
            mask_i, _found_i = _source_mask(
                sci_before - np.nan_to_num(wisp, nan=0.0),
                nsigma=mask_nsigma, dilate=mask_dilate)
            W_i, wisp_i, _sky_i, used_i = _nmf_amplitudes(
                sci_before, model.err, mask_i, tmpl, wmask, hsnr,
                region=fit_region_name, sigma=fit_sigma)
            if used_i is None:
                # A later pass starving is not a reason to throw away a fit
                # that worked; keep the previous pass and stop.
                break
            # Converge on the MODEL, not on W. The components are near-
            # degenerate and NNLS trades amplitude between them freely --
            # nrca3 walks [0.76,0.62,1.50] -> [1.43,0,0.89] -> [1.72,0,0]
            # while the model it implies moves by <1%. The model is what gets
            # subtracted, so 1% of its peak (far under the pixel noise) is the
            # criterion that matters.
            #
            # Scale on BOTH models, not just the new one. NNLS can drive every
            # component to the boundary on a low-wisp frame, and scaling on the
            # new peak alone would make that all-zero model look converged
            # against any predecessor -- replacing a good fit with "subtract
            # nothing" and stamping it as processed. Zero is only settled when
            # the previous model was zero too.
            scale = max(float(np.nanmax(np.abs(wisp_i))),
                        float(np.nanmax(np.abs(wisp))))
            settled = (np.nanmax(np.abs(wisp_i - wisp)) <= 0.01 * scale
                       if scale > 0 else True)
            W, wisp, mask, region_used = W_i, wisp_i, mask_i, used_i
            n_passes += 1
            if settled:
                break
        else:
            if mask_passes > 1:
                log(f'NMF mask iteration did not settle in {mask_passes} '
                    f'passes for {rootname}; using the last fit')

        if region_used is None:
            # Nothing left to fit. Subtracting the all-zero model and stamping
            # success would mark an UNSUBTRACTED exposure as processed, and
            # should_skip would then never revisit it. Leave SCI untouched and
            # stamp a distinct sentinel instead.
            log(f'NMF fit region is exhausted for {rootname} (source mask '
                f'leaves too few pixels in both {fit_region_name} and hSNR); '
                f'stamping skipped, SCI left untouched')
            # model.data is still pristine here — the subtraction happens below
            atomic_save(
                model, exposure_file,
                header_updates=cfp.format(
                    CFP_WISP=f'skipped (fit region exhausted: '
                             f'{fit_region_name})'),
            )
            model.close()
            return
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

    # Record what the fit DECIDED, not just that it ran. The model is exactly
    # W . templates over versioned reference data, so storing W makes this
    # otherwise-destructive step invertible: the pre-wisp frame is recoverable
    # from a deployed product. It is also the only way to tell an old-fit
    # product from a new-fit one, since the nmfwisp version string does not
    # change when the region/sigma defaults do.
    prov = (f'nmf {ver} region={region_used} sigma={sigma_used} '
            f'passes={n_passes}')
    if W is not None:
        prov += ' W=' + ','.join(f'{x:.5g}' for x in np.atleast_1d(W))
    else:
        prov += ' correct_1f=True'

    atomic_save(
        model, exposure_file,
        header_updates=cfp.format(CFP_WISP=prov),
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
