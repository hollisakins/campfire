"""
expmap: per-filter exposure maps + footprint region files + diagnostic plots.

Stacks each input exposure's S_REGION polygon weighted by its XPOSURE into
a coarse-pixel mosaic. No drizzling — exposure time is a scalar per-exposure
property; drizzle's fractional-kernel weighting would underestimate exposure
at footprint edges, which is the wrong physics for an exposure map.

Auto-WCS: TAN projection at a user-chosen pixel scale, centered on the
centroid of the union of all S_REGIONs and sized to enclose every polygon
plus padding. The WCS is shared across all filters in the invocation
(union of polygons across every filter), so per-filter expmaps are
pixel-registered for direct stacking/comparison. No tile dependency —
works on fields without a ``[tiles]`` block, suitable for full-field
diagnostics.

Outputs (per invocation, rooted at ``{products_dir}`` = ``products/nircam/<field>/``).
The fiducial ``canonical`` map is undecorated — to a user it is simply *the*
exposure map — while the reducer-only ``uncal`` quick-look keeps an explicit
``_uncal`` suffix (shown below as ``[_uncal]``):

    <filter>/expmap_{field}_{filter}[_uncal].fits   float32, ``BUNIT='s'``, ``AREA`` header, WCS
    <filter>/expmap_{field}_{filter}[_uncal].pdf    light diagnostic (RA/Dec grid + colorbar)
    <filter>/expmap_{field}_{filter}[_uncal].png    dark web plot (same view, deployable)
    {field}_layout[_uncal].png                      stacked-filter coverage + tile outlines (dark)
    {field}_layout[_uncal].json                     coverage summary: exact area, per-filter areas
    footprints[_uncal].reg                          ds9 fk5 polygons across all filters

The per-filter FITS sits in the canonical filter directory alongside the
mosaics/exposures so the deployed coverage map carries a real filter in the
registry (same key shape as every other per-filter NIRCam product).

Both the per-filter ``.pdf`` (light, local diagnostic) and ``.png`` (dark,
deployable) share the same colorbar ``vmin``/``vmax`` (log-norm across the
union of nonzero pixels) so the plots show the same colour scale — easy to
tab through. The PDF carries a title (stage labelled only for ``uncal``);
the deployable PNGs carry no title at all, since the web page embedding
them already labels field/filter. The dark PNG matches CAMPFIRE's data
wells, which stay dark in both app themes. The ``{field}_layout`` plot stacks every per-filter
expmap (total exposure across filters) and overlays the tile footprints; the
companion ``.json`` records the exact survey area (non-zero pixels of the
stack × pixel area) for the deploy/DB layer.

The .reg file only contains polygons for filters that were (re)built in this
invocation. To regenerate a combined .reg after up-to-date FITS already exist,
pass ``--overwrite``.

Stages: ``'uncal'`` (raw, fast quick-look) or ``'canonical'`` (processed
canonical exposures post-jhat). These are the only two stages campfire
writes; ``crf``/``cal`` files don't exist in the canonical-exposure layout.
"""

from __future__ import annotations

import concurrent.futures
import json
import multiprocessing as mp
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import tqdm
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_area
from regions import PolygonSkyRegion

from campfire_pipeline.common.io import log
from campfire_pipeline.nircam.field import Field


# Cycled per-filter colors for the combined .reg overlay.
_REG_COLORS = [
    'green', 'red', 'blue', 'yellow', 'cyan', 'magenta',
    'orange', 'white', 'pink', 'purple',
]

# Header-scan tuning. Header reads are I/O-bound (open syscall +
# two header blocks per file), so threads release the GIL during reads
# and give near-linear speedup against IOPS-limited backends. The cache
# lives under ``out_dir`` keyed by (abspath, mtime_ns, size), which
# self-invalidates on rsync or pipeline re-reduction.
_DEFAULT_SCAN_THREADS = 8
_CACHE_VERSION = 1
_CACHE_FILENAME = '.expmap_cache.json'


@dataclass
class _ExposureMeta:
    path: str
    rootname: str
    xposure: float
    ra: List[float]
    dec: List[float]


