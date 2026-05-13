"""
Build a reference catalog by extracting sources from a mosaic.

The intended use case is bootstrapping the relative astrometric reference
once one filter has been aligned to an absolute frame: e.g. F277W has been
aligned to Gaia, and the resulting F277W mosaic is then the reference for
F115W, F150W, F200W, F444W, etc.

The extraction follows the SEP-on-SNR recipe from
``Dropbox/research/UDS/astrometry.ipynb`` (sigma-clipping the SNR map,
Kron radius + circle-fallback photometry, windowed positions for refined
astrometry, AB magnitudes via ``PIXAR_SR``).
"""

import os
import re

import numpy as np
from astropy.io import fits
from astropy.table import Table
from astropy.wcs import WCS

from campfire_pipeline.common.io import log


# Mosaic naming convention: produced by `cfpipe nircam resample`. See
# pipeline/campfire_pipeline/nircam/steps/resample.py.
#
#   mosaic_nircam_<filter>_<field>_<scale>mas_<version>_<tile>_i2d.fits
#
# Examples:
#   mosaic_nircam_f277w_rj0911_30mas_v0_1_venus_i2d.fits
#   mosaic_nircam_f277w_rj0911_30mas_latest_venus_i2d.fits
_MOSAIC_TEMPLATE = (
    "mosaic_nircam_{filter}_{field}_{scale}_{version}_{tile}_i2d.fits"
)


# ---------------------------------------------------------------------------
# Mosaic resolution + IO
# ---------------------------------------------------------------------------

def resolve_mosaic_path(field, *, filter_name, tile, scale="30mas",
                        version="latest"):
    """Build the canonical mosaic path for ``filter / tile / scale / version``.

    ``field`` is a :class:`Field` with workspace already set up. Returns
    the full path; raises ``FileNotFoundError`` if the mosaic does not
    exist on disk so the caller gets a useful error rather than a confusing
    SEP failure.
    """
    fname = _MOSAIC_TEMPLATE.format(
        filter=filter_name.lower(), field=field.name, scale=scale,
        version=version, tile=tile,
    )
    path = os.path.join(field.filter_dir(filter_name.lower()), fname)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Mosaic not found: {path}")
    return path


def _open_sci_err(mosaic_path, err_path=None):
    """Return ``(sci, err, header, wcs)`` for a mosaic.

    Two conventions are supported:

    1. JWST-style i2d FITS with SCI/ERR (and WHT, optional) extensions.
       This is what ``cfpipe nircam resample`` writes.
    2. ``*_sci.fits`` + sibling ``*_rms.fits`` (or ``*_err.fits``) — the
       layout used by externally-aligned mosaics like the v0p6 UDS
       reductions.

    Pass ``err_path`` to override the error-map auto-detection.
    """
    with fits.open(mosaic_path) as hdul:
        ext_names = [h.name for h in hdul]
        if "SCI" in ext_names:
            sci = hdul["SCI"].data.astype(np.float32)
            header = hdul["SCI"].header
            if err_path is not None:
                err = fits.getdata(err_path).astype(np.float32)
            elif "ERR" in ext_names:
                err = hdul["ERR"].data.astype(np.float32)
            else:
                raise ValueError(
                    f"{mosaic_path}: SCI extension present but no ERR; "
                    "pass --err <path> to point at the error map."
                )
        else:
            sci = hdul[0].data.astype(np.float32)
            header = hdul[0].header
            if err_path is None:
                err_path = _find_sibling_err(mosaic_path)
                if err_path is None:
                    raise ValueError(
                        f"{mosaic_path}: no SCI extension and no sibling "
                        "*_rms.fits / *_err.fits; pass --err <path>."
                    )
            err = fits.getdata(err_path).astype(np.float32)

    # Need PIXAR_SR + BUNIT for the AB-mag conversion. JWST i2d files
    # carry these on the SCI header; legacy primary-array mosaics also.
    wcs = WCS(header)
    return sci, err, header, wcs


def _find_sibling_err(mosaic_path):
    """Locate a sibling ``*_rms.fits`` or ``*_err.fits`` next to ``*_sci.fits``."""
    for suffix in ("_rms.fits", "_err.fits"):
        m = re.match(r"^(.+)_sci\.fits$", os.path.basename(mosaic_path))
        if m:
            cand = os.path.join(os.path.dirname(mosaic_path),
                                m.group(1) + suffix)
            if os.path.exists(cand):
                return cand
    return None


# ---------------------------------------------------------------------------
# Source extraction
# ---------------------------------------------------------------------------

