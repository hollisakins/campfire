"""SEP segmentation detection + calibrated magnitudes for the NIRCam ``align``
phase.

Produces ``(x, y)`` source positions plus Kron photometry for a detector image
— the image-side catalog that the align solve worker projects to the tangent
plane and matches against the reference catalog.

**Same recipe as the refcat, deliberately.** Detection mirrors the refcat
build's SEP-on-SNR segmentation (``refcat/extract.py``) via the shared
:func:`~campfire_pipeline.nircam.refcat.extract.sep_extract_sources` core, so
image-side detections correspond to refcat entries by construction. The
previous point-source finder (``DAOStarFinder``) was retired after the COSMOS
A2/A3 misregistration: over a bright extended galaxy it returned thousands of
*bright* spurious peaks (star-forming clumps, substructure) that trace the same
galaxy clustering as the refcat, and the coarse gross-shift 2-D offset
histogram then grew clustering-scale peaks that out-voted the true (0,0) peak
inside ``coarse_searchrad`` — dragging well-pointed exposures tens of arcsec.
Segmentation detects the galaxies themselves (one detection per refcat-like
object), which removes the mismatch at the source rather than capping the
search radius.

``mag`` is **calibrated AB** (Kron flux + the JHAT zeropoint convention: for
MJy/sr cal frames ``ZP = -2.5·log10(PIXAR_SR · 1e6 / 3631)``) when a
*zeropoint* is given; otherwise it falls back to the uncalibrated
``-2.5·log10(flux)`` and the ``objmag_lim`` cut is *skipped loudly* (an AB
window applied to instrumental mags would cut everything).
``table.meta['mag_calibrated']`` records which, and the solve's
``delta_mag_lim`` pair cut only ever consumes calibrated mags. No aperture
correction is applied (irrelevant to the wide ``objmag_lim``/``delta_mag_lim``
windows, and ill-defined for galaxies).

Coordinates are **0-indexed** detector pixels (SEP's, windowed positions — the
gwcs convention ``tweakwcs``'s ``det_to_world`` / ``det_to_tanp`` expect).

Import-light and ``jwst``-free: detection is WCS-free (works in detector
pixels), so it reads SCI/ERR/DQ via plain ``astropy.io.fits`` rather than a
jwst ``ImageModel``; ``sep`` is imported lazily inside the shared core.
"""

import numpy as np
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.table import Table

from campfire_pipeline.common.io import log

# jwst.datamodels.dqflags.pixel bit values, hardcoded to keep this module free
# of the jwst/CRDS import chain (cf. field.py's _DO_NOT_USE). Detection must
# skip pixels that are unusable (DO_NOT_USE), saturated (a saturated core
# corrupts both the position and the flux), or lack a valid nonlinearity
# correction (NO_LIN_CORR) — the latter two are exactly the bright-star failure
# modes a magnitude cut can't catch because the measured flux is already wrong.
_DO_NOT_USE = np.uint32(1)
_SATURATED = np.uint32(2)
_NO_LIN_CORR = np.uint32(1 << 20)          # 1048576
DETECT_DQ_BITS = _DO_NOT_USE | _SATURATED | _NO_LIN_CORR

_FLOAT_COLUMNS = ('x', 'y', 'flux', 'fluxerr', 'mag', 'snr')

# Warn at most once per process when an objmag_lim is configured but the frame
# provided no AB zeropoint (the cut would be nonsense on instrumental mags).
_UNCAL_OBJMAG_WARNED = False


def _empty_catalog(mag_calibrated=False):
    cols = {c: np.array([], dtype=float) for c in _FLOAT_COLUMNS}
    cols['npix'] = np.array([], dtype=int)
    t = Table(cols)
    t.meta['mag_calibrated'] = bool(mag_calibrated)
    return t


