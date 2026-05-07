"""
resample: drizzle-combine canonical exposures into mosaic tiles.

Per-tile ensemble step. Selects exposures whose footprints intersect each
tile polygon, builds an ASN, and runs ``Image3Pipeline`` (resample only) to
produce ``_i2d.fits`` mosaic tiles in ``field.mosaic_dir/<filter>/``.

Input source for the canonical-exposure layout is
``field.get_exposure_files(filter, with_step='CFP_OUT')`` — only exposures
that have completed outlier detection are eligible to be drizzled.

Mosaic outputs and the manifest format are unchanged from the legacy
implementation: ``CMPFRTIM`` / ``CMPFRVER`` stamping on the primary header,
optional 2D background subtraction via ``SubtractBackground``, optional
extension splitting into ``_sci/_err/_wht/_srcmask`` files, and a
``_latest_`` symlink to the versioned output.
"""

import os
import shutil
from datetime import datetime

import numpy as np
from astropy.io import fits
from shapely.geometry import Polygon

from campfire_pipeline.common.io import log
from campfire_pipeline.nircam.geometry import select_overlapping_files


def _resolve_pixel_scale(value):
    """Return ``(scale_arcsec_float, scale_str)`` from a config value."""
    if isinstance(value, str):
        assert value.endswith('mas')
        return float(value[:-3]) / 1000, str(value)
    if value > 1:
        return float(value) / 1000, f'{int(value)}mas'
    return float(value), f'{int(value * 1000)}mas'


def _drizzle_tile_via_campfire(
    selected_files,
    output_path,
    *,
    crpix,
    crval,
    shape,
    rotation,
    pixel_scale,
    resample_cfg,
    reduction_version,
):
    """Drizzle ``selected_files`` to ``output_path`` via the campfire-native
    drizzle (issue #138).

    Same observable contract as ``_drizzle_tile_via_jwst`` — produces an i2d
    at ``output_path`` with SCI/ERR/WHT/CON extensions and ``CMPFRTIM`` /
    ``CMPFRVER`` stamped on the primary header.
    """
    from campfire_pipeline.nircam.drizzle import drizzle_tile

    drizzle_tile(
        selected_files,
        output_path,
        crpix=crpix,
        crval=crval,
        shape=shape,
        rotation=rotation,
        pixel_scale=pixel_scale,
        pixfrac=resample_cfg.get('pixfrac', 1.0),
        kernel=resample_cfg.get('kernel', 'square'),
        weight_type=resample_cfg.get('weight_type', 'ivm'),
        good_bits=resample_cfg.get('good_bits', '~DO_NOT_USE'),
        reduction_version=reduction_version,
    )


def _drizzle_tile_via_jwst(
    selected_files,
    output_path,
    *,
    crpix,
    crval,
    shape,
    rotation,
    pixel_scale,
    resample_cfg,
    reduction_version,
):
    """Drizzle ``selected_files`` to ``output_path`` via JWST ``Image3Pipeline``.

    Builds an ASN next to ``output_path``, runs ``Image3Pipeline`` with
    every substep but ``resample`` skipped, and stamps ``CMPFRTIM`` /
    ``CMPFRVER`` on the primary header of the resulting i2d.

    Parameters
    ----------
    selected_files : list of str
        CRF input paths.
    output_path : str
        Destination i2d path. Must end in ``_i2d.fits``.
    crpix, crval, shape, rotation : tile WCS parameters from
        ``Field.get_tile_wcs``.
    pixel_scale : float
        Output pixel scale in arcseconds.
    resample_cfg : dict
        ``[nircam.resample]`` config block (used for pixfrac/kernel etc.).
    reduction_version : str
        Stamped as ``CMPFRVER`` on the primary header.
    """
    from jwst.associations.lib.rules_level3_base import DMS_Level3_Base
    from jwst.associations import asn_from_list
    from jwst.pipeline import calwebb_image3

    mosaic_outdir = os.path.dirname(output_path)
    mosaic_name = os.path.basename(output_path).removesuffix('_i2d.fits')

    asn_file = os.path.join(mosaic_outdir, f'{mosaic_name}_asn.json')
    asn = asn_from_list.asn_from_list(
        selected_files, rule=DMS_Level3_Base, product_name=mosaic_name,
    )
    with open(asn_file, 'w') as fp:
        _, serialized = asn.dump(format='json')
        fp.write(serialized)

    params = {
        'assign_mtwcs': {'skip': True},
        'tweakreg': {'skip': True},
        'skymatch': {'skip': True},
        'outlier_detection': {'skip': True},
        'resample': {
            'pixfrac': resample_cfg.get('pixfrac', 1),
            'kernel': resample_cfg.get('kernel', 'square'),
            'pixel_scale': pixel_scale,
            'rotation': rotation,
            'output_shape': shape,
            'crpix': crpix,
            'crval': crval,
            'fillval': 'indef',
            'weight_type': 'ivm',
            'single': False,
            'blendheaders': True,
            'save_results': True,
        },
        'source_catalog': {'skip': True},
    }

    calwebb_image3.Image3Pipeline.call(
        asn_file, output_dir=mosaic_outdir, steps=params,
        save_results=True,
    )

    with fits.open(output_path, mode='update') as hdul:
        hdul[0].header['CMPFRTIM'] = (
            str(datetime.now()),
            'Date/time of CAMPFIRE reduction',
        )
        hdul[0].header['CMPFRVER'] = (
            reduction_version,
            'CAMPFIRE git commit (or pinned version)',
        )