def _ab_conversion_factor(header, wcs):
    """Multiplier that takes per-pixel ``BUNIT=MJy/sr`` flux to ``uJy/pixel``.

    Mirrors the notebook recipe: ``flux_uJy = pixel_val * PIXAR_SR * 1e12``.
    Falls back to a WCS-derived pixel area when PIXAR_SR is absent (e.g.
    cfpipe i2d output that hasn't been stamped) and assumes BUNIT=MJy/sr
    when unset.
    """
    bunit = (header.get("BUNIT") or "").strip()
    if bunit and bunit.lower() not in ("mjy/sr", "mjy / sr"):
        log(f"refcat extract: warning — BUNIT={bunit!r}, expected MJy/sr; "
            "AB-mag conversion may be wrong. Mags are only used as a "
            "selection filter for jhat, so a constant offset is harmless.")
    elif not bunit:
        log("refcat extract: warning — BUNIT missing from header, "
            "assuming MJy/sr (cfpipe convention).")

    pixar_sr = header.get("PIXAR_SR")
    if pixar_sr is None:
        pixar_sr = _wcs_pixel_area_sr(wcs)
        log(f"refcat extract: PIXAR_SR not in header; using WCS-derived "
            f"pixel area = {pixar_sr:.3e} sr.")
    return 1e12 * float(pixar_sr)


def _wcs_pixel_area_sr(wcs):
    """Pixel area in steradians from a 2D celestial WCS.

    Uses ``proj_plane_pixel_scales`` for CD/CDELT-style WCS.
    """
    from astropy.wcs.utils import proj_plane_pixel_scales

    scales_deg = proj_plane_pixel_scales(wcs.celestial)
    pixel_area_deg2 = float(scales_deg[0] * scales_deg[1])
    return pixel_area_deg2 * (np.pi / 180.0) ** 2


