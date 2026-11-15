"""crreject: cosmic-ray flags for the exposures of one visit and filter.

An ACS/WFC exposure is a single CCD read and carries every cosmic ray that hit
it (1.5 to 3 percent of pixels per 1000 s); HST visits often have only two to
four exposures per filter. This step uses the tool built for that regime,
AstroDrizzle's rejection sequence (``drizzlepac``):

    drizzle each exposure separately -> median with ``minmed`` -> blot the
    median back to each exposure -> flag pixels that exceed it (``driz_cr``)

and nothing else of AstroDrizzle: no final combination. ``minmed`` takes the
minimum instead of the median where only two or three exposures overlap and one
stands out, which is what makes two-exposure rejection work at all.

The exposures must share a WCS frame good to a small fraction of a pixel; within
one visit HST's guiding provides that. The whole sequence runs on copies in a
private directory. Only the resulting cosmic-ray bit (4096) is transferred to
the canonical files' DQ arrays, so AstroDrizzle's own sky estimate and header
edits never reach them. The flags are read from the per-chip mask files
``driz_cr`` writes: AstroDrizzle itself puts the bit into its inputs' DQ arrays
only during the final drizzle, which is not run here.

A step that reports zero flagged pixels on a real CCD exposure has failed; the
step raises in that case instead of stamping the files.

Default thresholds (``[<instrument>.crreject]``): ACS/WFC uses the values of the legacy surveys and the
AstroDrizzle defaults (SNR ``3.5 3.0``, scale ``1.2 0.7``); WFC3/IR uses looser
ones (SNR ``8.0 5.0``, scale ``2.5 0.7``), because its up-the-ramp fit has
already removed most hits and its undersampled stellar cores are easily
mistaken for cosmic rays.
"""

import os
import shutil
import tempfile

import numpy as np
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.config import get_hst_step_config
from campfire_pipeline.hst.exposure import open_exposure

CR_BIT = 4096


def _read_crmasks(work, rootname, extvers):
    """Cosmic-ray masks AstroDrizzle left in ``work`` for one exposure.

    ``driz_cr`` writes one ``*_sci<extver>_crmask.fits`` per chip, holding 1
    where the pixel passed and 0 where it was rejected. (It sets the DQ bit of
    the inputs only during the final drizzle, which this step does not run.)
    Returns ``{extver: bool array, True where a cosmic ray was found}``.
    """
    masks = {}
    for v in extvers:
        found = [n for n in os.listdir(work)
                 if n.startswith(rootname) and n.endswith(f'_sci{v}_crmask.fits')]
        if len(found) != 1:
            raise RuntimeError(
                f'crreject: expected one cosmic-ray mask for {rootname} [SCI,{v}] '
                f'in the AstroDrizzle work directory, found {found}')
        masks[v] = fits.getdata(os.path.join(work, found[0])) == 0
    return masks


def _transfer_flags(canonical, masks):
    """Set bit 4096 in ``canonical`` exactly where ``masks`` say so.

    Any 4096 bit already in the canonical file is cleared first, so a re-run
    replaces the flags instead of accumulating them. Returns the number of
    flagged pixels per chip.
    """
    tmp = f'{canonical}.cr.tmp'
    counts = []
    try:
        shutil.copyfile(canonical, tmp)
        with fits.open(tmp, mode='update', memmap=False) as dst:
            for v, new in masks.items():
                dq = np.asarray(dst['DQ', v].data)
                if new.shape != dq.shape:
                    raise RuntimeError(
                        f'crreject: mask shape {new.shape} does not match '
                        f'[DQ,{v}] {dq.shape} of {os.path.basename(canonical)}')
                bit = np.asarray(CR_BIT, dtype=dq.dtype)
                dst['DQ', v].data = (dq & ~bit) | (new.astype(dq.dtype) * bit)
                counts.append(int(new.sum()))
        os.replace(tmp, canonical)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return counts


