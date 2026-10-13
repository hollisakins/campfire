"""
mosaic_exp: per-tile exposure maps on the mosaic pixel grid.

``<mosaic>_exp.fits`` sits next to the split ``_sci/_err/_wht`` extensions. It is
the full-resolution, per-tile member of the ``exp`` family whose low-resolution,
field-wide member is ``expmap_<field>_<filter>.fits`` (``nircam/expmap.py``):

``EXPTIME`` (first image, float32, ``BUNIT = 's'``)
    Summed ``XPOSURE`` of the inputs contributing to each pixel. Same quantity
    and units as the field expmap.
``NEXP`` (second image, int16)
    Number of inputs (detector-level CRFs) contributing to each pixel. Catalog
    noise models stratify on it: a pixel's noise depends on how many reads
    were averaged, not only on the summed weight.

"Contributing" means the input put non-zero drizzle weight on the pixel, so
chip gaps, masked pixels and SCI-NaN pixels are exact (both planes are 0
wherever SCI is NaN). The field expmap stacks S_REGION footprints instead, so
the two agree in units but not in masking.

How the planes are obtained is recorded in ``EXPSRC``:

``drizzle``
    Counted while drizzling (``drizzle_tile``). After each input's
    ``add_image``, the output weight changes exactly where that input landed:
    ``NEXP += landed`` and ``EXPTIME += landed * XPOSURE`` inside the input's
    bbox. This is the input's context bit, except where its weight on a pixel
    is below float32 resolution of the running weight sum (relative weight
    ~< 1e-7): that contribution changes neither WHT nor SCI, so it is not
    counted, while cdriz still sets its context bit. Measured on a COSMOS A1
    f444w sub-tile: 14 of 2.25M pixels, each one input lower than CON. It costs
    two planes instead of ``ceil(n_inputs/32)`` int32 planes, so it is produced
    with ``write_context = false`` too, the setting deep tiles need (a
    3,903-input COSMOS f200w tile would need 521 GiB of context).
``context``
    From an existing drizzle CON cube: popcount for NEXP, and each bit weighted
    by its input's XPOSURE for EXPTIME. Bit ``i`` is the ``i``-th contributing
    input, which is also row ``i`` of the i2d's ``HDRTAB`` (both follow the
    drizzle order). Used by the jwst backend, and by backfills of mosaics
    drizzled before this product existed.
``footprint``
    Detector footprints of the manifest inputs, from their SCI headers alone
    (backfill when CON is a placeholder). Ignores per-pixel masking inside a
    detector, so it can over-count where an input was flagged; SCI-NaN pixels
    are still zeroed.

File layout: an empty primary HDU, then ``EXPTIME`` and ``NEXP`` as GZIP_2
``CompImageHDU``s on the SCI WCS. ``EXPTIME`` is stored losslessly, without
quantisation. A full tile is a few MB. Cards on the primary header and both
images: ``EXPSRC``, ``NINPUTS`` (inputs selected for the tile) and ``NUSED``
(inputs that contributed to at least one pixel, when known).
"""

from __future__ import annotations

import json
import os
import warnings

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning

from campfire_pipeline.common.io import log

EXP_SUFFIX = '_exp.fits'
EXPTIME_EXTNAME = 'EXPTIME'
NEXP_EXTNAME = 'NEXP'

_POP8 = np.array([bin(i).count('1') for i in range(256)], dtype=np.int16)


def _cards(src, ninputs, nused=None):
    cards = [
        ('EXPSRC', src, 'exp map source: drizzle/context/footprint'),
        ('NINPUTS', int(ninputs), 'input exposures selected for this tile'),
    ]
    if nused is not None:
        cards.append(('NUSED', int(nused), 'inputs contributing to >=1 pixel'))
    return cards


def exp_hdus(exptime, nexp, wcs_header, *, src, ninputs, nused=None):
    """``[EXPTIME, NEXP]`` GZIP_2 image HDUs carrying the SCI WCS + provenance."""
    base = WCS(wcs_header).celestial.to_header()
    for card in _cards(src, ninputs, nused):
        base.set(*card)
    th = base.copy()
    th['BUNIT'] = ('s', 'summed XPOSURE of contributing inputs')
    nh = base.copy()
    nh['BUNIT'] = ('', 'number of contributing inputs')
    return [
        fits.CompImageHDU(np.asarray(exptime, dtype=np.float32), header=th,
                          name=EXPTIME_EXTNAME, compression_type='GZIP_2',
                          quantize_level=0.0),
        fits.CompImageHDU(np.asarray(nexp, dtype=np.int16), header=nh,
                          name=NEXP_EXTNAME, compression_type='GZIP_2'),
    ]


