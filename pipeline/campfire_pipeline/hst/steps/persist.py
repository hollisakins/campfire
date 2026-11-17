"""persist: WFC3/IR persistence, masked from the archive's predictions.

An IR pixel that was filled close to or beyond full well keeps releasing
charge for hours, so a bright star of an earlier exposure shows up as a faint
ghost in later ones: of the same visit ("internal") or of whatever the detector
looked at before, in any program ("external"). Only the second kind moves with
the dithers, so the visit's own outlier pass does not reliably remove either.

**Where the prediction comes from.** STScI runs a persistence model on every
WFC3/IR exposure and serves the result per visit (the "WFC3 Persistence
Project", https://archive.stsci.edu/prepds/persist/; WFC3 Data Handbook
section 8.2). For each exposure the model takes every WFC3/IR exposure of the
16 hours before it, whatever the program or filter, predicts from the fluence
each pixel received and the time since what it still releases (a power law in
time whose amplitude and slope depend on the fluence, the "A-gamma" model),
and keeps the largest prediction per pixel. ``<rootname>_persist.fits`` is a
copy of the calibrated exposure whose first extension holds that prediction,
in electrons per second on the exposure's own pixels (the software that writes
it: https://github.com/kslong/Persistence, ``subtract_persist.py``).

This is why the prediction is used rather than NIRCam's rule (a pixel
saturated in one exposure is masked in the same detector's exposures of the
next 2500 s, ``nircam/steps/persistence.py``): that rule sees only the
exposures being reduced, and an HST field is often reduced in one filter of a
visit that observed in three, after another program's target.

**What the step does.** Pixels whose predicted persistence exceeds
``threshold`` (electrons per second) get DQ bit 16384, which calwf3 does not
use and the adapter (``hst/exposure.py``) does not tolerate, so they are left
out of the outlier pass and the mosaic. The handbook's own advice is to mask
rather than subtract (the model removes about 90% of the signal and can over-
or under-subtract; section 8.3), with 0.01 e-/s as its example level.
``SCI`` is not touched. ``CFP_PERS`` records the threshold, the number of
pixels and the largest predicted value.

**Getting the files.** They are not in MAST's API. A visit is one archive,
``<base>/<proposal>/Visit<VV>/<proposal>.Visit<VV>.tar.gz``, holding the
products of every exposure of the visit; the step downloads it when a
prediction it needs is not on disk yet and keeps the ``_persist.fits`` files
under ``raw/<instrument>/<proposal>/persist/``.

**When there is no prediction** (the archive does not answer, or has not
processed the exposure) the exposure is left as it is and gets no stamp, so
the next run tries again. Nothing is held back: the run says which exposures
are not masked, here and again before their mosaic is drizzled.
"""

import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request

import numpy as np
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.imaging.outlier import warn_if_suspect

PERSIST_BIT = 16384
SUFFIX = '_persist.fits'


class PredictionUnavailable(Exception):
    """The archive did not hand over a visit's persistence products."""


def rootname_of(path):
    return os.path.basename(path).split('_')[0]


def visit_of(rootname):
    """The visit of an HST rootname ``ipppssoot``: the two characters ``ss``,
    as the archive writes them (upper case)."""
    return rootname[4:6].upper()


def persist_dir(raw_root, proposal):
    return os.path.join(raw_root, str(proposal), 'persist')


def product_path(raw_root, proposal, rootname):
    return os.path.join(persist_dir(raw_root, proposal), rootname + SUFFIX)


def visit_url(base_url, proposal, visit):
    return (f'{base_url.rstrip("/")}/{proposal}/Visit{visit}/'
            f'{proposal}.Visit{visit}.tar.gz')


