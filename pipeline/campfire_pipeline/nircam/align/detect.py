"""Centroid-only point-source detection for the NIRCam ``align`` phase.

Produces ``(x, y)`` star centroids plus a brightness proxy for a detector image
— the image-side catalog that the align solve worker projects to the tangent
plane and triangle-matches against the Gaia-tied reference catalog.

**Centroids only, no aperture photometry.** JHAT's aperture annulus, run on
CAMPFIRE's already sky-subtracted frames, averages *negative* for ~46% of
detections and trips a ``-99.99`` sky sentinel, producing a spurious constant
magnitude that floods the matcher (handoff §2a). A peak / PSF-fit finder
(``photutils.DAOStarFinder``) has no sky annulus and structurally cannot hit
that bug — its ``flux``/``mag`` come from the PSF fit, not a background-
subtracted aperture.

Coordinates are **0-indexed** detector pixels (the ``DAOStarFinder`` and gwcs
convention — what the ``tweakwcs`` corrector's ``det_to_world`` / ``det_to_tanp``
expect). ``mag = -2.5·log10(flux)`` (smaller = brighter) rides along **only**
to rank the bootstrap triangle-vertex cap (``matcher.py``), never as a match
constraint.

Import-light and ``jwst``-free: detection is WCS-free (works in detector
pixels), so it reads SCI/ERR/DQ via plain ``astropy.io.fits`` rather than a
jwst ``ImageModel``.
"""

import warnings

import numpy as np
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.table import Table
from photutils.detection import DAOStarFinder
from photutils.utils.exceptions import NoDetectionsWarning

from campfire_pipeline.common.io import log

# jwst.datamodels.dqflags.pixel bit values, hardcoded to keep this module free
# of the jwst/CRDS import chain (cf. field.py's _DO_NOT_USE). Centroiding must
# skip pixels that are unusable (DO_NOT_USE), saturated (a saturated core
# corrupts both the centroid and the kernel flux), or lack a valid nonlinearity
# correction (NO_LIN_CORR) — the latter two are exactly the bright-star failure
# modes a magnitude cut can't catch because the measured flux is already wrong.
_DO_NOT_USE = np.uint32(1)
_SATURATED = np.uint32(2)
_NO_LIN_CORR = np.uint32(1 << 20)          # 1048576
DETECT_DQ_BITS = _DO_NOT_USE | _SATURATED | _NO_LIN_CORR

_FLOAT_COLUMNS = ('x', 'y', 'flux', 'mag', 'sharpness',
                  'roundness1', 'roundness2', 'peak')


def _empty_catalog():
    cols = {c: np.array([], dtype=float) for c in _FLOAT_COLUMNS}
    cols['npix'] = np.array([], dtype=int)
    return Table(cols)