def _parse_sregion(s):
    """Parse 'POLYGON ICRS ra1 dec1 ra2 dec2 ...' into parallel RA/Dec lists."""
    parts = s.split()
    ra = [float(x) for x in parts[2::2]]
    dec = [float(x) for x in parts[3::2]]
    return ra, dec


def _read_metadata(path):
    """Return ``_ExposureMeta`` for one file, or ``None`` if keys are missing.

    Both uncal and canonical files put XPOSURE on the primary and S_REGION
    on the SCI extension; uncals also carry both on primary as a fallback.
    """
    rootname = os.path.basename(path)
    if rootname.endswith('.fits'):
        rootname = rootname[:-5]
    rootname = rootname.split('_')[0]

    with fits.open(path, memmap=False) as hdul:
        prim = hdul[0].header
        sci_hdr = None
        for hdu in hdul[1:]:
            if hdu.header.get('EXTNAME', '').upper() == 'SCI':
                sci_hdr = hdu.header
                break
        if sci_hdr is None and len(hdul) > 1:
            sci_hdr = hdul[1].header
        xposure = prim.get('XPOSURE')
        if xposure is None and sci_hdr is not None:
            xposure = sci_hdr.get('XPOSURE')
        s_region = None
        if sci_hdr is not None:
            s_region = sci_hdr.get('S_REGION')
        if s_region is None:
            s_region = prim.get('S_REGION')

    if xposure is None or s_region is None:
        return None
    ra, dec = _parse_sregion(s_region)
    return _ExposureMeta(path=path, rootname=rootname,
                         xposure=float(xposure), ra=ra, dec=dec)


def _auto_wcs(metas, pixel_scale_arcsec, padding_arcsec):
    """Build a TAN WCS sized to enclose the union of all polygons + padding.

    Uses a small-area tangent-plane approximation (cos(dec) at field center).
    NIRCam fields are at most a few arcmin across, well inside the regime
    where this is accurate to <0.1 pix.
    """
    all_ra = np.concatenate([np.asarray(m.ra) for m in metas])
    all_dec = np.concatenate([np.asarray(m.dec) for m in metas])
    ra_c = 0.5 * (all_ra.min() + all_ra.max())
    dec_c = 0.5 * (all_dec.min() + all_dec.max())
    cosd = np.cos(np.deg2rad(dec_c))

    dx_arcsec = (all_ra.max() - all_ra.min()) * cosd * 3600.0 + 2 * padding_arcsec
    dy_arcsec = (all_dec.max() - all_dec.min()) * 3600.0 + 2 * padding_arcsec
    nx = int(np.ceil(dx_arcsec / pixel_scale_arcsec))
    ny = int(np.ceil(dy_arcsec / pixel_scale_arcsec))

    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crval = [ra_c, dec_c]
    w.wcs.crpix = [nx / 2.0 + 0.5, ny / 2.0 + 0.5]
    w.wcs.cdelt = [-pixel_scale_arcsec / 3600.0, pixel_scale_arcsec / 3600.0]
    return w, (ny, nx)


def _accumulate_expmap(metas, wcs, shape, *, desc=''):
    """Sum polygon-masks × XPOSURE across all exposures into one map."""
    expmap = np.zeros(shape, dtype=np.float32)
    for m in tqdm.tqdm(metas, desc=desc):
        vertices = SkyCoord(ra=m.ra, dec=m.dec, unit='deg', frame='icrs')
        sky_reg = PolygonSkyRegion(vertices=vertices)
        pix_reg = sky_reg.to_pixel(wcs)
        mask = pix_reg.to_mask().to_image(shape)
        if mask is None:
            continue
        expmap += np.asarray(mask, dtype=np.float32) * m.xposure
    return expmap


