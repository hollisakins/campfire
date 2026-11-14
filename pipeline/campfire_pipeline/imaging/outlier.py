"""Field-level outlier detection on instrument-neutral chips.

Given every chip of one detector and filter that has been aligned to one
frame, whatever visit or program it came from, find the pixels that disagree
with the rest of the stack: cosmic rays a two-exposure visit could not reject
(a hit at the same place in both frames), residual trails, persistence that
moves with the dither.

The method is the one AstroDrizzle and ``jwst.outlier_detection`` use, written
against :class:`~campfire_pipeline.imaging.exposure.Chip` so that it needs no
instrument's file layout:

1. **Single drizzles.** Each chip is drizzled by itself onto one north-up grid
   at about the native pixel scale (:func:`stack_grid`), in the surface
   brightness the adapter hands over (background already removed).
2. **Median.** The per-pixel median over the chips that cover a grid pixel.
   Where only two or three chips overlap, the median is not robust (with two it
   is the mean), so there the **minimum** is taken instead whenever the median
   exceeds it by more than ``minmed_nsigma`` times the noise expected for the
   minimum value: AstroDrizzle's ``minmed`` rule, including its second, lower
   threshold for the neighbors of pixels where the minimum was taken.
3. **Blot.** The median is interpolated back onto each chip's own pixels.
4. **Test.** A pixel is an outlier when::

       |pixel - blot| > scale * gradient(blot) + snr * sigma

   ``gradient`` is the largest difference between the blotted value and its
   four neighbors: it allows for what a small misregistration or a change of
   sampling does to a steep source, and is what keeps stellar cores from being
   flagged. ``sigma`` is the noise *expected* for the blotted value, the chip's
   background variance plus a source term proportional to the model (not to
   the pixel's own, possibly contaminated, value). The test runs twice: with
   ``snr[0]`` / ``scale[0]`` everywhere, then with the lower ``snr[1]`` /
   ``scale[1]`` on the neighbors of pixels already flagged (``grow``).

A pixel is tested only where at least ``min_contributors`` chips went into the
median. Where a chip stands alone the median is the chip itself and nothing can
be said.

What this does not do: it adds nothing along the readout direction of CTE
trails (AstroDrizzle's ``ctegrow``), and it builds one grid for everything it is
given, so a caller with a field much larger than a few detector footprints must
hand it one overlap group at a time.

Memory: the single drizzles are kept on disk in a private directory (one array
per chip, its footprint only) and read back block by block for the median; the
full-grid arrays held in memory are the median, the minimum, its variance and
the contributor count.
"""

import os
import shutil
import tempfile
import warnings

import numpy as np

from campfire_pipeline.common.io import log

# A single drizzle's pixel enters the median only if its weight is at least
# this fraction of the chip's typical weight: an output pixel fed mostly by bad
# input pixels is not a fair sample of the sky (jwst.outlier_detection's
# ``maskpt``).
_MASKPT = 0.7


def stack_grid(chips, pixel_scale=None, pad=8):
    """North-up :class:`~campfire_pipeline.imaging.grid.TileGrid` covering all
    of ``chips`` at ``pixel_scale`` arcsec (default: their native scale)."""
    from campfire_pipeline.imaging.grid import TileGrid

    scale = float(pixel_scale or np.sqrt(np.median(
        [c.pixel_area_arcsec2 for c in chips])))
    ras, decs = [], []
    t = np.linspace(0.0, 1.0, 9)
    for c in chips:
        ny, nx = c.shape
        xs = np.concatenate([t * (nx - 1), np.full(9, nx - 1.0),
                             t * (nx - 1), np.zeros(9)])
        ys = np.concatenate([np.zeros(9), t * (ny - 1),
                             np.full(9, ny - 1.0), t * (ny - 1)])
        ra, dec = c.pixel_to_world(xs, ys)
        ras.append(np.asarray(ra, float))
        decs.append(np.asarray(dec, float))
    ras, decs = np.concatenate(ras), np.concatenate(decs)
    ras = (ras - ras[0] + 180.0) % 360.0 - 180.0 + ras[0]     # no jump at RA 0
    crval = (float(0.5 * (ras.min() + ras.max())) % 360.0,
             float(0.5 * (decs.min() + decs.max())))
    probe = TileGrid(crval=crval, crpix=(0.0, 0.0), shape=(1, 1), rotation=0.0,
                     pixel_scale=scale)
    x, y = probe.world_to_pixel(ras, decs)
    x0, y0 = np.floor(x.min()) - pad, np.floor(y.min()) - pad
    nx = int(np.ceil(x.max()) - x0) + pad + 1
    ny = int(np.ceil(y.max()) - y0) + pad + 1
    return TileGrid(crval=crval, crpix=(float(-x0), float(-y0)), shape=(nx, ny),
                    rotation=0.0, pixel_scale=scale)


