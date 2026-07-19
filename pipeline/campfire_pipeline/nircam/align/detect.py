"""Point-source detection + calibrated magnitudes for the NIRCam ``align`` phase.

Produces ``(x, y)`` star centroids plus magnitudes for a detector image — the
image-side catalog that the align solve worker projects to the tangent plane
and matches against the Gaia-tied reference catalog.

**Annulus-free aperture photometry.** JHAT's sky *annulus*, run on CAMPFIRE's
already sky-subtracted frames, averages *negative* for ~46% of detections and
trips a ``-99.99`` sky sentinel, producing a spurious constant magnitude that
floods the matcher (handoff §2a). Detection is ``photutils.DAOStarFinder``
(no annulus, structurally immune), and when a *zeropoint* is supplied the
magnitudes come from a plain circular-aperture sum (default radius 2×FWHM,
JHAT's ``radii_Nfwhm=[2.0]``) on the median-subtracted frame with **no local
annulus** — the frames are already sky-flat, so the annulus is exactly the
piece of JHAT photometry that must NOT be ported. No aperture correction is
applied (a ~0.2–0.4 mag point-source offset, irrelevant to the wide
``objmag_lim``/``delta_mag_lim`` windows, and ill-defined for galaxies).

``mag`` is **calibrated AB** when *zeropoint* is given (for the MJy/sr cal
frames: ``ZP = -2.5·log10(PIXAR_SR · 1e6 / 3631)``, the same surface-brightness
× pixel-area conversion JHAT uses); otherwise it falls back to the uncalibrated
DAOStarFinder kernel estimate and the ``objmag_lim`` cut is *skipped loudly*
(an AB window applied to instrumental mags would cut everything).

Coordinates are **0-indexed** detector pixels (the ``DAOStarFinder`` and gwcs
convention — what the ``tweakwcs`` corrector's ``det_to_world`` /
``det_to_tanp`` expect).

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

_FLOAT_COLUMNS = ('x', 'y', 'flux', 'mag', 'aper_flux', 'sharpness',
                  'roundness1', 'roundness2', 'peak')

# Warn at most once per process when an objmag_lim is configured but the frame
# provided no AB zeropoint (the cut would be nonsense on instrumental mags).
_UNCAL_OBJMAG_WARNED = False


def _empty_catalog(mag_calibrated=False):
    cols = {c: np.array([], dtype=float) for c in _FLOAT_COLUMNS}
    cols['npix'] = np.array([], dtype=int)
    t = Table(cols)
    t.meta['mag_calibrated'] = bool(mag_calibrated)
    return t


def detect_star_centroids(data, *, mask=None, fwhm=2.5, nsigma=5.0,
                          sharplo=0.2, sharphi=1.0, roundlo=-1.0, roundhi=1.0,
                          edge=8, snr_min=None, objmag_lim=None, brightest=None,
                          zeropoint=None, aper_radius_px=None,
                          sigma=3.0, maxiters=5):
    """Detect point-source centroids on a detector image.

    Returns an astropy ``Table`` with columns ``x, y, flux, mag, aper_flux,
    sharpness, roundness1, roundness2, peak, npix`` (0-indexed pixels), sorted
    by ``flux`` descending; an empty (but typed) Table when nothing is found.
    ``table.meta['mag_calibrated']`` records whether ``mag`` is calibrated AB
    (aperture sum + *zeropoint*) or the uncalibrated DAO kernel estimate.

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
        Keep only ``bright <= mag <= faint`` in **calibrated AB mag** (JHAT's
        ``objmag_lim``; its validated COSMOS window is ``[19, 28]``). Applied
        only when *zeropoint* is available — on an uncalibrated frame the cut
        is skipped with a loud (once-per-process) log, because an AB window on
        instrumental mags would cut everything.
    brightest : int, optional
        Keep only the ``brightest`` sources (by flux) after all cuts. The align
        path leaves this unset so the full quality-selected catalog reaches the
        solve.
    zeropoint : float, optional
        AB zeropoint for one image unit in an aperture sum: ``mag = zeropoint
        - 2.5·log10(aperture_sum)``. For jwst cal frames in MJy/sr,
        ``zeropoint = -2.5·log10(PIXAR_SR · 1e6 / 3631)`` (see
        :func:`ab_zeropoint_from_sci_header`). When given, ``mag`` is measured
        by annulus-free circular-aperture photometry (module docstring).
    aper_radius_px : float, optional
        Aperture radius (pixels) for the calibrated photometry; default
        ``2 × fwhm`` (JHAT's ``radii_Nfwhm = [2.0]``).
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

    calibrated = zeropoint is not None
    out = Table({
        'x': x[keep],
        'y': y[keep],
        'flux': np.asarray(sources['flux'], dtype=float)[keep],
        'mag': np.asarray(sources['mag'], dtype=float)[keep],
        'aper_flux': np.full(int(np.count_nonzero(keep)), np.nan),
        'sharpness': np.asarray(sources['sharpness'], dtype=float)[keep],
        'roundness1': np.asarray(sources['roundness1'], dtype=float)[keep],
        'roundness2': np.asarray(sources['roundness2'], dtype=float)[keep],
        'peak': np.asarray(sources['peak'], dtype=float)[keep],
        'npix': np.asarray(sources['npix'], dtype=int)[keep],
    })
    out.meta['mag_calibrated'] = calibrated
    if len(out) == 0:
        return _empty_catalog(calibrated)

    if calibrated:
        # Annulus-free aperture photometry on the median-subtracted frame
        # (module docstring); non-positive sums -> NaN mag (dropped by an
        # objmag_lim cut, passed through otherwise).
        from photutils.aperture import CircularAperture, aperture_photometry
        radius = float(aper_radius_px) if aper_radius_px else 2.0 * float(fwhm)
        aper = CircularAperture(
            np.column_stack([np.asarray(out['x']), np.asarray(out['y'])]),
            r=radius)
        phot = aperture_photometry(data - median, aper, method='exact',
                                   mask=mask)
        aper_sum = np.asarray(phot['aperture_sum'], dtype=float)
        out['aper_flux'] = aper_sum
        with np.errstate(divide='ignore', invalid='ignore'):
            out['mag'] = np.where(
                aper_sum > 0,
                float(zeropoint) - 2.5 * np.log10(aper_sum), np.nan)

    # Quality cuts. sharpness/roundness are already applied inside DAOStarFinder;
    # snr_min and objmag_lim trim what the kernel threshold alone can't.
    quality = np.ones(len(out), dtype=bool)
    if snr_min is not None:
        quality &= (np.asarray(out['peak'], dtype=float) / std) >= float(snr_min)
    if objmag_lim is not None:
        if calibrated:
            bright, faint = float(objmag_lim[0]), float(objmag_lim[1])
            mag = np.asarray(out['mag'], dtype=float)
            quality &= np.isfinite(mag) & (mag >= bright) & (mag <= faint)
        else:
            _warn_uncalibrated_objmag()
    out = out[quality]
    if len(out) == 0:
        return _empty_catalog(calibrated)

    out.sort('flux', reverse=True)
    if brightest is not None and len(out) > brightest:
        out = out[:int(brightest)]
    return out


def _warn_uncalibrated_objmag():
    """Log (once per process) that objmag_lim was skipped for lack of an AB
    zeropoint — applying an AB window to instrumental mags would cut all
    sources, which is worse than not cutting."""
    global _UNCAL_OBJMAG_WARNED
    if _UNCAL_OBJMAG_WARNED:
        return
    _UNCAL_OBJMAG_WARNED = True
    log("detect: objmag_lim is set but no AB zeropoint is available "
        "(missing/unknown PIXAR_SR or BUNIT != MJy/sr); SKIPPING the "
        "magnitude cut. Logged once per process.")


def ab_zeropoint_from_sci_header(header):
    """AB zeropoint for a jwst cal SCI extension header, or ``None``.

    Requires ``BUNIT = MJy/sr`` and a positive ``PIXAR_SR``; then one image
    unit summed over pixels is ``PIXAR_SR·1e6`` Jy, so
    ``ZP = -2.5·log10(PIXAR_SR · 1e6 / 3631)`` (JHAT's surface-brightness ×
    pixel-area conversion, ~26.5 for the 0.063" LW pixel).
    """
    bunit = str(header.get('BUNIT', '')).replace(' ', '').lower()
    pixar_sr = header.get('PIXAR_SR')
    if bunit != 'mjy/sr' or pixar_sr is None:
        return None
    pixar_sr = float(pixar_sr)
    if not np.isfinite(pixar_sr) or pixar_sr <= 0:
        return None
    return -2.5 * np.log10(pixar_sr * 1.0e6 / 3631.0)


def detect_in_exposure(path, *, fwhm=2.5, nsigma=5.0, **kwargs):
    """Detect star centroids on a canonical NIRCam exposure's SCI image.

    Reads SCI/ERR/DQ via ``astropy.io.fits`` (no jwst datamodel needed —
    detection is WCS-free and works in detector pixels). Masks off-detector
    pixels (non-finite ERR) and the ``DETECT_DQ_BITS`` DQ pixels (DO_NOT_USE +
    SATURATED + NO_LIN_CORR — unusable, saturated, or nonlinearity-uncorrected;
    JUMP_DET etc. are already-corrected and kept). ERR/DQ are read defensively
    (skipped if absent). The AB zeropoint is resolved from the SCI header
    (:func:`ab_zeropoint_from_sci_header`) unless the caller passes one. Extra
    keyword arguments pass through to :func:`detect_star_centroids`.
    """
    with fits.open(path, memmap=False) as hdul:
        sci = np.asarray(hdul['SCI'].data, dtype=float)
        kwargs.setdefault('zeropoint',
                          ab_zeropoint_from_sci_header(hdul['SCI'].header))
        mask = ~np.isfinite(sci)
        if 'ERR' in hdul and hdul['ERR'].data is not None:
            mask |= ~np.isfinite(np.asarray(hdul['ERR'].data, dtype=float))
        if 'DQ' in hdul and hdul['DQ'].data is not None:
            dq = np.asarray(hdul['DQ'].data).astype(np.uint32)
            mask |= (dq & DETECT_DQ_BITS) != 0
    return detect_star_centroids(sci, mask=mask, fwhm=fwhm, nsigma=nsigma,
                                 **kwargs)