def _write_fits(path, expmap, wcs, *, field_name, filter_name, stage, metas):
    hdr = wcs.to_header()
    hdr['BUNIT'] = ('s', 'Pixel value is exposure time in seconds')
    hdr['EXTNAME'] = 'EXPMAP'
    hdr['FIELD'] = (field_name, 'Campfire field name')
    hdr['FILTER'] = (filter_name.upper(), 'Filter')
    hdr['STAGE'] = (stage, "Source stage: 'uncal' or 'canonical'")
    hdr['NEXP'] = (len(metas), 'Number of contributing exposures')
    hdr['TEXPTOT'] = (float(sum(m.xposure for m in metas)),
                      'Sum of XPOSURE across all exposures [s]')
    n_cov = int(np.count_nonzero(expmap > 0))
    hdr['AREA'] = (round(n_cov * proj_plane_pixel_area(wcs) * 3600.0, 6),
                   'Covered area [arcmin2] (nonzero pixels)')
    fits.writeto(path, expmap, header=hdr, overwrite=True)


def _render_expmap_figure(path, data, wcs, *, title, cbar_label,
                          vmin=None, vmax=None, dark=False,
                          tile_outlines=None):
    """Render one exposure map (per-filter or stacked layout) to ``path``.

    ``vmin``/``vmax`` default to the map's own nonzero min/max; the per-filter
    caller overrides both with values shared across all filters so the plots
    are identical apart from the data. ``title=None`` omits the title entirely
    — used by the deployable PNGs, where the web page already labels the plot.

    ``dark=True`` produces the deployable web plot on a dark plot well
    (matching CAMPFIRE's map + spectrum wells, which stay dark in both app
    themes); ``dark=False`` keeps the light local-diagnostic look. Zeros are
    masked so the off-footprint background renders via ``set_bad`` and stays
    distinct from the lowest exposure values. ``tile_outlines`` is an optional
    list of ``(name, [[ra, dec], ...])`` footprints drawn as thin polygons —
    used by the field layout plot. Returns ``False`` (writing nothing) when the
    map is all zero.
    """
    import matplotlib.pyplot as plt
    import matplotlib as mpl

    nonzero = data[data > 0]
    if nonzero.size == 0:
        log('  expmap is all zero; skipping plot')
        return False

    if vmin is None:
        vmin = max(float(nonzero.min()), 1.0)
    if vmax is None:
        vmax = float(nonzero.max())
    if vmax <= vmin:
        vmax = vmin * 10
    norm = mpl.colors.LogNorm(vmin=vmin, vmax=vmax)
    masked = np.ma.masked_where(data <= 0, data)

    bg = '#0d0b12' if dark else 'white'
    fg = '#c9c4d6' if dark else 'black'
    grid_c = '#4a4a5a' if dark else 'lightgray'
    tile_c = '#e6e1f0' if dark else '#333333'

    fig = plt.figure(figsize=(7.5, 6.5), dpi=200, constrained_layout=True)
    fig.patch.set_facecolor(bg)
    ax = fig.add_subplot(111, projection=wcs)
    ax.set_facecolor(bg)
    cmap = mpl.colormaps['magma'].copy()
    cmap.set_bad(bg if dark else 'w')
    im = ax.imshow(masked, origin='lower', cmap=cmap, norm=norm,
                   interpolation='nearest')

    ax.coords[0].set_major_formatter('hh:mm:ss')
    ax.coords[1].set_major_formatter('dd:mm:ss')
    ax.coords[0].set_axislabel('RA', color=fg)
    ax.coords[1].set_axislabel('Dec', color=fg)
    ax.grid(color=grid_c, lw=0.4, alpha=0.6)
    if dark:
        for coord in ax.coords:
            coord.set_ticklabel(color=fg)
            coord.set_ticks(color=fg)
        ax.coords.frame.set_color(fg)

    if tile_outlines:
        import matplotlib.patheffects as path_effects

        # Stroke outlines/labels with the background colour so they stay
        # readable over both the bright (pale) and dark ends of the colormap.
        stroke = [path_effects.withStroke(linewidth=2.5, foreground=bg)]
        world = ax.get_transform('world')
        for name, corners in tile_outlines:
            ras = [c[0] for c in corners] + [corners[0][0]]
            decs = [c[1] for c in corners] + [corners[0][1]]
            ax.plot(ras, decs, transform=world, color=tile_c,
                    lw=1.5, path_effects=stroke)
            ax.text(float(np.mean([c[0] for c in corners])),
                    float(np.mean([c[1] for c in corners])),
                    name, transform=world, color=tile_c, fontsize=10,
                    fontweight='bold', ha='center', va='center',
                    path_effects=stroke)

    if title:
        ax.set_title(title, fontsize=10, color=fg)
    cbar = fig.colorbar(im, ax=ax, orientation='vertical',
                        shrink=0.85, pad=0.02)
    cbar.set_label(cbar_label, color=fg)
    if dark:
        cbar.ax.yaxis.set_tick_params(color=fg)
        cbar.outline.set_edgecolor(fg)
        plt.setp(plt.getp(cbar.ax, 'yticklabels'), color=fg)

    fig.savefig(path, facecolor=bg)
    plt.close(fig)
    return True


