"""persist: WFC3/IR persistence, masked in DQ.

An IR pixel that was filled close to or beyond full well keeps releasing
charge for hours, so a bright star of an earlier exposure shows up as a faint
ghost in later ones: of the same visit ("internal") or of whatever the detector
looked at before, in any program ("external"). Only the first kind stays put
on the detector while the sky moves with the dithers, and neither is reliably
removed by the outlier pass.

Two sources, in this order for each exposure:

1. **The archive's prediction**, when ``cfpipe download`` has put it on disk
   (``common/wfc3_persist.py``): STScI's model of what every pixel still
   releases, from every WFC3/IR exposure of the hours before, in any program
   or filter. ``<rootname>_persist.fits`` is a copy of the calibrated exposure
   whose first extension holds the prediction in electrons per second on the
   exposure's own pixels (the software that writes it:
   https://github.com/kslong/Persistence, ``subtract_persist.py``). Pixels
   predicted above ``threshold`` are flagged. The handbook's advice is to mask
   rather than subtract (the model removes about 90% of the signal and can
   over- or under-subtract; WFC3 Data Handbook 8.3), with 0.01 e-/s as its
   example level.

2. **The in-field rule**, when there is no prediction: NIRCam's rule
   (``nircam/steps/persistence.py``, ``snowblind``). A pixel saturated in one
   exposure is flagged in the exposures that start within ``time`` seconds
   after it. Saturated means the exposure collected at least ``fluence``
   electrons there (rate times exposure time) or calwf3 flagged it as full-well
   (DQ 256). The earlier exposures are those of the field on disk, in every
   filter, since the detector is one; NIRCam's step looks within one filter.
   This sees nothing of what the detector observed before the field's own
   exposures, and nothing of a filter that is not reduced, which is why the
   prediction is preferred.

Flagged pixels get DQ bit 16384, which calwf3 does not use and the adapter
(``hst/exposure.py``) does not tolerate, so they are left out of the outlier
pass and the mosaic. ``SCI`` is not touched. ``CFP_PERS`` says which source was
used, the number of pixels, and for the in-field rule which earlier exposures
counted; the step runs again for an exposure when its prediction arrives or
when the set of earlier exposures changes.
"""

import hashlib
import os
import shutil

import numpy as np
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.common.wfc3_persist import product_path
from campfire_pipeline.imaging.outlier import warn_if_suspect

PERSIST_BIT = 16384
SATURATED_BIT = 256
ARCHIVE = 'archive model'
IN_FIELD = 'in-field rule'


def rootname_of(path):
    return os.path.basename(path).split('_')[0]


def predicted(product_file, shape):
    """The predicted persistence image (e-/s) of one exposure, checked against
    the exposure's own shape."""
    with fits.open(product_file, memmap=False) as hdul:
        if len(hdul) < 2 or hdul[1].data is None:
            raise ValueError(f'{os.path.basename(product_file)} has no image in '
                             f'its first extension')
        data = np.asarray(hdul[1].data, dtype=np.float64)
    if data.shape != tuple(shape):
        raise ValueError(
            f'{os.path.basename(product_file)} is {data.shape}, the exposure '
            f'{tuple(shape)}: not a prediction for this exposure')
    return data


def saturated(exposure_file, fluence):
    """Where an exposure filled its pixels: at least ``fluence`` electrons
    collected, or flagged full-well by calwf3."""
    with fits.open(exposure_file, memmap=False) as hdul:
        sci = np.asarray(hdul['SCI', 1].data, dtype=np.float64)
        dq = np.asarray(hdul['DQ', 1].data)
        unit = str(hdul['SCI', 1].header.get('BUNIT', 'ELECTRONS/S')).upper()
        exptime = float(hdul[0].header['EXPTIME'])
    electrons = sci * exptime if unit.endswith('/S') else sci
    with np.errstate(invalid='ignore'):
        return (electrons >= fluence) | ((dq & SATURATED_BIT) != 0)


def earlier_exposures(exposure_file, candidates, window):
    """The candidates that started before ``exposure_file`` and no more than
    ``window`` seconds before it (start to start, as NIRCam's rule counts)."""
    start = float(fits.getval(exposure_file, 'EXPSTART', ext=0))
    found = []
    for other in candidates:
        if os.path.abspath(other) == os.path.abspath(exposure_file):
            continue
        dt = (start - float(fits.getval(other, 'EXPSTART', ext=0))) * 86400.0
        if 0.0 < dt <= window:
            found.append(other)
    return sorted(found)


def _stimuli_tag(earlier):
    names = ','.join(sorted(rootname_of(f) for f in earlier))
    return hashlib.sha256(names.encode()).hexdigest()[:8]