def detect_sources(sci, err=None, *, mask=None, fwhm=1.5, snr_thresh=3.0,
                   minarea=15, deblend_nthresh=32, deblend_cont=0.001,
                   edge=8, snr_min=10.0, objmag_lim=None, zeropoint=None):
    """Detect sources on a detector image by SEP segmentation of the SNR map.

    Returns an astropy ``Table`` with columns ``x, y, flux, fluxerr, mag, snr,
    npix`` (0-indexed pixels, windowed positions), sorted by ``flux``
    descending; an empty (but typed) Table when nothing is found.
    ``table.meta['mag_calibrated']`` records whether ``mag`` is calibrated AB
    (Kron flux + *zeropoint*) or the uncalibrated ``-2.5·log10(flux)``.

    Parameters
    ----------
    sci : 2-D array
        Detector SCI image.
    err : 2-D array, optional
        Per-pixel 1-sigma error map; detection thresholds the ``sci/err`` SNR
        map. When absent (or carrying no positive pixels), a sigma-clipped
        global RMS of ``sci`` stands in as a constant error — degraded but
        functional.
    mask : 2-D bool array, optional
        Pixels to ignore (bad pixels, off-detector). Non-finite ``sci``/``err``
        pixels are always excluded.
    fwhm : float
        Matched-filter Gaussian kernel FWHM in pixels — should track the PSF
        core, which is filter-dependent, so the align worker keys this off the
        exposure's filter (``psf_fwhm_by_filter``) rather than one value.
    snr_thresh : float
        Per-pixel detection threshold on the SNR map.
    minarea : int
        Minimum number of connected pixels above ``snr_thresh``.
    deblend_nthresh, deblend_cont : int, float
        SEP deblender knobs.
    edge : int
        Reject detections within this many pixels of any border (unreliable).
    snr_min : float, optional
        Drop sources whose *integrated* flux SNR (``flux/fluxerr``) is below
        this — the refcat build applies the same cut, keeping the two catalogs
        selection-matched. ``None`` disables.
    objmag_lim : (float, float), optional
        Keep only ``bright <= mag <= faint`` in **calibrated AB mag** (JHAT's
        ``objmag_lim``; its validated COSMOS window is ``[19, 28]``). Applied
        only when *zeropoint* is available — on an uncalibrated frame the cut
        is skipped with a loud (once-per-process) log, because an AB window on
        instrumental mags would cut everything.
    zeropoint : float, optional
        AB zeropoint for one image unit of integrated flux: ``mag = zeropoint
        - 2.5·log10(flux)``. For jwst cal frames in MJy/sr, ``zeropoint =
        -2.5·log10(PIXAR_SR · 1e6 / 3631)`` (see
        :func:`ab_zeropoint_from_sci_header`).
    """
    from campfire_pipeline.nircam.refcat.extract import sep_extract_sources

    sci = np.asarray(sci, dtype=float)
    if mask is not None:
        mask = np.asarray(mask, dtype=bool)
    calibrated = zeropoint is not None

    if err is None or not np.any(np.isfinite(err) & (np.asarray(err) > 0)):
        _, _, std = sigma_clipped_stats(sci, mask=mask, sigma=3.0, maxiters=5)
        if not np.isfinite(std) or std <= 0:
            log("detect: no usable ERR and non-finite/zero background noise; "
                "skipping detection.")
            return _empty_catalog(calibrated)
        log("detect: no usable ERR map; using a constant sigma-clipped "
            f"RMS ({std:.4g}) as the error map.")
        err = np.full(sci.shape, float(std))

    cat = sep_extract_sources(sci, err, mask=mask, snr_thresh=snr_thresh,
                              minarea=minarea, deblend_nthresh=deblend_nthresh,
                              deblend_cont=deblend_cont, filter_fwhm=fwhm)
    if len(cat) == 0:
        return _empty_catalog(calibrated)

    x = np.asarray(cat['x'], dtype=float)
    y = np.asarray(cat['y'], dtype=float)
    flux = np.asarray(cat['flux'], dtype=float)
    fluxerr = np.asarray(cat['fluxerr'], dtype=float)
    with np.errstate(divide='ignore', invalid='ignore'):
        snr = flux / fluxerr
        mag = np.where(flux > 0, -2.5 * np.log10(np.abs(flux)), np.nan)
        if calibrated:
            mag = mag + float(zeropoint)

    ny, nx = sci.shape
    keep = (np.isfinite(x) & np.isfinite(y) &
            (x >= edge) & (x <= nx - 1 - edge) &
            (y >= edge) & (y <= ny - 1 - edge))
    if snr_min is not None:
        keep &= np.isfinite(snr) & (snr >= float(snr_min))
    if objmag_lim is not None:
        if calibrated:
            bright, faint = float(objmag_lim[0]), float(objmag_lim[1])
            keep &= np.isfinite(mag) & (mag >= bright) & (mag <= faint)
        else:
            _warn_uncalibrated_objmag()

    out = Table({
        'x': x[keep], 'y': y[keep],
        'flux': flux[keep], 'fluxerr': fluxerr[keep],
        'mag': np.asarray(mag, dtype=float)[keep],
        'snr': np.asarray(snr, dtype=float)[keep],
        'npix': np.asarray(cat['npix'], dtype=int)[keep],
    })
    out.meta['mag_calibrated'] = calibrated
    if len(out) == 0:
        return _empty_catalog(calibrated)
    out.sort('flux', reverse=True)
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


def detect_in_exposure(path, **kwargs):
    """Detect sources on a canonical NIRCam exposure's SCI image.

    Reads SCI/ERR/DQ via ``astropy.io.fits`` (no jwst datamodel needed —
    detection is WCS-free and works in detector pixels). Masks off-detector
    pixels (non-finite ERR) and the ``DETECT_DQ_BITS`` DQ pixels (DO_NOT_USE +
    SATURATED + NO_LIN_CORR — unusable, saturated, or nonlinearity-uncorrected;
    JUMP_DET etc. are already-corrected and kept). ERR/DQ are read defensively
    (skipped if absent). The AB zeropoint is resolved from the SCI header
    (:func:`ab_zeropoint_from_sci_header`) unless the caller passes one. Extra
    keyword arguments pass through to :func:`detect_sources`.
    """
    with fits.open(path, memmap=False) as hdul:
        sci = np.asarray(hdul['SCI'].data, dtype=float)
        kwargs.setdefault('zeropoint',
                          ab_zeropoint_from_sci_header(hdul['SCI'].header))
        mask = ~np.isfinite(sci)
        err = None
        if 'ERR' in hdul and hdul['ERR'].data is not None:
            err = np.asarray(hdul['ERR'].data, dtype=float)
            mask |= ~np.isfinite(err)
        if 'DQ' in hdul and hdul['DQ'].data is not None:
            dq = np.asarray(hdul['DQ'].data).astype(np.uint32)
            mask |= (dq & DETECT_DQ_BITS) != 0
    return detect_sources(sci, err, mask=mask, **kwargs)
