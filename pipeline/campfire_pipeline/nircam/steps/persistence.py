"""
persistence: ``snowblind.PersistenceFlagStep`` across all exposures of a filter.

Per-filter ensemble step. Reads each canonical exposure file plus its
``<rootname>_jump.fits`` sidecar (left behind by detector1), runs snowblind
in memory, and atomically saves each mutated model back to its canonical
path with ``CFP_PERS`` stamped.

Runs *immediately after* detector1 — earlier than the legacy stage1 sequence.
This lets the 1/f striping step's source-mask construction see the persistence
DQ flags, which produces a slightly cleaner stripe fit. Algorithm change,
worth a CHANGELOG entry under Algorithm.

Snowblind's ``jumpify`` (snowblind/persist.py:105-107) reconstructs each jump
filename from ``model.meta.filename`` assuming a trailing ``_rate.fits``
suffix. Our canonical files lack that suffix, so we temporarily set
``meta.filename = '<rootname>_rate.fits'`` while the step runs, then restore
to ``<rootname>.fits`` before atomic_save.
"""

import gc
import os

from astropy.io import fits

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp


def _jump_path(exposure_path):
    rootname = os.path.basename(exposure_path).removesuffix('.fits')
    return os.path.join(os.path.dirname(exposure_path), f'{rootname}_jump.fits')


def _run_one_detector(detector, det_files, ImageModel, ModelContainer,
                      snowblind):
    """Run snowblind on one detector's exposures and save back.

    Returns the list of canonical paths that were processed (same as
    ``det_files`` on success). Closes both the input models and the
    snowblind-owned copies, then forces a gc to release the deep-copied
    arrays before the caller moves to the next detector.
    """
    images = ModelContainer()
    original_filenames = []
    for f in det_files:
        m = ImageModel(f)
        rootname = os.path.basename(f).removesuffix('.fits')
        original_filenames.append(m.meta.filename)
        # Munge filename so snowblind's jumpify hits <rootname>_jump.fits.
        m.meta.filename = f'{rootname}_rate.fits'
        images.append(m)

    input_dir = os.path.dirname(det_files[0])
    log(f"  detector {detector}: {len(det_files)} exposures")
    output = snowblind.PersistenceFlagStep.call(
        images,
        save_results=False,
        input_dir=input_dir,
    )

    try:
        for model, canonical, orig_name in zip(output, det_files,
                                               original_filenames):
            # Restore filename to canonical form before saving.
            model.meta.filename = orig_name or os.path.basename(canonical)
            atomic_save(
                model, canonical,
                header_updates=cfp.format(CFP_PERS=None),
            )
            model.close()
    finally:
        # Snowblind's process() does ``results = images.copy()``, deep-copying
        # every array. The originals are independent objects from this point
        # on and must be closed explicitly so their SCI/ERR/DQ buffers get
        # released — refcount alone does not always reclaim asdf-backed
        # ndarrays promptly.
        for m in images:
            try:
                m.close()
            except Exception:
                pass

    return det_files


def persistence_step(exposure_files, field, step_config, overwrite=False,
                     status=None):
    """Flag persistence across a per-filter set of canonical exposure files.

    Snowblind groups by detector internally (``persist.py``), so we hand it
    one detector at a time. That caps peak memory at roughly
    ``exposures_per_detector × 2`` (the deep copy in snowblind's ``process``)
    instead of the whole filter, and lets us close + gc between detector
    batches before the next ensemble step inherits a fat parent process.

    Parameters
    ----------
    exposure_files : list of str
        Canonical ``<rootname>.fits`` paths in
        ``products/nircam/<field>/<filter>/``.
    field : Field
        NIRCam field with workspace set up. (Unused here; kept for orchestrator
        signature uniformity.)
    step_config : dict
        ``[nircam.persistence]`` block (currently empty; reserved for future
        snowblind tunables).
    overwrite : bool
        If True, re-run even when every exposure already has ``CFP_PERS``.
    status : StepStatus, optional
        Pre-scanned CFP_* status cache.
    """
    if not exposure_files:
        return

    if not overwrite:
        if status is not None:
            all_done = all(status.has(f, 'CFP_PERS') for f in exposure_files)
        else:
            all_done = all(cfp.has_step(f, 'CFP_PERS') for f in exposure_files)
        if all_done:
            log("Skipping persistence; CFP_PERS already set on all exposures")
            return

    # Group eligible files by detector before loading any datamodels —
    # only the primary header is read here, no SCI/ERR/DQ.
    by_detector = {}
    for f in exposure_files:
        jump = _jump_path(f)
        if not os.path.exists(jump):
            log(f"Skipping persistence on {os.path.basename(f)}: no _jump.fits")
            continue
        detector = fits.getval(f, 'DETECTOR', ext=0).lower()
        by_detector.setdefault(detector, []).append(f)

    if not by_detector:
        log("No _jump.fits sidecars found; persistence skipped")
        return

    from jwst.datamodels import ImageModel, ModelContainer
    import snowblind

    total = sum(len(v) for v in by_detector.values())
    log(f"Running persistence on {total} exposures across "
        f"{len(by_detector)} detector(s): {sorted(by_detector)}")

    saved = []
    for detector, det_files in sorted(by_detector.items()):
        try:
            saved.extend(
                _run_one_detector(detector, det_files,
                                  ImageModel, ModelContainer, snowblind)
            )
        finally:
            gc.collect()

    for f in saved:
        try:
            os.remove(_jump_path(f))
        except OSError:
            pass
    log(f"Persistence flagged {len(saved)} exposures; "
        f"removed _jump.fits sidecars")
