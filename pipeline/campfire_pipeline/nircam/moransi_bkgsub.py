"""
Moran's-I source masking for NIRCam background subtraction.

An *alternative source-masking strategy* for the background fit — NOT a new
background estimator. The actual background is still fit by
``photutils.Background2D`` in :meth:`SubtractBackground.estimate_background`
(``nircam/bkgsub.py``); this module only decides *which pixels* that fit is
allowed to see.

Moran's I is a spatial-autocorrelation statistic. Computed per non-overlapping
patch, source-contaminated patches (coherent structure) score high and blank-sky
patches (near-random) score low. Keeping the low-scoring patches as background
gives a source mask without thresholding on flux level. See
``docs/moransi-background-scoping.md`` for the design and the A/B plan.

Ported and hardened from research code by H. C. Ferguson (STScI). Changes vs.
the donated version:

  * **Correctness:** the statistic is NaN/valid-aware. The donated code set
    masked/off-detector pixels to ``0.0`` *before* computing each patch's
    mean/std, biasing every partially-masked patch. Here invalid pixels are
    excluded from the per-patch mean, std, and neighbour sums, and patches with
    too few valid pixels return NaN (treated as source).
  * **Performance:** ``morans_i_local`` is fully vectorized over patches
    (no Python per-patch loop), matching the naive statistic bit-for-bit on
    fully-valid patches.
  * **Convention:** :func:`morans_source_mask` returns the CAMPFIRE mask
    polarity ``True = source/exclude`` (the donated ``patch_mask`` was
    ``True = background/keep``).

The returned per-patch value uses the donated W-normalisation, which is a
global constant across equal-sized patches — a monotone *ranking* statistic, not
a true Moran's I. That is fine here: the mask is derived by a percentile cut on
the ranking, so absolute scale is irrelevant.
"""

import warnings

import numpy as np
from astropy.nddata import block_replicate

# Default "queen" spatial-weights kernel: 8 neighbours, no self-weight.
_QUEEN_KERNEL = np.array([[1, 1, 1],
                          [1, 0, 1],
                          [1, 1, 1]], dtype=np.float64)


def _resolve_kernel(kernel):
    """Return a 2-D float weights kernel from a name or array."""
    if kernel is None or (isinstance(kernel, str) and kernel == "queen"):
        return _QUEEN_KERNEL
    if isinstance(kernel, str):
        raise ValueError(f"Unknown kernel name: {kernel!r} (use 'queen' or an array)")
    k = np.asarray(kernel, dtype=np.float64)
    if k.ndim != 2 or k.shape[0] % 2 == 0 or k.shape[1] % 2 == 0:
        raise ValueError("kernel must be a 2-D array with odd dimensions")
    return k


def _block_neighbour_sum(z, kernel):
    """Per-block neighbour-weighted sum with zero-fill at block edges.

    ``z`` has shape ``(N, p, p)``. Reproduces
    ``scipy.ndimage.convolve(block, kernel, mode='constant', cval=0.0)`` applied
    independently to each block, but batched over all ``N`` blocks via shifted
    adds. ``mode='constant'`` means neighbours outside the *block* count as 0.
    The kernel is flipped so this matches convolution (not correlation) for
    asymmetric kernels; symmetric kernels (the default queen) are unaffected.
    """
    _, p, _ = z.shape
    k = kernel[::-1, ::-1]                      # flip -> convolution semantics
    kh, kw = k.shape
    ci, cj = kh // 2, kw // 2
    out = np.zeros_like(z)
    for di in range(kh):
        for dj in range(kw):
            w = k[di, dj]
            if w == 0:
                continue
            si, sj = di - ci, dj - cj           # neighbour offset within block
            r0s, r1s = max(0, si), p + min(0, si)
            c0s, c1s = max(0, sj), p + min(0, sj)
            r0d, r1d = max(0, -si), p + min(0, -si)
            c0d, c1d = max(0, -sj), p + min(0, -sj)
            out[:, r0d:r1d, c0d:c1d] += w * z[:, r0s:r1s, c0s:c1s]
    return out