def _write_region_file(path, per_filter_metas):
    """Write a single ds9 fk5 polygon region file with one color per filter."""
    lines = [
        '# Region file format: DS9 version 4.1',
        'global dashlist=8 3 width=1 font="helvetica 8 normal roman"',
        'fk5',
    ]
    for i, (filt, metas) in enumerate(per_filter_metas):
        color = _REG_COLORS[i % len(_REG_COLORS)]
        for m in metas:
            coords = ','.join(f'{r:.7f},{d:.7f}'
                              for r, d in zip(m.ra, m.dec))
            lines.append(
                f'polygon({coords}) # color={color} '
                f'text={{{filt}:{m.rootname}}}'
            )
    with open(path, 'w') as fh:
        fh.write('\n'.join(lines) + '\n')


def _load_metadata_cache(cache_path: str) -> Dict[str, dict]:
    """Load the on-disk metadata cache. Returns ``{}`` on any failure
    (missing file, corrupted JSON, version mismatch)."""
    try:
        with open(cache_path) as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if data.get('version') != _CACHE_VERSION:
        return {}
    return data.get('entries', {})


def _save_metadata_cache(cache_path: str, entries: Dict[str, dict]) -> None:
    """Atomically write the metadata cache (write to .tmp then rename)."""
    tmp_path = cache_path + '.tmp'
    payload = {'version': _CACHE_VERSION, 'entries': entries}
    try:
        with open(tmp_path, 'w') as fh:
            json.dump(payload, fh)
        os.replace(tmp_path, cache_path)
    except OSError as e:
        log(f'  warning: failed to write expmap metadata cache ({e})')


def _meta_from_cache_entry(path, entry):
    return _ExposureMeta(
        path=path,
        rootname=entry['rootname'],
        xposure=float(entry['xposure']),
        ra=list(entry['ra']),
        dec=list(entry['dec']),
    )


def _cache_entry_from_meta(meta, stat_key):
    mtime_ns, size = stat_key
    return {
        'mtime_ns': int(mtime_ns),
        'size': int(size),
        'rootname': meta.rootname,
        'xposure': float(meta.xposure),
        'ra': list(meta.ra),
        'dec': list(meta.dec),
    }


def _read_metadata_safe(path):
    """``_read_metadata`` wrapped with the original error/missing-key logging.
    Returns ``None`` on either failure mode."""
    try:
        meta = _read_metadata(path)
    except Exception as e:
        log(f'  {os.path.basename(path)}: header read failed ({e}); skipping')
        return None
    if meta is None:
        log(f'  {os.path.basename(path)}: missing XPOSURE or S_REGION; skipping')
    return meta


