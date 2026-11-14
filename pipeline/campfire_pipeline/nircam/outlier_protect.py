"""Star protection for the NIRCam outlier step.

With ``[nircam.outlier].cross_program_overlap = true`` a visit is compared with
every exposure that overlaps it, whatever the program, and those can be years
apart. A star with a proper motion then sits at a different place in the other
exposures, and the comparison removes its core from the visit. The rule is the
one the HST field-level pass uses (``imaging/outlier.py::star_protection``): a
star of the reference catalog is protected where the catalog proper motion
moves it by ``protect_min_motion`` native pixels or more between the first and
the last exposure that contains it, inside a circle of ``protect_radius`` arcsec
around its position on each exposure's date.

The NIRCam test (``jwst``'s ``flag_resampled_model_crs``) takes no mask, so the
protection is applied afterwards: the values of the protected pixels are kept
before the test, and a flag the test sets there is taken back together with the
pixel values the test blanked. One difference from the HST pass follows: a
flag inside a circle can still lower the threshold for its neighbors just
outside it.

Nothing here runs with the default intra-program comparison.
"""

import numpy as np
from astropy.io import fits

from campfire_pipeline.common.io import log

# the arrays jwst blanks (NaN) where it sets DO_NOT_USE
_ARRAYS = ('data', 'err', 'var_poisson', 'var_rnoise', 'var_flat')


class _WcsChip:
    """The part of the chip interface ``star_protection`` uses, from a gwcs."""

    def __init__(self, key, wcs, shape):
        self.key = key
        self.shape = (int(shape[0]), int(shape[1]))
        self._wcs = wcs
        ny, nx = self.shape
        x0, y0 = 0.5 * (nx - 1), 0.5 * (ny - 1)
        ra, dec = self.pixel_to_world(np.array([x0, x0 + 1.0, x0]),
                                      np.array([y0, y0, y0 + 1.0]))
        cosd = np.cos(np.radians(dec[0]))
        ax, ay = (ra[1] - ra[0]) * cosd * 3600.0, (dec[1] - dec[0]) * 3600.0
        bx, by = (ra[2] - ra[0]) * cosd * 3600.0, (dec[2] - dec[0]) * 3600.0
        self.pixel_area_arcsec2 = float(abs(ax * by - ay * bx))

    def pixel_to_world(self, x, y):
        ra, dec = self._wcs(np.asarray(x, float), np.asarray(y, float),
                            with_bounding_box=False)
        return np.asarray(ra, float), np.asarray(dec, float)


def _open_chip(path):
    from copy import deepcopy

    from stdatamodels.jwst.datamodels import ImageModel

    with ImageModel(path) as model:
        return _WcsChip(path, deepcopy(model.meta.wcs), model.data.shape)


def protection_for_visit(all_inputs, visit_files, refcat, *, radius=0.4,
                         min_motion=0.15):
    """Masks of the pixels to leave alone in a visit's exposures.

    ``all_inputs`` are the exposures compared (the visit's and the ones that
    overlap it), ``visit_files`` the ones that get flags. ``refcat`` is the
    reference catalog the field was aligned to.

    Returns ``(masks, info)``. ``masks`` is ``{path: bool array}`` for the
    visit's exposures that contain a protected star, empty when no star moved
    enough. ``info`` has ``n_stars`` (catalog stars with a proper motion that
    could have moved enough, on the exposures), ``n_protected`` and
    ``span_years``.
    """
    from campfire_pipeline.imaging.outlier import star_protection
    from campfire_pipeline.imaging.refcat.io import has_proper_motion
    from campfire_pipeline.imaging.refcat.motion import _float_col
    from campfire_pipeline.nircam.align.apply import _exposure_mid_mjd

    info = {'n_stars': 0, 'n_protected': 0, 'span_years': 0.0}
    if refcat is None or len(refcat) == 0 or not has_proper_motion(refcat):
        return {}, info
    dates = {f: _exposure_mid_mjd(f) for f in all_inputs}
    undated = [f for f in all_inputs if dates[f] is None]
    if undated:
        log(f"  outlier: {len(undated)} exposure(s) carry no date, so stars "
            f"cannot be placed and none is protected")
        return {}, info
    span = (max(dates.values()) - min(dates.values())) / 365.25
    info['span_years'] = float(span)

    # only a star fast enough to cross the threshold within the whole time
    # span can qualify; when none is, no exposure needs to be opened
    native = float(np.sqrt(fits.getheader(visit_files[0], 'SCI')['PIXAR_A2']))
    speed = np.hypot(_float_col(refcat, 'pmra'),
                     _float_col(refcat, 'pmdec')) / 1000.0        # arcsec / yr
    fast = np.isfinite(speed) & (speed * span >= float(min_motion) * native)
    if not fast.any():
        return {}, info

    chips = [_open_chip(f) for f in all_inputs]
    masks, found = star_protection(
        chips, refcat[fast], dates, radius=float(radius),
        min_motion=float(min_motion), masks_for=visit_files)
    info['n_stars'] = found['n_stars']
    info['n_protected'] = found['n_protected']
    return {f: m for f, m in masks.items() if m.any()}, info


def _array(model, name):
    """``model.<name>`` when the model carries an array of that name."""
    has = getattr(model, 'hasattr', None)
    if has is not None and not has(name):
        return None
    return getattr(model, name, None)


def snapshot(model, mask):
    """The values of ``model`` at the pixels of ``mask``, to give back to
    :func:`release` after the outlier test."""
    kept = {'dq': np.array(model.dq[mask])}
    for name in _ARRAYS:
        arr = _array(model, name)
        if arr is not None and getattr(arr, 'shape', None) == mask.shape:
            kept[name] = np.array(arr[mask])
    return kept


def release(model, mask, kept, outlier_bit):
    """Take back the outlier flags set at pixels of ``mask`` since
    :func:`snapshot`, with the pixel values blanked along with them.

    A pixel that already carried the outlier bit is left as it is. Returns
    the number of pixels released.
    """
    now = model.dq[mask]
    new = ((now & outlier_bit) != 0) & ((kept['dq'] & outlier_bit) == 0)
    if not new.any():
        return 0
    rows, cols = np.nonzero(mask)
    at = (rows[new], cols[new])
    model.dq[at] = kept['dq'][new]
    for name in _ARRAYS:
        if name in kept:
            _array(model, name)[at] = kept[name][new]
    return int(new.sum())
