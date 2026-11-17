"""Plain-FITS mosaic writer for instruments without JWST datamodels.

Writes the same extension layout the NIRCam route writes through
``stdatamodels`` — ``SCI``, ``ERR``, ``CON``, ``WHT``, then ``EXPTIME`` and
``NEXP`` — so the steps that run on a mosaic afterwards (background
subtraction, extension splitting, thumbnails, exposure-map backfill) read either
kind the same way. In place of the JWST ``HDRTAB`` it writes an ``INPUTS``
table with one row per contributing chip (:meth:`Chip.provenance`).

Header keywords follow the names a JWST i2d carries where one exists. The
primary has ``PIXAR_SR``, ``PIXAR_A2``, ``PIXFRAC``, ``KERNEL``, ``RESWHT``,
``NDRIZ`` (chips), ``NCOMBINE`` (exposures), ``PXSCLRT``, ``TEXPTIME``,
``S_REGION``, ``DATE`` and the CAMPFIRE stamps ``CMPFRVER`` / ``CMPFRTIM``.
``SCI`` repeats what a reader of the science image alone needs (``XPOSURE``,
``PIXAR_SR``, ``PIXAR_A2``, ``S_REGION``), because the split files are written
from the ``SCI`` header and have no primary. The instrument adds its identity,
times, photometric conversion and calibration versions to both through
``Chip.mosaic_header`` (``hst/exposure.py``). Exposure time and ``NCOMBINE``
count an exposure once, however many chips it has.
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
    pixar_sr = pixar_a2 * _ARCSEC_TO_RAD ** 2
    s_region = grid.footprint_sregion()
    now = datetime.now(timezone.utc)

    # Exposure time and count are per exposure. A file with several chips (an
    # ACS/WFC exposure has two) is one exposure: its time counts once. The
    # ``exptime`` argument is the drizzle's own sum, which runs over chips.
    if chips:
        per_exposure = {}
        for c in chips:
            per_exposure.setdefault(c.path, float(c.xposure))
        xposure, n_exposures = float(sum(per_exposure.values())), len(per_exposure)
    else:
        xposure, n_exposures = float(exptime), None

    # What the instrument adds: its identity as the archive names it, the
    # times of the inputs, the photometric conversion, the calibration
    # versions (``Chip.mosaic_header``, optional).
    extra_primary, extra_sci = [], []
    if chips and hasattr(type(chips[0]), 'mosaic_header'):
        extra_primary, extra_sci = type(chips[0]).mosaic_header(chips)

    primary = fits.PrimaryHDU()
    ph = primary.header
    ph['DATE'] = (now.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3],
                  'UTC date this file was written')
    if chips:
        instruments = sorted({c.instrument for c in chips})
        filters = sorted({c.filter for c in chips})
        # The CAMPFIRE tokens (product names, directories). INSTRUME / FILTER
        # hold them too unless the instrument gives its archive names below.
        ph['INSTRUME'] = (','.join(instruments), 'instrument token(s) of the inputs')
        ph['FILTER'] = (','.join(filters), 'filter(s) of the inputs')
        ph['CFINSTR'] = (','.join(instruments), 'CAMPFIRE instrument token(s)')
        ph['CFFILT'] = (','.join(filters), 'CAMPFIRE filter token(s)')
    for key, value, comment in extra_primary:
        ph[key] = (value, comment)
    ph['BUNIT'] = (bunit, 'physical units of SCI and ERR')
    ph['PIXAR_SR'] = (pixar_sr, 'output pixel area (sr)')
    ph['PIXAR_A2'] = (pixar_a2, 'output pixel area (arcsec^2)')
    ph['PIXFRAC'] = (float(pixfrac), 'drizzle pixfrac')
    ph['KERNEL'] = (str(kernel), 'drizzle kernel')
    ph['RESWHT'] = (str(weight_type), 'drizzle weight type')
    if n_pointings is not None:
        ph['NDRIZ'] = (int(n_pointings), 'number of contributing chips')
    if n_exposures is not None:
        ph['NCOMBINE'] = (n_exposures, 'number of contributing exposures')
    if pixel_scale_ratio is not None:
        ph['PXSCLRT'] = (float(pixel_scale_ratio), 'output / native pixel scale')
    ph['TEXPTIME'] = (xposure, '[s] summed exposure time of the exposures')
    ph['S_REGION'] = s_region
    ph['CMPFRTIM'] = (now.isoformat(),
                      'UTC date/time of CAMPFIRE reduction (ISO 8601)')
    ph['CMPFRVER'] = (reduction_version, 'CAMPFIRE git commit (or pinned version)')
    if ctx is None:
        ph['CFNOCTX'] = (True, 'CAMPFIRE: CON is a placeholder (write_context=false)')

    def image(name, data, unit=None):
        hdu = fits.ImageHDU(data=data, header=wcs_header.copy(), name=name)
        if unit:
            hdu.header['BUNIT'] = (unit, 'physical units of the array values')
        return hdu

    # SCI carries what a reader of the science image alone needs, as a JWST
    # i2d's SCI does: the split files (`_sci.fits`, `_err.fits`, ...) are
    # written from this header and have no primary of their own.
    sci_hdu = image('SCI', sci.astype(np.float32, copy=False), bunit)
    sh = sci_hdu.header
    for key, value, comment in extra_sci:
        sh[key] = (value, comment)
    sh['XPOSURE'] = (xposure, '[s] summed exposure time of the exposures')
    if n_exposures is not None:
        sh['NCOMBINE'] = (n_exposures, 'number of contributing exposures')
    sh['PIXAR_SR'] = (pixar_sr, 'output pixel area (sr)')
    sh['PIXAR_A2'] = (pixar_a2, 'output pixel area (arcsec^2)')
    sh['S_REGION'] = (s_region, 'footprint of the tile')

    if ctx is None:
        con = np.zeros((1, 1, 1), dtype=np.int32)
    else:
        con = ctx[0] if (ctx.ndim == 3 and ctx.shape[0] == 1) else ctx
        con = con.astype(np.int32, copy=False)

    hdul = fits.HDUList([
        primary,
        sci_hdu,
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