def morans_i_local(image, patch_size, kernel=None, valid=None, min_valid=1):
    """Per-patch Moran's-I ranking statistic on non-overlapping patches.

    Vectorized and NaN/valid-aware. Identical to the naive per-patch statistic
    on fully-valid patches.

    Parameters
    ----------
    image : 2-D array
        Input image. NaN pixels are treated as invalid.
    patch_size : int
        Side length ``p`` of the square patches (pixels).
    kernel : str or 2-D array, optional
        Spatial-weights kernel; ``"queen"``/None → 8-neighbour.
    valid : 2-D bool array, optional
        ``True`` where a pixel is usable. Combined with ``~isnan(image)``.
    min_valid : int
        Patches with fewer than this many valid pixels return NaN.

    Returns
    -------
    2-D float array of shape ``(H_pad // p, W_pad // p)`` — one value per patch,
    NaN where the patch had too few valid pixels.
    """
    kernel = _resolve_kernel(kernel)
    p = int(patch_size)
    if p < 2:
        raise ValueError(f"patch_size must be >= 2 (got {p})")

    img = np.asarray(image, dtype=np.float64)
    invalid = np.isnan(img)
    if valid is not None:
        invalid = invalid | ~np.asarray(valid, dtype=bool)

    H, W = img.shape
    ph, pw = (-H) % p, (-W) % p                 # pad only when not a multiple
    nby, nbx = (H + ph) // p, (W + pw) // p

    # NaN-pad so padded pixels are ignored by the nan-aware reductions.
    work = np.full((H + ph, W + pw), np.nan, dtype=np.float32)
    work[:H, :W] = np.where(invalid, np.nan, img).astype(np.float32)

    # (nby, nbx, p, p) -> (N, p, p). The final reshape forces one copy.
    blocks = (work.reshape(nby, p, nbx, p)
                  .transpose(0, 2, 1, 3)
                  .reshape(nby * nbx, p, p))

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", r"(Mean|Degrees).*", RuntimeWarning)
        warnings.filterwarnings("ignore", "All-NaN slice.*", RuntimeWarning)
        nvalid = np.sum(~np.isnan(blocks), axis=(1, 2))
        mean = np.nanmean(blocks, axis=(1, 2))
        std = np.nanstd(blocks, axis=(1, 2))

    # Standardize; invalid pixels and undefined stats collapse to 0 so they add
    # nothing to the spatial-autocorrelation sum.
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (blocks - mean[:, None, None]) / std[:, None, None]
    z = np.nan_to_num(z, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)

    ns = _block_neighbour_sum(z, kernel)
    n = p * p
    edges = 2 * p * 4 - 4
    w_norm = 8 * n - edges
    result = (n / w_norm) * np.sum(z * ns, axis=(1, 2), dtype=np.float64)

    # Constant (zero-variance) valid patches -> 0 (no structure); too-empty
    # patches -> NaN (untrustworthy).
    result[(std == 0) & (nvalid > 0)] = 0.0
    result[nvalid < min_valid] = np.nan
    return result.reshape(nby, nbx)


def morans_source_mask(sci, patch_size, *, input_mask=None, error=None,
                       kernel="queen", percentile=40.0, min_valid_frac=0.25):
    """Source mask from the Moran's-I statistic (CAMPFIRE polarity).

    Site-agnostic: operates on any 2-D science array plus the usual
    off-detector / error inputs, so it plugs into the mosaic
    ``SubtractBackground`` path and (later) the per-exposure masking seams with
    the same signature.

    Parameters
    ----------
    sci : 2-D array
        Science image (any units).
    patch_size : int
        Patch side in pixels. Also the natural background box scale, though the
        downstream ``Background2D`` box size is configured independently.
    input_mask : 2-D bool array, optional
        ``True`` = mask (do not use), e.g. off-detector / DQ / NaN. Matches the
        pipeline fit-mask convention.
    error : 2-D array, optional
        Error/uncertainty. Pixels with NaN or ``error <= 0`` are treated as no
        coverage and excluded.
    kernel : str or 2-D array
        Spatial-weights kernel (default ``"queen"``).
    percentile : float
        Keep the lowest-I ``percentile`` fraction (of valid pixels) as
        background. Lower preserves more bright-source wings; higher removes
        them.
    min_valid_frac : float
        Minimum fraction of valid pixels for a patch to be judged; sparser
        patches are marked as source.

    Returns
    -------
    2-D bool array, ``True = source/exclude`` from the background fit.
    """
    sci = np.asarray(sci)
    valid = ~np.isnan(sci)
    if error is not None:
        error = np.asarray(error)
        with np.errstate(invalid="ignore"):
            valid &= ~np.isnan(error) & (error > 0)
    if input_mask is not None:
        valid &= ~np.asarray(input_mask, dtype=bool)

    p = int(patch_size)
    min_valid = max(1, int(round(min_valid_frac * p * p)))

    i_grid = morans_i_local(sci, p, kernel=kernel, valid=valid,
                            min_valid=min_valid)

    # Replicate the per-patch value to full (padded) resolution, then crop.
    rmi = block_replicate(i_grid, p, conserve_sum=False).astype(np.float32)
    rmi = rmi[:sci.shape[0], :sci.shape[1]]

    finite = valid & np.isfinite(rmi)
    if not finite.any():
        # Nothing usable to judge -> exclude everything from the fit.
        return np.ones(sci.shape, dtype=bool)

    threshold = np.percentile(rmi[finite], percentile)
    background_keep = np.isfinite(rmi) & (rmi < threshold)
    # True = source: high-autocorrelation patches AND untrustworthy (NaN-I)
    # patches are excluded from the background fit.
    return ~background_keep
