"""Build a FitsGL tile-pyramid dataset for a NIRCam field (epic #337, Phase 2).

`campfire fitsgl build` gathers a field's native (rotated) mosaics — **no
reprojection** — generates a `fitsgl.toml` programmatically, and calls the FitsGL
producer (`fitsgl.build.build_dataset`) as a library. Two shapes:

- **fiducial composite** (default): each filter becomes one FitsGL band whose input
  is the LIST of that filter's fiducial-tile mosaics (the SP8 pre-tiled path);
  `[build].shared_grid` co-grids the filters so they RGB-composite.
- **single tile** (`--tile`): each filter band's input is that one tile's mosaic —
  the only path for off-grid tiles (e.g. PRIMER) that can't join the composite.

The module is split into a **pure** layer (`select_mosaics` / `group_bands` /
`derive_viewer` / `build_fitsgl_toml` — no FitsGL, unit-testable in CI) and an
**orchestration** layer (`run_build`) that defers the `import fitsgl` so the base
client stays lean and the extra's absence surfaces as a pointed install hint.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import click
import toml

# Prefer the split single-HDU SCI image; fall back to the combined i2d cube
# (FitsGL's build_pyramid auto-selects the first 2D image HDU, so a multi-extension
# _i2d.fits resolves to SCI too). Other extensions (err/wht/srcmask) are not images
# FitsGL should tile.
_EXTENSION_PREFERENCE = ('sci', 'i2d')

# Default display knobs (mirrored into fitsgl.toml [build]/[viewer]).
_QUANTIZE_LEVEL = 8      # RICE Q=8 — ~0.03% lossy display pyramid (design §7)
_TILE_SIZE = 256


# ---------------------------------------------------------------------------
# Pure layer (no fitsgl import; unit-tested without real data)
# ---------------------------------------------------------------------------

def select_mosaics(dirs, field, filters, *, pixel_scale, tiles=None):
    """Discover the mosaics to feed FitsGL for a field at one pixel scale.

    Wraps :func:`campfire.deploy.nircam.discover_mosaics` and keeps one mosaic per
    (filter, tile): full-field mosaics matching ``pixel_scale`` (the string form,
    e.g. ``'30mas'``) and — when ``tiles`` is given — whose tile is in that set.
    Within a (filter, tile) the science image is chosen by
    :data:`_EXTENSION_PREFERENCE` (``sci`` then ``i2d``). Returns the underlying
    ``discover_mosaics`` dicts unchanged — ``{path, filter, tile, pixel_scale, epoch,
    extension, storage_key}`` (``storage_key`` is consumed by the deploy source-hash
    path, so keep it on the passthrough).
    """
    from campfire.deploy.nircam import discover_mosaics

    tileset = set(tiles) if tiles is not None else None
    best: dict[tuple[str, str], dict] = {}
    for m in discover_mosaics(dirs, field, filters):
        # Skip epoch subset mosaics (`cfpipe nircam combine --epoch`): they share a
        # (filter, tile) with the full-field mosaic but cover only part of it, so
        # they'd yield an incomplete map. `epoch == ''` is the full-field mosaic —
        # the only input the map pyramid should ever use.
        if m.get('epoch'):
            continue
        if m['pixel_scale'] != pixel_scale:
            continue
        if tileset is not None and m['tile'] not in tileset:
            continue
        if m['extension'] not in _EXTENSION_PREFERENCE:
            continue
        key = (m['filter'], m['tile'])
        incumbent = best.get(key)
        if incumbent is None or _extension_rank(m['extension']) < _extension_rank(
            incumbent['extension']
        ):
            best[key] = m
    return list(best.values())


def _extension_rank(ext: str) -> int:
    """Lower is preferred; unknown extensions sort last."""
    try:
        return _EXTENSION_PREFERENCE.index(ext)
    except ValueError:
        return len(_EXTENSION_PREFERENCE)


def group_bands(mosaics, *, single_tile):
    """Group selected mosaics into FitsGL bands keyed by filter.

    Composite ⇒ each filter's value is the LIST of its tile mosaic paths (sorted by
    tile, so band inputs are deterministic). Single-tile ⇒ each filter's value is
    its one mosaic path. Filters with no surviving mosaic are simply absent.
    Returns an ordered dict filter → (Path | list[Path]), filters sorted blue→red.
    """
    by_filter: dict[str, list] = {}
    for m in mosaics:
        by_filter.setdefault(m['filter'], []).append(m)

    bands: dict[str, object] = {}
    for filt in sorted(by_filter, key=_filter_wavelength):
        entries = sorted(by_filter[filt], key=lambda m: str(m['tile']))
        paths = [Path(m['path']) for m in entries]
        if single_tile:
            # A single-tile build should have exactly one mosaic per filter; if a
            # filter somehow has more, keep the first (deterministic) rather than
            # silently building a bogus multi-input band.
            bands[filt] = paths[0]
        else:
            bands[filt] = paths
    return bands


def derive_viewer(rgb_channels, band_filters):
    """Derive the fitsgl.toml ``[viewer]`` default from CAMPFIRE's RGB config.

    ``rgb_channels`` maps filter → (r, g, b) color weights (from
    ``get_rgb_configs``), or ``None`` when no imaging config is available.
    ``band_filters`` is the list of filters that became bands. When ≥3 of the RGB
    config's filters are present, the default is an RGB view with ``stretch =
    "trilogy"`` and r/g/b assigned to the filter carrying the most weight in each
    channel (mirroring the roles, not the exact trilogy normalization — the live
    viewer restretches). Otherwise a single-band view on the reddest present filter.
    Every filter is a band regardless; this only sets the initial view.
    """
    usable = [f for f in band_filters if rgb_channels and f in rgb_channels]
    if len(usable) >= 3:
        pick = lambda ch: max(usable, key=lambda f: rgb_channels[f][ch])
        return {
            'default': 'rgb',
            'r': pick(0),
            'g': pick(1),
            'b': pick(2),
            'stretch': 'trilogy',
        }
    if not band_filters:
        return {'default': 'single', 'stretch': 'asinh'}
    return {
        'default': 'single',
        'band': _reddest(band_filters),
        'stretch': 'asinh',
        'colormap': 'gray',
        'north_up': False,
    }


def build_fitsgl_toml(field, *, bands, viewer, pixel_scale, single_tile, tile=None):
    """Assemble the fitsgl.toml structure (a plain dict) for a field/tile build.

    Band ``input`` paths are made **absolute**, so FitsGL's "resolve relative to the
    toml dir" behavior is a no-op and the toml can live anywhere. Dataset name is
    ``<field>`` for the composite and ``<field>__<tile>`` for a single tile (a
    stable R2-prefix/dir identity). Catalog is deferred (a later phase); the seam is
    left as a comment. Returns the dict; the caller serializes it with ``toml``.
    """
    name = f"{field}__{tile}" if single_tile else field
    scope = f"tile {tile}" if single_tile else "fiducial composite"
    band_tables = []
    for filt, inp in bands.items():
        if isinstance(inp, (list, tuple)):
            value = [str(Path(p).resolve()) for p in inp]
        else:
            value = str(Path(inp).resolve())
        band_tables.append({'name': filt, 'label': filt.upper(), 'input': value})

    return {
        'dataset': {
            'name': name,
            'title': f"{field} ({scope}, {pixel_scale})",
            # catalog: deferred to a later phase (NIRSpec export → catalog.csv).
            'bands': band_tables,
        },
        'build': {
            'quantize_level': _QUANTIZE_LEVEL,
            'tile_size': _TILE_SIZE,
            'shared_grid': True,
        },
        'viewer': viewer,
    }


_WAVELEN_RE = re.compile(r'f(\d{3,4})[wmn]', re.IGNORECASE)


def _filter_wavelength(name: str) -> tuple:
    """Sort key: approximate central wavelength (nm) from a JWST filter name.

    ``f150w`` → 1500, ``f444w`` → 4440. Unparseable names sort last, then
    alphabetically, so order is always deterministic.
    """
    m = _WAVELEN_RE.search(name or '')
    if not m:
        return (1, 0.0, name or '')
    digits = m.group(1)
    # f150w -> 150 -> 1.50 µm; f0900 style would be 4 digits already in nm*10.
    val = int(digits) * (10 if len(digits) == 3 else 1)
    return (0, val, name)


def _reddest(band_filters):
    """The longest-wavelength filter present (the default single-band view)."""
    return max(band_filters, key=_filter_wavelength)


# ---------------------------------------------------------------------------
# Orchestration (imports the deploy primitives + defers `import fitsgl`)
# ---------------------------------------------------------------------------

def resolve_fitsgl_dir(out_dir=None) -> Path:
    """Resolve the local dataset output root: ``--out-dir`` or ``$CAMPFIRE_ROOT/fitsgl``."""
    if out_dir:
        return Path(out_dir)
    root = os.environ.get('CAMPFIRE_ROOT')
    if root:
        return Path(root) / 'fitsgl'
    click.echo("Error: no output directory. Use --out-dir <path> or set $CAMPFIRE_ROOT.")
    sys.exit(1)


def resolve_fiducial_tiles(field, *, pixel_scale='30mas'):
    """The field's fiducial tile set (composite build), via the pipeline when present.

    Prefers ``campfire_pipeline.nircam.field.Field`` (reuses the co-grid WCS
    validation in ``fiducial_tile_set``); falls back to a direct read of
    ``$CAMPFIRE_ROOT/config/fields.toml`` ``[<field>].fiducial_tiles`` when the
    pipeline isn't installed (deploy machines needn't carry it). Returns ``[]`` when
    undeclared; the caller errors with guidance.
    """
    try:
        from campfire_pipeline.nircam.field import Field
    except ImportError:
        return _fiducial_tiles_from_toml(field)
    return Field.load(field).fiducial_tile_set(pixel_scale=pixel_scale)


def _fields_toml_table(field):
    """The ``[<field>]`` table from ``$CAMPFIRE_ROOT/config/fields.toml``, or ``None``."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py<3.11
        import tomli as tomllib  # type: ignore
    root = os.environ.get('CAMPFIRE_ROOT')
    if not root:
        return None
    path = Path(root) / 'config' / 'fields.toml'
    if not path.is_file():
        return None
    with path.open('rb') as f:
        data = tomllib.load(f)
    table = data.get(field)
    return table if isinstance(table, dict) else None


