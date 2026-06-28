"""
Canonical NIRSpec spectrum-exposure file primitives (issue #212).

One canonical ``MultiSlitModel`` FITS per ``(exposure, detector, source)``,
mutated in place across stage2->3 and named by the bare product rootname (no
``_cal``/``_s2d`` suffix), mirroring the NIRCam canonical-exposure model
(``common/io.atomic_save``). It replaces the old four-file quartet
(``_cal`` / ``_cal_bkgsub`` / ``_s2d`` / ``_s2d_bkgsub``).

The live per-slit ``SCI``/``ERR``/``DQ``/``VAR_*`` arrays hold the *current*
reduction state — the calibrated frame after stage2a, the background-subtracted
frame after stage2b. Two classes of extra, non-schema HDU ride along:

* ``PRE_BKGSUB_{SCI,ERR,DQ,VAR_RNOISE,VAR_POISSON}`` — the pre-subtraction
  (cal-state) slit arrays, so stage2b's in-place leapfrog subtraction is
  reversible via :func:`restore_pre_bkgsub`. We stash the *full arrays* (not a
  single additive background a la the ``_rate`` ``CFBKG`` trick) because stage2b
  inverts/reapplies pathloss and pads/unpads to a common nod region, so the
  revert state is not a simple additive offset (design §3 PR-3).
* ``S2D_{SCI,DQ,VAR_RNOISE}`` / ``S2D_BKGSUB_{...}`` — the rectified (resampled)
  visualization views, cached for ``plots.py`` and ``stuck_shutters.py`` (was
  the standalone ``_s2d`` / ``_s2d_bkgsub`` files).

**Hard constraint** (same as the NIRCam canonical model, and as the ``_rate``
bkgsub precedent in ``masks.py``): ``DataModel.save()`` snapshots asdf refs to
whatever extensions exist at save time and drops non-schema HDUs. So every
custom HDU is written via a post-save astropy pass and read back the same way.
The PR-3 pre-implementation gate proved ``Spec3Pipeline`` reads the live slit
SCI and ignores these HDUs (byte-identical ``_spec``/``_x1d``/``_s2d``).
"""

import os

import numpy as np
from astropy.io import fits

# Per-slit data extensions carried into the revert (PRE_BKGSUB_*) HDUs, in
# FITS-extension order. (VAR_FLAT is intentionally omitted: stage2b's bkgsub
# does not modify it, so it never needs reverting.)
SLIT_ARRAY_EXTS = ('SCI', 'ERR', 'DQ', 'VAR_RNOISE', 'VAR_POISSON')

PRE_BKGSUB_PREFIX = 'PRE_BKGSUB'
S2D_PREFIX = 'S2D'
S2D_BKGSUB_PREFIX = 'S2D_BKGSUB'

# EXTNAME prefixes for the non-schema HDUs this module manages. These are the
# HDUs that must be preserved across a ``DataModel.save()`` (which drops them).
CUSTOM_HDU_PREFIXES = (PRE_BKGSUB_PREFIX, S2D_PREFIX)


def is_custom_hdu(hdu):
    """True if ``hdu`` is one of the canonical file's managed non-schema HDUs."""
    name = (hdu.name or '').upper()
    return any(name.startswith(p) for p in CUSTOM_HDU_PREFIXES)


def _tmp_path(path):
    """Sibling ``<base>.tmp<ext>`` (NOT ``<path>.tmp``): jwst DataModel.save()
    dispatches on the extension and rejects ``.tmp``. Matches common/io."""
    base, ext = os.path.splitext(path)
    return f'{base}.tmp{ext}' if ext else f'{path}.tmp'


def _remove_matching(hdulist, name, ver):
    """Delete every HDU in ``hdulist`` matching (EXTNAME, EXTVER) — replace step."""
    name = (name or '').upper()
    idx = [i for i, h in enumerate(hdulist)
           if (h.name or '').upper() == name and int(getattr(h, 'ver', 1) or 1) == int(ver)]
    for i in reversed(idx):
        del hdulist[i]


def read_slit_arrays(path, slit_extver=1, exts=SLIT_ARRAY_EXTS):
    """Return ``{ext: ndarray copy}`` for one slit's data extensions.

    Used to capture the pre-bkgsub (cal-state) arrays before stage2b overwrites
    the live slit. Missing extensions are skipped.
    """
    out = {}
    with fits.open(path, memmap=False) as hdul:
        for ext in exts:
            try:
                out[ext] = np.array(hdul[(ext, slit_extver)].data)
            except KeyError:
                pass
    return out


def make_prefixed_hdus(arrays, prefix, slit_extver=1):
    """Build ``ImageHDU``s named ``{prefix}_{ext}`` from an ``{ext: array}`` dict.

    Empty / None arrays are skipped — jwst's ResampleSpecStep leaves the slit
    DQ unallocated (shape ``(0,)``), and writing it as a real extension would
    desync it from the data array; absence matches the old ``DataModel.save()``
    behaviour (no extension for an empty array) so consumers fall back cleanly.
    """
    hdus = []
    for ext, data in arrays.items():
        if data is None:
            continue
        arr = np.asarray(data)
        if arr.size == 0:
            continue
        hdu = fits.ImageHDU(arr, name=f'{prefix}_{ext}')
        hdu.ver = int(slit_extver)
        hdus.append(hdu)
    return hdus