def _noise_terms(prep):
    """Background variance per pixel, and the source term's coefficient.

    From what the drizzle preparation returns for a chip: ``weight`` is the
    inverse background variance (zero where the pixel is unusable) and
    ``var_total`` the full variance. The source term is modelled as
    ``coefficient * flux``; the coefficient is the median of (variance above
    background) / flux over the chip's bright pixels, and 0 when the chip has
    too few of them to say.
    """
    weight = prep['weight']
    good = weight > 0
    var_bg = np.zeros(weight.shape, dtype=np.float32)
    np.divide(1.0, weight, out=var_bg, where=good)
    data = prep['data']
    excess = prep['var_total'] - var_bg
    with np.errstate(invalid='ignore'):
        bright = good & np.isfinite(data) & (data > 10.0 * np.sqrt(var_bg)) \
            & np.isfinite(excess) & (excess > 0)
    coefficient = float(np.median(excess[bright] / data[bright])) \
        if bright.sum() >= 50 else 0.0
    return good, var_bg, coefficient


def abs_gradient(image):
    """Largest absolute difference between each pixel and its four neighbors.

    Neighbors that do not exist (the frame edge) or are not finite count as no
    difference.
    """
    image = np.asarray(image, dtype=np.float32)
    out = np.zeros(image.shape, dtype=np.float32)
    for axis in (0, 1):
        for step in (1, -1):
            shifted = np.roll(image, step, axis=axis)
            edge = [slice(None), slice(None)]
            edge[axis] = 0 if step == 1 else -1
            shifted[tuple(edge)] = image[tuple(edge)]
            with np.errstate(invalid='ignore'):
                d = np.abs(image - shifted)
            np.fmax(out, d, out=out)
    return out


def _blot(image, pixmap, in_shape, interp):
    from drizzle.resample import blot_image

    return blot_image(
        np.ascontiguousarray(image, dtype=np.float32), pixmap=pixmap,
        pix_ratio=1.0, exptime=1.0,
        output_pixel_shape=(int(in_shape[1]), int(in_shape[0])),
        interp=interp, sinscl=1.0)


def _world_to_pixel(chip, ra, dec, iterations=6):
    """Pixel positions of sky positions on ``chip``, from ``pixel_to_world``
    alone (Newton steps with a numerical Jacobian, started at the chip center).

    The chip interface only maps pixels to the sky. Detector distortion is
    smooth, so the steps converge to well under a hundredth of a pixel for a
    position on or near the chip; a position far off the chip comes back far
    off the chip, which is all a caller needs to discard it.
    """
    ra = np.atleast_1d(np.asarray(ra, float))
    dec = np.atleast_1d(np.asarray(dec, float))
    ny, nx = chip.shape
    x = np.full(ra.shape, 0.5 * (nx - 1))
    y = np.full(ra.shape, 0.5 * (ny - 1))
    cosd = np.cos(np.radians(dec))
    limit = 4.0 * max(nx, ny)

    def sky(px, py):
        a, d = chip.pixel_to_world(px, py)
        return np.asarray(a, float), np.asarray(d, float)

    for _ in range(int(iterations)):
        a0, d0 = sky(x, y)
        ax, dx_ = sky(x + 1.0, y)
        ay, dy_ = sky(x, y + 1.0)
        wrap = lambda v: (v + 180.0) % 360.0 - 180.0     # noqa: E731
        # offsets on the sky in degrees, east and north
        j11, j21 = wrap(ax - a0) * cosd, dx_ - d0        # d(east, north) / dx
        j12, j22 = wrap(ay - a0) * cosd, dy_ - d0        # d(east, north) / dy
        e, n = wrap(ra - a0) * cosd, dec - d0
        det = j11 * j22 - j12 * j21
        with np.errstate(divide='ignore', invalid='ignore'):
            step_x = (e * j22 - n * j12) / det
            step_y = (n * j11 - e * j21) / det
        x = np.clip(x + np.nan_to_num(step_x), -limit, limit)
        y = np.clip(y + np.nan_to_num(step_y), -limit, limit)
    return x, y


