"""
`cfpipe nircam rgb` orchestration.

For each tile in the field, locates the per-filter NIRCam mosaic i2d
cubes produced by `cfpipe nircam combine` and combines them into a
trilogy-style RGB PNG (one native-resolution image plus one downsampled
preview). Filter→color mapping and stretch tunables come from the
optional ``[<field>.rgb]`` block in fields.toml; reused trilogy stretch
core lives in ``campfire_pipeline.nircam.trilogy``.

This step does not produce a tile pyramid — that's the deploy layer's
job (`campfire deploy tiles`). It also does not stitch tiles into a
field-wide composite; each tile gets its own pair of PNGs sharing the
mosaic's native WCS.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from glob import glob

import numpy as np
from astropy.io import fits

from campfire_pipeline.common.io import log
from campfire_pipeline.config import get_nircam_step_config
from campfire_pipeline.nircam.field import Field
from campfire_pipeline.nircam.trilogy import (
    RGBConfig,
    apply_rgb_stretch,
    compute_rgb_stretch_params,
)


def _resolve_pixel_scale_str(value):
    """Coerce a config pixel-scale value to a ``'NNmas'`` string.

    Mirrors ``steps/resample._resolve_pixel_scale`` so the RGB
    subcommand resolves the default the same way the resample step did.
    """
    if isinstance(value, str):
        if not value.endswith('mas'):
            raise ValueError(f"pixel_scale must look like 'NNmas' (got {value!r})")
        return value
    if value > 1:
        return f'{int(value)}mas'
    return f'{int(value * 1000)}mas'


def _find_mosaic(filter_dir, field_name, filtname, pixel_scale_str, tile):
    """Find the i2d cube for a (filter, tile) pair, agnostic to ``version``.

    The resample step bakes ``version`` into the filename as ``v0_1``
    (or whatever override is configured); we glob it out so the RGB
    command works against existing reductions without having to know
    the reduction version. If multiple versions are present, the
    lexicographically last (typically the newest) is used and a warning
    is logged.
    """
    pattern = os.path.join(
        filter_dir,
        f'mosaic_nircam_{filtname}_{field_name}_{pixel_scale_str}_*_{tile}_i2d.fits',
    )
    matches = sorted(glob(pattern))
    if not matches:
        return None
    if len(matches) > 1:
        log(
            f"  [{tile}/{filtname}] multiple mosaic versions found, "
            f"using {os.path.basename(matches[-1])}"
        )
    return matches[-1]


def _load_sci_wht(path):
    """Read SCI + WHT from a NIRCam i2d cube; NaN-out SCI where WHT==0.

    The resample step already sets SCI=NaN at WHT=0 pixels before
    extension splitting, but older mosaics on disk may predate that
    pass — the explicit NaN-fill here is idempotent and protects the
    stretch computation either way.
    """
    with fits.open(path, memmap=False) as hdul:
        sci = np.asarray(hdul['SCI'].data, dtype=np.float32)
        try:
            wht = np.asarray(hdul['WHT'].data, dtype=np.float32)
        except KeyError:
            wht = None
    if wht is not None:
        sci = np.where(wht > 0, sci, np.nan)
    return sci


def _validate_rgb_config(field):
    """Pull ``[<field>.rgb]`` off the Field and check it against ``field.filters``."""
    rgb_block = field.rgb
    if not rgb_block:
        raise ValueError(
            f"No [{field.name}.rgb] block defined in fields.toml"
        )
    channels = rgb_block.get('channels')
    if not channels:
        raise ValueError(
            f"[{field.name}.rgb] is missing a `channels` table "
            f"(map of filter → [r,g,b] weights)"
        )
    missing = [f for f in channels if f not in field.filters]
    if missing:
        raise ValueError(
            f"[{field.name}.rgb.channels] references filters not declared in "
            f"`filters`: {missing}. Add them to the field's `filters` list "
            f"or remove from the rgb channels."
        )
    filter_channels = {}
    for filt, weights in channels.items():
        arr = np.asarray(weights, dtype=np.float64)
        if arr.shape != (3,):
            raise ValueError(
                f"[{field.name}.rgb.channels.{filt}] must be a 3-element "
                f"[r,g,b] list (got shape {arr.shape})"
            )
        filter_channels[filt] = {'color': arr}
    return RGBConfig(
        filter_channels=filter_channels,
        noisesig=float(rgb_block.get('noisesig', 2.0)),
        noiselum=float(rgb_block.get('noiselum', 0.12)),
        satpercent=float(rgb_block.get('satpercent', 0.01)),
    )


def _output_paths(field, tile, pixel_scale_str):
    base = os.path.join(field.products_dir, f'{tile}_{pixel_scale_str}_rgb')
    return base + '.png', base + '_preview.png'


def _save_pngs(rgb, alpha, native_path, preview_path, preview_max_dim):
    """Write the RGBA array as native-res + downsampled PNGs (PIL).

    The stretch operates on the FITS pixel array as-is (row 0 = bottom,
    matching `origin='lower'` everywhere else in the pipeline's
    diagnostic plots). PIL treats arrays as `origin='upper'`, so we
    flip vertically here so the saved PNGs render with North up / sky
    orientation when opened in any standard image viewer.
    """
    from PIL import Image

    rgba = np.dstack([rgb, alpha[..., None]])
    rgba = np.flipud(rgba)
    img = Image.fromarray(rgba, mode='RGBA')
    img.save(native_path, format='PNG', optimize=False)
    log(f"  wrote {os.path.basename(native_path)} ({img.size[0]}×{img.size[1]})")

    preview = img.copy()
    preview.thumbnail((preview_max_dim, preview_max_dim), Image.LANCZOS)
    preview.save(preview_path, format='PNG', optimize=False)
    log(
        f"  wrote {os.path.basename(preview_path)} "
        f"({preview.size[0]}×{preview.size[1]})"
    )


def _render_tile(args):
    """Worker: render a single tile's RGB PNGs.

    Lives at module top-level so the multiprocessing 'spawn' start method
    on macOS can pickle it. Returns a status string; raises if the tile
    is unrecoverable so the pool surfaces the error.
    """
    (tile, field, rgb_config, pixel_scale_str, preview_max_dim, overwrite) = args

    native_path, preview_path = _output_paths(field, tile, pixel_scale_str)
    if not overwrite and os.path.exists(native_path) and os.path.exists(preview_path):
        log(f"[{tile}] skip (exists; pass --overwrite to rebuild)")
        return f'{tile}: skipped'

    log(f"[{tile}] loading mosaics ({pixel_scale_str})")
    per_filter_data = {}
    for filt in rgb_config.filter_channels:
        path = _find_mosaic(
            field.filter_dir(filt), field.name, filt, pixel_scale_str, tile,
        )
        if path is None:
            log(f"  [{tile}/{filt}] no mosaic found, skipping tile")
            return f'{tile}: skipped (missing {filt} mosaic)'
        per_filter_data[filt] = _load_sci_wht(path)

    shapes = {f: arr.shape for f, arr in per_filter_data.items()}
    if len(set(shapes.values())) > 1:
        raise ValueError(
            f"[{tile}] mosaic shapes differ across filters: {shapes}. "
            f"All filters must be drizzled to the same tile WCS."
        )

    log(f"[{tile}] computing stretch")
    stretch = compute_rgb_stretch_params(per_filter_data, rgb_config)
    log(
        f"[{tile}] stretch: blackpoint={stretch.blackpoint:.4e}, "
        f"whitepoint={stretch.whitepoint:.4e}, noiselum={stretch.noiselum}"
    )

    rgb, alpha = apply_rgb_stretch(per_filter_data, rgb_config, stretch)
    _save_pngs(rgb, alpha, native_path, preview_path, preview_max_dim)
    return f'{tile}: ok'


def run_rgb(
    field: Field,
    config: dict,
    *,
    tiles=None,
    pixel_scale=None,
    preview_max_dim: int = 2048,
    n_processes: int = 1,
    overwrite: bool = False,
):
    """Generate per-tile RGB PNGs for ``field`` using its ``[rgb]`` config.

    Parameters
    ----------
    field
        Loaded Field with ``setup_workspace()`` already called.
    config
        Top-level pipeline config (only used to resolve the default
        resample pixel scale, so the RGB subcommand picks the same
        scale ``cfpipe nircam combine`` produced).
    tiles
        Optional iterable of tile names; defaults to ``field.tiles.keys()``.
    pixel_scale
        Optional override (e.g. ``'30mas'``). Falls back to the
        resample step config (``[nircam.resample].pixel_scale``).
    preview_max_dim
        Long-axis pixel cap for the downsampled preview PNG.
    n_processes
        Tiles are independent — pool over them when >1.
    overwrite
        Re-render even if both output PNGs already exist.
    """
    rgb_config = _validate_rgb_config(field)

    if pixel_scale is None:
        resample_cfg = get_nircam_step_config('resample', config, field)
        pixel_scale = resample_cfg.get('pixel_scale', '60mas')
    pixel_scale_str = _resolve_pixel_scale_str(pixel_scale)

    tile_names = list(tiles) if tiles else list(field.tiles.keys())
    if not tile_names:
        log(f"Field '{field.name}' has no tiles defined; nothing to do.")
        return
    unknown = [t for t in tile_names if t not in field.tiles]
    if unknown:
        raise ValueError(
            f"Unknown tiles for field '{field.name}': {unknown}. "
            f"Available: {list(field.tiles.keys())}"
        )

    log(
        f"RGB: field={field.name}, tiles={tile_names}, "
        f"pixel_scale={pixel_scale_str}, "
        f"channels={list(rgb_config.filter_channels.keys())}"
    )

    work = [
        (tile, field, rgb_config, pixel_scale_str, preview_max_dim, overwrite)
        for tile in tile_names
    ]

    if n_processes <= 1 or len(work) == 1:
        results = [_render_tile(w) for w in work]
    else:
        ctx = mp.get_context('spawn')
        with ctx.Pool(processes=min(n_processes, len(work))) as pool:
            results = pool.map(_render_tile, work)

    for r in results:
        log(f"  {r}")
