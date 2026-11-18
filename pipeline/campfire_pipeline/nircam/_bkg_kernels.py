"""Exact fast kernels for :class:`~campfire_pipeline.nircam.bkgsub.SubtractBackground`.

Every function here returns, bit for bit, what the library call it stands in
for returns; only the work is split differently. ``threads`` spreads the work
over a thread pool (scipy.ndimage and numpy release the GIL in their kernels),
and ``threads <= 1`` runs the library call itself.

- :func:`gaussian` — ``scipy.ndimage.gaussian_filter``. It is a sequence of
  1-D correlations whose every output pixel is a fixed-order sum over its own
  ``r = int(4 * sigma + 0.5)`` neighbourhood, so row bands filtered with an
  ``r``-row halo (or meeting the real edge, where the boundary mode is the
  same rule) give the full-image result.
- :func:`dilate` — ``distance_transform_edt(~mask) <= radius``. A pixel whose
  nearest mask pixel lies within ``radius`` finds it inside a band with a
  ``floor(radius) + 1``-row halo, at the same exact distance; one whose
  nearest mask pixel lies outside the band is further than ``radius`` either
  way.
- :func:`median_filter` — ``scipy.ndimage.median_filter(img, footprint=...)``,
  a rank filter over the footprint, in row bands with a footprint-high halo.
- :func:`zoom` — ``scipy.ndimage.zoom``. Each band of output rows is resampled
  by scipy's own kernel (``_nd_image.zoom_shift``) from the same prefiltered
  coefficients and zoom ratios, with the band's first row passed as an
  integer shift, so every output index maps to the same input coordinate
  (``kk + y0`` is exact in double). :class:`ExactZoomInterpolator` plugs it
  into photutils' threaded zoom hook, whose own band resampler (through
  ``affine_transform``) matches only up to rounding.
- :func:`median` — ``np.median`` of a 1-D array. The order statistics are
  located from a strided sample, everything outside a bracket around them is
  counted in parallel, and only the bracket is partitioned; the result is the
  same element(s), combined the way ``np.median`` combines them.
- :func:`biweight_scale` / :func:`biweight_location` — the astropy functions of
  the same name for 1-D data with default arguments: the same expressions in
  the same order, evaluated elementwise in parallel into full-size arrays,
  then summed with one ``np.sum`` over the full array, so the pairwise
  summation is the one astropy performs; the data median is computed once
  instead of twice.
- :func:`detect` / :func:`segments` — the label image of
  ``photutils.segmentation.detect_sources`` (8-connected): the same threshold
  and ``ndi.label``, segments under ``npixels`` dropped through one bincount
  instead of a ``find_objects`` loop, and the survivors renumbered
  consecutively in label order exactly as photutils renumbers them.
- :func:`mesh_nanmedian_filter` — photutils' mesh filter,
  ``generic_filter(data, np.nanmedian, size, mode='constant', cval=nan)``,
  vectorized over the windows instead of one Python callback per mesh cell;
  :class:`FastBackground2D` is ``Background2D`` with it.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
import inspect

from photutils.background import Background2D, BkgZoomInterpolator
from scipy import ndimage
from scipy.ndimage import distance_transform_edt, gaussian_filter
from scipy.ndimage import median_filter as _median_filter

# Elements per parallel chunk of a 1-D pass, and the strided sample size the
# median bracket is located from.
_CHUNK = 1 << 22
_SAMPLE = 1 << 21


def _run(fn, items, threads):
    items = list(items)
    if threads <= 1 or len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(min(threads, len(items))) as ex:
        return list(ex.map(fn, items))


def _bands(n, threads, halo=0, min_rows=32):
    """Row bands for ``threads`` workers: about two per worker, each at least
    ``max(min_rows, 4 * halo)`` rows so the halo stays a small overhead."""
    rows = max(min_rows, 4 * halo, -(-n // (2 * threads)))
    return [(y, min(y + rows, n)) for y in range(0, n, rows)]


def _chunks(n):
    return [(i, min(i + _CHUNK, n)) for i in range(0, n, _CHUNK)]


# ---------------------------------------------------------------------------
# Image filters
# ---------------------------------------------------------------------------

def gaussian(img, sigma, mode="reflect", threads=1):
    """``scipy.ndimage.gaussian_filter(img, sigma, mode=mode)`` (2-D, scalar
    sigma, default truncate)."""
    if threads <= 1 or img.ndim != 2:
        return gaussian_filter(img, sigma, mode=mode)
    r = int(4.0 * float(sigma) + 0.5)
    ny = img.shape[0]
    out = np.empty(img.shape, dtype=img.dtype)

    def band(b):
        y0, y1 = b
        w0, w1 = max(y0 - r, 0), min(y1 + r, ny)
        out[y0:y1] = gaussian_filter(img[w0:w1], sigma, mode=mode)[
            y0 - w0:y1 - w0]

    _run(band, _bands(ny, threads, r), threads)
    return out


def _zoom_banded(inp, zoom_factor, order, mode, cval, grid_mode, threads):
    from scipy.ndimage import _nd_image, _ni_support
    from scipy.ndimage._interpolation import _prepad_for_spline_filter

    zf = _ni_support._normalize_sequence(zoom_factor, inp.ndim)
    out_shape = tuple(int(round(i * z)) for i, z in zip(inp.shape, zf))
    if order > 1:
        padded, npad = _prepad_for_spline_filter(inp, mode, cval)
        filtered = ndimage.spline_filter(padded, order, output=np.float64,
                                         mode=mode)
    else:
        npad, filtered = 0, inp
    num = np.array(inp.shape)
    div = np.array(out_shape)
    if not grid_mode:
        num, div = num - 1, div - 1
    ratio = np.ascontiguousarray(np.divide(
        num, div, out=np.ones_like(inp.shape, dtype=np.float64),
        where=div != 0))
    code = _ni_support._extend_mode_to_code(mode)
    out = np.empty(out_shape, dtype=inp.dtype)

    def band(b):
        y0, y1 = b
        shift = np.ascontiguousarray([float(y0), 0.0])
        _nd_image.zoom_shift(filtered, ratio, shift, out[y0:y1], order, code,
                             cval, npad, grid_mode)

    _run(band, _bands(out_shape[0], threads), threads)
    return out


def zoom(inp, zoom_factor, order=3, mode="constant", cval=0.0,
         grid_mode=False, threads=1):
    """``scipy.ndimage.zoom(inp, zoom_factor, order=order, mode=mode,
    cval=cval, grid_mode=grid_mode)`` for a real 2-D array."""
    inp = np.asarray(inp)
    if threads > 1 and inp.ndim == 2 and not np.iscomplexobj(inp):
        zf = np.atleast_1d(zoom_factor)
        if not np.all(zf == 1):
            try:
                return _zoom_banded(inp, zoom_factor, order, mode, cval,
                                    grid_mode, threads)
            except (AttributeError, ImportError, TypeError):
                pass                    # scipy internals moved: plain call
    return ndimage.zoom(inp, zoom_factor, order=order, mode=mode, cval=cval,
                        grid_mode=grid_mode)


class ExactZoomInterpolator(BkgZoomInterpolator):
    """photutils' zoom interpolator whose threaded path (taken when
    ``Background2D`` has ``n_threads > 1``) is :func:`zoom`, the
    single-threaded result bit for bit."""

    def _threaded_zoom(self, data, zoom_factor, n_threads):
        return zoom(data, zoom_factor, order=self.order, mode=self.mode,
                    cval=self.cval, grid_mode=True, threads=n_threads)


def background2d_threads(n_threads):
    """``{'n_threads': n}`` for Background2D when it takes the argument
    (photutils >= 3.0) and the zoom hook it calls is the exact one above."""
    if n_threads <= 1:
        return {}
    params = inspect.signature(Background2D.__init__).parameters
    if "n_threads" not in params or not hasattr(BkgZoomInterpolator,
                                                "_threaded_zoom"):
        return {}
    return {"n_threads": int(n_threads)}


def median_filter(img, footprint, threads=1):
    """``scipy.ndimage.median_filter(img, footprint=footprint)`` (2-D)."""
    if threads <= 1 or img.ndim != 2:
        return _median_filter(img, footprint=footprint)
    h = int(np.shape(footprint)[0])
    ny = img.shape[0]
    out = np.empty(img.shape, dtype=img.dtype)

    def band(b):
        y0, y1 = b
        w0, w1 = max(y0 - h, 0), min(y1 + h, ny)
        out[y0:y1] = _median_filter(img[w0:w1], footprint=footprint)[
            y0 - w0:y1 - w0]

    _run(band, _bands(ny, threads, h), threads)
    return out


def dilate(mask, radius, threads=1):
    """``distance_transform_edt(~mask) <= radius`` for a 2-D boolean ``mask``
    holding at least one True pixel."""
    if threads <= 1:
        return distance_transform_edt(~mask) <= radius
    h = int(math.floor(radius)) + 1
    ny = mask.shape[0]
    out = np.zeros(mask.shape, dtype=bool)

    def band(b):
        y0, y1 = b
        w0, w1 = max(y0 - h, 0), min(y1 + h, ny)
        win = mask[w0:w1]
        if not win.any():
            return                      # every mask pixel is > radius away
        out[y0:y1] = (distance_transform_edt(~win) <= radius)[
            y0 - w0:y1 - w0]

    _run(band, _bands(ny, threads, h), threads)
    return out


# ---------------------------------------------------------------------------
# Robust statistics
# ---------------------------------------------------------------------------

def _select(x, ks, threads):
    """The ``ks``-th smallest elements of the finite 1-D float array ``x``
    (sorted order statistics, ``ks`` ascending), or None when the sampled
    bracket missed them (the caller then partitions in full)."""
    n = x.size
    stride = max(1, n // _SAMPLE)
    sample = x[::stride].copy()
    s = sample.size
    # bracket ±(6 sigma of the sample quantile + slack) around the targets
    delta = int(6.0 * math.sqrt(s) + 64)
    jl = max(0, ks[0] * s // n - delta)
    jh = min(s - 1, ks[-1] * s // n + delta)
    sample.partition([jl, jh])
    lo, hi = sample[jl], sample[jh]

    def part(c):
        a = x[c[0]:c[1]]
        below = a < lo
        inside = ~below & (a <= hi)
        return int(np.count_nonzero(below)), a[inside]

    res = _run(part, _chunks(n), threads)
    n_below = sum(r[0] for r in res)
    mid = np.concatenate([r[1] for r in res])
    if not (n_below <= ks[0] and ks[-1] < n_below + mid.size):
        return None
    kk = [k - n_below for k in ks]
    mid.partition(kk)
    return [mid[k] for k in kk]


def _has_nan(x, threads):
    return any(_run(lambda c: bool(np.isnan(x[c[0]:c[1]]).any()),
                    _chunks(x.size), threads))


def median(x, threads=1):
    """``np.median(x)`` for a 1-D float array."""
    x = np.asarray(x)
    n = x.size
    if threads <= 1 or n < 4 * _SAMPLE or x.ndim != 1:
        return np.median(x)
    if _has_nan(x, threads):
        return np.median(x)
    ks = [(n - 1) // 2] if n % 2 else [n // 2 - 1, n // 2]
    got = _select(x, ks, threads)
    if got is None:
        return np.median(x)
    # np.median: part[indexer] then np.mean, i.e. the element itself or
    # add.reduce of the two middle elements over 2
    return np.mean(np.asarray(got, dtype=x.dtype))


def _as_float64(data, threads):
    data = np.asarray(data)
    if data.dtype == np.float64:
        return data
    out = np.empty(data.shape, dtype=np.float64)

    def conv(c):
        out[c[0]:c[1]] = data[c[0]:c[1]]

    _run(conv, _chunks(data.size), threads)
    return out


def _fast_ok(data, threads):
    return (threads > 1 and isinstance(data, np.ndarray)
            and not isinstance(data, np.ma.MaskedArray)
            and data.ndim == 1 and data.size >= 4 * _SAMPLE)


def biweight_scale(data, threads=1, c=9.0):
    """``astropy.stats.biweight_scale(data, c=c)`` for 1-D data."""
    from astropy.stats import biweight_scale as ref

    if not _fast_ok(data, threads):
        return ref(data, c=c)
    x = _as_float64(data, threads)
    n = x.size
    M = median(x, threads)
    d = np.empty(n)
    ad = np.empty(n)

    def dev(ch):
        s = slice(*ch)
        np.subtract(x[s], M, out=d[s])
        np.abs(d[s], out=ad[s])

    _run(dev, _chunks(n), threads)
    mad = median(ad, threads)            # median_absolute_deviation(x)
    del ad
    if mad == 0.0 or np.isnan(mad):
        return np.sqrt(mad ** 2)
    f1 = np.empty(n)
    f2 = np.empty(n)

    def terms(ch):
        s = slice(*ch)
        with np.errstate(divide="ignore", invalid="ignore"):
            u = d[s] / (c * mad)
        with np.errstate(invalid="ignore"):
            mask = np.abs(u) < 1
        u = u ** 2
        a = d[s] * d[s] * (1.0 - u) ** 4
        a[~mask] = 0.0
        f1[s] = a
        b = (1.0 - u) * (1.0 - 5.0 * u)
        b[~mask] = 0.0
        f2[s] = b

    _run(terms, _chunks(n), threads)
    del d
    nn = np.float64(n)                   # np.sum(np.ones(n)), exact
    s1 = np.sum(f1)
    s2 = np.abs(np.sum(f2)) ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.sqrt(nn * s1 / s2)


def biweight_location(data, threads=1, c=6.0):
    """``astropy.stats.biweight_location(data, c=c)`` for 1-D data."""
    from astropy.stats import biweight_location as ref

    if not _fast_ok(data, threads):
        return ref(data, c=c)
    x = _as_float64(data, threads)
    n = x.size
    M = median(x, threads)
    d = np.empty(n)
    ad = np.empty(n)

    def dev(ch):
        s = slice(*ch)
        np.subtract(x[s], M, out=d[s])
        np.abs(d[s], out=ad[s])

    _run(dev, _chunks(n), threads)
    mad = median(ad, threads)
    del ad
    if mad == 0.0 or np.isnan(mad):
        return M
    du = np.empty(n)
    uu = np.empty(n)

    def terms(ch):
        s = slice(*ch)
        with np.errstate(divide="ignore", invalid="ignore"):
            u = d[s] / (c * mad)
        with np.errstate(invalid="ignore"):
            mask = np.abs(u) >= 1
        u = (1 - u ** 2) ** 2
        u[mask] = 0
        uu[s] = u
        du[s] = d[s] * u

    _run(terms, _chunks(n), threads)
    del d
    with np.errstate(divide="ignore", invalid="ignore"):
        return M + np.sum(du) / np.sum(uu)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

_CONN8 = np.ones((3, 3), dtype=bool)


def map_bands(fn, shape, threads):
    """Call ``fn(rows)`` for row-band slices covering ``shape[0]``."""
    _run(lambda b: fn(slice(*b)), _bands(shape[0], threads), threads)


def bincount(labels, minlength, threads=1):
    """``np.bincount(labels.ravel(), minlength=minlength)``."""
    flat = labels.reshape(-1)
    if threads <= 1:
        return np.bincount(flat, minlength=minlength)
    parts = _run(lambda c: np.bincount(flat[c[0]:c[1]], minlength=minlength),
                 _chunks(flat.size), threads)
    return np.sum(parts, axis=0)


def segments(above, n_pixels, threads=1):
    """The ``detect_sources`` label image of the boolean ``above`` image
    (8-connected, segments under ``n_pixels`` dropped, renumbered), or None
    when no segment survives."""
    if not any(_run(lambda b: bool(above[b[0]:b[1]].any()),
                    _bands(above.shape[0], threads), threads)):
        return None
    labels, n = ndimage.label(above, structure=_CONN8)
    areas = bincount(labels, n + 1, threads)
    keep = areas >= n_pixels
    keep[0] = False
    n_keep = int(np.count_nonzero(keep))
    if n_keep == 0:
        return None
    if n_keep == n:
        return labels
    label_map = np.zeros(n + 1, dtype=labels.dtype)
    label_map[keep] = np.arange(1, n_keep + 1, dtype=labels.dtype)

    def relabel(rows):
        labels[rows] = label_map[labels[rows]]

    map_bands(relabel, labels.shape, threads)
    return labels


def detect(data, threshold, n_pixels, mask=None, threads=1):
    """``detect_sources(data, threshold, n_pixels, mask=mask).data`` (or
    None when it finds nothing), for a scalar ``threshold``."""
    above = np.empty(data.shape, dtype=bool)

    def thresh(rows):
        a = data[rows] > threshold
        if mask is not None:
            a &= ~mask[rows]
        above[rows] = a

    map_bands(thresh, data.shape, threads)
    return segments(above, n_pixels, threads)


# ---------------------------------------------------------------------------
# photutils mesh filter
# ---------------------------------------------------------------------------

def mesh_nanmedian_filter(data, size):
    """``generic_filter(data, np.nanmedian, size=size, mode='constant',
    cval=np.nan)`` for a 2-D float mesh: each window's finite values sorted,
    the middle one taken (or the mean of the middle two, as ``np.median``
    forms it); all-NaN windows give NaN."""
    fy, fx = (int(size), int(size)) if np.ndim(size) == 0 else map(int, size)
    data = np.asarray(data)
    dtype = data.dtype
    data = data.astype(np.float64, copy=False)
    # generic_filter centres a window of even size at index size // 2
    py0, px0 = fy // 2, fx // 2
    padded = np.pad(data, ((py0, fy - 1 - py0), (px0, fx - 1 - px0)),
                    constant_values=np.nan)
    win = sliding_window_view(padded, (fy, fx)).reshape(data.size, fy * fx)
    srt = np.sort(win, axis=1)                      # NaN sorts last
    k = np.count_nonzero(~np.isnan(srt), axis=1)
    rows = np.arange(data.size)
    lo = srt[rows, np.maximum((k - 1) // 2, 0)]
    hi = srt[rows, np.maximum(k // 2, 0)]
    two = np.empty((data.size, 2))
    two[:, 0], two[:, 1] = lo, hi
    even = np.mean(two, axis=1)                     # add.reduce / 2
    out = np.where(k % 2 == 1, lo, even)
    out[k == 0] = np.nan
    return out.reshape(data.shape).astype(dtype, copy=False)


class FastBackground2D(Background2D):
    """``Background2D`` whose full-mesh median filter (the only one used here,
    ``filter_threshold=None``) is :func:`mesh_nanmedian_filter`; any other
    filter mode is photutils' own."""

    def _filter_grid(self, data, bkg_grid):
        size = tuple(np.atleast_1d(self.filter_size))
        if getattr(self, "filter_threshold", None) is not None:
            return super()._filter_grid(data, bkg_grid)
        if size in ((1,), (1, 1)):
            return data
        return mesh_nanmedian_filter(data, size if len(size) == 2 else size[0])