def _fiducial_tiles_from_toml(field):
    """Fallback: read ``[<field>].fiducial_tiles`` straight from fields.toml (no WCS check)."""
    raw = (_fields_toml_table(field) or {}).get('fiducial_tiles', [])
    if isinstance(raw, str):
        raw = [raw]
    return list(raw) if isinstance(raw, list) else []


def _rgb_channels_from_fields_toml(field):
    """Filter → (r,g,b) weights from fields.toml ``[<field>.rgb.channels]``, or ``None``.

    The same block ``cfpipe nircam rgb`` consumes; only the color weights matter
    for the default view (the stretch tunables wait on defaultView support in the
    fitsgl.json contract). Pure TOML — no mosaic files are resolved, so this works
    on any machine that can see fields.toml. Filter keys are lowercased to match
    the discovered band names; malformed weight entries are skipped.
    """
    table = _fields_toml_table(field)
    raw = ((table or {}).get('rgb') or {}).get('channels')
    if not isinstance(raw, dict):
        return None
    out = {}
    for filt, weights in raw.items():
        try:
            r, g, b = (float(x) for x in weights)
        except (TypeError, ValueError):
            click.echo(
                f"Warning: [{field}.rgb.channels.{filt}] is not a 3-element "
                f"[r,g,b] list — skipping that filter."
            )
            continue
        out[str(filt).lower()] = (r, g, b)
    return out or None


