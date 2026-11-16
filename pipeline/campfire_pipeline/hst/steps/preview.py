"""preview: per-exposure quick-look PNGs of an HST exposure, for review.

The NIRCam pair (``nircam/steps/preview.py``), written next to the canonical
file:

* ``<file name>_preview.png``  thumbnail, long axis ``max_dim`` pixels
* ``<file name>_full.png``     every detector pixel, the canvas a mask editor
                               draws on

Both show the signal-to-noise per pixel, ``(SCI - sky) / ERR``, on one ZScale
stretch, with row 0 of the data at the bottom. The chips of an exposure are
stacked bottom to top in the order of their extensions with no gap (ACS/WFC:
``[SCI,1]`` below ``[SCI,2]``, 4096 x 4096). A region drawn on this canvas in
image coordinates is read back in the same frame by ``apply_masks``.

**What is blanked.** A pixel the pipeline already leaves out (a DQ bit outside
the tolerated set, which includes the cosmic rays of ``crreject`` and the
satellite trails of ``trails``) is drawn at zero, the noise floor. On a CCD
this is what makes the preview usable at all: a single ACS exposure carries
tens of thousands of cosmic-ray hits. What stays visible is what would go
into a mosaic, so what a reviewer sees is what is left to mask; a satellite
trail that was masked shows as a clean stripe, and one that was missed shows
as the trail.

Runs after ``crreject`` and before ``align``, so the flags are in and no
alignment has moved anything. Reads pixels only. Stamps ``CFP_PREV`` with the
render kind; a file whose stamp is of another kind is rendered again.
"""

import os
import shutil

import numpy as np
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.config import get_hst_step_config
from campfire_pipeline.hst.exposure import open_exposure

PREVIEW_KIND = 'snr-clean'


def preview_paths(exposure_file):
    base = exposure_file.removesuffix('.fits')
    return f'{base}_preview.png', f'{base}_full.png'


def snr_canvas(exposure_file):
    """The exposure as one signal-to-noise image: the chips stacked bottom to
    top in extension order, pixels the pipeline leaves out at zero."""
    chips = open_exposure(exposure_file)
    rows = []
    width = max(c.shape[1] for c in chips)
    with fits.open(exposure_file, memmap=False) as hdul:
        for c in chips:
            sci = np.asarray(hdul['SCI', c.extver].data, dtype=np.float64)
            err = np.asarray(hdul['ERR', c.extver].data, dtype=np.float64)
            dq = np.asarray(hdul['DQ', c.extver].data).astype(np.int64)
            bits = (c.detector.tolerated_bits if c.tolerated_bits is None
                    else c.tolerated_bits)
            good = ((dq & ~np.int64(bits)) == 0) & np.isfinite(sci) \
                & np.isfinite(err) & (err > 0)
            sky = c.header_sky
            if sky is None:
                sky = float(np.median(sci[good])) if good.any() else 0.0
            with np.errstate(divide='ignore', invalid='ignore'):
                snr = np.where(good, (sci - sky) / err, 0.0)
            snr = np.where(np.isfinite(snr), snr, 0.0)
            if snr.shape[1] < width:
                snr = np.pad(snr, ((0, 0), (0, width - snr.shape[1])))
            rows.append(snr)
    return np.vstack(rows)


def preview_step(exposure_file, step_config=None, overwrite=False):
    """Render the two preview PNGs of one canonical exposure. Returns their
    paths, or ``None`` when both exist with the current render kind and
    ``overwrite`` is not set."""
    from campfire_pipeline.nircam.steps._plots import _block_reduce, _zscale_limits
    from campfire_pipeline.nircam.steps.preview import _atomic_imsave

    rootname = os.path.basename(exposure_file).split('_')[0]
    thumb_path, full_path = preview_paths(exposure_file)
    if (not overwrite and os.path.exists(thumb_path) and os.path.exists(full_path)
            and str(cfp.step_value(exposure_file, 'CFP_PREV', keyset=cfp.HST)
                    ).startswith(PREVIEW_KIND)):
        log(f'Skipping preview on {rootname}: CFP_PREV ({PREVIEW_KIND}) already set')
        return None

    primary = fits.getheader(exposure_file, 0)
    from campfire_pipeline.hst.instruments import detector_for
    token = detector_for(primary.get('INSTRUME'), primary.get('DETECTOR')).token
    params = get_hst_step_config('preview', token, overrides=step_config)

    snr = snr_canvas(exposure_file)
    block = max(1, int(np.ceil(max(snr.shape) / int(params['max_dim']))))
    small = _block_reduce(snr, block)
    vmin, vmax = _zscale_limits(small)
    _atomic_imsave(thumb_path, small, cmap=params['cmap'], vmin=vmin, vmax=vmax)
    _atomic_imsave(full_path, snr, cmap=params['cmap'], vmin=vmin, vmax=vmax)

    tmp = f'{exposure_file}.prev.tmp'
    try:
        shutil.copyfile(exposure_file, tmp)
        with fits.open(tmp, mode='update', memmap=False) as hdul:
            for key, val in cfp.format(keyset=cfp.HST,
                                       CFP_PREV=PREVIEW_KIND).items():
                hdul[0].header[key] = val
        os.replace(tmp, exposure_file)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    log(f'preview: {rootname} {os.path.basename(thumb_path)} '
        f'({small.shape[1]}x{small.shape[0]}), {os.path.basename(full_path)} '
        f'({snr.shape[1]}x{snr.shape[0]})')
    return thumb_path, full_path
