"""resample: drizzle the exposures of one HST instrument and filter onto tiles.

The HST counterpart of ``nircam/steps/resample.py``, and deliberately the same
step with a different front end. The tile is the field's: tangent point,
rotation, ``crpix`` and shape come from ``fields.toml`` through the same
``Field.get_tile_wcs`` NIRCam uses, so pixel (i, j) of an HST mosaic and of
the NIRCam mosaic of that tile are the same patch of sky. The drizzle is the
shared ``imaging/drizzle.py`` on chips (``hst/exposure.py``); what happens to
the tile afterwards (background subtraction, split files, plots) is the shared
``imaging/mosaic_finish.py``.

What differs from NIRCam:

* **Which exposures overlap a tile** is decided from each chip's corners
  through its own WCS (an HST file has no ``S_REGION`` footprint keyword).
* **Input identity** in the manifest is the hash of the whole file
  (``imaging/manifest.compute_whole_file_hash``): both ACS chips, the outlier
  flags, the sky keywords and the distortion tables all count.
* **Name**: ``mosaic_<instrument>_<filter>_<field>_<scale>_<tile>``, from
  ``[<instrument>.resample].mosaic_name``.

Inputs are the combine working copies (``HstField.materialize_work``), which
carry the field-level outlier flags where an exposure overlaps others.
"""

import os

import numpy as np

from campfire_pipeline.common.io import log
from campfire_pipeline.hst.exposure import open_exposure


def _pixel_scale(value):
    """``'30mas'`` or a number to ``(arcsec, '<n>mas')``."""
    if isinstance(value, str):
        if not value.endswith('mas'):
            raise ValueError(f"pixel_scale must look like '30mas', got {value!r}")
        return float(value[:-3]) / 1000.0, value
    if value > 1:
        return float(value) / 1000.0, f'{int(value)}mas'
    return float(value), f'{int(round(value * 1000))}mas'


def chip_polygon(chip, n=5):
    """Outline of a chip on the sky as a shapely polygon (degrees), from
    ``n`` points along each edge through the chip's own WCS."""
    from shapely.geometry import Polygon

    ny, nx = chip.shape
    t = np.linspace(0.0, 1.0, n)[:-1]
    xs = np.concatenate([t * (nx - 1), np.full(t.size, nx - 1.0),
                         (1 - t) * (nx - 1), np.zeros(t.size)])
    ys = np.concatenate([np.zeros(t.size), t * (ny - 1),
                         np.full(t.size, ny - 1.0), (1 - t) * (ny - 1)])
    ra, dec = chip.pixel_to_world(xs, ys)
    return Polygon(np.column_stack([np.asarray(ra, float), np.asarray(dec, float)]))


def exposure_polygon(chips):
    """Outline of an exposure: the union of its chips' outlines."""
    from shapely.ops import unary_union

    return unary_union([chip_polygon(c) for c in chips])


def _input_record(path, chips):
    from campfire_pipeline.imaging.manifest import whole_file_entry

    first = chips[0]
    return whole_file_entry(path, extra={
        'visit': first.rootname[:6],
        'detector': first.instrument,
        'date_obs': first.date_obs,
        'exptime': first.exptime,
        'proposid': first.proposid,
    })