def _rgb_channels_from_imaging_toml(field):
    """Legacy: filter → (r,g,b) weights via imaging.toml, or ``None`` if unavailable.

    PNG-tile-era path kept for fields not yet migrated to fields.toml. Note
    ``get_rgb_configs`` resolves mosaic file globs on the local filesystem and
    drops filters whose files are missing — prefer ``[<field>.rgb]`` in
    fields.toml, which has no filesystem dependency.
    """
    try:
        from campfire.deploy.config import resolve_imaging_config
        from campfire.deploy.tiles_engine import get_rgb_configs, load_imaging_config

        path = resolve_imaging_config()
        if path is None:
            return None
        rgbs = get_rgb_configs(load_imaging_config(path), fields=[field])
        if not rgbs:
            return None
        return {
            filt: tuple(float(x) for x in info['color'])
            for filt, info in rgbs[0].filter_channels.items()
        }
    except Exception:
        return None


def _load_rgb_channels(field):
    """Filter → (r,g,b) color weights for the default view, or ``None``.

    Sources, in order: fields.toml ``[<field>.rgb.channels]`` (preferred), then
    the legacy imaging.toml. Always says which source won — or why none did — so
    a single-band fallback is never silent (the failure mode that shipped EGS
    with a single-band default).
    """
    channels = _rgb_channels_from_fields_toml(field)
    if channels:
        click.echo(
            f"RGB default view from fields.toml [{field}.rgb]: "
            f"{len(channels)} filter(s)."
        )
        return channels
    channels = _rgb_channels_from_imaging_toml(field)
    if channels:
        click.echo(
            f"RGB default view from legacy imaging.toml [{field}.rgb]: "
            f"{len(channels)} filter(s). Consider moving this block to "
            f"fields.toml [{field}.rgb]."
        )
        return channels
    click.echo(
        f"Note: no RGB config for '{field}' — the dataset will open in a "
        f"single-band view. Add [{field}.rgb.channels] (filter → [r,g,b] "
        f"weights) to $CAMPFIRE_ROOT/config/fields.toml to set an RGB default."
    )
    return None


