"""
apply_masks: record user region-file masks on the canonical exposure.

First step of the combine phase. Reads ``.reg`` files from
``mask_dir/<filter>/<rootname>.reg``, rasterizes each region to a pixel mask
using the exposure WCS, and writes the union as a ``CFMASK`` extension on the
canonical file. The canonical's SCI and DQ are left untouched — the mask is a
per-exposure property recorded *alongside* the science data, not baked into it.

The mask's actual effect on the mosaic (exclusion from outlier detection and
resample, via ``good_bits='~DO_NOT_USE'``) happens later, on the disposable
combine working copy, where ``Field.materialize_work`` fuses CFMASK into the
working DQ as ``DO_NOT_USE``. Keeping the canonical DQ mask-free means (a) the
canonical stays byte-identical to the process output apart from the added
CFMASK extension — so it deploys and re-reviews cleanly — and (b) mask edits
are non-destructive and fully reversible, since CFMASK is rebuilt from scratch
every run.

The skip decision follows the mask *input*, not just the stamp. ``CFP_MASK``
records which ``.reg`` it was built from (``<timestamp> reg=<hash>``, a hash of
the file's region lines, comments excluded), and the step re-runs whenever the
file on disk no longer matches: a mask drawn after the first combine, an edited
mask, or a removed one (CFMASK is then dropped). Issue #478: the step used to
skip on the stamp's presence alone, so a mask pulled after the first combine was
silently left out of the mosaic. Stamps written before the hash existed (a bare
timestamp) are judged by the ``.reg`` mtime against that timestamp.

If there is no ``.reg`` file for an exposure, the step still stamps
``CFP_MASK = 'no .reg file'`` so the status command can distinguish
"ran-but-n/a" from "not yet run". CFMASK is not created in this case.

Rewriting the canonical bumps its mtime, so ``Field.materialize_work`` refreshes
the working copy and bad_pixel / outlier re-derive their flags on it. A changed
mask also clears ``CFP_SPKE`` (the spike mask counts CFMASK pixels as
uncovered), so spike_mask recomputes the filter.
"""

import hashlib
import os
import warnings
from datetime import datetime

import numpy as np
from astropy.io import fits

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp


# CFP_MASK value for an exposure with no .reg file.
NO_REG = 'no .reg file'
_REG_TOKEN = 'reg='


def reg_file_for(exposure_file, mask_dir):
    """The ``.reg`` path ``apply_masks`` reads for a canonical exposure."""
    rootname = os.path.basename(exposure_file).removesuffix('.fits')
    filtname = os.path.basename(os.path.dirname(exposure_file))
    return os.path.join(mask_dir, filtname, f'{rootname}.reg')