def star_protection(chips, stars, epochs, *, radius=0.4, min_motion=0.15,
                    masks_for=None):
    """Masks of the pixels around stars that moved between the exposures.

    The comparison in :func:`detect_outliers` assumes the sky is the same in
    every exposure. A star with a proper motion breaks that when the exposures
    are years apart: its core sits at a different place in the later ones, the
    median follows the majority, and the test removes the core from the rest.
    Those pixels are real signal, and the star comes out too faint.

    ``stars`` is a reference catalog with ``RA``, ``DEC``, ``pmra``, ``pmdec``
    (mas/yr) and ``ref_epoch`` (Julian year); rows without a finite proper
    motion are ignored. ``epochs`` maps ``chip.key`` to the exposure date (MJD).
    A star is protected when, between the earliest and the latest exposure that
    contains it, it moved by at least ``min_motion`` native pixels. It is then
    protected in every exposure that contains it, inside a circle of ``radius``
    arcsec around its position on that exposure's date.

    Returns ``(masks, info)``: ``masks`` is ``{chip.key: bool array}`` (True
    where :func:`detect_outliers` should not flag), ``info`` counts the stars
    with a proper motion on the chips, the stars protected and the pixels
    masked. ``masks_for`` limits the masks to those chip keys (every chip
    still counts for where and when a star was seen); a caller that flags only
    some of the exposures it compares does not pay for the rest.
    """
    from campfire_pipeline.imaging.refcat.io import has_proper_motion
    from campfire_pipeline.imaging.refcat.motion import (_float_col,
                                                         propagate_to_epoch)

    chips = list(chips)
    wanted = None if masks_for is None else set(masks_for)
    masks = {c.key: np.zeros(c.shape, dtype=bool) for c in chips
             if wanted is None or c.key in wanted}
    info = {'n_stars': 0, 'n_protected': 0, 'n_pixels': 0,
            'radius': float(radius), 'min_motion': float(min_motion)}
    if stars is None or len(stars) == 0 or not has_proper_motion(stars):
        return masks, info
    pmra, pmdec = _float_col(stars, 'pmra'), _float_col(stars, 'pmdec')
    moving = np.isfinite(pmra) & np.isfinite(pmdec) \
        & np.isfinite(_float_col(stars, 'ref_epoch'))
    if not moving.any():
        return masks, info
    stars = stars[moving]
    speed = np.hypot(pmra[moving], pmdec[moving]) / 1000.0      # arcsec / yr

    at_epoch = {}
    positions = {}                 # chip.key -> (x, y, on chip)
    first = np.full(len(stars), np.inf)
    last = np.full(len(stars), -np.inf)
    for c in chips:
        mjd = float(epochs[c.key])
        if mjd not in at_epoch:
            moved = propagate_to_epoch(stars, mjd)
            at_epoch[mjd] = (_float_col(moved, 'RA'), _float_col(moved, 'DEC'))
        x, y = _world_to_pixel(c, *at_epoch[mjd])
        ny, nx = c.shape
        native = float(np.sqrt(c.pixel_area_arcsec2))
        pad = float(radius) / native + 1.0
        close = (x > -pad) & (x < nx + pad) & (y > -pad) & (y < ny + pad)
        if close.any():
            # a position far off a distorted chip can land anywhere; keep
            # only those that map back to the star (0.1 native pixel)
            ra, dec = at_epoch[mjd]
            back_ra, back_dec = c.pixel_to_world(x[close], y[close])
            miss = np.hypot(
                ((np.asarray(back_ra, float) - ra[close] + 180.0) % 360.0 - 180.0)
                * np.cos(np.radians(dec[close])),
                np.asarray(back_dec, float) - dec[close]) * 3600.0
            close[np.nonzero(close)[0][~(miss < 0.1 * native)]] = False
        on = close & (x > -0.5) & (x < nx - 0.5) & (y > -0.5) & (y < ny - 0.5)
        positions[c.key] = (x, y, close)
        first[on] = np.minimum(first[on], mjd)
        last[on] = np.maximum(last[on], mjd)
    seen = np.isfinite(first)
    info['n_stars'] = int(seen.sum())
    scale = float(np.sqrt(np.median([c.pixel_area_arcsec2 for c in chips])))
    span = np.where(seen, last - first, 0.0) / 365.25           # years
    protect = seen & (speed * span >= float(min_motion) * scale)
    info['n_protected'] = int(protect.sum())
    if not protect.any():
        return masks, info

    for c in chips:
        if c.key not in masks:
            continue
        x, y, close = positions[c.key]
        r = float(radius) / float(np.sqrt(c.pixel_area_arcsec2))
        ny, nx = c.shape
        mask = masks[c.key]
        near = protect & close & (x > -r) & (x < nx + r) & (y > -r) & (y < ny + r)
        for a, b in zip(x[near], y[near]):
            x0, x1 = max(int(np.floor(a - r)), 0), min(int(np.ceil(a + r)) + 1, nx)
            y0, y1 = max(int(np.floor(b - r)), 0), min(int(np.ceil(b + r)) + 1, ny)
            if x1 <= x0 or y1 <= y0:
                continue
            yy, xx = np.mgrid[y0:y1, x0:x1]
            mask[y0:y1, x0:x1] |= np.hypot(xx - a, yy - b) <= r
        info['n_pixels'] += int(mask.sum())
    return masks, info