def write_exp_file(path, exptime, nexp, wcs_header, *, src, ninputs, nused=None):
    """Write a standalone ``_exp.fits`` (empty primary + EXPTIME + NEXP), atomically."""
    primary = fits.PrimaryHDU()
    for card in _cards(src, ninputs, nused):
        primary.header.set(*card)
    tmp = f'{path}.tmp'
    try:
        fits.HDUList([primary] + exp_hdus(exptime, nexp, wcs_header, src=src,
                                          ninputs=ninputs, nused=nused)
                     ).writeto(tmp, overwrite=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def append_exp_extensions(i2d_path, exptime, nexp, *, src, ninputs, nused=None):
    """Append ``EXPTIME`` + ``NEXP`` to an i2d (WCS taken from its SCI header).

    Opened in append mode, so the existing (multi-GB) HDUs are not rewritten.
    """
    wcs_header = fits.getheader(i2d_path, 'SCI')
    with fits.open(i2d_path, mode='append') as hdul:
        for hdu in exp_hdus(exptime, nexp, wcs_header, src=src,
                            ninputs=ninputs, nused=nused):
            hdul.append(hdu)


def popcount_planes(ctx):
    """Per-pixel number of set bits in an int32 context cube, summed over planes."""
    ctx = np.ascontiguousarray(ctx)
    u8 = ctx.view(np.uint8).reshape(ctx.shape + (4,))
    return _POP8[u8].sum(axis=(-1, 0) if ctx.ndim == 3 else -1, dtype=np.int16)


def context_is_placeholder(con_shape):
    """True for the 1x1(x1) CON written when the context cube was skipped."""
    return len(con_shape) < 2 or con_shape[-1] == 1


def _bit_masks(xposure, nplanes):
    """``{xposure: uint32[nplanes] bitmask}`` — the context bits of each distinct
    exposure time, so EXPTIME is one masked popcount per distinct value."""
    masks = {}
    for i, t in enumerate(np.asarray(xposure, dtype=np.float64)):
        if i >= 32 * nplanes:
            break
        m = masks.setdefault(float(t), np.zeros(nplanes, dtype=np.uint32))
        m[i // 32] |= np.uint32(1) << np.uint32(i % 32)
    return masks


def exp_from_context(con_hdu, out_shape, xposure, rows=256):
    """``(exptime, nexp, nused)`` from a CON HDU, streamed ``rows`` rows at a time.

    ``xposure[i]`` is the exposure time of context bit ``i`` (the ``i``-th
    contributing input; ``HDRTAB['XPOSURE']`` of the i2d). ``nused`` is the
    number of distinct context bits set anywhere.
    """
    ny, nx = out_shape
    shape = tuple(con_hdu.shape)
    if shape[-2:] != (ny, nx):
        raise ValueError(f'CON shape {shape} does not match tile {out_shape}')
    nplanes = shape[0] if len(shape) == 3 else 1
    masks = _bit_masks(xposure, nplanes)
    nexp = np.zeros(out_shape, dtype=np.int16)
    exptime = np.zeros(out_shape, dtype=np.float32)
    used = np.zeros(nplanes, dtype=np.uint32)
    for r0 in range(0, ny, rows):
        r1 = min(r0 + rows, ny)
        if len(shape) == 3:
            block = con_hdu.section[:, r0:r1, :]
        else:
            block = con_hdu.section[r0:r1, :][None]
        block = np.ascontiguousarray(block).astype(np.uint32, copy=False)
        nexp[r0:r1] = popcount_planes(block)
        acc = np.zeros(block.shape[1:], dtype=np.float64)
        for t, m in masks.items():
            acc += popcount_planes(block & m[:, None, None]) * t
        exptime[r0:r1] = acc
        used |= np.bitwise_or.reduce(block.reshape(nplanes, -1), axis=1)
    nused = int(popcount_planes(used.astype(np.int32)[:, None]).sum())
    return exptime, nexp, nused


def _footprint_polygon(sci_header, tile_wcs, nedge=16):
    """Detector outline (pixel edges, ``nedge`` points per side) in tile pixels."""
    n1, n2 = sci_header['NAXIS1'], sci_header['NAXIS2']
    e = np.linspace(0.0, 1.0, nedge + 1)[:-1]
    px = np.concatenate([e * n1, np.full_like(e, n1), (1 - e) * n1, np.zeros_like(e)]) - 0.5
    py = np.concatenate([np.zeros_like(e), e * n2, np.full_like(e, n2), (1 - e) * n2]) - 0.5
    with warnings.catch_warnings():
        # CRF headers trip astropy's datfix/obsfix notes, once per input.
        warnings.simplefilter('ignore', FITSFixedWarning)
        ra, dec = WCS(sci_header).pixel_to_world_values(px, py)
    return tile_wcs.world_to_pixel_values(ra, dec)


def _footprint_runs(header, tile_wcs, ny, nx):
    """``(rows, x_lo, x_hi)`` pixel-centre runs of one (convex) detector outline."""
    x, y = _footprint_polygon(header, tile_wcs)
    empty = (np.zeros(0, np.int64),) * 3
    if not (np.isfinite(x).all() and np.isfinite(y).all()):
        return empty
    y0 = max(0, int(np.ceil(y.min())))
    y1 = min(ny - 1, int(np.floor(y.max())))
    if y1 < y0 or x.max() < 0 or x.min() > nx - 1:
        return empty
    rows = np.arange(y0, y1 + 1, dtype=np.float64)
    lo = np.full(rows.size, np.inf)
    hi = np.full(rows.size, -np.inf)
    for xa, ya, xb, yb in zip(x, y, np.roll(x, -1), np.roll(y, -1)):
        if ya == yb:
            continue
        sel = (rows >= min(ya, yb)) & (rows <= max(ya, yb))
        if not sel.any():
            continue
        xr = xa + (rows[sel] - ya) * (xb - xa) / (yb - ya)
        lo[sel] = np.minimum(lo[sel], xr)
        hi[sel] = np.maximum(hi[sel], xr)
    ok = np.isfinite(lo) & np.isfinite(hi)
    xlo = np.clip(np.ceil(lo[ok]), 0, nx).astype(np.int64)
    xhi = np.clip(np.floor(hi[ok]) + 1, 0, nx).astype(np.int64)
    keep = xhi > xlo
    return rows[ok][keep].astype(np.int64), xlo[keep], xhi[keep]


def _count_runs(runs, ny, nx):
    """Per-pixel count of the runs: +1/-1 into a row-difference array, one cumsum."""
    diff = np.zeros((ny, nx + 1), dtype=np.int16)
    if runs:
        r = np.concatenate([x[0] for x in runs])
        np.add.at(diff, (r, np.concatenate([x[1] for x in runs])), 1)
        np.add.at(diff, (r, np.concatenate([x[2] for x in runs])), -1)
    return np.cumsum(diff[:, :nx], axis=1, dtype=np.int16)


def exp_from_footprints(input_headers, xposure, tile_header):
    """``(exptime, nexp)`` from detector footprints: the inputs whose outline
    contains each output pixel centre.

    Footprints are convex (a detector outline under a smooth distortion), so
    each one is one ``[x_lo, x_hi]`` run per output row, counted with a
    row-difference array. EXPTIME accumulates one count map per distinct
    exposure time, so float round-off never leaves residue outside coverage.
    """
    ny, nx = tile_header['NAXIS2'], tile_header['NAXIS1']
    tile_wcs = WCS(tile_header).celestial
    groups = {}
    for hdr, t in zip(input_headers, xposure):
        groups.setdefault(float(t), []).append(_footprint_runs(hdr, tile_wcs, ny, nx))
    nexp = np.zeros((ny, nx), dtype=np.int16)
    exptime = np.zeros((ny, nx), dtype=np.float32)
    for t, runs in groups.items():
        count = _count_runs(runs, ny, nx)
        nexp += count
        exptime += count * np.float32(t)
    return exptime, nexp


def _zero_where_sci_nan(planes, sci_path, rows=2048):
    """Zero every plane where SCI is non-finite; returns pixels zeroed that had
    coverage in the first plane."""
    zeroed = 0
    with fits.open(sci_path, memmap=True) as hdul:
        hdu = hdul[0] if hdul[0].data is not None else hdul[1]
        ny = planes[0].shape[0]
        for r0 in range(0, ny, rows):
            r1 = min(r0 + rows, ny)
            bad = ~np.isfinite(hdu.section[r0:r1, :])
            zeroed += int((bad & (planes[0][r0:r1] > 0)).sum())
            for p in planes:
                p[r0:r1][bad] = 0
    return zeroed


def _sci_header(sci_path):
    hdr = fits.getheader(sci_path, 0)
    return hdr if hdr.get('NAXIS', 0) >= 2 else fits.getheader(sci_path, 1)


def _xposure(path):
    """XPOSURE of a CRF (primary, else SCI header), the field expmap's quantity."""
    t = fits.getval(path, 'XPOSURE', 0) if 'XPOSURE' in fits.getheader(path, 0) else None
    return float(t if t is not None else fits.getval(path, 'XPOSURE', 'SCI'))


def backfill_mosaic_exp(filter_dir, mosaic_name, *, overwrite=False):
    """Write ``<mosaic_name>_exp.fits`` for an existing mosaic.

    Uses the i2d's CON cube + HDRTAB when CON is real (``context``), else the
    manifest inputs' detector footprints (``footprint``). The CON is looked for
    in the ``_i2d.fits`` and, failing that, an ``_aux.fits`` (the i2d minus its
    SCI/ERR/WHT, as some trees are slimmed to). Never touches the mosaic.
    Returns a one-line summary string.
    """
    out = os.path.join(filter_dir, f'{mosaic_name}{EXP_SUFFIX}')
    if os.path.exists(out) and not overwrite:
        return f'{mosaic_name}: exists'
    sci_path = os.path.join(filter_dir, f'{mosaic_name}_sci.fits')
    if not os.path.exists(sci_path):
        return f'{mosaic_name}: no _sci.fits, skipped'
    sci_hdr = _sci_header(sci_path)
    out_shape = (sci_hdr['NAXIS2'], sci_hdr['NAXIS1'])
    with open(os.path.join(filter_dir, f'{mosaic_name}_manifest.json')) as fh:
        inputs = json.load(fh)['inputs']

    planes = nused = None
    for suffix in ('_i2d.fits', '_aux.fits'):
        path = os.path.join(filter_dir, f'{mosaic_name}{suffix}')
        if not os.path.exists(path):
            continue
        with fits.open(path, memmap=True) as hdul:
            if ('CON' in hdul and 'HDRTAB' in hdul
                    and not context_is_placeholder(tuple(hdul['CON'].shape))):
                xposure = np.asarray(hdul['HDRTAB'].data['XPOSURE'], dtype=np.float64)
                exptime, nexp, nused = exp_from_context(hdul['CON'], out_shape, xposure)
                planes, src = (exptime, nexp), 'context'
        break
    if planes is None:
        src = 'footprint'
        paths = [os.path.join(filter_dir, os.path.basename(i['filename'])) for i in inputs]
        planes = exp_from_footprints([fits.getheader(p, 'SCI') for p in paths],
                                     [_xposure(p) for p in paths], sci_hdr)
    exptime, nexp = planes
    zeroed = _zero_where_sci_nan([nexp, exptime], sci_path)
    write_exp_file(out, exptime, nexp, sci_hdr, src=src, ninputs=len(inputs), nused=nused)
    pos = nexp > 0
    return (f'{mosaic_name}: {src} NINPUTS {len(inputs)}'
            + (f' NUSED {nused}' if nused is not None else '')
            + f' NEXP 1..{int(nexp.max()) if pos.any() else 0}'
            + f' EXPTIME {float(exptime[pos].min()) if pos.any() else 0:.0f}..'
              f'{float(exptime.max()):.0f} s'
            + f' (zeroed {zeroed} px outside SCI coverage)')


def run_exp_backfill(field, filters, *, tiles=None, overwrite=False,
                     n_processes=1):
    """Backfill ``_exp.fits`` for every existing mosaic of ``field``/``filters``."""
    import concurrent.futures
    import multiprocessing as mp

    jobs = []
    for filtname in filters:
        filter_dir = field.filter_dir(filtname)
        if not os.path.isdir(filter_dir):
            continue
        for name in sorted(os.listdir(filter_dir)):
            if not (name.startswith('mosaic_') and name.endswith('_manifest.json')):
                continue
            with open(os.path.join(filter_dir, name)) as fh:
                m = json.load(fh)
            if tiles and m.get('tile') not in tiles:
                continue
            jobs.append((str(filter_dir), name[:-len('_manifest.json')]))
    log(f'mosaic exp maps: {len(jobs)} mosaic(s) to check')
    if n_processes <= 1:
        for fd, nm in jobs:
            log(f'  {backfill_mosaic_exp(fd, nm, overwrite=overwrite)}')
        return
    with concurrent.futures.ProcessPoolExecutor(
            n_processes, mp_context=mp.get_context('spawn')) as pool:
        futs = {pool.submit(backfill_mosaic_exp, fd, nm, overwrite=overwrite): nm
                for fd, nm in jobs}
        for fut in concurrent.futures.as_completed(futs):
            log(f'  {fut.result()}')