def extract_from_mosaic(
    mosaic_path,
    *,
    err_path=None,
    snr_thresh=3.0,
    minarea=15,
    deblend_nthresh=32,
    deblend_cont=0.001,
    filter_fwhm=1.5,
    snr_min=10.0,
    mag_range=None,
):
    """Extract a refcat from a mosaic using SEP-on-SNR.

    Parameters
    ----------
    mosaic_path : str
    err_path : str, optional
        Override the sibling/extension error-map detection.
    snr_thresh : float
        SEP detection threshold in units of the SNR map (the map is
        ``sci/err``, so this is per-pixel SNR).
    minarea : int
        Minimum number of connected pixels above ``snr_thresh``.
    deblend_nthresh, deblend_cont : int, float
        SEP deblender knobs.
    filter_fwhm : float
        Matched-filter Gaussian FWHM (pixels).
    snr_min : float
        Lower SNR cut on the integrated source flux. Drops faint detections
        that hurt the JHAT match.
    mag_range : (min, max), optional
        AB-mag bracket applied after photometry. Pairs naturally with the
        ``[<field>.jhat.objmag_lim]`` knob.

    Returns
    -------
    table : astropy.table.Table
        ``RA``, ``DEC``, ``mag``, ``mag_err`` columns. Mags are AB.
    info : dict
        Provenance — input paths, SEP params, count of detections at each
        cut. Stash into ``meta['params']`` of the saved refcat.
    """
    import sep
    from photutils.segmentation import make_2dgaussian_kernel

    # Honor SEP's pixel-stack overflow knobs the way the notebook does;
    # large mosaics blow past defaults. ``set_extract_pixstack`` is global
    # state in the C extension, but it's idempotent.
    sep.set_extract_pixstack(int(1e7))
    sep.set_sub_object_limit(2048)

    sci, err, header, wcs = _open_sci_err(mosaic_path, err_path=err_path)
    bad = (sci == 0) | (err == 0) | ~np.isfinite(err) | ~np.isfinite(sci)
    sci_clean = np.where(bad, np.nan, sci)
    err_clean = np.where(bad, np.nan, err)
    snr = sci_clean / err_clean
    mask = ~np.isfinite(snr)

    kernel = make_2dgaussian_kernel(fwhm=filter_fwhm, size=5).array

    log(f"refcat extract: SEP detection on {os.path.basename(mosaic_path)} "
        f"(thresh={snr_thresh}, minarea={minarea})")
    objs, segmap = sep.extract(
        snr, thresh=snr_thresh, minarea=minarea,
        deblend_nthresh=deblend_nthresh, deblend_cont=deblend_cont,
        mask=mask, filter_type="matched", filter_kernel=kernel,
        clean=True, clean_param=1.0, segmentation_map=True,
    )
    objs = Table(objs)
    n_detected = len(objs)
    log(f"refcat extract: {n_detected} raw detections")

    if n_detected == 0:
        raise RuntimeError(
            f"SEP found no sources in {mosaic_path}. Check SCI/ERR data "
            "and the SNR threshold."
        )

    ids = np.arange(1, len(objs) + 1, dtype=np.int32)
    objs["theta"][objs["theta"] > np.pi / 2] -= np.pi
    nan_axes = np.isnan(objs["a"]) | np.isnan(objs["b"])
    objs["a"][nan_axes] = 5
    objs["b"][nan_axes] = 5

    kronrad, krflag = sep.kron_radius(
        snr, objs["x"], objs["y"], objs["a"], objs["b"], objs["theta"],
        6.0, mask=mask, seg_id=ids, segmap=segmap,
    )
    flux_snr, _, _ = sep.sum_ellipse(
        snr, objs["x"], objs["y"], objs["a"], objs["b"], objs["theta"],
        2.5 * kronrad, subpix=1, mask=mask,
        seg_id=ids, segmap=segmap,
    )
    rhalf, _ = sep.flux_radius(
        snr, objs["x"], objs["y"], 6.0 * objs["a"], 0.5,
        seg_id=ids, segmap=segmap, mask=mask, normflux=flux_snr, subpix=5,
    )
    sigma = 2.0 / 2.35 * rhalf
    xwin, ywin, _ = sep.winpos(snr, objs["x"], objs["y"], sigma, mask=mask)

    # Photometry on the SCI map (Kron + small-source circle fallback)
    flux, fluxerr, kron_a, kron_b = _kron_with_circle_fallback(
        sci_clean, err_clean, objs, kronrad, krflag, segmap, ids, mask,
    )

    conv = _ab_conversion_factor(header, wcs)
    with np.errstate(divide="ignore", invalid="ignore"):
        mag = -2.5 * np.log10(flux * conv / 3631e6)
        mag_err = 2.5 / np.log(10) * (fluxerr / flux)
    snr_int = flux / fluxerr

    coords = wcs.pixel_to_world(xwin, ywin)
    ra = coords.ra.value
    dec = coords.dec.value

    table = Table({
        "RA": ra, "DEC": dec,
        "mag": mag.astype(np.float32),
        "mag_err": mag_err.astype(np.float32),
    })

    # Cuts: finite, SNR, optional mag bracket
    keep = (
        np.isfinite(table["RA"]) & np.isfinite(table["DEC"])
        & np.isfinite(table["mag"]) & np.isfinite(table["mag_err"])
        & (snr_int >= snr_min)
    )
    n_after_snr = int(keep.sum())
    if mag_range is not None:
        mag_min, mag_max = mag_range
        keep &= (table["mag"] >= mag_min) & (table["mag"] <= mag_max)
    n_after_mag = int(keep.sum())
    table = table[keep]

    log(f"refcat extract: {n_detected} -> {n_after_snr} (snr>={snr_min}) "
        f"-> {n_after_mag} (mag cut) -> {len(table)} final")

    info = {
        "mosaic": os.path.abspath(mosaic_path),
        "err": os.path.abspath(err_path) if err_path else None,
        "snr_thresh": snr_thresh,
        "minarea": minarea,
        "deblend_nthresh": deblend_nthresh,
        "deblend_cont": deblend_cont,
        "filter_fwhm": filter_fwhm,
        "snr_min": snr_min,
        "mag_range": list(mag_range) if mag_range is not None else None,
        "n_detected": n_detected,
        "n_after_snr": n_after_snr,
        "n_after_mag": n_after_mag,
        "n_final": len(table),
    }
    return table, info


def _kron_with_circle_fallback(sci, err, objs, kronrad, krflag,
                               segmap, ids, mask, kron_params=(2.5, 3.5)):
    """Kron-elliptical photometry with a circular fallback for small sources.

    Same logic as ``auto_photometry`` in the UDS notebook.
    """
    import sep

    flux, fluxerr, flag = sep.sum_ellipse(
        sci, objs["x"], objs["y"], objs["a"], objs["b"], objs["theta"],
        err=err, r=kron_params[0] * kronrad, mask=mask,
        segmap=segmap, seg_id=ids,
    )
    flag |= krflag
    a = kron_params[0] * kronrad * objs["a"]
    b = kron_params[0] * kronrad * objs["b"]

    use_circle = (
        kron_params[0] * kronrad * np.sqrt(a * b) < kron_params[1] / 2
    )
    if use_circle.any():
        cflux, cfluxerr, _ = sep.sum_circle(
            sci,
            objs["x"][use_circle], objs["y"][use_circle],
            kron_params[1] / 2, err=err, mask=mask,
            segmap=segmap, seg_id=ids[use_circle],
        )
        flux[use_circle] = cflux
        fluxerr[use_circle] = cfluxerr
        a[use_circle] = kron_params[1] / 2
        b[use_circle] = kron_params[1] / 2

    return flux, fluxerr, a, b
