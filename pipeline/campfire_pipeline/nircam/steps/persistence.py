"""
persistence: ``snowblind.PersistenceFlagStep`` across all exposures of a filter.

Per-filter ensemble step. Reads each canonical exposure file, runs snowblind
in memory, and atomically saves each mutated model back to its canonical
path with ``CFP_PERS`` stamped.

Runs *immediately after* detector1 (and jackknife). This lets the 1/f striping
step's source-mask construction see the persistence DQ flags, which produces a
slightly cleaner stripe fit.

snowblind >= 1 takes each exposure's saturated pixels from the ``SATURATED``
flag of its own ``dq`` (set by the ramp fit wherever any group saturated), so
this step needs nothing but the canonical files. The ``<rootname>_jump.fits``
sidecar detector1 writes is the jackknife step's input, not this one's; this
step only removes it once jackknife is done with it.

A pixel saturated in one exposure is flagged ``DO_NOT_USE | PERSISTENCE`` in
the later exposures of the same detector whose middle falls within ``time``
seconds of the end of that one. ``[nircam.persistence].time = "packaged"``
uses the time snowblind measured for each detector.
"""

import gc
import os

from astropy.io import fits

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp


def _jump_path(exposure_path):
    rootname = os.path.basename(exposure_path).removesuffix('.fits')
    return os.path.join(os.path.dirname(exposure_path), f'{rootname}_jump.fits')


def persistence_time_for(detector, value='packaged'):
    """Persistence time in seconds for *detector*.

    *value* is a number of seconds, or ``"packaged"`` (or None) for the time
    snowblind measured for the detector, falling back to snowblind's default
    for a detector it has no measurement for.
    """
    if value is None or str(value).lower() == 'packaged':
        from snowblind.persist import DEFAULT_TIME, packaged_time
        packaged = packaged_time(str(detector))
        return float(DEFAULT_TIME if packaged is None else packaged)
    return float(value)


def _run_one_detector(detector, det_files, ImageModel, ModelContainer,
                      snowblind, time=None):
    """Run snowblind on one detector's exposures and save back.

    *time* is the flagging window in seconds; None lets snowblind use the time
    it measured for the detector (recorded per exposure in ``PERSTIME``).

    Returns the list of canonical paths that were processed (same as
    ``det_files`` on success). Closes both the input models and any copies
    snowblind made, then lets the caller gc before the next detector.
    """
    images = ModelContainer()
    for f in det_files:
        images.append(ImageModel(f))

    log(f"  detector {detector}: {len(det_files)} exposures")
    output = snowblind.PersistenceFlagStep.call(
        images,
        time=time,
        save_results=False,
    )

    try:
        # snowblind returns the exposures in the order it was given them
        for model, canonical in zip(output, det_files):
            atomic_save(
                model, canonical,
                header_updates=cfp.format(CFP_PERS=None),
            )
            model.close()
    finally:
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
    one detector at a time. That caps peak memory at one detector's exposures
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
        ``[nircam.persistence]`` block: ``time`` (seconds, or ``"packaged"``
        for snowblind's measured per-detector time).
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

    # Group by detector before loading any datamodels — only the primary
    # header is read here, no SCI/ERR/DQ.
    by_detector = {}
    for f in exposure_files:
        detector = fits.getval(f, 'DETECTOR', ext=0).lower()
        by_detector.setdefault(detector, []).append(f)

    from jwst.datamodels import ImageModel, ModelContainer
    import snowblind

    total = sum(len(v) for v in by_detector.values())
    log(f"Running persistence on {total} exposures across "
        f"{len(by_detector)} detector(s): {sorted(by_detector)}")

    time = step_config.get('time', 'packaged')
    time = None if time is None or str(time).lower() == 'packaged' else float(time)

    saved = []
    for detector, det_files in sorted(by_detector.items()):
        try:
            saved.extend(
                _run_one_detector(detector, det_files,
                                  ImageModel, ModelContainer, snowblind,
                                  time=time)
            )
        finally:
            gc.collect()

    # Sidecar cleanup. This step does not read the _jump.fits sidecar, but it
    # is the last step that can know jackknife is done with it. The jackknife
    # step's only possible input is that sidecar: once deleted, the per-pixel excluded-group pattern is unrecoverable
    # (the 2-D DQ keeps JUMP_DET presence but not which groups were
    # dropped). When jackknife is enabled, retain the sidecar both for
    # exposures it has not stamped yet (normal ordering runs jackknife
    # first, so that only bites standalone/interrupted runs) and for
    # RETRYABLE failure stamps — 'skipped (error: ...)' / 'skipped
    # (identity check failed)' — so a transient failure can be re-run with
    # --overwrite instead of forcing a detector1 rebuild. Permanent skips
    # (nints>1, ngroups out of range) delete like successes: their sidecar
    # can never become usable.
    jackknife_enabled = bool(step_config.get('jackknife_enabled', True))

    def _jack_retain_reason(f):
        if not jackknife_enabled:
            return None
        if not cfp.has_step(f, 'CFP_JACK'):
            return 'jackknife has not run on this exposure yet'
        value = str(cfp.step_value(f, 'CFP_JACK'))
        if value.startswith('skipped (error') or value.startswith(
                'skipped (identity'):
            return f'jackknife recorded a retryable failure ({value})'
        return None

    removed, retained = 0, 0
    for f in saved:
        if not os.path.exists(_jump_path(f)):
            continue
        reason = _jack_retain_reason(f)
        if reason is not None:
            retained += 1
            log(f"  retaining {os.path.basename(_jump_path(f))}: {reason}")
            continue
        try:
            os.remove(_jump_path(f))
            removed += 1
        except OSError:
            pass
    log(f"Persistence flagged {len(saved)} exposures; removed {removed} "
        f"_jump.fits sidecars"
        + (f", retained {retained} pending jackknife" if retained else ""))