def save_canonical(model, path, extra_hdus=None, header_updates=None,
                   preserve_existing_custom=True):
    """Save a jwst ``MultiSlitModel`` as the canonical file, re-attaching the
    non-schema custom HDUs that ``DataModel.save()`` drops.

    Mirrors ``common/io.atomic_save`` but for the MultiSlit canonical:

      1. (optional) snapshot the existing file's custom-prefix HDUs
         (``PRE_BKGSUB_*`` / ``S2D_*``) so a re-save doesn't lose them;
      2. ``model.save(tmp)`` — writes schema HDUs only;
      3. reopen ``tmp`` with astropy, (re)append the preserved + new
         ``extra_hdus`` (replace-or-append by EXTNAME/EXTVER), apply primary
         ``header_updates``;
      4. ``os.replace(tmp, path)`` — atomic.

    ``extra_hdus`` win over preserved HDUs of the same name.
    """
    tmp = _tmp_path(path)

    preserved = []
    if preserve_existing_custom and os.path.exists(path):
        with fits.open(path, memmap=False) as old:
            preserved = [h.copy() for h in old if is_custom_hdu(h)]

    model.save(tmp)

    extra_hdus = list(extra_hdus or [])
    # Drop any preserved HDU that an incoming extra replaces (match by name+ver).
    incoming_keys = {((h.name or '').upper(), int(getattr(h, 'ver', 1) or 1))
                     for h in extra_hdus}
    preserved = [h for h in preserved
                 if ((h.name or '').upper(), int(getattr(h, 'ver', 1) or 1)) not in incoming_keys]

    with fits.open(tmp, mode='update', memmap=False) as hdul:
        for hdu in preserved + extra_hdus:
            _remove_matching(hdul, hdu.name, getattr(hdu, 'ver', 1) or 1)
            hdul.append(hdu)
        if header_updates:
            for k, v in header_updates.items():
                hdul[0].header[k] = v
        hdul.flush()
    os.replace(tmp, path)


def append_extras(path, extra_hdus=None, header_updates=None):
    """Attach custom HDUs and/or primary header cards to an existing canonical
    file in place, without re-saving the datamodel (atomic via sibling tmp).

    Used by the resample step (append ``S2D_*`` views) and the exclusion paths
    (stamp ``CFP_BKG`` only). Replace-or-append by EXTNAME/EXTVER.
    """
    tmp = _tmp_path(path)
    with fits.open(path, memmap=False) as hdul:
        out = fits.HDUList([h.copy() for h in hdul])
        for hdu in (extra_hdus or []):
            _remove_matching(out, hdu.name, getattr(hdu, 'ver', 1) or 1)
            out.append(hdu)
        if header_updates:
            for k, v in header_updates.items():
                out[0].header[k] = v
        out.writeto(tmp, overwrite=True)
    os.replace(tmp, path)


def has_pre_bkgsub(path):
    """True if the canonical file carries revert (``PRE_BKGSUB_*``) arrays."""
    with fits.open(path, memmap=False) as hdul:
        return any((h.name or '').upper().startswith(PRE_BKGSUB_PREFIX) for h in hdul)


def restore_pre_bkgsub(path, slit_extver=1):
    """Reverse stage2b's in-place subtraction on the canonical file.

    Copies each ``PRE_BKGSUB_{ext}`` array back into the live slit ``{ext}``
    extension, drops the ``PRE_BKGSUB_*`` and now-stale ``S2D_BKGSUB_*`` HDUs,
    and clears the ``CFP_BKG`` / ``CFP_S2D`` state cards — returning the file to
    its calibrated (pre-bkgsub) state. Atomic via sibling tmp.

    Raises
    ------
    RuntimeError
        If no ``PRE_BKGSUB_*`` arrays are present (file was never bkgsub'd in
        place, or predates the canonical cutover).
    """
    if not has_pre_bkgsub(path):
        raise RuntimeError(
            f"{path}: no PRE_BKGSUB_* revert arrays; cannot restore "
            f"(re-run stage2 from the calibrated frame)")
    tmp = _tmp_path(path)
    with fits.open(path, memmap=False) as hdul:
        out = fits.HDUList([h.copy() for h in hdul])
        for arr_ext in SLIT_ARRAY_EXTS:
            pre_name = f'{PRE_BKGSUB_PREFIX}_{arr_ext}'
            if pre_name in out:
                out[(arr_ext, slit_extver)].data = np.array(out[pre_name].data)
        # Drop the revert arrays and the stale bkgsub s2d view.
        for i in reversed(range(len(out))):
            name = (out[i].name or '').upper()
            if name.startswith(PRE_BKGSUB_PREFIX) or name.startswith(S2D_BKGSUB_PREFIX):
                del out[i]
        for card in ('CFP_BKG', 'CFP_S2D'):
            if card in out[0].header:
                del out[0].header[card]
        out.writeto(tmp, overwrite=True)
    os.replace(tmp, path)
