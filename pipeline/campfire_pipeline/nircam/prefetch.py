"""CRDS reference file pre-fetching for the NIRCam pipeline.

Warming the CRDS cache before ``dispatch()`` lets parallel workers start
against a populated cache. It has to be *complete*: a reference a worker has
to download itself goes down the JWST pipeline's own per-dataset fetch, which
the CRDS cache lock does not serialize across spawned workers, so two workers
can download the same file at once and one of them fail (#700). And since
``dispatch`` maps over the exposures, that one failure stops the whole phase.

So the warm-up does not guess which header keys an rmap selects on (a dark is
selected by ``DATE-OBS`` through ``USEAFTER``, not by readout pattern). For
every pending uncal it asks CRDS what that exposure will use
(``crds.getrecommendations``: a lookup against the local rmaps, nothing is
downloaded), groups the exposures by the exact set of files recommended, and
downloads each distinct set once. Whatever selection keys any rmap uses now or
later, the warmed set is the set the workers will request.

Mirrors the NIRSpec pattern in ``nirspec/stage1.py`` and ``nirspec/stage2.py``.

Two anchors keep the warmed set aligned with what the run will actually use:

1. The reftypes are ``Detector1Pipeline.reference_file_types`` /
   ``Image2Pipeline.reference_file_types`` (the calibration references, incl.
   the flat that wisp/striping resolve directly) plus the ``pars-<step>``
   parameter references stpipe asks CRDS for, one per pipeline and per step
   (``Step.get_config_reftype()``).
2. The CRDS lookup parameters come from ``model.get_crds_parameters()`` on the
   uncal datamodel, the same call ``Step.get_reference_file()`` makes, so the
   rmap match resolves to byte-identical files.

Exposures whose detector1/image2 output already exists are skipped without
opening them, so re-running a finished field opens nothing.
"""

import os
import time

from campfire_pipeline.common.io import log


_REFTYPES_CACHE = {}


def _config_reftype(cls):
    """The ``pars-*`` reftype stpipe looks up for a step or pipeline class."""
    getter = getattr(cls, 'get_config_reftype', None)
    if getter is not None:
        try:
            return getter()
        except Exception:
            pass
    return f"pars-{cls.__name__.lower()}"


def _reftypes(pipeline_name):
    """Return the deduplicated reftypes a JWST pipeline run asks CRDS for.

    The calibration ``reference_file_types`` (an instance property aggregated
    from the substeps, with duplicates when steps share refs) followed by the
    ``pars-*`` parameter reftypes of the pipeline and of each of its steps.
    """
    if pipeline_name in _REFTYPES_CACHE:
        return _REFTYPES_CACHE[pipeline_name]
    from jwst import pipeline as jwst_pipeline
    cls = getattr(jwst_pipeline, pipeline_name)
    candidates = list(cls().reference_file_types)
    candidates.append(_config_reftype(cls))
    for step_cls in getattr(cls, 'step_defs', {}).values():
        candidates.append(_config_reftype(step_cls))
    seen = set()
    out = []
    for r in candidates:
        if r not in seen:
            seen.add(r)
            out.append(r)
    _REFTYPES_CACHE[pipeline_name] = out
    return out


def _crds_params(uncal_file):
    """Build the canonical CRDS lookup parameters dict from an uncal.

    Goes through the JWST datamodel layer so the rmap match resolves to the
    same files ``Step.get_reference_file()`` will pick at run-time. Building
    the dict by hand from ``fits.getheader`` risks drift on derived/normalized
    keys (e.g. ``CHANNEL`` for NIRCam) and silently downloads the wrong
    references.
    """
    from stdatamodels.jwst import datamodels
    with datamodels.open(uncal_file) as model:
        return dict(model.get_crds_parameters())


def _is_file(value):
    """True for a recommended reference file name (not ``N/A`` / not found)."""
    if not isinstance(value, str) or not value:
        return False
    upper = value.upper()
    return 'N/A' not in upper and 'NOT FOUND' not in upper


def _recommend(params, reftypes):
    """``{reftype: file}`` CRDS recommends for ``params``; downloads nothing."""
    import crds
    return crds.getrecommendations(params, reftypes=reftypes,
                                   observatory='jwst')


