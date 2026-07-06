"""Footprint-clip the reference catalog to one exposure's sky coverage.

The field reference catalog is field-wide (e.g. ~550k rows for COSMOS), but a
single NIRCam exposure covers only its ~10' detector union. Feeding the whole
catalog to the matcher forces the brightest-N vertex cap to pick globally
brightest sources — almost all of them off-frame — so the triangle bootstrap
sees essentially no real correspondences. Clipping the refcat to the exposure's
footprint (plus a border sized for the acquisition-pointing error) is what makes
the in-frame sources the ones the cap keeps.

Geometry is done in a local **gnomonic tangent plane** (arcsec) about the mean
exposure pointing, not in raw RA/Dec: the border buffer is then an isotropic
true-angle margin, and the projection is immune to the RA wrap / cos(dec)
stretch / pole degeneracies that bite a naive RA/Dec polygon. Over NIRCam's
~10' field the tangent-plane distortion is negligible.

The clip is deliberately a **superset** of the true footprint — the border
means we keep a ring of just-outside sources — because the border doubles as an
implicit pointing prior: the footprint derives from the (possibly offset) input
WCS, so it must be grown by the largest acquisition error we expect to recover.
Over-inclusion is harmless (the robust refine rejects the extras); under-
inclusion would starve the solve.
"""

from dataclasses import dataclass

import numpy as np

from campfire_pipeline.common.io import log

_ARCSEC_PER_DEG = 3600.0
# NIRCam detectors are all 2048^2; the corner sampling grid falls back to this
# when a gwcs carries no bounding box.
_DEFAULT_SHAPE = (2048, 2048)


@dataclass
class FootprintClip:
    """Result of :func:`clip_refcat_to_exposure`."""

    table: object          # the clipped refcat (astropy Table)
    n_total: int           # rows in the input refcat
    n_kept: int            # rows inside footprint + border
    clipped: bool          # False when the clip failed open (table == input)

    @property
    def starved(self) -> bool:
        """True when the clip succeeded but left too few rows to bootstrap."""
        return self.clipped and self.n_kept < 3


def _detector_corner_sky(wcs, in_shape=_DEFAULT_SHAPE):
    """Return ``(ra[deg], dec[deg])`` sampled around one detector's boundary.

    Samples the four corners plus the four edge midpoints of the detector's
    pixel grid (its gwcs ``bounding_box`` if set, else a full ``in_shape``
    frame) and transforms them through the gwcs. Eight boundary points capture
    the (small) edge curvature from distortion better than corners alone, at
    negligible cost.
    """
    (x0, x1), (y0, y1) = _pixel_extent(wcs, in_shape)
    xm, ym = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
    xs = np.array([x0, xm, x1, x1, x1, xm, x0, x0], dtype=float)
    ys = np.array([y0, y0, y0, ym, y1, y1, y1, ym], dtype=float)
    ra, dec = wcs(xs, ys)
    ra = np.asarray(ra, dtype=float)
    dec = np.asarray(dec, dtype=float)
    good = np.isfinite(ra) & np.isfinite(dec)
    return ra[good], dec[good]


def _pixel_extent(wcs, in_shape):
    """``((x0, x1), (y0, y1))`` pixel bounds for *wcs* (bounding_box or frame)."""
    bb = getattr(wcs, 'bounding_box', None)
    try:
        # gwcs bounding_box is ((x_lo, x_hi), (y_lo, y_hi)); tolerate the
        # ModelBoundingBox wrapper by indexing.
        (x0, x1), (y0, y1) = bb[0], bb[1]
        return (float(x0), float(x1)), (float(y0), float(y1))
    except (TypeError, ValueError, IndexError):
        nx, ny = in_shape
        return (0.0, float(nx - 1)), (0.0, float(ny - 1))


def _gnomonic(ra_deg, dec_deg, ra0_deg, dec0_deg):
    """Gnomonic (TAN) projection to tangent-plane ``(xi, eta)`` in **arcsec**.

    Standard tangent-plane projection about ``(ra0, dec0)``. Points on the far
    hemisphere (``cos c <= 0``) map to NaN — they are never part of a NIRCam
    footprint, and NaN simply fails the downstream containment test.
    """
    ra = np.radians(np.asarray(ra_deg, dtype=float))
    dec = np.radians(np.asarray(dec_deg, dtype=float))
    ra0 = np.radians(float(ra0_deg))
    dec0 = np.radians(float(dec0_deg))
    dra = ra - ra0
    sin_dec, cos_dec = np.sin(dec), np.cos(dec)
    sin_dec0, cos_dec0 = np.sin(dec0), np.cos(dec0)
    cos_c = sin_dec0 * sin_dec + cos_dec0 * cos_dec * np.cos(dra)
    with np.errstate(divide='ignore', invalid='ignore'):
        xi = cos_dec * np.sin(dra) / cos_c
        eta = (cos_dec0 * sin_dec - sin_dec0 * cos_dec * np.cos(dra)) / cos_c
    bad = ~np.isfinite(cos_c) | (cos_c <= 0)
    xi = np.where(bad, np.nan, xi)
    eta = np.where(bad, np.nan, eta)
    scale = np.degrees(1.0) * _ARCSEC_PER_DEG
    return xi * scale, eta * scale


