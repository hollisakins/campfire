"""What happens to a mosaic tile after the drizzle, for every imaging instrument.

Moved unchanged out of the NIRCam resample step (``nircam/steps/resample.py``)
so that the HST resample step runs the same code: the optional 2D background
subtraction with its ``CFP_BKGS`` stamp and rollback snapshot, the split
``_sci`` / ``_err`` / ``_wht`` / ``_srcmask`` / ``_exp`` files, and the
thumbnail, quick-look and background plots. It reads and writes plain FITS
extensions (``SCI``, ``ERR``, ``WHT``, ``SRCMASK``, ``EXPTIME``, ``NEXP``),
which the JWST route and the plain-FITS mosaic writer
(``imaging/mosaic_io.py``) both produce.
"""

import os
import shutil

from astropy.io import fits

from campfire_pipeline.common.io import log
from campfire_pipeline.imaging.manifest import (
    BKGSUB_PIXEL_DEFAULTS, MOSAIC_BKGSUB_KEY, bkgsub_stamp_value,
)


def finish_mosaic(mosaic_file, mosaic_name, step_config, needs_rebuild):
    """Background-subtract, split and plot one mosaic tile.

    ``mosaic_file`` is the ``_i2d.fits`` of the tile and ``mosaic_name`` its
    basename without that suffix. ``step_config`` is the instrument's
    ``resample`` block. ``needs_rebuild`` says whether the tile was drizzled in
    this run: a freshly drizzled tile is always subtracted, split and plotted;
    an up-to-date one only has missing pieces restored.
    """
    mosaic_outdir = os.path.dirname(mosaic_file)

    if step_config.get('background_subtract', True):
        from campfire_pipeline.imaging.bkgsub import SubtractBackground

        pre_bkg = mosaic_file.replace('_i2d.fits', '_i2d_before_bkgsub.fits')
        stamp_card = (
            bkgsub_stamp_value(step_config),
            'campfire: mosaic bkgsub (alg ver, params hash)',
        )

        # The CFP_BKGS primary-header stamp on the i2d — not the existence
        # of the _i2d_before_bkgsub.fits snapshot — is the record that the
        # on-disk pixels are already background-subtracted (issue #427):
        # deriving bkgsub_done from the snapshot made a deleted snapshot
        # silently subtract the background a second time. The snapshot is
        # a rollback convenience copy, deletable once its mosaic carries
        # the stamp.
        if needs_rebuild:
            bkgsub_done = False  # freshly drizzled, stamp gone with it
        else:
            with fits.open(mosaic_file) as hdul:
                bkgsub_done = MOSAIC_BKGSUB_KEY in hdul[0].header
                has_srcmask = 'SRCMASK' in hdul
            if not bkgsub_done and os.path.exists(pre_bkg):
                # No stamp but a snapshot on disk: either a legacy mosaic
                # subtracted before the stamp existed, or a rollback where
                # the snapshot was copied over the i2d (restoring
                # unsubtracted pixels) with the snapshot left in place.
                # SubtractBackground always appends a SRCMASK extension
                # and the snapshot (copied from the pre-subtraction
                # drizzle output) never carries one, so SRCMASK presence
                # tells the two apart.
                if has_srcmask:
                    # Legacy subtracted mosaic. The manifest config check
                    # passed to get here, so the current bkgsub settings
                    # are the ones that produced it — backfill the stamp
                    # so the snapshot becomes deletable from now on.
                    bkgsub_done = True
                    log(f"  backfilling {MOSAIC_BKGSUB_KEY} stamp on "
                        f"pre-stamp mosaic {os.path.basename(mosaic_file)}")
                    with fits.open(mosaic_file, mode='update') as hdul:
                        hdul[0].header[MOSAIC_BKGSUB_KEY] = stamp_card
                else:
                    log("  snapshot present but i2d has no SRCMASK — "
                        "restored pre-bkgsub data; re-running bkgsub")

        if not bkgsub_done:
            if os.path.exists(pre_bkg):
                os.remove(pre_bkg)

            # Pixel-affecting settings and their defaults come from the
            # manifest's BKGSUB_PIXEL_DEFAULTS — the same dict the tile
            # config hash iterates — so nothing applied here can change
            # mosaic pixels without also marking existing tiles stale.
            bkg = SubtractBackground(
                **{k: step_config.get(k, d)
                   for k, d in BKGSUB_PIXEL_DEFAULTS.items()},
                wht_aware=step_config.get('wht_aware', True),
                suffix='bkgsub',
                replace_sci=True,
            )
            bkg.call(mosaic_file)

            # Stamp the subtracted output *before* it is renamed into
            # place, so stamp and pixels land together atomically and the
            # pre-bkgsub snapshot (copied from the un-stamped input) never
            # carries the stamp.
            with fits.open(bkg.outfile, mode='update') as hdul:
                hdul[0].header[MOSAIC_BKGSUB_KEY] = stamp_card

            if step_config.get('keep_pre_bkgsub', True):
                log(f"  copying input → {os.path.basename(pre_bkg)}")
                shutil.copy2(mosaic_file, pre_bkg)
            else:
                log("  keep_pre_bkgsub = false; "
                    "not snapshotting the pre-bkgsub mosaic")

            log(f"  renaming {os.path.basename(bkg.outfile)} → "
                f"{os.path.basename(mosaic_file)}")
            shutil.move(bkg.outfile, mosaic_file)
        else:
            log(f"  skipping background subtraction "
                f"for {os.path.basename(mosaic_file)}")

    # No post-drizzle "SCI=NaN where WHT=0" pass is needed: both drizzle
    # backends use fillval='NaN', so uncovered / fully-masked pixels are
    # already NaN, and bkgsub preserves that (it masks isnan(sci) before
    # fitting and subtracts elementwise, so NaN - background = NaN).

    split_enabled = step_config.get('split_extensions', True)

    # Recover from a prior run that produced the i2d but failed (or was
    # interrupted) before writing the separated extension files. Since
    # splitting only reads from the existing i2d, we can re-split without
    # re-drizzling. needs_rebuild stays False, so bkgsub and the NaN-fill
    # (both already baked into the on-disk i2d) are not redone.
    missing_extensions = False
    if split_enabled and not needs_rebuild and os.path.exists(mosaic_file):
        base = os.path.basename(mosaic_file)
        required = ('_sci.fits', '_err.fits', '_wht.fits')
        missing_extensions = any(
            not os.path.exists(os.path.join(
                mosaic_outdir, base.replace('_i2d.fits', suffix)))
            for suffix in required
        )
        if not missing_extensions:
            # SRCMASK / EXPTIME are only expected when the i2d carries them
            # (the exposure maps are absent from i2ds drizzled before them).
            optional = [(ext, os.path.join(
                            mosaic_outdir, base.replace('_i2d.fits', suffix)))
                        for ext, suffix in (('SRCMASK', '_srcmask.fits'),
                                            ('EXPTIME', '_exp.fits'))]
            absent = [ext for ext, path in optional
                      if not os.path.exists(path)]
            if absent:
                with fits.open(mosaic_file) as hdul:
                    missing_extensions = any(ext in hdul for ext in absent)
        if missing_extensions:
            log(f"  {mosaic_name} i2d present but split extensions "
                f"missing; re-splitting")

    do_split = (needs_rebuild or missing_extensions) and split_enabled
    do_plot = needs_rebuild and step_config.get('plot', True)

    if do_split or do_plot:
        with fits.open(mosaic_file) as hdul:
            sci = hdul['SCI'].data.copy()
            hdr = hdul['SCI'].header.copy()
            err = hdul['ERR'].data.copy() if do_split else None
            wht = hdul['WHT'].data.copy() if do_split else None
            srcmask = (hdul['SRCMASK'].data.copy()
                       if do_split and 'SRCMASK' in hdul else None)
            exp_planes = exp_hdr = None
            if do_split and 'EXPTIME' in hdul and 'NEXP' in hdul:
                exp_planes = (hdul['EXPTIME'].data.copy(),
                              hdul['NEXP'].data.copy())
                exp_hdr = hdul['NEXP'].header.copy()

    if do_split:
        log("  splitting extensions")
        base = os.path.basename(mosaic_file)
        fits.PrimaryHDU(data=sci, header=hdr).writeto(
            os.path.join(mosaic_outdir,
                         base.replace('_i2d.fits', '_sci.fits')),
            overwrite=True,
        )
        hdr.update({'EXTNAME': 'ERR'})
        fits.PrimaryHDU(data=err, header=hdr).writeto(
            os.path.join(mosaic_outdir,
                         base.replace('_i2d.fits', '_err.fits')),
            overwrite=True,
        )
        hdr.update({'EXTNAME': 'WHT'})
        fits.PrimaryHDU(data=wht, header=hdr).writeto(
            os.path.join(mosaic_outdir,
                         base.replace('_i2d.fits', '_wht.fits')),
            overwrite=True,
        )

        if srcmask is not None:
            hdr.update({'EXTNAME': 'SRCMASK'})
            fits.PrimaryHDU(data=srcmask, header=hdr).writeto(
                os.path.join(
                    mosaic_outdir,
                    base.replace('_i2d.fits', '_srcmask.fits'),
                ),
                overwrite=True,
            )
        else:
            log(f"  {mosaic_name} has no SRCMASK extension")

        if exp_planes is not None:
            from campfire_pipeline.imaging.mosaic_exp import write_exp_file
            write_exp_file(
                os.path.join(mosaic_outdir,
                             base.replace('_i2d.fits', '_exp.fits')),
                *exp_planes, exp_hdr, src=exp_hdr['EXPSRC'],
                ninputs=exp_hdr['NINPUTS'], nused=exp_hdr.get('NUSED'),
            )
        else:
            log(f"  {mosaic_name} has no EXPTIME/NEXP extensions "
                f"(`cfpipe nircam mosaic-exp` backfills _exp.fits)")

    if do_plot:
        from campfire_pipeline.nircam.steps._plots import (
            plot_mosaic_bkgsub, plot_mosaic_thumbnail,
        )
        downsample = int(step_config.get('plot_downsample', 4))

        # Thumbnail pair: a small table rendition + a large quick-look
        # for the web popup, both size-capped (see plot_mosaic_thumbnail).
        thumb_png = mosaic_file.replace('_i2d.fits', '_thumb.png')
        plot_mosaic_thumbnail(
            sci, thumb_png,
            max_dim=int(step_config.get('thumbnail_max_dim', 500)),
        )
        log(f"  saved {os.path.basename(thumb_png)}")

        quicklook_png = mosaic_file.replace('_i2d.fits', '_quicklook.png')
        plot_mosaic_thumbnail(
            sci, quicklook_png,
            max_dim=int(step_config.get('quicklook_max_dim', 4096)),
        )
        log(f"  saved {os.path.basename(quicklook_png)}")

        if step_config.get('background_subtract', True):
            pre_bkg_path = mosaic_file.replace(
                '_i2d.fits', '_i2d_before_bkgsub.fits',
            )
            if os.path.exists(pre_bkg_path):
                sci_before_arr = fits.getdata(pre_bkg_path, extname='SCI')
                bg_model = sci_before_arr - sci
                bkg_png = mosaic_file.replace('_i2d.fits', '_bkgsub.png')
                plot_mosaic_bkgsub(
                    sci_before_arr, sci, bg_model,
                    save_file=bkg_png, downsample=downsample,
                    title=mosaic_name,
                )
                log(f"  saved {os.path.basename(bkg_png)}")