def _collect_metas(field, filter_name, stage, *,
                   cache: Optional[Dict[str, dict]] = None,
                   n_threads: int = _DEFAULT_SCAN_THREADS):
    """Header-only pass across all matching files for one filter.

    When ``cache`` is supplied, files whose ``(mtime_ns, size)`` match a
    cached entry skip the FITS open entirely. Cache misses are scanned
    in parallel via a thread pool — header reads are I/O-bound and
    threads release the GIL during reads, so this gives near-linear
    speedup against IOPS-limited backends (network FS, slow disks).
    The cache is mutated in place; the caller is responsible for
    persisting it.
    """
    if stage == 'uncal':
        files = field.get_uncal_files(filter_name)
    elif stage == 'canonical':
        files = field.get_exposure_files(filter_name)
    else:
        raise ValueError(
            f"unknown stage {stage!r} (use 'uncal' or 'canonical')")

    metas: List[_ExposureMeta] = []
    misses: List[tuple] = []
    hits = 0
    for f in files:
        path = os.path.abspath(f)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            log(f'  {os.path.basename(path)}: not found; skipping')
            continue
        stat_key = (st.st_mtime_ns, st.st_size)
        entry = cache.get(path) if cache is not None else None
        if (entry is not None
                and entry.get('mtime_ns') == stat_key[0]
                and entry.get('size') == stat_key[1]):
            metas.append(_meta_from_cache_entry(path, entry))
            hits += 1
        else:
            misses.append((path, stat_key))

    if not misses:
        return metas

    desc = f'[{filter_name}] scanning headers'
    if hits:
        desc += f' (cached {hits})'

    def _store(path, stat_key, meta):
        if meta is not None:
            metas.append(meta)
            if cache is not None:
                cache[path] = _cache_entry_from_meta(meta, stat_key)

    if n_threads <= 1 or len(misses) == 1:
        with tqdm.tqdm(total=len(misses), desc=desc,
                       unit='file', leave=False) as pbar:
            for path, stat_key in misses:
                _store(path, stat_key, _read_metadata_safe(path))
                pbar.update(1)
        return metas

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(n_threads, len(misses))) as ex:
        futures = {ex.submit(_read_metadata_safe, path): (path, stat_key)
                   for path, stat_key in misses}
        with tqdm.tqdm(total=len(misses), desc=desc,
                       unit='file', leave=False) as pbar:
            for fut in concurrent.futures.as_completed(futures):
                path, stat_key = futures[fut]
                _store(path, stat_key, fut.result())
                pbar.update(1)
    return metas


def _expmap_stem(field_name, filter_name, stage):
    """Filename stem for a per-filter expmap.

    The ``canonical`` map is the fiducial, deployable coverage map and carries no
    stage decoration (``expmap_<field>_<filter>``) — to a user it is simply *the*
    exposure map, the default rather than one option among several. The ``uncal``
    map is a reducer-only quick-look and keeps an explicit ``_uncal`` suffix so it
    never collides with the fiducial map and the deploy step can pick up only the
    undecorated one.
    """
    stem = f'expmap_{field_name}_{filter_name}'
    if stage != 'canonical':
        stem += f'_{stage}'
    return stem


def _expmap_paths(base_dir, field_name, filter_name, stage):
    """Per-filter FITS + PDF + PNG paths.

    Expmaps live in the filter directory alongside the mosaics/exposures
    (``<base_dir>/<filter>/``) so the deployed FITS/PNG carry a real filter in
    the registry — same key shape as every other per-filter NIRCam product.
    ``base_dir`` is the per-field products dir.
    """
    base = os.path.join(base_dir, filter_name,
                        _expmap_stem(field_name, filter_name, stage))
    return base + '.fits', base + '.pdf', base + '.png'


def _nz_range(expmap):
    """Return (min, max) of strictly-positive pixels, or (None, None)."""
    nz = expmap[expmap > 0]
    if nz.size == 0:
        return None, None
    return float(nz.min()), float(nz.max())