def crreject_step(exposure_files, step_config=None, overwrite=False):
    """Flag cosmic rays in the exposures of one visit and filter.

    ``exposure_files`` are canonical files of one detector and filter that
    overlap on the sky. Returns ``{path: flagged pixel count}``, or ``None``
    when every file already carries ``CFP_CR`` and ``overwrite`` is not set.
    A single exposure cannot be treated this way and raises ``ValueError``.
    """
    files = sorted(exposure_files)
    if not overwrite and all(cfp.has_step(f, 'CFP_CR', keyset=cfp.HST) for f in files):
        log(f'Skipping crreject: CFP_CR already set on all {len(files)} exposures')
        return None
    if len(files) < 2:
        raise ValueError(
            'crreject needs at least two exposures; a single exposure has to be '
            'cleaned from its own pixels (not implemented yet)')

    chips = {f: open_exposure(f) for f in files}
    tokens = {c.instrument for cs in chips.values() for c in cs}
    filters = {c.filter for cs in chips.values() for c in cs}
    if len(tokens) != 1 or len(filters) != 1:
        raise ValueError(
            f'crreject takes one detector and filter at a time, got '
            f'{sorted(tokens)} / {sorted(filters)}')
    token = tokens.pop()
    first = chips[files[0]][0]
    params = get_hst_step_config('crreject', token, overrides=step_config)
    bits = first.detector.tolerated_bits

    from drizzlepac import astrodrizzle

    work = tempfile.mkdtemp(prefix='.crrej_', dir=os.path.dirname(files[0]))
    cwd = os.getcwd()
    result = {}
    try:
        names = []
        for f in files:
            shutil.copyfile(f, os.path.join(work, os.path.basename(f)))
            names.append(os.path.basename(f))
        os.chdir(work)
        astrodrizzle.AstroDrizzle(
            # clean=False: the per-chip cosmic-ray masks are the product we
            # want; the whole work directory is removed below.
            names, output='crrej', build=False, preserve=False, clean=False,
            context=False, num_cores=1, updatewcs=False,
            resetbits=CR_BIT, crbit=CR_BIT,
            static=True, skysub=True, skymethod='localmin',
            driz_separate=True, driz_sep_bits=bits,
            median=True, combine_type=params.get('combine_type', 'minmed'),
            combine_nsigma=params.get('combine_nsigma', '4 3'),
            combine_grow=int(params.get('combine_grow', 1)),
            blot=True, driz_cr=True, driz_cr_corr=False,
            driz_cr_snr=params['driz_cr_snr'],
            driz_cr_scale=params['driz_cr_scale'],
            driz_cr_grow=int(params.get('driz_cr_grow', 1)),
            driz_cr_ctegrow=int(params.get('driz_cr_ctegrow', 0)),
            driz_combine=False,
        )
        os.chdir(cwd)

        stamp_params = f"snr={params['driz_cr_snr']} scale={params['driz_cr_scale']}"
        all_masks = {
            f: _read_crmasks(work, chips[f][0].rootname,
                             [c.extver for c in chips[f]])
            for f in files
        }
        # An ACS/WFC exposure of any length has cosmic rays. Finding none means
        # the rejection did not run as intended; say so before touching a file.
        if token == 'acswfc':
            empty = [os.path.basename(f) for f, m in all_masks.items()
                     if not any(a.any() for a in m.values())]
            if empty:
                raise RuntimeError(
                    f'crreject: no cosmic rays flagged in {empty}; refusing to '
                    f'stamp CFP_CR on a result that cannot be right')
        for f in files:
            counts = _transfer_flags(f, all_masks[f])
            npix = sum(c.shape[0] * c.shape[1] for c in chips[f])
            frac = sum(counts) / npix
            with fits.open(f, mode='update', memmap=False) as hdul:
                value = f'adz n={len(files)} {stamp_params} frac={frac:.4f}'
                for key, val in cfp.format(keyset=cfp.HST, CFP_CR=value).items():
                    hdul[0].header[key] = val
            result[f] = sum(counts)
            log(f'crreject: {os.path.basename(f)} flagged {sum(counts)} px '
                f'({100 * frac:.2f}%)')
    finally:
        os.chdir(cwd)
        shutil.rmtree(work, ignore_errors=True)
    return result
