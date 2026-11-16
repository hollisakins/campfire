"""apply_masks: hand-drawn region masks on a canonical HST exposure.

The NIRCam workflow (``nircam/steps/apply_masks.py``), for the HST
instruments. First step of the combine phase. A reviewer's regions for an
exposure are a DS9 region file,

    reference/<instrument>/<field>/masks/<filter>/<file name without .fits>.reg

for example ``.../masks/f814w/j8pu6eicq_flc.reg``. The step rasterizes them and
writes the result to the canonical file as ``CFMASK`` extensions, one per chip
(``[CFMASK,1]``, ``[CFMASK,2]`` on ACS/WFC), 1 inside a region. ``SCI`` and
``DQ`` are not touched: the mask is recorded next to the data, and is rebuilt
from the region file every time, so removing a region undoes it.

The mask takes effect wherever the exposure is read for a mosaic: the adapter
(``hst/exposure.py``) leaves ``CFMASK`` pixels out, so the field-level outlier
pass and the drizzle do not see them.

**Coordinates.** A region in sky coordinates is projected onto each chip
through that chip's WCS, distortion included. A region in image (pixel)
coordinates is in the frame of the exposure's preview (``hst/steps/preview.py``),
the canvas a mask editor draws on: the chips stacked bottom to top in the order
of their extensions, with no gap. For ACS/WFC that is 4096 x 4096 with
``[SCI,1]`` (chip WFC2) in rows 1 to 2048 and ``[SCI,2]`` (chip WFC1) above it;
for WFC3/IR it is the detector itself.

**When it runs again.** ``CFP_MASK`` records the hash of the region file
(``reg <sha> px=<n>``). The step is skipped while that matches what is on
disk, and redone when the region file is added, edited or removed (the stamp
then reads ``no .reg file``); no reset is needed. An exposure nobody drew on
is not touched at all. Rewriting a canonical file makes its working copy
stale, so the outlier pass and the tiles that hold the exposure follow.
"""

import hashlib
import os
import shutil
import warnings

import numpy as np
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.hst.exposure import MASK_EXT, open_exposure

NO_FILE = 'no .reg file'


def reg_hash(reg_file):
    with open(reg_file, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


def is_current(exposure_file, reg_file):
    """Whether the file's ``CFMASK`` matches the region file on disk.

    An exposure that has no region file and never had one is current as it
    is, with no stamp: the step does not rewrite a file to say that nothing
    was drawn on it.
    """
    value = cfp.step_value(exposure_file, 'CFP_MASK', keyset=cfp.HST)
    if reg_file is None or not os.path.exists(reg_file):
        return value is None or value == NO_FILE
    return value is not None and str(value).startswith(
        f'reg {reg_hash(reg_file)} ')


def rasterize(reg_file, chips):
    """Masks of the regions in ``reg_file`` for the chips of one exposure:
    ``{extver: uint8 array, 1 inside a region}``.

    A region that cannot be projected, or that falls off a chip, is skipped
    with a warning for that chip.
    """
    from regions import PixelRegion, Regions

    regs = Regions.read(reg_file)
    name = os.path.basename(reg_file)
    width = max(c.shape[1] for c in chips)
    height = sum(c.shape[0] for c in chips)
    canvas = np.zeros((height, width), dtype=bool)
    masks = {c.extver: np.zeros(c.shape, dtype=bool) for c in chips}
    for reg in regs:
        if isinstance(reg, PixelRegion):
            try:
                arr = reg.to_mask(mode='center').to_image(canvas.shape)
            except (ValueError, TypeError) as e:
                log(f'Warning: skipping a region of {name}: {e}')
                continue
            if arr is not None:
                canvas |= arr.astype(bool)
            continue
        for c in chips:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    arr = reg.to_pixel(c._wcs).to_mask(mode='center').to_image(c.shape)
            except Exception as e:  # a region far off the chip may not invert
                log(f'Warning: a region of {name} could not be placed on '
                    f'[SCI,{c.extver}]: {e}')
                continue
            if arr is not None:
                masks[c.extver] |= arr.astype(bool)
    row = 0
    for c in chips:                       # the preview stacks them in this order
        ny, nx = c.shape
        masks[c.extver] |= canvas[row:row + ny, :nx]
        row += ny
    return {v: m.astype(np.uint8) for v, m in masks.items()}


def _write(exposure_file, masks, stamp):
    """Replace the ``CFMASK`` extensions of the file (none when ``masks`` is
    empty) and stamp ``CFP_MASK``. All or nothing."""
    tmp = f'{exposure_file}.mask.tmp'
    try:
        shutil.copyfile(exposure_file, tmp)
        with fits.open(tmp, mode='update', memmap=False) as hdul:
            for i in reversed(range(len(hdul))):
                if hdul[i].name == MASK_EXT:
                    del hdul[i]
            for extver, mask in sorted(masks.items()):
                hdu = fits.ImageHDU(data=mask, name=MASK_EXT, ver=extver)
                hdu.header['COMMENT'] = (
                    'campfire: 1 inside a hand-drawn region (apply_masks)')
                hdul.append(hdu)
            for key, val in cfp.format(keyset=cfp.HST, CFP_MASK=stamp).items():
                hdul[0].header[key] = val
        os.replace(tmp, exposure_file)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def apply_masks_step(exposure_file, reg_file, overwrite=False):
    """Bring the ``CFMASK`` extensions of one canonical exposure in line with
    its region file. Returns the number of masked pixels, or ``None`` when the
    file is already up to date and ``overwrite`` is not set."""
    rootname = os.path.basename(exposure_file).split('_')[0]
    if not overwrite and is_current(exposure_file, reg_file):
        return None
    if reg_file is None or not os.path.exists(reg_file):
        had = cfp.step_value(exposure_file, 'CFP_MASK', keyset=cfp.HST)
        if had is not None and had != NO_FILE:
            _write(exposure_file, {}, NO_FILE)
            log(f'apply_masks: {rootname} region file is gone; mask removed')
        return 0
    chips = open_exposure(exposure_file)
    masks = rasterize(reg_file, chips)
    npix = int(sum(int(m.sum()) for m in masks.values()))
    _write(exposure_file, masks, f'reg {reg_hash(reg_file)} px={npix}')
    log(f'apply_masks: {rootname} {npix} pixels masked from '
        f'{os.path.basename(reg_file)}')
    return npix