def _accumulate_filter(args):
    """Parallel worker: build (or reuse) one filter's FITS and report
    nonzero pixel range.

    Returns ``(filter_name, metas, fits_path, nz_min, nz_max)``. Only
    the scalar range crosses the multiprocessing boundary — the full
    array stays on disk so pool workers don't pickle large buffers.
    """
    (field, filter_name, stage, wcs, shape, metas,
     out_dir, overwrite) = args

    fits_path, _, _ = _expmap_paths(out_dir, field.name, filter_name, stage)

    if not metas:
        log(f'[{filter_name}] no {stage} files found; skipping')
        return filter_name, [], None, None, None

    if not overwrite and os.path.exists(fits_path):
        log(f'[{filter_name}] FITS up to date; loading nz-range '
            f'for shared colorbar')
        with fits.open(fits_path, memmap=False) as hdul:
            data = hdul['EXPMAP'].data
            nz_min, nz_max = _nz_range(data)
        return filter_name, metas, fits_path, nz_min, nz_max

    expmap = _accumulate_expmap(metas, wcs, shape,
                                desc=f'[{filter_name}] stacking')
    os.makedirs(os.path.dirname(fits_path), exist_ok=True)
    _write_fits(fits_path, expmap, wcs,
                field_name=field.name, filter_name=filter_name,
                stage=stage, metas=metas)
    log(f'[{filter_name}] wrote {os.path.basename(fits_path)}')
    nz_min, nz_max = _nz_range(expmap)
    return filter_name, metas, fits_path, nz_min, nz_max


def _render_filter_plots(field_name, filter_name, stage, metas, fits_path,
                         out_dir, wcs, vmin, vmax):
    """Re-read the filter FITS and render the light PDF + dark PNG.

    Both share the invocation-wide ``vmin``/``vmax`` so tabbing through
    filters shows the same colour scale. The PDF is the local diagnostic
    (kept) and carries a title; the fiducial canonical stage is undecorated
    there too, so only the reducer-only ``uncal`` quick-look labels its
    stage. The PNG is the deployable web plot and carries no title at all —
    the web page it's embedded in already says field/filter.
    """
    _, pdf_path, png_path = _expmap_paths(out_dir, field_name, filter_name,
                                          stage)
    with fits.open(fits_path, memmap=False) as hdul:
        expmap = np.asarray(hdul['EXPMAP'].data, dtype=np.float32)
    title = f'{field_name} · {filter_name.upper()} · N={len(metas)}'
    if stage != 'canonical':
        title = f'{field_name} · {filter_name.upper()} · {stage} · N={len(metas)}'
    _render_expmap_figure(pdf_path, expmap, wcs, title=title,
                          cbar_label='Exposure time [s]',
                          vmin=vmin, vmax=vmax, dark=False)
    _render_expmap_figure(png_path, expmap, wcs, title=None,
                          cbar_label='Exposure time [s]',
                          vmin=vmin, vmax=vmax, dark=True)
    log(f'[{filter_name}] wrote {os.path.basename(pdf_path)} + '
        f'{os.path.basename(png_path)}')
    return pdf_path


def _layout_paths(base_dir, field_name, stage):
    """Field layout plot (PNG) + coverage summary (JSON) at the products root.

    The fiducial ``canonical`` layout is undecorated (``<field>_layout.png``);
    the reducer-only ``uncal`` quick-look keeps a ``_uncal`` suffix so the two
    never collide and the deploy step picks up only the canonical one.
    """
    stem = f'{field_name}_layout'
    if stage != 'canonical':
        stem += f'_{stage}'
    base = os.path.join(base_dir, stem)
    return base + '.png', base + '.json'