def _fetch(params, reftypes, key_label):
    """Run one ``crds.getreferences()`` under the CRDS cache lock; non-fatal.

    A failure is logged and swallowed: the warm-up is best-effort, and a
    worker that still misses a reference downloads it itself.
    """
    import crds
    from crds.core import crds_cache_locking
    try:
        with crds_cache_locking.get_cache_lock():
            refs = crds.getreferences(params, reftypes=reftypes,
                                      observatory='jwst')
        cached = [k for k, v in refs.items() if _is_file(v)]
        log(f"  Cached {len(cached)}/{len(reftypes)} references: "
            f"{', '.join(sorted(cached))}")
    except Exception as e:
        log(f"  CRDS prefetch warning for {key_label}: {e}")


def prefetch_process_references(field, filters, status=None, overwrite=False):
    """Warm Detector1 + Image2 CRDS references for the pending NIRCam work.

    One serial pass over the uncals in ``filters``. For every exposure whose
    detector1 or image2 output is still pending (per ``status``), the CRDS
    parameters are built and CRDS is asked which file it recommends for each
    reftype the pending stages need. Exposures are grouped by that exact
    recommendation, and ``crds.getreferences`` runs once per distinct group,
    so every reference any pending exposure will use is in the cache before
    ``dispatch()``. Exposures whose output already exists are skipped without
    being opened, so re-running a finished field is free.

    ``status`` is the phase's pre-scanned ``StepStatus`` (``None`` warms
    everything — used by callers that don't track status). With
    ``overwrite=True`` the skip check is bypassed and every exposure is warmed.
    """
    det1_reftypes = _reftypes('Detector1Pipeline')
    img2_reftypes = _reftypes('Image2Pipeline')
    # recommendation (frozenset of (reftype, file)) -> (params, uncal)
    groups = {}
    n_exposures = 0
    t0 = time.monotonic()

    for filt in filters:
        try:
            uncals = field.get_uncal_files(filt)
        except RuntimeError:
            # Workspace not set up for this filter — nothing to warm.
            continue
        for f in uncals:
            rootname = os.path.basename(f).removesuffix('_uncal.fits')
            canonical = field.get_exposure_path(rootname, filt)
            # Only consult status for a canonical that exists; short-circuit
            # keeps status.has() off nonexistent paths (first-run canonicals).
            done = (not overwrite and status is not None
                    and os.path.exists(canonical))
            need_det1 = not (done and status.has(canonical, 'CFP_DET1'))
            need_img2 = not (done and status.has(canonical, 'CFP_IMG2'))
            if not (need_det1 or need_img2):
                continue

            reftypes = ((det1_reftypes if need_det1 else [])
                        + [r for r in (img2_reftypes if need_img2 else [])
                           if not (need_det1 and r in det1_reftypes)])
            name = os.path.basename(f)
            n_exposures += 1
            try:
                params = _crds_params(f)
            except Exception as e:
                log(f"  CRDS prefetch warning for {name}: {e}")
                continue
            try:
                recs = _recommend(params, reftypes)
            except Exception as e:
                # No recommendation: warm this exposure on its own.
                log(f"  CRDS recommendation failed for {name} ({e}); "
                    f"fetching its references directly")
                _fetch(params, reftypes, key_label=name)
                continue
            key = frozenset((k, v) for k, v in recs.items() if _is_file(v))
            if key not in groups:
                groups[key] = (params, name)

    if not groups:
        if n_exposures:
            log(f"NIRCam CRDS reference pre-fetch: no references resolved "
                f"for {n_exposures} pending exposures")
        else:
            log("NIRCam CRDS reference pre-fetch: nothing pending")
        return

    files = sorted({v for key in groups for _, v in key})
    log(f"NIRCam CRDS pre-fetch: {n_exposures} pending exposures resolve to "
        f"{len(files)} references in {len(groups)} distinct sets "
        f"({time.monotonic() - t0:.0f} s)")
    for i, (key, (params, name)) in enumerate(groups.items(), 1):
        # Only the reftypes that resolved to a file: an N/A entry (most
        # ``pars-*`` steps) has nothing to download.
        reftypes = sorted(k for k, _ in key)
        if not reftypes:
            continue
        log(f"Pre-fetching CRDS reference set {i}/{len(groups)} using {name}")
        _fetch(params, reftypes, key_label=name)
    log(f"NIRCam CRDS reference pre-fetch complete ({len(groups)} lookups)")
