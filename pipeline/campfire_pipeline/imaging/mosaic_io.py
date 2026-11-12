"""Plain-FITS mosaic writer for instruments without JWST datamodels.

Writes the same extension layout the NIRCam route writes through
``stdatamodels`` — ``SCI``, ``ERR``, ``CON``, ``WHT``, then ``EXPTIME`` and
``NEXP`` — so the steps that run on a mosaic afterwards (background
subtraction, extension splitting, thumbnails, exposure-map backfill) read either
kind the same way. In place of the JWST ``HDRTAB`` it writes an ``INPUTS``
table with one row per contributing chip (:meth:`Chip.provenance`).

Primary-header keywords follow the names a JWST i2d carries where one exists
(``PIXAR_SR``, ``PIXAR_A2``, ``PIXFRAC``, ``KERNEL``, ``RESWHT``, ``NDRIZ``,
``PXSCLRT``) plus the CAMPFIRE stamps ``CMPFRVER`` / ``CMPFRTIM``.
"""

import os
from datetime import datetime, timezone

import numpy as np
from astropy.io import fits
from astropy.table import Table

_ARCSEC_TO_RAD = np.pi / (180.0 * 3600.0)


def _inputs_hdu(rows):
    """``INPUTS`` binary table from provenance dicts (union of their keys)."""
    keys = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    table = Table()
    for k in keys:
        values = [row.get(k) for row in rows]
        if any(isinstance(v, str) for v in values):
            table[k] = ['' if v is None else str(v) for v in values]
        else:
            table[k] = [np.nan if v is None else v for v in values]
    hdu = fits.table_to_hdu(table)
    hdu.name = 'INPUTS'
    return hdu


def write_mosaic_fits(output_path, *, sci, err, wht, ctx, grid, chips,
                      reduction_version, exptime, pixfrac, kernel, weight_type,
                      n_pointings=None, pixel_scale_ratio=None,
                      compress_context=True, exp_planes=None, exp_inputs=None,
                      exp_used=None, bunit='MJy/sr'):
    """Write a drizzled tile as a multi-extension FITS file.

    ``chips`` are the contributing :class:`~campfire_pipeline.imaging.exposure.Chip`
    objects, in drizzle order (row ``i`` of ``INPUTS`` is context bit ``i``).
    ``ctx`` is the context cube or ``None`` when it was not accumulated, in which
    case a 1x1x1 placeholder is written and marked with ``CFNOCTX``.
    """
    wcs_header = grid.header()
    pixar_a2 = float(grid.pixel_scale) ** 2

    primary = fits.PrimaryHDU()
    ph = primary.header
    if chips:
        instruments = sorted({c.instrument for c in chips})
        filters = sorted({c.filter for c in chips})
        ph['INSTRUME'] = (','.join(instruments), 'instrument token(s) of the inputs')
        ph['FILTER'] = (','.join(filters), 'filter(s) of the inputs')
    ph['BUNIT'] = (bunit, 'physical units of SCI and ERR')
    ph['PIXAR_SR'] = (pixar_a2 * _ARCSEC_TO_RAD ** 2, 'output pixel area (sr)')
    ph['PIXAR_A2'] = (pixar_a2, 'output pixel area (arcsec^2)')
    ph['PIXFRAC'] = (float(pixfrac), 'drizzle pixfrac')
    ph['KERNEL'] = (str(kernel), 'drizzle kernel')
    ph['RESWHT'] = (str(weight_type), 'drizzle weight type')
    if n_pointings is not None:
        ph['NDRIZ'] = (int(n_pointings), 'number of contributing inputs')
    if pixel_scale_ratio is not None:
        ph['PXSCLRT'] = (float(pixel_scale_ratio), 'output / native pixel scale')
    ph['TEXPTIME'] = (float(exptime), 'summed exposure time of the inputs (s)')
    ph['S_REGION'] = grid.footprint_sregion()
    ph['CMPFRTIM'] = (datetime.now(timezone.utc).isoformat(),
                      'UTC date/time of CAMPFIRE reduction (ISO 8601)')
    ph['CMPFRVER'] = (reduction_version, 'CAMPFIRE git commit (or pinned version)')
    if ctx is None:
        ph['CFNOCTX'] = (True, 'CAMPFIRE: CON is a placeholder (write_context=false)')

    def image(name, data, unit=None):
        hdu = fits.ImageHDU(data=data, header=wcs_header.copy(), name=name)
        if unit:
            hdu.header['BUNIT'] = unit
        return hdu

    if ctx is None:
        con = np.zeros((1, 1, 1), dtype=np.int32)
    else:
        con = ctx[0] if (ctx.ndim == 3 and ctx.shape[0] == 1) else ctx
        con = con.astype(np.int32, copy=False)

    hdul = fits.HDUList([
        primary,
        image('SCI', sci.astype(np.float32, copy=False), bunit),
        image('ERR', err.astype(np.float32, copy=False), bunit),
        fits.ImageHDU(data=con, name='CON'),
        image('WHT', wht.astype(np.float32, copy=False)),
    ])
    if chips:
        hdul.append(_inputs_hdu([c.provenance() for c in chips]))

    tmp = f'{output_path}.mosaic.tmp'
    try:
        hdul.writeto(tmp, overwrite=True)
        os.replace(tmp, output_path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

    if compress_context and ctx is not None:
        from campfire_pipeline.imaging.drizzle import compress_context_extension
        compress_context_extension(output_path)

    if exp_planes is not None:
        from campfire_pipeline.imaging.mosaic_exp import append_exp_extensions
        append_exp_extensions(output_path, *exp_planes, src='drizzle',
                              ninputs=exp_inputs, nused=exp_used)