def _render_layout(out_dir, field, stage, results, wcs, pixel_scale,
                   tile_outlines, texp_total):
    """Stack every per-filter expmap into the field layout plot + JSON summary.

    The stack is the per-pixel sum of exposure across all filters (the
    "stacked exposure map"); the exact survey area is the area of its non-zero
    pixels. Writes ``<field>_layout.png`` (dark, with tile outlines + axes +
    colorbar so it stands alone as a cfpipe product) and ``<field>_layout.json``
    (coverage area, per-filter areas, filters, total exposure) for the
    deploy/DB layer to surface on the web.
    """
    built = [(name, metas, fp)
             for name, metas, fp, *_ in results if fp and metas]
    if not built:
        return

    pix_area_deg2 = proj_plane_pixel_area(wcs)
    stack = None
    per_filter_area = {}
    for name, _metas, fp in built:
        with fits.open(fp, memmap=False) as hdul:
            arr = np.asarray(hdul['EXPMAP'].data, dtype=np.float64)
            area = hdul['EXPMAP'].header.get('AREA')
        if area is None:
            # Cached FITS written before the AREA card existed: recompute the
            # per-filter area from the array on the shared WCS rather than
            # reporting a spurious zero.
            area = int(np.count_nonzero(arr > 0)) * pix_area_deg2 * 3600.0
        per_filter_area[name] = float(area)
        stack = arr if stack is None else stack + arr

    n_cov = int(np.count_nonzero(stack > 0))
    area_deg2 = n_cov * pix_area_deg2
    area_arcmin2 = area_deg2 * 3600.0

    png_path, json_path = _layout_paths(out_dir, field.name, stage)
    filters = [name for name, _, _ in built]
    ok = _render_expmap_figure(
        png_path, stack, wcs, title=None,
        cbar_label='Total exposure [s]',
        dark=True, tile_outlines=tile_outlines,
    )

    summary = {
        'field': field.name,
        'stage': stage,
        'pixel_scale_arcsec': float(pixel_scale),
        'n_filters': len(filters),
        'filters': filters,
        'n_tiles': len(tile_outlines),
        'coverage_area_arcmin2': round(area_arcmin2, 6),
        'coverage_area_deg2': round(area_deg2, 8),
        'per_filter_area_arcmin2': {k: round(v, 6)
                                    for k, v in per_filter_area.items()},
        'texp_total_s': float(texp_total),
    }
    with open(json_path, 'w') as fh:
        json.dump(summary, fh, indent=2)
    log(f'wrote {os.path.basename(json_path)}'
        + (f' + {os.path.basename(png_path)}' if ok else '')
        + f' (area {area_arcmin2:.1f} arcmin², {len(filters)} filters)')