def run_build(field, *, pixel_scale='30mas', tile=None, processes=None,
              overwrite=False, out_dir=None):
    """Build one field's FitsGL dataset and return its directory.

    Composite (``tile=None``) uses the field's fiducial set; ``tile`` builds a
    single-tile standalone dataset. Writes a generated ``fitsgl.toml`` next to the
    output, then calls FitsGL's ``build_dataset``. ``overwrite=False`` keeps
    FitsGL's resumable per-band reuse (a re-run skips finished bands); pass
    ``overwrite=True`` after a mosaic's pixels change, since reuse keys on a band's
    presence, not its input content.
    """
    from campfire.deploy.nircam import _discover_filters, _resolve_nircam_dirs

    dirs = _resolve_nircam_dirs(field)
    if not dirs['products'].exists():
        click.echo(f"Error: no reduced products for field '{field}' at {dirs['products']}.")
        sys.exit(1)
    filters = _discover_filters(dirs)
    if not filters:
        click.echo(f"Error: no filter products found under {dirs['products']}.")
        sys.exit(1)

    single_tile = tile is not None
    if single_tile:
        tileset = [tile]
    else:
        tileset = resolve_fiducial_tiles(field, pixel_scale=pixel_scale)
        if not tileset:
            click.echo(
                f"Error: field '{field}' declares no fiducial tiles. Add "
                f"`fiducial_tiles = [...]` under [{field}] in fields.toml, or build a "
                f"single tile with --tile <name>."
            )
            sys.exit(1)

    mosaics = select_mosaics(dirs, field, filters, pixel_scale=pixel_scale, tiles=tileset)
    bands = group_bands(mosaics, single_tile=single_tile)
    if not bands:
        click.echo(
            f"Error: no {pixel_scale} mosaics found for "
            f"{'tile ' + tile if single_tile else 'fiducial tiles ' + ', '.join(tileset)}. "
            f"Reduce/combine them first, or pick another --pixel-scale."
        )
        sys.exit(1)

    n_inputs = sum(len(v) if isinstance(v, list) else 1 for v in bands.values())
    click.echo(
        f"Building FitsGL {'tile ' + tile if single_tile else 'composite'} for "
        f"'{field}' at {pixel_scale}: {len(bands)} band(s) / {n_inputs} mosaic(s)."
    )

    rgb_channels = _load_rgb_channels(field)
    viewer = derive_viewer(rgb_channels, list(bands))
    if rgb_channels and viewer.get('default') != 'rgb':
        usable = sorted(f for f in bands if f in rgb_channels)
        click.echo(
            f"Note: only {len(usable)} of the RGB config's filters are among the "
            f"built bands (need 3) — defaulting to single-band {viewer.get('band')}."
        )
    toml_dict = build_fitsgl_toml(
        field, bands=bands, viewer=viewer, pixel_scale=pixel_scale,
        single_tile=single_tile, tile=tile,
    )

    out_root = resolve_fitsgl_dir(out_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    toml_path = out_root / f"{toml_dict['dataset']['name']}.toml"
    toml_path.write_text(toml.dumps(toml_dict))

    try:
        from fitsgl.build import build_dataset
        from fitsgl.config import load_config
    except ImportError:
        click.echo("FitsGL producer not installed. Run: pip install campfire[fitsgl]")
        sys.exit(1)

    cfg = load_config(toml_path)
    result = build_dataset(
        cfg, out_root=out_root, processes=processes, verify=True,
        with_site=True, overwrite=overwrite,
        on_progress=lambda msg: click.echo(f"  {msg}"),
    )
    click.echo(f"\n✓ Built {result.dataset_dir}")
    click.echo(f"  Serve it:  fitsgl serve {result.dataset_dir}")
    click.echo(f"  Then open /prototype/fitsgl and paste a band manifest.json URL.")
    return result.dataset_dir
