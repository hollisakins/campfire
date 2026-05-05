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

import os

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp


def _jump_path(exposure_path):
    rootname = os.path.basename(exposure_path).removesuffix('.fits')
    return os.path.join(os.path.dirname(exposure_path), f'{rootname}_jump.fits')


def persistence_step(exposure_files, field, step_config, overwrite=False,
                     status=None):
    """Flag persistence across a per-filter set of canonical exposure files.

    Parameters
    ----------
    exposure_files : list of str
        Canonical ``<rootname>.fits`` paths in ``exposures/<filter>/``.
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

    from jwst.datamodels import ImageModel, ModelContainer

    images = ModelContainer()
    saved_paths = []
    original_filenames = []
    for f in exposure_files:
        jump = _jump_path(f)
        if not os.path.exists(jump):
            log(f"Skipping persistence on {os.path.basename(f)}: no _jump.fits")
            continue
        m = ImageModel(f)
        rootname = os.path.basename(f).removesuffix('.fits')
        original_filenames.append(m.meta.filename)
        # Munge filename so snowblind's jumpify hits <rootname>_jump.fits.
        m.meta.filename = f'{rootname}_rate.fits'
        images.append(m)
        saved_paths.append(f)

    if not saved_paths:
        log("No _jump.fits sidecars found; persistence skipped")
        return

    import snowblind
    input_dir = os.path.dirname(saved_paths[0])

    log(f"Running persistence on {len(saved_paths)} exposures in {input_dir}")
    output = snowblind.PersistenceFlagStep.call(
        images,
        save_results=False,
        input_dir=input_dir,
    )

    for model, canonical, orig_name in zip(output, saved_paths,
                                           original_filenames):
        # Restore filename to canonical form before saving.
        model.meta.filename = orig_name or os.path.basename(canonical)
        atomic_save(
            model, canonical,
            header_updates=cfp.format(CFP_PERS=None),
        )
        model.close()

    for f in saved_paths:
        try:
            os.remove(_jump_path(f))
        except OSError:
            pass
    log(f"Persistence flagged {len(saved_paths)} exposures; "
        f"removed _jump.fits sidecars")
