"""Click CLI for `campfire fitsgl` (epic #337).

Registered lazily by ``campfire.cli`` (like ``deploy``) so the base client never
imports the FitsGL producer. Phase 2 ships ``build``; ``deploy`` follows in Phase 3.

Usage:
    campfire fitsgl build --field cosmos
    campfire fitsgl build --field cosmos --pixel-scale 30mas
    campfire fitsgl build --field cosmos --tile PRIMER      # single-tile / off-grid
"""

import click

from campfire.fitsgl import build as _build


@click.group()
def fitsgl_group():
    """Build & deploy FitsGL tile-pyramid datasets for the map/cutout service."""
    pass


@fitsgl_group.command('build')
@click.option('--field', required=True, help='NIRCam field name (e.g. cosmos).')
@click.option('--tile', default=None,
              help='Build a single-tile standalone dataset instead of the fiducial '
                   'composite (the only path for off-grid tiles like PRIMER).')
@click.option('--pixel-scale', default='30mas', show_default=True,
              help='Mosaic pixel scale to build. All bands must share it to co-grid.')
@click.option('--processes', type=int, default=None,
              help='Per-level worker pool size (default: auto, one per level up to CPU count).')
@click.option('--out-dir', default=None,
              help='Dataset output root (default: $CAMPFIRE_ROOT/fitsgl).')
@click.option('--overwrite', is_flag=True,
              help='Rebuild every band from scratch. Pass this after a mosaic\'s '
                   'pixels change (band reuse keys on presence, not content) or after '
                   'changing a [build] knob.')
def build_cmd(field, tile, pixel_scale, processes, out_dir, overwrite):
    """Build a field's FitsGL dataset from native (rotated) mosaics — no reproject.

    The default builds the fiducial composite (each filter is one band; the filters
    co-grid so they RGB-composite). CAMPFIRE generates the fitsgl.toml; you never
    edit it. The display pyramid is RICE Q=8 (~0.03% lossy) — for photometry read
    the raw lossless mosaic, not the pyramid.
    """
    _build.run_build(
        field, pixel_scale=pixel_scale, tile=tile, processes=processes,
        overwrite=overwrite, out_dir=out_dir,
    )