def detect_star_centroids(data, *, mask=None, fwhm=2.5, nsigma=5.0,
                          sharplo=0.2, sharphi=1.0, roundlo=-1.0, roundhi=1.0,
                          edge=8, snr_min=None, objmag_lim=None, brightest=None,
                          sigma=3.0, maxiters=5):
    """Detect point-source centroids on a detector image.

    Returns an astropy ``Table`` with columns ``x, y, flux, mag, sharpness,
    roundness1, roundness2, peak, npix`` (0-indexed pixels), sorted by ``flux``
    descending; an empty (but typed) Table when nothing is found.

    Parameters
    ----------
    data : 2-D array
        Detector SCI image (any residual smooth background is removed by the
        scalar median subtraction below; DAOStarFinder's kernel suppresses the
        rest).
    mask : 2-D bool array, optional
        Pixels to ignore (bad pixels, off-detector). Non-finite ``data`` pixels
        are always added to the mask.
    fwhm : float
        PSF FWHM in pixels. NIRCam's PSF core is filter-dependent (F070W→F480M),
        so the align worker keys this off the exposure's filter
        (``psf_fwhm_by_filter``) rather than using one value.
    nsigma : float
        Detection threshold in units of the sigma-clipped background RMS.
    edge : int
        Reject centroids within this many pixels of any border (unreliable).
    snr_min : float, optional
        Drop detections below this peak SNR (``peak / background_rms``). Raises
        the effective floor above the kernel ``nsigma`` threshold; a matched-
        filter detection can trip ``nsigma`` on a noise peak whose real SNR is
        marginal.
    objmag_lim : (float, float), optional
        Keep only ``bright <= mag <= faint``. ``mag = -2.5·log10(flux)`` is the
        DAOStarFinder kernel estimate — **uncalibrated** (no zeropoint), so this
        is a coarse per-run trim (drop the saturated-bright and the faint junk),
        not a physical magnitude cut; prefer ``snr_min`` + DQ saturation masking
        for physically meaningful cuts.
    brightest : int, optional
        Keep only the ``brightest`` sources (by flux) after all cuts. The align
        path leaves this unset — the triangle-vertex cap is applied later, on
        the bootstrap only (see ``matcher.py``) — so the full quality-selected
        catalog reaches the all-source refine.
    """
    data = np.asarray(data, dtype=float)
    mask = np.zeros(data.shape, dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
    mask = mask | ~np.isfinite(data)

    mean, median, std = sigma_clipped_stats(data, mask=mask, sigma=sigma,
                                            maxiters=maxiters)
    if not np.isfinite(std) or std <= 0:
        log("detect: non-finite/zero background noise; skipping detection.")
        return _empty_catalog()

    finder = DAOStarFinder(threshold=nsigma * float(std), fwhm=fwhm,
                           sharplo=sharplo, sharphi=sharphi,
                           roundlo=roundlo, roundhi=roundhi)
    with warnings.catch_warnings():
        # A starved detector legitimately finds nothing — not worth a warning.
        warnings.simplefilter('ignore', NoDetectionsWarning)
        sources = finder(data - median, mask=mask)
    if sources is None or len(sources) == 0:
        return _empty_catalog()

    x = np.asarray(sources['xcentroid'], dtype=float)
    y = np.asarray(sources['ycentroid'], dtype=float)
    ny, nx = data.shape
    keep = ((x >= edge) & (x <= nx - 1 - edge) &
            (y >= edge) & (y <= ny - 1 - edge))

    out = Table({
        'x': x[keep],
        'y': y[keep],
        'flux': np.asarray(sources['flux'], dtype=float)[keep],
        'mag': np.asarray(sources['mag'], dtype=float)[keep],
        'sharpness': np.asarray(sources['sharpness'], dtype=float)[keep],
        'roundness1': np.asarray(sources['roundness1'], dtype=float)[keep],
        'roundness2': np.asarray(sources['roundness2'], dtype=float)[keep],
        'peak': np.asarray(sources['peak'], dtype=float)[keep],
        'npix': np.asarray(sources['npix'], dtype=int)[keep],
    })
    if len(out) == 0:
        return _empty_catalog()

    # Quality cuts. sharpness/roundness are already applied inside DAOStarFinder;
    # snr_min and objmag_lim trim what the kernel threshold alone can't.
    quality = np.ones(len(out), dtype=bool)
    if snr_min is not None:
        quality &= (np.asarray(out['peak'], dtype=float) / std) >= float(snr_min)
    if objmag_lim is not None:
        bright, faint = float(objmag_lim[0]), float(objmag_lim[1])
        mag = np.asarray(out['mag'], dtype=float)
        quality &= np.isfinite(mag) & (mag >= bright) & (mag <= faint)
    out = out[quality]
    if len(out) == 0:
        return _empty_catalog()

    out.sort('flux', reverse=True)
    if brightest is not None and len(out) > brightest:
        out = out[:int(brightest)]
    return out


def detect_in_exposure(path, *, fwhm=2.5, nsigma=5.0, **kwargs):
    """Detect star centroids on a canonical NIRCam exposure's SCI image.

    Reads SCI/ERR/DQ via ``astropy.io.fits`` (no jwst datamodel needed —
    detection is WCS-free and works in detector pixels). Masks off-detector
    pixels (non-finite ERR) and the ``DETECT_DQ_BITS`` DQ pixels (DO_NOT_USE +
    SATURATED + NO_LIN_CORR — unusable, saturated, or nonlinearity-uncorrected;
    JUMP_DET etc. are already-corrected and kept). ERR/DQ are read defensively
    (skipped if absent). Extra keyword arguments pass through to
    :func:`detect_star_centroids`.
    """
    with fits.open(path, memmap=False) as hdul:
        sci = np.asarray(hdul['SCI'].data, dtype=float)
        mask = ~np.isfinite(sci)
        if 'ERR' in hdul and hdul['ERR'].data is not None:
            mask |= ~np.isfinite(np.asarray(hdul['ERR'].data, dtype=float))
        if 'DQ' in hdul and hdul['DQ'].data is not None:
            dq = np.asarray(hdul['DQ'].data).astype(np.uint32)
            mask |= (dq & DETECT_DQ_BITS) != 0
    return detect_star_centroids(sci, mask=mask, fwhm=fwhm, nsigma=nsigma,
                                 **kwargs)