def detect_outliers(chips, *, snr=(3.5, 3.0), scale=(1.2, 0.7), grow=1,
                    pixfrac=1.0, kernel='square', pixel_scale=None,
                    minmed_nsigma=(4.0, 3.0), minmed_max_n=3,
                    min_contributors=2, block=1024, workdir=None,
                    protect=None):
    """Outlier masks for ``chips`` (one detector and filter, one frame).

    ``protect`` is an optional ``{chip.key: bool array}`` of pixels that are
    never flagged (see :func:`star_protection`); they still enter the median.

    Returns ``{'flags': {chip.key: bool array, True at an outlier},
    'stats': {chip.key: {...}}, 'grid': TileGrid, 'median': float32 array,
    'n_contributors': int16 array}``. The masks cover good pixels only: a pixel
    the adapter already calls bad is never reported.
    """
    from scipy.ndimage import binary_dilation, binary_erosion

    from campfire_pipeline.imaging.drizzle import (_prepare_chip_input,
                                                   drizzle_tile_singles)

    chips = list(chips)
    keys = [c.key for c in chips]
    if len(set(keys)) != len(keys):
        raise ValueError('detect_outliers: chip keys must be unique')
    grid = stack_grid(chips, pixel_scale=pixel_scale)
    out_shape = grid.array_shape
    ny, nx = out_shape
    log(f'outlier: {len(chips)} chips on a {nx}x{ny} grid at '
        f'{grid.pixel_scale:.4f} arcsec')

    tmp = tempfile.mkdtemp(prefix='.outlier_', dir=workdir)
    try:
        # -- 1. single drizzles, kept on disk ------------------------------
        singles = []          # (path, sly, slx, var_bg scalar, coefficient)
        order = iter(chips)
        for sci, wht, prep in drizzle_tile_singles(
                chips, grid, out_shape, pixfrac=pixfrac, kernel=kernel,
                weight_type='ivm'):
            # drizzle_tile_singles yields in input order and skips chips that
            # miss the grid; stack_grid covers every chip, so none is skipped.
            chip = next(order)
            good, var_bg, coefficient = _noise_terms(prep)
            positive = wht[wht > 0]
            if positive.size:
                sci = np.where(wht >= _MASKPT * np.median(positive), sci, np.nan)
            path = os.path.join(tmp, f'{len(singles):05d}.npy')
            np.save(path, sci.astype(np.float32))
            level = float(np.median(var_bg[good])) if good.any() else np.inf
            singles.append((path, prep['sly'], prep['slx'], level, coefficient))
            del sci, wht, prep
        if len(singles) != len(chips):
            raise RuntimeError('outlier: a chip did not land on the stack grid')

        # -- 2. median, minimum and contributor count, block by block -------
        median = np.full(out_shape, np.nan, dtype=np.float32)
        minimum = np.full(out_shape, np.nan, dtype=np.float32)
        var_min = np.zeros(out_shape, dtype=np.float32)
        count = np.zeros(out_shape, dtype=np.int16)
        for y0 in range(0, ny, block):
            for x0 in range(0, nx, block):
                y1, x1 = min(y0 + block, ny), min(x0 + block, nx)
                members = [s for s in singles
                           if s[1].start < y1 and s[1].stop > y0
                           and s[2].start < x1 and s[2].stop > x0]
                if not members:
                    continue
                stack = np.full((len(members), y1 - y0, x1 - x0), np.nan,
                                dtype=np.float32)
                for k, (path, sly, slx, _, _) in enumerate(members):
                    single = np.load(path, mmap_mode='r')
                    ya, yb = max(sly.start, y0), min(sly.stop, y1)
                    xa, xb = max(slx.start, x0), min(slx.stop, x1)
                    stack[k, ya - y0:yb - y0, xa - x0:xb - x0] = single[
                        ya - sly.start:yb - sly.start,
                        xa - slx.start:xb - slx.start]
                n = np.isfinite(stack).sum(axis=0).astype(np.int16)
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', RuntimeWarning)
                    med = np.nanmedian(stack, axis=0)
                    low = np.nanmin(stack, axis=0)
                which = np.argmin(np.where(np.isfinite(stack), stack, np.inf),
                                  axis=0)
                levels = np.array([m[3] for m in members], dtype=np.float32)
                coefs = np.array([m[4] for m in members], dtype=np.float32)
                with np.errstate(invalid='ignore'):
                    v = levels[which] + coefs[which] * np.clip(low, 0, None)
                median[y0:y1, x0:x1] = med
                minimum[y0:y1, x0:x1] = low
                var_min[y0:y1, x0:x1] = np.where(n > 0, v, 0.0)
                count[y0:y1, x0:x1] = n
                del stack

        # minmed: where few chips overlap, take the minimum if the median
        # stands above it by more than the noise allows.
        n_min = 0
        if minmed_max_n >= 2:
            few = (count >= 2) & (count <= int(minmed_max_n))
            with np.errstate(invalid='ignore'):
                excess = (median - minimum) / np.sqrt(var_min)
            take = few & (excess > float(minmed_nsigma[0]))
            near = binary_dilation(take, structure=np.ones((3, 3), bool)) & few
            take |= near & (excess > float(minmed_nsigma[1]))
            median[take] = minimum[take]
            n_min = int(take.sum())
        del minimum, var_min
        log(f'outlier: median built; minimum taken at {n_min} grid pixels; '
            f'{int((count >= min_contributors).sum())} of '
            f'{int((count > 0).sum())} covered pixels have at least '
            f'{min_contributors} contributors')

        # -- 3 and 4. blot back and test, chip by chip ----------------------
        finite = np.isfinite(median)
        filled = np.where(finite, median, 0.0).astype(np.float32)
        # the interpolation reaches three pixels: keep that far from any hole
        solid = binary_erosion(finite, structure=np.ones((3, 3), bool),
                               iterations=3).astype(np.float32)
        enough = (count >= int(min_contributors)).astype(np.float32)
        flags, stats = {}, {}
        for chip in chips:
            prep = _prepare_chip_input(chip, grid, out_shape, weight_type='ivm')
            good, var_bg, coefficient = _noise_terms(prep)
            in_shape = prep['in_shape']
            pixmap = prep['pixmap']
            model = _blot(filled, pixmap, in_shape, 'poly5')
            covered = _blot(solid, pixmap, in_shape, 'linear') > 0.999
            shared = _blot(enough, pixmap, in_shape, 'nearest') > 0.5
            testable = good & covered & shared & np.isfinite(model)
            kept_out = 0
            if protect is not None and protect.get(chip.key) is not None:
                kept_out = int((testable & protect[chip.key]).sum())
                testable &= ~protect[chip.key]
            sigma = np.sqrt(var_bg + coefficient * np.clip(model, 0, None))
            gradient = abs_gradient(model)
            with np.errstate(invalid='ignore'):
                diff = np.abs(prep['data'] - model)
                first = testable & (diff > scale[0] * gradient + snr[0] * sigma)
                mask = first
                if grow and first.any():
                    near = binary_dilation(
                        first, structure=np.ones((3, 3), bool),
                        iterations=int(grow))
                    mask = first | (testable & near & (
                        diff > scale[1] * gradient + snr[1] * sigma))
            flags[chip.key] = mask
            stats[chip.key] = {
                'n_flagged': int(mask.sum()), 'n_first_pass': int(first.sum()),
                'n_testable': int(testable.sum()), 'n_good': int(good.sum()),
                'n_protected': kept_out,
                'poisson_coefficient': coefficient,
            }
            del prep, model, pixmap
        return {'flags': flags, 'stats': stats, 'grid': grid, 'median': median,
                'n_contributors': count}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