def fetch_visit(proposal, visit, out_dir, *, base_url, timeout=600):
    """Download one visit's persistence archive and keep its
    ``*_persist.fits`` members in ``out_dir``. Returns the rootnames kept.

    Raises :class:`PredictionUnavailable` with the reason when the archive
    cannot be fetched or read.
    """
    url = visit_url(base_url, proposal, visit)
    os.makedirs(out_dir, exist_ok=True)
    request = urllib.request.Request(
        url, headers={'User-Agent': 'campfire-pipeline'})
    kept = []
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        tar_path = os.path.join(tmp, 'visit.tar.gz')
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response, \
                    open(tar_path, 'wb') as out:
                shutil.copyfileobj(response, out)
        except urllib.error.HTTPError as e:
            raise PredictionUnavailable(
                f'{url} answered HTTP {e.code} ({e.reason})') from e
        except (urllib.error.URLError, OSError) as e:
            raise PredictionUnavailable(f'{url} could not be fetched: {e}') from e
        try:
            with tarfile.open(tar_path, 'r:*') as tar:
                for member in tar:
                    # Only the name's last component is used: nothing a
                    # member's path says decides where its file is written.
                    name = os.path.basename(member.name)
                    if not member.isfile() or not name.endswith(SUFFIX):
                        continue
                    part = os.path.join(tmp, name)
                    with tar.extractfile(member) as src, open(part, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                    os.replace(part, os.path.join(out_dir, name))
                    kept.append(name[:-len(SUFFIX)])
        except (tarfile.TarError, EOFError) as e:
            raise PredictionUnavailable(f'{url} is not a readable archive: {e}') from e
    return sorted(kept)


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


def persist_step(exposure_file, product_file, step_config=None, overwrite=False):
    """Flag the predicted persistence of one canonical WFC3/IR exposure.

    Returns the number of pixels flagged; ``None`` when the exposure is
    already stamped and ``overwrite`` is not set; ``-1`` when there is no
    prediction on disk (nothing is written then).
    """
    cfg = step_config or {}
    threshold = float(cfg.get('threshold', 0.01))
    name = os.path.basename(exposure_file)
    if not overwrite and cfp.has_step(exposure_file, 'CFP_PERS', keyset=cfp.HST):
        return None
    if product_file is None or not os.path.exists(product_file):
        return -1

    tmp = f'{exposure_file}.persist.tmp'
    try:
        shutil.copyfile(exposure_file, tmp)
        with fits.open(tmp, mode='update', memmap=False) as hdul:
            dq_hdu = hdul['DQ', 1]
            model = predicted(product_file, dq_hdu.data.shape)
            flag = np.isfinite(model) & (model > threshold)
            dq = np.asarray(dq_hdu.data)
            # Start from a DQ without the bit, so a second run (another
            # threshold, a newer prediction) replaces the first.
            dq = dq & ~np.asarray(PERSIST_BIT, dtype=dq.dtype)
            usable_before = int((dq == 0).sum())
            dq[flag] |= np.asarray(PERSIST_BIT, dtype=dq.dtype)
            dq_hdu.data = dq
            n = int(flag.sum())
            peak = float(np.nanmax(model)) if np.isfinite(model).any() else 0.0
            stamp = (f'archive model >{threshold:g} e-/s px={n} '
                     f'({100.0 * n / flag.size:.2f}%) max={peak:.3f}')
            for key, val in cfp.format(keyset=cfp.HST, CFP_PERS=stamp).items():
                hdul[0].header[key] = val
        os.replace(tmp, exposure_file)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    log(f'persist: {rootname_of(exposure_file)} {n} pixels '
        f'({100.0 * n / flag.size:.2f}%) above {threshold:g} e-/s, largest '
        f'prediction {peak:.3f} e-/s')
    warn_if_suspect('persist', name, n / max(usable_before, 1), log)
    return n


def persist_exposures(exposure_files, raw_root, step_config=None, overwrite=False):
    """The step over a set of canonical exposures: fetch the visits whose
    predictions are not on disk, then flag each exposure.

    Returns ``{rootname: reason}`` for the exposures left without a mask.
    """
    cfg = step_config or {}
    base_url = cfg.get('archive_url', 'https://archive.stsci.edu/pub/wfc3_persist')
    timeout = float(cfg.get('timeout', 600))

    todo = [f for f in exposure_files
            if overwrite or not cfp.has_step(f, 'CFP_PERS', keyset=cfp.HST)]
    proposals = {f: fits.getval(f, 'PROPOSID', ext=0) for f in todo}

    reasons = {}
    visits = {}
    for f in todo:
        root = rootname_of(f)
        if not os.path.exists(product_path(raw_root, proposals[f], root)):
            visits.setdefault((proposals[f], visit_of(root)), []).append(root)
    for (proposal, visit), roots in sorted(visits.items()):
        log(f'persist: fetching the predictions of program {proposal} visit '
            f'{visit} ({len(roots)} exposure(s) need one)')
        try:
            got = fetch_visit(proposal, visit, persist_dir(raw_root, proposal),
                              base_url=base_url, timeout=timeout)
        except PredictionUnavailable as e:
            for root in roots:
                reasons[root] = str(e)
            continue
        for root in roots:
            if root not in got:
                reasons[root] = (f'the archive of program {proposal} visit '
                                 f'{visit} has no prediction for it')

    for f in todo:
        root = rootname_of(f)
        if root in reasons:
            continue
        persist_step(f, product_path(raw_root, proposals[f], root),
                     step_config=cfg, overwrite=overwrite)
    return reasons