def resample_step(filtname, exposure_files, field, step_config,
                  reduction_version, overwrite=False):
    """Drizzle-combine canonical exposure files into mosaic tiles.

    Parameters
    ----------
    filtname : str
    exposure_files : list of str
        Canonical exposure paths (``CFP_OUT`` already stamped).
    field : Field
    step_config : dict
        ``[nircam.resample]`` (legacy ``[nircam.stage3.resample]``).
    reduction_version : str
        Campfire reduction version stamped onto each mosaic primary header
        as ``CMPFRVER``.
    overwrite : bool
    """
    from campfire_pipeline.nircam.manifest import (
        check_config_changed, check_inputs_changed,
        create_manifest, write_manifest,
    )

    pixel_scale, pixel_scale_str = _resolve_pixel_scale(
        step_config.get('pixel_scale', '60mas'),
    )
    mode = step_config.get('mode', 'tile')
    if mode != 'tile':
        raise NotImplementedError(f"resample mode {mode!r} not supported")

    version = step_config.get('version', 'v0_1')
    tiles = step_config.get('tile', None) or list(field.tiles.keys())
    if isinstance(tiles, str):
        tiles = [tiles]

    for tile in tiles:
        log(f"resample: tile {tile}, {filtname}, {pixel_scale_str}")

        mosaic_name = step_config.get(
            'mosaic_name',
            'mosaic_nircam_[filter]_[field_name]_[pixel_scale]_[version]_[tile]',
        )
        mosaic_name = (mosaic_name
                       .replace('[filter]', filtname)
                       .replace('[field_name]', field.name)
                       .replace('[pixel_scale]', pixel_scale_str)
                       .replace('[version]', version)
                       .replace('[tile]', tile))
        mosaic_outdir = os.path.join(field.mosaic_dir, filtname)
        os.makedirs(mosaic_outdir, exist_ok=True)
        mosaic_file = os.path.join(mosaic_outdir, f'{mosaic_name}_i2d.fits')
        manifest_dir = os.path.join(mosaic_outdir, 'manifests')
        manifest_path = os.path.join(
            manifest_dir, f'{mosaic_name}_manifest.json',
        )

        log(f"  mosaic → {mosaic_file}")

        tile_polygon = Polygon(field.get_tile_corners(tile))
        selected = select_overlapping_files(exposure_files, tile_polygon)
        if not selected:
            log(f"  no exposures overlap {tile}; skipping")
            continue

        # Decide if we need to rebuild
        needs_rebuild = overwrite
        if not needs_rebuild and not os.path.exists(mosaic_file):
            needs_rebuild = True
            log(f"  mosaic does not exist; building")
        if not needs_rebuild:
            inputs_changed, reasons = check_inputs_changed(
                manifest_path, selected,
            )
            cfg_changed = check_config_changed(
                manifest_path, {'resample': step_config}, pixel_scale_str,
            )
            if inputs_changed or cfg_changed:
                needs_rebuild = True
                all_reasons = list(reasons) if inputs_changed else []
                if cfg_changed:
                    all_reasons.append('processing config changed')
                log(f"  tile {tile} stale: {'; '.join(all_reasons)}")
            else:
                log(f"  tile {tile} up-to-date "
                    f"({len(selected)} inputs unchanged); skipping")

        if needs_rebuild:
            log(f"  drizzling {len(selected)} exposures")

            crpix, crval, shape, rotation = field.get_tile_wcs(
                tile, pixel_scale=pixel_scale_str,
            )

            implementation = step_config.get('implementation', 'jwst')
            if implementation == 'campfire':
                drizzle_fn = _drizzle_tile_via_campfire
            elif implementation == 'jwst':
                drizzle_fn = _drizzle_tile_via_jwst
            else:
                raise ValueError(
                    f"Unknown resample.implementation {implementation!r}; "
                    f"expected 'jwst' or 'campfire'"
                )

            drizzle_fn(
                selected,
                mosaic_file,
                crpix=crpix,
                crval=crval,
                shape=shape,
                rotation=rotation,
                pixel_scale=pixel_scale,
                resample_cfg=step_config,
                reduction_version=reduction_version,
            )

            manifest = create_manifest(
                mosaic_name, field, filtname, tile, pixel_scale_str,
                version, selected, {'resample': step_config},
            )
            write_manifest(manifest, manifest_dir)

        if step_config.get('background_subtract', True):
            from campfire_pipeline.nircam.bkgsub import SubtractBackground

            pre_bkg = mosaic_file.replace('_i2d.fits', '_i2d_before_bkgsub.fits')
            bkgsub_done = os.path.exists(pre_bkg)
            if needs_rebuild or not bkgsub_done:
                if needs_rebuild and bkgsub_done:
                    os.remove(pre_bkg)

                bkg = SubtractBackground(
                    ring_radius_in=step_config.get('ring_radius_in', 80),
                    ring_width=step_config.get('ring_width', 4),
                    ring_downsample=step_config.get('ring_downsample', 4),
                    ring_clip_max_sigma=step_config.get(
                        'ring_clip_max_sigma', 5.0),
                    ring_clip_box_size=step_config.get(
                        'ring_clip_box_size', 100),
                    ring_clip_filter_size=step_config.get(
                        'ring_clip_filter_size', 3),
                    tier_kernel_size=step_config.get(
                        'tier_kernel_size', [25, 15, 5, 2]),
                    tier_npixels=step_config.get(
                        'tier_npixels', [15, 10, 3, 1]),
                    tier_nsigma=step_config.get(
                        'tier_nsigma', [1.5, 1.5, 1.5, 1.5]),
                    tier_dilate_size=step_config.get(
                        'tier_dilate_size', [33, 25, 21, 19]),
                    bg_box_size=step_config.get('bg_box_size', 10),
                    bg_filter_size=step_config.get('bg_filter_size', 5),
                    bg_exclude_percentile=step_config.get(
                        'bg_exclude_percentile', 90),
                    bg_sigma=step_config.get('bg_sigma', 3),
                    bg_interpolator=step_config.get('bg_interpolator', 'zoom'),
                    suffix='bkgsub',
                    replace_sci=True,
                )
                bkg.call(mosaic_file)

                log(f"  copying input → {os.path.basename(pre_bkg)}")
                shutil.copy2(mosaic_file, pre_bkg)

                log(f"  renaming {os.path.basename(bkg.outfile)} → "
                    f"{os.path.basename(mosaic_file)}")
                shutil.move(bkg.outfile, mosaic_file)
            else:
                log(f"  skipping background subtraction "
                    f"for {os.path.basename(mosaic_file)}")

        if needs_rebuild:
            # Enforce SCI = NaN where WHT = 0 (matches the ERR convention),
            # and on the jwst path convert any PC+CDELT WCS encoding to
            # CD-matrix form for DS9 compatibility. The campfire drizzle
            # path already writes CD directly via _write_i2d_fits. Applied
            # after bkgsub and before extension splitting so the canonical
            # SCI header propagates to _sci/_err/_wht/_srcmask.
            with fits.open(mosaic_file, mode='update') as hdul:
                sci_hdr = hdul['SCI'].header
                wht = hdul['WHT'].data
                sci = hdul['SCI'].data

                no_cov = wht == 0
                n_no_cov = int(no_cov.sum())
                if n_no_cov:
                    sci[no_cov] = np.nan
                    hdul['SCI'].data = sci
                    log(
                        f"  set SCI=NaN at {n_no_cov:,} WHT=0 pixels "
                        f"({n_no_cov / sci.size * 100:.1f}%)"
                    )

                if 'CD1_1' not in sci_hdr and (
                    'PC1_1' in sci_hdr or 'PC1_2' in sci_hdr
                    or 'PC2_1' in sci_hdr or 'PC2_2' in sci_hdr
                ):
                    cdelt1 = float(sci_hdr.get('CDELT1', 1.0))
                    cdelt2 = float(sci_hdr.get('CDELT2', 1.0))
                    pc11 = float(sci_hdr.get('PC1_1', 1.0))
                    pc12 = float(sci_hdr.get('PC1_2', 0.0))
                    pc21 = float(sci_hdr.get('PC2_1', 0.0))
                    pc22 = float(sci_hdr.get('PC2_2', 1.0))
                    cd_comment = 'Linear transformation matrix element'
                    sci_hdr['CD1_1'] = (pc11 * cdelt1, cd_comment)
                    sci_hdr['CD1_2'] = (pc12 * cdelt1, cd_comment)
                    sci_hdr['CD2_1'] = (pc21 * cdelt2, cd_comment)
                    sci_hdr['CD2_2'] = (pc22 * cdelt2, cd_comment)
                    for k in ('PC1_1', 'PC1_2', 'PC2_1', 'PC2_2'):
                        if k in sci_hdr:
                            del sci_hdr[k]
                    cdelt_comment = (
                        '[deg] Coordinate increment at reference point'
                    )
                    sci_hdr['CDELT1'] = (1.0, cdelt_comment)
                    sci_hdr['CDELT2'] = (1.0, cdelt_comment)
                    log("  converted SCI WCS from PC+CDELT to CD matrix")

        if needs_rebuild and step_config.get('split_extensions', True):
            log("  splitting extensions")
            ext_outdir = os.path.join(mosaic_outdir, 'extensions')
            os.makedirs(ext_outdir, exist_ok=True)

            sci = fits.getdata(mosaic_file, extname='SCI')
            hdr = fits.getheader(mosaic_file, extname='SCI')
            err = fits.getdata(mosaic_file, extname='ERR')
            wht = fits.getdata(mosaic_file, extname='WHT')

            base = os.path.basename(mosaic_file)
            fits.PrimaryHDU(data=sci, header=hdr).writeto(
                os.path.join(ext_outdir, base.replace('_i2d.fits', '_sci.fits')),
                overwrite=True,
            )
            hdr.update({'EXTNAME': 'ERR'})
            fits.PrimaryHDU(data=err, header=hdr).writeto(
                os.path.join(ext_outdir, base.replace('_i2d.fits', '_err.fits')),
                overwrite=True,
            )
            hdr.update({'EXTNAME': 'WHT'})
            fits.PrimaryHDU(data=wht, header=hdr).writeto(
                os.path.join(ext_outdir, base.replace('_i2d.fits', '_wht.fits')),
                overwrite=True,
            )

            try:
                srcmask = fits.getdata(mosaic_file, extname='SRCMASK')
                hdr.update({'EXTNAME': 'SRCMASK'})
                fits.PrimaryHDU(data=srcmask, header=hdr).writeto(
                    os.path.join(
                        ext_outdir,
                        base.replace('_i2d.fits', '_srcmask.fits'),
                    ),
                    overwrite=True,
                )
            except KeyError:
                log(f"  {mosaic_name} has no SRCMASK extension")

        latest_name = mosaic_name.replace(f'_{version}_', '_latest_')
        latest_link = os.path.join(mosaic_outdir, f'{latest_name}_i2d.fits')
        if os.path.islink(latest_link) or os.path.exists(latest_link):
            os.remove(latest_link)
        os.symlink(os.path.basename(mosaic_file), latest_link)
        log(f"  symlinked {os.path.basename(latest_link)} → "
            f"{os.path.basename(mosaic_file)}")

        if step_config.get('split_extensions', True):
            ext_outdir = os.path.join(mosaic_outdir, 'extensions')
            base = os.path.basename(mosaic_file)
            for suffix in ('_sci.fits', '_err.fits',
                           '_wht.fits', '_srcmask.fits'):
                ver_ext = os.path.join(
                    ext_outdir, base.replace('_i2d.fits', suffix),
                )
                if os.path.exists(ver_ext):
                    latest_ext = os.path.join(
                        ext_outdir, f'{latest_name}{suffix}',
                    )
                    if (os.path.islink(latest_ext)
                            or os.path.exists(latest_ext)):
                        os.remove(latest_ext)
                    os.symlink(os.path.basename(ver_ext), latest_ext)
