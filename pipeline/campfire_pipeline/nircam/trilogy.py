"""
Trilogy-style RGB stretch for NIRCam mosaics.

This is intentionally a small, dependency-light copy of the core RGB
algorithm in ``python/campfire/deploy/tiles_engine.py`` (the
``RGBConfig`` / ``RGBStretchParams`` dataclasses plus
``compute_rgb_stretch_params`` and ``apply_rgb_stretch``). The deploy-
side version is wrapped in tile-pyramid machinery (reprojection onto a
unified output grid, supertile splitting, PNG pyramid build) that we
don't need here — per-tile NIRCam mosaics for different filters already
share a WCS, so the stretch can be applied to the pixel-aligned arrays
directly.

The eventual plan is to consolidate so ``tiles_engine`` imports from
this module; until then the core compute / apply functions are kept in
sync by hand. Edits to either side should be mirrored.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class RGBConfig:
    """Per-filter color weights plus trilogy-style stretch tunables."""
    filter_channels: dict[str, dict]   # {filter: {'color': np.ndarray[3]}}
    noisesig: float = 2.0
    noiselum: float = 0.12
    satpercent: float = 0.01


@dataclass
class RGBStretchParams:
    """Precomputed global RGB stretch parameters."""
    blackpoint: float
    whitepoint: float
    noiselum: float
    rgb_lum_sum: np.ndarray            # shape (3,), sum of all filter color weights


def compute_rgb_stretch_params(
    per_filter_data: dict[str, np.ndarray],
    rgb_config: RGBConfig,
    *,
    n_samples_per_filter: int = 200_000,
    rng_seed: int = 42,
) -> RGBStretchParams:
    """
    Precompute global blackpoint/whitepoint by sampling pixels from
    each filter's data.

    Mirrors ``tiles_engine.compute_rgb_stretch_params`` but takes
    already-loaded arrays (NaN where coverage is bad) instead of file
    paths — the per-tile NIRCam workflow has these in hand once the i2d
    cube has been opened.

    Steps:
      1. ``rgb_lum_sum`` = sum of color weights across all filters
      2. Sample ``n_samples_per_filter`` finite pixels from each filter
      3. Per-channel contributions = ``color[ch] * samples / rgb_lum_sum[ch]``
      4. ``blackpoint = noisesig * max(sigma_clipped_std per R,G,B)``
      5. ``whitepoint = nanpercentile(all_channels, 100*(1 - 0.01*satpercent))``
    """
    from astropy.stats import sigma_clipped_stats

    rgb_lum_sum = np.zeros(3, dtype=np.float64)
    for filt_info in rgb_config.filter_channels.values():
        rgb_lum_sum += np.asarray(filt_info['color'], dtype=np.float64)

    rng = np.random.default_rng(rng_seed)
    ch_samples: list[list[np.ndarray]] = [[], [], []]

    for filt_name, data in per_filter_data.items():
        color = np.asarray(
            rgb_config.filter_channels[filt_name]['color'], dtype=np.float64,
        )
        flat = data.ravel()
        finite = flat[np.isfinite(flat)]
        if finite.size == 0:
            continue
        n = min(n_samples_per_filter, finite.size)
        idx = rng.choice(finite.size, size=n, replace=False)
        samples = finite[idx].astype(np.float64)
        for ch in range(3):
            if color[ch] > 0 and rgb_lum_sum[ch] > 0:
                ch_samples[ch].append(color[ch] * samples / rgb_lum_sum[ch])

    ch_arrays = [
        np.concatenate(parts) if parts else np.array([], dtype=np.float64)
        for parts in ch_samples
    ]
    if all(len(a) == 0 for a in ch_arrays):
        raise ValueError("No finite pixels found for RGB stretch computation")

    stds = []
    for arr in ch_arrays:
        if len(arr) > 0:
            _, _, std = sigma_clipped_stats(arr)
            stds.append(std)

    blackpoint = float(rgb_config.noisesig * max(stds))

    all_channels = np.concatenate([a for a in ch_arrays if len(a) > 0])
    unsatpercent = 1.0 - 0.01 * rgb_config.satpercent
    whitepoint = float(np.nanpercentile(all_channels, 100.0 * unsatpercent))

    return RGBStretchParams(
        blackpoint=blackpoint,
        whitepoint=whitepoint,
        noiselum=rgb_config.noiselum,
        rgb_lum_sum=rgb_lum_sum,
    )


def apply_rgb_stretch(
    per_filter_data: dict[str, np.ndarray],
    rgb_config: RGBConfig,
    stretch_params: RGBStretchParams,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Apply the trilogy log stretch to per-filter SCI data.

    Verbatim port of ``tiles_engine.apply_rgb_stretch``. Per-pixel
    ``lum_sum_2d`` handles the case where one filter is NaN at a pixel
    but others contribute, so the channel weighting renormalises rather
    than dropping the pixel.

    Returns
    -------
    rgb_uint8 : np.ndarray
        Shape ``(H, W, 3)``, dtype uint8.
    alpha : np.ndarray
        Shape ``(H, W)``, dtype uint8 (255 where any filter is finite,
        else 0).
    """
    first_data = next(iter(per_filter_data.values()))
    H, W = first_data.shape

    rgb_total = np.zeros((3, H, W), dtype=np.float64)
    lum_sum_2d = np.zeros((3, H, W), dtype=np.float64)
    any_valid = np.zeros((H, W), dtype=bool)

    for filt_name, data in per_filter_data.items():
        color = np.asarray(
            rgb_config.filter_channels[filt_name]['color'], dtype=np.float64,
        )
        valid = np.isfinite(data)
        any_valid |= valid
        for ch in range(3):
            if color[ch] > 0:
                rgb_total[ch] += np.where(valid, color[ch] * data, 0)
                lum_sum_2d[ch] += np.where(valid, color[ch], 0)

    lum_sum_2d = np.where(lum_sum_2d > 0, lum_sum_2d, np.nan)
    rgb_avg = rgb_total / lum_sum_2d

    bp = stretch_params.blackpoint
    wp = stretch_params.whitepoint
    noiselum = stretch_params.noiselum
    log_bp = np.log10(bp)
    log_wp = np.log10(wp)
    log_range = log_wp - log_bp

    result = np.zeros((H, W, 3), dtype=np.uint8)
    for ch in range(3):
        ch_data = rgb_avg[ch]
        with np.errstate(invalid='ignore', divide='ignore'):
            stretched = (np.log10(ch_data) - log_bp) / log_range
        stretched = stretched * (255 * (1 - noiselum)) + 255 * noiselum
        stretched = np.where(stretched > 255, 255, stretched)
        stretched = np.where(np.isnan(stretched) | (stretched < 0), 0, stretched)
        result[:, :, ch] = stretched.astype(np.uint8)

    alpha = np.where(any_valid, np.uint8(255), np.uint8(0))
    return result, alpha