def reg_hash(reg_file):
    """sha256[:12] of a ``.reg`` file's region lines, or None if it is absent.

    Comment and blank lines are left out, so the generation header
    ``campfire config pull --masks`` writes does not count as a mask change.
    """
    try:
        with open(reg_file, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    except FileNotFoundError:
        return None
    lines = (ln.strip() for ln in text.splitlines())
    body = '\n'.join(ln for ln in lines if ln and not ln.startswith('#'))
    return hashlib.sha256(body.encode()).hexdigest()[:12]


def stamp_reg_hash(stamp):
    """The ``reg=`` hash recorded in a CFP_MASK value, or None."""
    for tok in str(stamp or '').split():
        if tok.startswith(_REG_TOKEN):
            return tok[len(_REG_TOKEN):]
    return None


def mask_stamp(current_hash):
    """The CFP_MASK value for a mask built from a ``.reg`` with this hash."""
    if current_hash is None:
        return NO_REG
    return f'{cfp.iso_now()} {_REG_TOKEN}{current_hash}'


def mask_staleness(stamp, reg_file):
    """Why the exposure's CFMASK no longer matches its ``.reg``, or None.

    ``stamp`` is the CFP_MASK value (None when the step never ran, reported as
    ``'not yet run'``). A stamp from before the ``reg=`` hash (a bare ISO
    timestamp) is judged by the file's mtime against it.
    """
    if stamp is None:
        return 'not yet run'
    stamp = str(stamp).strip()
    current = reg_hash(reg_file)
    if stamp == NO_REG:
        return None if current is None else 'a .reg file appeared after the last run'
    if current is None:
        return 'its .reg file was removed'
    recorded = stamp_reg_hash(stamp)
    if recorded is not None:
        return None if recorded == current else 'its .reg file changed'
    # Legacy stamp: a bare timestamp, written in local time by cfp.iso_now().
    try:
        stamped_at = datetime.fromisoformat(stamp.split()[0])
    except (ValueError, IndexError):
        return 'its CFP_MASK stamp is unreadable'
    if datetime.fromtimestamp(os.path.getmtime(reg_file)) > stamped_at:
        return 'its .reg file is newer than the stamp'
    return None


def _drop_from_model(model, extnames=(), header_keys=()):
    """Drop non-schema extensions / primary cards from an open datamodel.

    stdatamodels carries them through ``model.extra_fits``; removing them there
    (not from the written file) keeps the embedded ASDF tree consistent, the
    same rule as ``wcs_shift._scrub_alignment_state``.
    """
    extra = getattr(model, 'extra_fits', None)
    if extra is None:
        return
    for name in extnames:
        if hasattr(extra, name):
            delattr(extra, name)
    prim = getattr(extra, 'PRIMARY', None)
    header = getattr(prim, 'header', None) if prim is not None else None
    if header is not None and header_keys:
        prim.header = [list(card) for card in header
                       if card[0] not in header_keys]


def _rasterize_regions(regs, wcs, shape, reg_file=''):
    """Union a list of DS9 regions into a 0/1 ``uint8`` mask of ``shape``.

    Regions already in image/pixel coordinates (``PixelRegion`` — what
    ``campfire deploy pull-masks`` writes, since the web canvas is
    pixel-native) are rasterized directly; only sky regions (legacy
    FK5/ICRS hand-drawn masks) are projected through the exposure ``wcs``
    via ``to_pixel``. Calling ``to_pixel`` on a region that is *already*
    pixel raises ``AttributeError`` — the crash this guards against.

    A region that fails to project, or whose footprint falls entirely off
    the frame (``to_image`` → ``None``), is skipped with a warning rather
    than aborting the whole exposure.
    """
    from regions import PixelRegion

    cfmask = np.zeros(shape, np.uint8)
    for reg in regs:
        try:
            reg_pix = reg if isinstance(reg, PixelRegion) else reg.to_pixel(wcs)
            mask_arr = reg_pix.to_mask(mode='center').to_image(shape)
        except (ValueError, TypeError, AttributeError) as e:
            log(f"Warning: skipping region in {reg_file}: {e}")
            continue
        if mask_arr is None:
            # Region footprint doesn't overlap the frame — nothing to mask.
            continue
        cfmask |= mask_arr.astype(bool).astype(np.uint8)
    return cfmask


def _exposure_fits_wcs(exposure_file):
    """The FITS (TAN-SIP) WCS of a canonical exposure, from its SCI header.

    Only sky regions are projected through it; portal masks are pixel regions.
    ``DataModel.get_fits_wcs()``, used here before, was removed in
    stdatamodels 6 (jwst 3.0), which crashed every exposure with a mask file.
    """
    from astropy.wcs import WCS

    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        return WCS(fits.getheader(exposure_file, 'SCI'))


def apply_masks_step(exposure_file, field, step_config, overwrite=False,
                     status=None):
    """Apply region-file masks to a single canonical exposure.

    Parameters
    ----------
    exposure_file : str
    field : Field
    step_config : dict
        ``[nircam.apply_mask]`` (legacy ``[nircam.stage2.apply_mask]``). No
        tunable keys — the mask is recorded as a CFMASK extension and honored
        via DO_NOT_USE on the combine working copy (``Field.materialize_work``).
    overwrite : bool
    status : StepStatus, optional
        Pre-scanned CFP_* status cache.
    """
    rootname = os.path.basename(exposure_file).removesuffix('.fits')
    reg_file = reg_file_for(exposure_file, field.mask_dir)
    # The skip follows the .reg input, not the stamp's presence (#478), so the
    # status cache (presence only) is not consulted here.
    stamp = cfp.step_value(exposure_file, 'CFP_MASK')
    reason = mask_staleness(stamp, reg_file)

    if not overwrite and reason is None:
        log(f"Skipping apply_masks on {rootname}: CFP_MASK matches its .reg input")
        return
    if reason not in (None, 'not yet run'):
        log(f"WARNING: re-applying masks on {rootname}: {reason} "
            f"(CFP_MASK was {stamp!r})")

    from jwst.datamodels import ImageModel

    current = reg_hash(reg_file)
    if current is None:
        log(f"No mask file for {rootname}; stamping CFP_MASK={NO_REG!r}")
        with ImageModel(exposure_file) as m:
            # A removed .reg takes its CFMASK, and the spike mask computed
            # around it, with it.
            had_mask = stamp is not None and str(stamp).strip() != NO_REG
            _drop_from_model(m, extnames=('CFMASK',),
                             header_keys=('CFP_SPKE',) if had_mask else ())
            atomic_save(
                m, exposure_file,
                header_updates=cfp.format(CFP_MASK=NO_REG),
            )
        return

    log(f"Applying masks from {os.path.basename(reg_file)} to {rootname}")

    from regions import Regions

    with ImageModel(exposure_file) as model:
        wcs = _exposure_fits_wcs(exposure_file)
        shape = model.data.shape

        # CFMASK is a plain 0/1 union of the user regions, rebuilt from scratch
        # each run. It records *what* is masked; the DO_NOT_USE fuse (how the
        # mask is honored) happens on the combine working copy, so the canonical
        # SCI/DQ are never touched here.
        regs = Regions.read(reg_file)
        cfmask = _rasterize_regions(regs, wcs, shape, reg_file=reg_file)

        if stamp is not None and stamp_reg_hash(stamp) != current:
            # The mask changed: spike_mask counts CFMASK pixels as uncovered,
            # so it recomputes the filter once an exposure lacks CFP_SPKE.
            _drop_from_model(model, header_keys=('CFP_SPKE',))
        cfmask_hdu = fits.ImageHDU(cfmask, name='CFMASK')
        atomic_save(
            model, exposure_file,
            header_updates=cfp.format(CFP_MASK=mask_stamp(current)),
            extra_hdus=[cfmask_hdu],
        )
        log(f"Masks applied: {rootname}")