def is_current(exposure_file, product_file, earlier):
    """Whether the exposure's mask is the one this run would make."""
    value = cfp.step_value(exposure_file, 'CFP_PERS', keyset=cfp.HST)
    if value is None:
        return False
    value = str(value)
    has_prediction = product_file is not None and os.path.exists(product_file)
    if value.startswith(ARCHIVE):
        return True
    # in-field: redo when the prediction has arrived, or the earlier
    # exposures on disk are not the ones the mask was made from
    return not has_prediction and value.endswith(f'[{_stimuli_tag(earlier)}]')


def _write(exposure_file, flag, stamp):
    """Set the persistence bit where ``flag`` (clearing it elsewhere, so a
    second run replaces the first) and stamp ``CFP_PERS``. All or nothing.
    Returns the fraction of previously unflagged pixels now flagged."""
    tmp = f'{exposure_file}.persist.tmp'
    try:
        shutil.copyfile(exposure_file, tmp)
        with fits.open(tmp, mode='update', memmap=False) as hdul:
            dq_hdu = hdul['DQ', 1]
            dq = np.asarray(dq_hdu.data)
            dq = dq & ~np.asarray(PERSIST_BIT, dtype=dq.dtype)
            clean = dq == 0
            dq[flag] |= np.asarray(PERSIST_BIT, dtype=dq.dtype)
            dq_hdu.data = dq
            for key, val in cfp.format(keyset=cfp.HST, CFP_PERS=stamp).items():
                hdul[0].header[key] = val
        os.replace(tmp, exposure_file)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return float((flag & clean).sum()) / max(int(clean.sum()), 1)


def persist_step(exposure_file, product_file, earlier=(), step_config=None,
                 overwrite=False):
    """Flag the persistence of one canonical WFC3/IR exposure.

    ``product_file`` is the archive's prediction (used when it exists);
    ``earlier`` the exposures the in-field rule looks at otherwise. Returns
    ``(source, pixels flagged)``, or ``None`` when the mask on the file is
    current and ``overwrite`` is not set.
    """
    cfg = step_config or {}
    name = os.path.basename(exposure_file)
    root = rootname_of(exposure_file)
    if not overwrite and is_current(exposure_file, product_file, earlier):
        return None
    shape = fits.getdata(exposure_file, ('DQ', 1)).shape

    if product_file is not None and os.path.exists(product_file):
        threshold = float(cfg.get('threshold', 0.01))
        model = predicted(product_file, shape)
        flag = np.isfinite(model) & (model > threshold)
        n = int(flag.sum())
        peak = float(np.nanmax(model)) if np.isfinite(model).any() else 0.0
        stamp = (f'{ARCHIVE} >{threshold:g} e-/s px={n} '
                 f'({100.0 * n / flag.size:.2f}%) max={peak:.3f}')
        fraction = _write(exposure_file, flag, stamp)
        log(f'persist: {root} {n} pixels ({100.0 * n / flag.size:.2f}%) '
            f'predicted above {threshold:g} e-/s, largest {peak:.3f} e-/s')
        source = ARCHIVE
    else:
        fluence = float(cfg.get('fluence', 70000.0))
        flag = np.zeros(shape, dtype=bool)
        for other in earlier:
            flag |= saturated(other, fluence)
        n = int(flag.sum())
        stamp = (f'{IN_FIELD} n={len(earlier)} px={n} '
                 f'[{_stimuli_tag(earlier)}]')
        fraction = _write(exposure_file, flag, stamp)
        log(f'persist: {root} no archive prediction on disk; in-field rule: '
            f'{n} pixels saturated in {len(earlier)} earlier exposure(s)')
        source = IN_FIELD
    warn_if_suspect('persist', name, fraction, log)
    return source, n


def persist_exposures(exposure_files, field_exposures, raw_root,
                      step_config=None, overwrite=False):
    """The step over a set of canonical exposures.

    ``field_exposures`` are the calibrated WFC3/IR exposures of the field in
    every filter (the in-field rule's earlier exposures come from them).
    Returns ``{rootname: source}`` for the exposures whose mask was written.
    """
    cfg = step_config or {}
    window = float(cfg.get('time', 2500.0))
    done = {}
    for f in exposure_files:
        proposal = fits.getval(f, 'PROPOSID', ext=0)
        product = product_path(raw_root, proposal, rootname_of(f))
        earlier = earlier_exposures(f, field_exposures, window)
        result = persist_step(f, product, earlier, step_config=cfg,
                              overwrite=overwrite)
        if result is not None:
            done[rootname_of(f)] = result[0]
    return done