def _tangent_center(ra_deg, dec_deg):
    """Mean pointing of a set of sky points, via unit-vector averaging.

    Averaging unit vectors (rather than raw RA/Dec) keeps the center correct
    across the RA=0/360 seam and near the pole.
    """
    ra = np.radians(np.asarray(ra_deg, dtype=float))
    dec = np.radians(np.asarray(dec_deg, dtype=float))
    x = np.mean(np.cos(dec) * np.cos(ra))
    y = np.mean(np.cos(dec) * np.sin(ra))
    z = np.mean(np.sin(dec))
    ra0 = np.degrees(np.arctan2(y, x)) % 360.0
    dec0 = np.degrees(np.arctan2(z, np.hypot(x, y)))
    return ra0, dec0


def clip_refcat_to_exposure(refcat, detector_wcses, *, border_arcmin=0.5,
                            in_shape=_DEFAULT_SHAPE):
    """Clip *refcat* to the sky footprint of one exposure's detectors.

    Parameters
    ----------
    refcat : astropy.table.Table
        Field reference catalog with ``RA``/``DEC`` columns (degrees, ICRS).
    detector_wcses : iterable of gwcs.WCS
        One gwcs per detector in the exposure group (their union defines the
        footprint).
    border_arcmin : float
        Isotropic true-angle margin added around the detector union, sized for
        the maximum acquisition-pointing error to be recovered.
    in_shape : (int, int)
        Pixel frame used when a gwcs has no bounding box (NIRCam: 2048x2048).

    Returns
    -------
    FootprintClip
        ``.table`` is the clipped catalog (or, when the clip can't be built,
        the untouched input — fail-open, with ``.clipped == False``).
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    try:                                   # shapely >= 2.0
        from shapely import contains_xy as _contains_xy
    except ImportError:                    # shapely 1.7–1.x
        from shapely.vectorized import contains as _contains_xy

    n_total = len(refcat)
    wcses = [w for w in detector_wcses if w is not None]
    if n_total == 0 or not wcses:
        return FootprintClip(refcat, n_total, n_total, clipped=False)

    if 'RA' not in refcat.colnames or 'DEC' not in refcat.colnames:
        log("footprint: refcat has no RA/DEC; skipping clip (fail-open).")
        return FootprintClip(refcat, n_total, n_total, clipped=False)

    # Detector boundary points -> a single tangent center for the exposure.
    det_sky = [_detector_corner_sky(w, in_shape) for w in wcses]
    det_sky = [(ra, dec) for ra, dec in det_sky if ra.size >= 3]
    if not det_sky:
        log("footprint: no usable detector WCS footprints; skipping clip "
            "(fail-open).")
        return FootprintClip(refcat, n_total, n_total, clipped=False)

    all_ra = np.concatenate([ra for ra, _ in det_sky])
    all_dec = np.concatenate([dec for _, dec in det_sky])
    ra0, dec0 = _tangent_center(all_ra, all_dec)

    # Union of the per-detector quads in the tangent plane, buffered by border.
    quads = []
    for ra, dec in det_sky:
        xi, eta = _gnomonic(ra, dec, ra0, dec0)
        pts = np.column_stack([xi, eta])
        pts = pts[np.all(np.isfinite(pts), axis=1)]
        if len(pts) >= 3:
            quads.append(Polygon(pts).convex_hull)
    if not quads:
        log("footprint: detector footprints degenerate; skipping clip.")
        return FootprintClip(refcat, n_total, n_total, clipped=False)

    border_arcsec = float(border_arcmin) * 60.0
    footprint = unary_union(quads).buffer(border_arcsec)

    # Project the refcat once; cheap tangent-plane bbox pre-filter, then an
    # exact polygon test only on the survivors.
    ref_xi, ref_eta = _gnomonic(refcat['RA'], refcat['DEC'], ra0, dec0)
    minx, miny, maxx, maxy = footprint.bounds
    in_box = (np.isfinite(ref_xi) & np.isfinite(ref_eta)
              & (ref_xi >= minx) & (ref_xi <= maxx)
              & (ref_eta >= miny) & (ref_eta <= maxy))

    keep = np.zeros(n_total, dtype=bool)
    box_idx = np.flatnonzero(in_box)
    if box_idx.size:
        inside = _contains_xy(footprint, ref_xi[box_idx], ref_eta[box_idx])
        keep[box_idx] = np.asarray(inside, dtype=bool)

    clipped_table = refcat[keep]
    n_kept = int(keep.sum())
    return FootprintClip(clipped_table, n_total, n_kept, clipped=True)
