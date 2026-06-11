"""CRDS reference file pre-fetching for the NIRCam pipeline.

Warming the CRDS cache before ``dispatch()`` lets parallel workers start
against a populated cache instead of a cold one. It is a *performance* pass,
not a correctness one: the CRDS cache lock (see ``_fetch`` here and
``steps/_flat.resolve_flat``) makes any residual cache miss in a worker a
safe, serialized lazy download — the first worker fetches while the rest
block, then find the file already cached. So the dedup granularity below only
affects how much is warmed up front, never which references a step selects.
CRDS still resolves each file by the exposure's own parameters + USEAFTER
epoch at run time, exactly as ``Step.get_reference_file()`` does.

Mirrors the NIRSpec pattern in ``nirspec/stage1.py`` and ``nirspec/stage2.py``.

Two anchors keep the warmed set aligned with what the run will actually use:

1. The reftype lists come from ``Detector1Pipeline.reference_file_types`` and
   ``Image2Pipeline.reference_file_types`` — the same aggregation jwst itself
   uses, so we cover everything (incl. ``persat``/``trapdensity``/``trappars``
   and the flat that wisp/striping resolve directly) and nothing extra.
2. The CRDS lookup parameters come from ``model.get_crds_parameters()`` on the
   uncal datamodel — the same call ``Step.get_reference_file()`` makes — so
   the rmap match resolves to byte-identical files.

The scan reads each uncal header at most once (both the detector1 and image2
dedup keys come from one ``getheader``) and skips exposures whose
detector1/image2 output already exists, so re-running a finished field reads
no headers at all — the multi-minute NFS header scan only happens for work
that remains.
"""

import os

from astropy.io import fits

from campfire_pipeline.common.io import log


_REFTYPES_CACHE = {}


def _reftypes(pipeline_name):
    """Return the deduplicated ``reference_file_types`` for a JWST pipeline."""
    if pipeline_name in _REFTYPES_CACHE:
        return _REFTYPES_CACHE[pipeline_name]
    from jwst import pipeline as jwst_pipeline
    cls = getattr(jwst_pipeline, pipeline_name)
    # ``reference_file_types`` is an instance property aggregated from substeps
    # (with duplicates when steps share refs); dedup while preserving order.
    seen = set()
    out = []
    for r in cls().reference_file_types:
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


def _fetch(params, reftypes, key_label):
    """Run one ``crds.getreferences()`` under the CRDS cache lock; non-fatal.

    The lock is the same global ``crds.cache`` lock stpipe takes for its own
    lookups, so this serial warm-up shares a download queue with any lazy
    worker fetch rather than racing it. A lookup failure is logged and
    swallowed — warming is best-effort, and a genuine miss self-heals as a
    safe lazy download in the worker.
    """
    import crds
    from crds.core import crds_cache_locking
    try:
        with crds_cache_locking.get_cache_lock():
            refs = crds.getreferences(params, reftypes=reftypes,
                                      observatory='jwst')
        cached = [
            k for k, v in refs.items()
            if v and 'N/A' not in v.upper() and 'NOT FOUND' not in v.upper()
        ]
        log(f"  Cached {len(cached)}/{len(reftypes)} references: "
            f"{', '.join(sorted(cached))}")
    except Exception as e:
        log(f"  CRDS prefetch warning for {key_label}: {e}")


def prefetch_process_references(field, filters, status=None, overwrite=False):
    """Warm Detector1 + Image2 CRDS references for the pending NIRCam work.

    One serial pass over the uncals in ``filters``. Each uncal header is read
    at most once: the detector1 dedup key ``(DETECTOR, READPATT, SUBARRAY)``
    and the image2 dedup key ``(DETECTOR, FILTER, PUPIL)`` are both derived
    from it, and a CRDS lookup runs only for the first uncal of each unique
    key. Exposures whose detector1 *and* image2 output already exists (per
    ``status``) are skipped without reading their header, so re-running a
    finished field is free.

    ``status`` is the phase's pre-scanned ``StepStatus`` (``None`` warms
    everything — used by callers that don't track status). With
    ``overwrite=True`` the skip check is bypassed and every config is warmed.
    See the module docstring: this only *warms* the cache; the cache lock
    makes any miss a safe lazy worker download, so the dedup here never
    affects reference selection.
    """
    det1_reftypes = _reftypes('Detector1Pipeline')
    img2_reftypes = _reftypes('Image2Pipeline')
    det1_seen = set()
    img2_seen = set()
    det1_n = 0
    img2_n = 0

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

            hdr = fits.getheader(f)
            params = None  # built lazily, at most once per uncal

            if need_det1:
                key = (hdr.get('DETECTOR'), hdr.get('READPATT'),
                       hdr.get('SUBARRAY'))
                if key not in det1_seen:
                    det1_seen.add(key)
                    params = _crds_params(f)
                    log(f"Pre-fetching Detector1 CRDS references for "
                        f"{key[0]} ({key[1]}/{key[2]}) using "
                        f"{os.path.basename(f)}")
                    _fetch(params, det1_reftypes,
                           key_label='/'.join(map(str, key)))
                    det1_n += 1

            if need_img2:
                key = (hdr.get('DETECTOR'), hdr.get('FILTER'),
                       hdr.get('PUPIL'))
                if key not in img2_seen:
                    img2_seen.add(key)
                    if params is None:
                        params = _crds_params(f)
                    log(f"Pre-fetching Image2 CRDS references for "
                        f"{key[0]}/{key[1]}/{key[2]} using "
                        f"{os.path.basename(f)}")
                    _fetch(params, img2_reftypes,
                           key_label='/'.join(map(str, key)))
                    img2_n += 1

    if det1_n or img2_n:
        log(f"NIRCam CRDS reference pre-fetch complete "
            f"({det1_n} detector1, {img2_n} image2 lookups)")
    else:
        log("NIRCam CRDS reference pre-fetch: nothing pending")