def run_expmap(
    field: Field,
    *,
    filters: Optional[List[str]] = None,
    stage: str = 'canonical',
    pixel_scale: float = 0.5,
    padding: float = 30.0,
    out_dir: Optional[str] = None,
    n_processes: int = 1,
    overwrite: bool = False,
):
    """Build per-filter exposure maps + a combined region file.

    Parameters
    ----------
    field
        Loaded ``Field`` with ``setup_workspace()`` already called.
    filters
        Filters to process. Defaults to ``field.filters``.
    stage
        ``'uncal'`` (raw, fast quick-look) or ``'canonical'`` (processed
        canonical exposures post-jhat).
    pixel_scale
        Output pixel scale in arcsec/pix. Coarse by design.
    padding
        Sky padding in arcsec around the union footprint.
    out_dir
        Base products directory. Per-filter FITS + PDFs land in
        ``<out_dir>/<filter>/``; the combined footprints ``.reg`` and the
        metadata cache land at its root. Defaults to ``field.products_dir``
        (``products/nircam/<field>/``).
    n_processes
        Per-filter parallelism (one filter per worker).
    overwrite
        Rebuild even when both the FITS and PDF already exist.
    """
    if stage not in ('uncal', 'canonical'):
        raise ValueError(
            f"stage must be 'uncal' or 'canonical', got {stage!r}")

    filter_list = list(filters) if filters else list(field.filters)
    if not filter_list:
        log('No filters specified or defined for field; nothing to do.')
        return

    if out_dir is None:
        out_dir = field.products_dir
    os.makedirs(out_dir, exist_ok=True)

    log(f'Expmap: field={field.name}, filters={filter_list}, '
        f'stage={stage}, pixel_scale={pixel_scale}"/pix, out_dir={out_dir}')

    cache_path = os.path.join(out_dir, _CACHE_FILENAME)
    cache = _load_metadata_cache(cache_path)
    if cache:
        log(f'Loaded metadata cache: {len(cache)} entries '
            f'({os.path.basename(cache_path)})')

    # Phase 1: scan headers for every filter. Per-filter scan is
    # internally thread-parallel; outer loop is sequential so the tqdm
    # bars don't fight each other.
    per_filter_metas = {}
    for filt in filter_list:
        metas = _collect_metas(field, filt, stage, cache=cache)
        per_filter_metas[filt] = metas
        log(f'[{filt}] {len(metas)} {stage} exposures')

    _save_metadata_cache(cache_path, cache)

    all_metas = [m for ms in per_filter_metas.values() for m in ms]
    if not all_metas:
        log('No exposures found across any filter; nothing to do.')
        return

    # Phase 2: one shared WCS sized to enclose every polygon across every
    # filter, so the per-filter expmaps are pixel-registered.
    wcs, shape = _auto_wcs(all_metas, pixel_scale, padding)
    log(f'Shared WCS across {len(filter_list)} filters: '
        f'{shape[1]}×{shape[0]} @ {pixel_scale}"/pix')

    # Phase 3a: per-filter accumulation (parallelizable across filters).
    # Workers write FITS to disk and return only the nonzero pixel range
    # — avoids pickling large arrays back through the pool.
    work = [(field, f, stage, wcs, shape, per_filter_metas[f],
             out_dir, overwrite)
            for f in filter_list]

    if n_processes <= 1 or len(work) == 1:
        results = [_accumulate_filter(w) for w in work]
    else:
        ctx = mp.get_context('spawn')
        with ctx.Pool(processes=min(n_processes, len(work))) as pool:
            results = pool.map(_accumulate_filter, work)

    # Phase 3b: shared colorbar — vmin/vmax from the union of nonzero
    # pixels across every filter, so PDFs are visually identical apart
    # from the actual data.
    mins = [r[3] for r in results if r[3] is not None]
    maxes = [r[4] for r in results if r[4] is not None]
    if mins and maxes:
        global_vmin = max(min(mins), 1.0)
        global_vmax = max(maxes)
        if global_vmax <= global_vmin:
            global_vmax = global_vmin * 10
        log(f'Shared colorbar: vmin={global_vmin:.1f} s, '
            f'vmax={global_vmax:.1f} s '
            f'(LogNorm across {len(mins)} filters)')
    else:
        global_vmin = global_vmax = None

    # Phase 3c: per-filter plots (light PDF + dark PNG). Always regenerated
    # (cheap; the shared norm is invocation-dependent so a cached plot from a
    # prior run with a different filter set would not match the colorbar).
    for filter_name, metas, fits_path, _, _ in results:
        if fits_path is None or not metas:
            continue
        _render_filter_plots(field.name, filter_name, stage, metas, fits_path,
                             out_dir, wcs, global_vmin, global_vmax)

    # Phase 3d: field layout — stack of every per-filter expmap with tile
    # outlines + the exact survey area (non-zero pixels of the stack). Only a
    # full-field run may (over)write the canonical layout + coverage area: a
    # filtered rerun stacks a subset and would publish an under-reported survey
    # area over the complete one, so partial runs skip the layout entirely.
    if set(filter_list) >= set(field.filters):
        # Tile outlines are decorative and a bad/missing tile config never
        # blocks the plot; fields without a [tiles] block get coverage only.
        tile_outlines = []
        for tname in (getattr(field, 'tiles', None) or {}):
            try:
                tile_outlines.append((tname, field.get_tile_corners(tname)))
            except Exception as e:  # noqa: BLE001 - decorative overlay, never fatal
                log(f'  layout: skipping tile {tname} outline ({e})')
        _render_layout(out_dir, field, stage, results, wcs, pixel_scale,
                       tile_outlines, float(sum(m.xposure for m in all_metas)))
    else:
        missing = sorted(set(field.filters) - set(filter_list))
        log(f'Partial filter run (field filters missing: {missing}); '
            f'skipping {field.name}_layout to preserve the full-field coverage.')

    reg_metas = [(name, metas)
                 for name, metas, *_ in results if metas]
    if reg_metas:
        # Fiducial canonical footprints are undecorated (footprints.reg); the
        # uncal quick-look keeps its stage suffix so the two don't overwrite.
        reg_name = 'footprints.reg' if stage == 'canonical' else f'footprints_{stage}.reg'
        reg_path = os.path.join(out_dir, reg_name)
        _write_region_file(reg_path, reg_metas)
        n_poly = sum(len(m) for _, m in reg_metas)
        log(f'wrote {os.path.basename(reg_path)} '
            f'({n_poly} polygons across {len(reg_metas)} filters)')
    else:
        log('No new exposures stacked; .reg file not written.')
