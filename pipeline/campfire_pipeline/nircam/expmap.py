"""
expmap: per-filter exposure maps + footprint region files + diagnostic plots.

Stacks each input exposure's S_REGION polygon weighted by its XPOSURE into
a coarse-pixel mosaic. No drizzling — exposure time is a scalar per-exposure
property; drizzle's fractional-kernel weighting would underestimate exposure
at footprint edges, which is the wrong physics for an exposure map.

Auto-WCS: TAN projection at a user-chosen pixel scale, centered on the
centroid of the union of all S_REGIONs and sized to enclose every polygon
plus padding. No tile dependency — works on fields without a ``[tiles]``
block, suitable for full-field diagnostics.

Outputs (per invocation, under ``{products_dir}/expmaps/``):

    expmap_{filter}_{stage}.fits   float32, ``BUNIT='s'``, WCS in header
    expmap_{filter}_{stage}.pdf    diagnostic with RA/Dec gridlines + colorbar
    footprints_{stage}.reg         ds9 fk5 polygons across all filters

The .reg file only contains polygons for filters that were (re)built in this
invocation. To regenerate a combined .reg after up-to-date FITS already exist,
pass ``--overwrite``.

Stages: ``'uncal'`` (raw, fast quick-look) or ``'canonical'`` (processed
canonical exposures post-jhat). These are the only two stages campfire
writes; ``crf``/``cal`` files don't exist in the canonical-exposure layout.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from dataclasses import dataclass
from typing import List, Optional

import matplotlib
matplotlib.use('Agg')

import numpy as np
import tqdm
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS
from regions import PolygonSkyRegion

from campfire_pipeline.common.io import log
from campfire_pipeline.nircam.field import Field


# Cycled per-filter colors for the combined .reg overlay.
_REG_COLORS = [
    'green', 'red', 'blue', 'yellow', 'cyan', 'magenta',
    'orange', 'white', 'pink', 'purple',
]


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
    fits.writeto(path, expmap, header=hdr, overwrite=True)


def _write_pdf(path, expmap, wcs, *, field_name, filter_name, stage, metas):
    import matplotlib.pyplot as plt
    import matplotlib as mpl

    nonzero = expmap[expmap > 0]
    if nonzero.size == 0:
        log('  expmap is all zero; skipping PDF')
        return

    vmin = max(float(np.percentile(nonzero, 5)), 1.0)
    vmax = float(nonzero.max())
    if vmax <= vmin:
        vmax = vmin * 10
    norm = mpl.colors.LogNorm(vmin=vmin, vmax=vmax)

    fig = plt.figure(figsize=(7.5, 6.5), dpi=200, constrained_layout=True)
    ax = fig.add_subplot(111, projection=wcs)
    cmap = mpl.colormaps['Greys'].copy()
    cmap.set_bad('w')
    im = ax.imshow(expmap, origin='lower', cmap=cmap, norm=norm,
                   interpolation='nearest')

    ax.set_xlabel('RA')
    ax.set_ylabel('Dec')
    ax.coords[0].set_major_formatter('hh:mm:ss')
    ax.coords[1].set_major_formatter('dd:mm:ss')
    ax.grid(color='lightgray', lw=0.4, alpha=0.6)

    texp_total = sum(m.xposure for m in metas)
    ax.set_title(
        f'{field_name} · {filter_name.upper()} · {stage} · '
        f'N={len(metas)} · ΣXPOSURE={texp_total:.0f} s',
        fontsize=10,
    )

    cbar = fig.colorbar(im, ax=ax, orientation='vertical',
                        shrink=0.85, pad=0.02)
    cbar.set_label('Exposure time [s]')

    fig.savefig(path)
    plt.close(fig)


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


def _collect_metas(field, filter_name, stage):
    """Header-only pass across all matching files for one filter."""
    if stage == 'uncal':
        files = field.get_uncal_files(filter_name)
    elif stage == 'canonical':
        files = field.get_exposure_files(filter_name)
    else:
        raise ValueError(
            f"unknown stage {stage!r} (use 'uncal' or 'canonical')")

    metas = []
    for f in files:
        try:
            m = _read_metadata(f)
        except Exception as e:
            log(f'  {os.path.basename(f)}: header read failed ({e}); skipping')
            continue
        if m is None:
            log(f'  {os.path.basename(f)}: missing XPOSURE or S_REGION; skipping')
            continue
        metas.append(m)
    return metas


def _process_filter(args):
    """One-filter worker. Builds (or reuses) FITS + PDF, returns metas."""
    field, filter_name, stage, pixel_scale, padding, out_dir, overwrite = args

    fits_path = os.path.join(out_dir, f'expmap_{filter_name}_{stage}.fits')
    pdf_path = os.path.join(out_dir, f'expmap_{filter_name}_{stage}.pdf')

    if (not overwrite
            and os.path.exists(fits_path)
            and os.path.exists(pdf_path)):
        log(f'[{filter_name}] up to date (pass --overwrite to rebuild)')
        return filter_name, [], fits_path, pdf_path

    metas = _collect_metas(field, filter_name, stage)
    if not metas:
        log(f'[{filter_name}] no {stage} files found; skipping')
        return filter_name, [], None, None

    wcs, shape = _auto_wcs(metas, pixel_scale, padding)
    log(f'[{filter_name}] {len(metas)} exposures · WCS {shape[1]}×{shape[0]} '
        f'@ {pixel_scale}"/pix')

    expmap = _accumulate_expmap(metas, wcs, shape,
                                desc=f'[{filter_name}] stacking')
    _write_fits(fits_path, expmap, wcs,
                field_name=field.name, filter_name=filter_name,
                stage=stage, metas=metas)
    log(f'[{filter_name}] wrote {os.path.basename(fits_path)}')
    _write_pdf(pdf_path, expmap, wcs,
               field_name=field.name, filter_name=filter_name,
               stage=stage, metas=metas)
    log(f'[{filter_name}] wrote {os.path.basename(pdf_path)}')
    return filter_name, metas, fits_path, pdf_path


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
        Output directory. Defaults to ``{field.products_dir}/expmaps/``.
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
        out_dir = os.path.join(field.products_dir, 'expmaps')
    os.makedirs(out_dir, exist_ok=True)

    log(f'Expmap: field={field.name}, filters={filter_list}, '
        f'stage={stage}, pixel_scale={pixel_scale}"/pix, out_dir={out_dir}')

    work = [(field, f, stage, pixel_scale, padding, out_dir, overwrite)
            for f in filter_list]

    if n_processes <= 1 or len(work) == 1:
        results = [_process_filter(w) for w in work]
    else:
        ctx = mp.get_context('spawn')
        with ctx.Pool(processes=min(n_processes, len(work))) as pool:
            results = pool.map(_process_filter, work)

    per_filter_metas = [(name, metas)
                        for name, metas, *_ in results if metas]
    if per_filter_metas:
        reg_path = os.path.join(out_dir, f'footprints_{stage}.reg')
        _write_region_file(reg_path, per_filter_metas)
        n_poly = sum(len(m) for _, m in per_filter_metas)
        log(f'wrote {os.path.basename(reg_path)} '
            f'({n_poly} polygons across {len(per_filter_metas)} filters)')
    else:
        log('No new exposures stacked; .reg file not written.')