def _tile_plan(filtname, exposure_files, field, step_config, tiles):
    """For each requested tile: its mosaic name and files, the exposures that
    overlap it, and whether the mosaic on disk is stale.

    Returns ``(plan, chips_of, pixel_scale, scale_str)`` with ``plan`` a list
    of dicts (``tile``, ``name``, ``mosaic_file``, ``manifest_path``,
    ``selected``, ``stale``, ``reasons``).
    """
    from shapely.geometry import Polygon

    from campfire_pipeline.imaging.manifest import (
        build_mosaic_name, check_config_changed, check_inputs_changed,
    )

    pixel_scale, scale_str = _pixel_scale(step_config['pixel_scale'])
    template = step_config.get('mosaic_name') or (
        f'mosaic_{field.instrument}_[filter]_[field_name]_[pixel_scale]_[tile]')
    if f'_{field.instrument}_' not in f'_{template}_':
        raise ValueError(
            f"[{field.instrument}.resample].mosaic_name = {template!r} does "
            f"not contain the instrument name; a mosaic of another instrument "
            f"with the same filter name would overwrite it")

    if tiles is None:
        tiles = list(field.tiles)
    elif isinstance(tiles, str):
        tiles = [tiles]
    if not tiles:
        raise ValueError(f"field '{field.name}' defines no tiles to drizzle onto")

    files = sorted(exposure_files)
    chips_of = {f: open_exposure(f) for f in files}
    wrong = sorted({c.filter for cs in chips_of.values() for c in cs} - {filtname})
    if wrong:
        raise ValueError(
            f'resample {filtname}: the inputs include exposures taken in {wrong}')
    outline = {f: exposure_polygon(chips_of[f]) for f in files}

    out_dir = field.filter_dir(filtname)
    plan = []
    for tile in tiles:
        name = build_mosaic_name(filtname, field.name, scale_str, tile,
                                 template=template)
        mosaic_file = os.path.join(out_dir, f'{name}_i2d.fits')
        manifest_path = os.path.join(out_dir, f'{name}_manifest.json')
        tile_polygon = Polygon(field.get_tile_corners(tile))
        selected = [f for f in files if outline[f].intersects(tile_polygon)]
        stale, reasons = False, []
        if selected:
            if not os.path.exists(mosaic_file):
                stale, reasons = True, ['mosaic does not exist']
            else:
                stale, reasons = check_inputs_changed(manifest_path, selected)
                reasons = list(reasons)
                if check_config_changed(manifest_path, {'resample': step_config},
                                        scale_str):
                    stale = True
                    reasons.append('processing config changed')
        plan.append({'tile': tile, 'name': name, 'mosaic_file': mosaic_file,
                     'manifest_path': manifest_path, 'selected': selected,
                     'n_inputs': len(selected), 'stale': stale,
                     'reasons': reasons})
    return plan, chips_of, pixel_scale, scale_str


def tile_status(filtname, exposure_files, field, step_config, tiles=None):
    """Which tiles ``resample_step`` would rebuild for these inputs, without
    building anything (``cfpipe <instrument> check``)."""
    return _tile_plan(filtname, exposure_files, field, step_config, tiles)[0]


def resample_step(filtname, exposure_files, field, step_config,
                  reduction_version, overwrite=False, tiles=None):
    """Drizzle ``exposure_files`` (working copies of one filter) onto the
    field's tiles; return the list of mosaic files that exist afterwards.

    ``field`` is an :class:`~campfire_pipeline.hst.field.HstField`;
    ``step_config`` the effective ``[<instrument>.resample]`` block. ``tiles``
    names the tiles to build (default: every tile of the field); a tile no
    exposure overlaps is skipped. A tile is rebuilt when its mosaic is
    missing, an input was added, removed or changed, or a pixel-affecting
    setting changed; otherwise only missing split files are restored.
    """
    from campfire_pipeline.imaging.drizzle import drizzle_tile
    from campfire_pipeline.imaging.manifest import create_manifest, write_manifest
    from campfire_pipeline.imaging.mosaic_finish import finish_mosaic

    plan, chips_of, pixel_scale, scale_str = _tile_plan(
        filtname, exposure_files, field, step_config, tiles)
    n_files = len(chips_of)

    built = []
    for entry in plan:
        tile, name, selected = entry['tile'], entry['name'], entry['selected']
        mosaic_file, manifest_path = entry['mosaic_file'], entry['manifest_path']
        log(f'resample: tile {tile}, {field.instrument} {filtname}, {scale_str}: '
            f'{len(selected)} of {n_files} exposures overlap')
        if not selected:
            continue

        needs_rebuild = overwrite or entry['stale']
        if entry['stale']:
            log(f"  tile {tile} stale: {'; '.join(entry['reasons'])}")
        elif not overwrite:
            log(f'  tile {tile} up to date ({len(selected)} inputs unchanged)')

        if needs_rebuild:
            crpix, crval, shape, rotation = field.get_tile_wcs(
                tile, pixel_scale=scale_str)
            chips = [c for f in selected for c in chips_of[f]]
            log(f'  drizzling {len(chips)} chips of {len(selected)} exposures '
                f'-> {os.path.basename(mosaic_file)}')
            drizzle_tile(
                chips, mosaic_file,
                crpix=crpix, crval=crval, shape=shape, rotation=rotation,
                pixel_scale=pixel_scale,
                pixfrac=float(step_config['pixfrac']),
                kernel=step_config['kernel'],
                weight_type=step_config['weight_type'],
                reduction_version=reduction_version,
                compress_context=step_config['compress_context'],
                write_context=step_config['write_context'],
            )
            manifest = create_manifest(
                name, field, filtname, tile, scale_str, selected,
                {'resample': step_config},
                inputs=[_input_record(f, chips_of[f]) for f in selected],
                instrument=field.instrument)
            write_manifest(manifest, manifest_path)

        finish_mosaic(mosaic_file, name, step_config, needs_rebuild)
        built.append(mosaic_file)
    return built
