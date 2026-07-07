"""
Shared flat-field helpers used by ``wisp_step`` (and other in-memory flat
callers).

Callers need to apply a NIRCam flat in-memory (without writing a
flat-fielded copy to disk). The CRDS lookup falls back to a custom-flat
override when present. The ``crds.getreferences`` call is taken under the
global CRDS cache lock (the same lock stpipe holds for its own lookups),
so a cold-fetch is serialized across workers — the first downloads, the
rest block and then find the file already cached — rather than several
workers racing to write the same cache file. The apply call keeps a short
retry loop as defense in depth.

Flat reference data is cached per worker process: the same ~50MB flat
applies to every exposure sharing a (detector, filter, pupil), and on the
cluster the products live on NFS, so re-reading it per exposure is pure
network round trips. The cache holds *pristine* FlatModels and hands
``do_correction`` a deep copy each call — jwst's ``apply_flat_field``
mutates the flat it is given in place (NaN/zero -> 1.0, DQ bit updates),
so reusing a model directly would depend on that mutation staying
idempotent across jwst versions. ``dispatch()`` builds a fresh Pool per
step, so the cache lives for exactly one step's dispatch and never goes
stale. The orchestrator sorts each step's task list by detector, so a
two-slot cache covers a worker's (detector-contiguous) chunk.
"""

import os
from time import sleep

from campfire_pipeline.common.io import log


# path -> pristine FlatModel, per worker process. Insertion-ordered dict
# used as a tiny LRU; evicted models are closed explicitly.
_FLAT_CACHE = {}
_FLAT_CACHE_MAX = 2


def _get_flat(flatfile):
    """Return the pristine cached FlatModel for ``flatfile`` (read on miss)."""
    from jwst.datamodels import FlatModel

    flat = _FLAT_CACHE.pop(flatfile, None)
    if flat is None:
        # memmap=False: the arrays are consumed in full, and one sequential
        # read beats memmap's page-faulted small reads on NFS.
        flat = FlatModel(flatfile, memmap=False)
    _FLAT_CACHE[flatfile] = flat  # (re-)insert as most recently used
    while len(_FLAT_CACHE) > _FLAT_CACHE_MAX:
        oldest = next(iter(_FLAT_CACHE))
        _FLAT_CACHE.pop(oldest).close()
    return flat


def resolve_flat(model, field, use_custom):
    """Pick the flat reference: custom (if requested + present) else CRDS."""
    crds_dict = {
        'INSTRUME': 'NIRCAM',
        'DETECTOR': model.meta.instrument.detector,
        'FILTER': model.meta.instrument.filter,
        'PUPIL': model.meta.instrument.pupil,
        'DATE-OBS': model.meta.observation.date,
        'TIME-OBS': model.meta.observation.time,
    }

    if use_custom:
        fn = crds_dict['FILTER'].upper()
        det = crds_dict['DETECTOR'].upper()
        flatfile = os.path.join(
            field.flats_dir, f'flat_nircam_{fn}_{det}_CLEAR.fits',
        )
        if os.path.exists(flatfile):
            return flatfile
        log(f"Custom flat {os.path.basename(flatfile)} not found; "
            f"falling back to CRDS")

    try:
        crds_context = os.environ['CRDS_CONTEXT']
    except KeyError:
        import crds
        crds_context = crds.get_default_context()
    import crds
    from crds.core import crds_cache_locking
    # Hold the global "crds.cache" lock around the lookup. This is a direct
    # getreferences (not via stpipe), so without the lock two workers could
    # cold-fetch the same flat at once and one read a half-written file. CRDS
    # double-checks cache existence inside the lock, so a blocked worker finds
    # the file already downloaded and skips. Forkserver workers share one lock
    # object (crds is in the preload list), so the coordination is real on the
    # cluster; on macOS spawn each worker gets a private lock (dev-only no-op).
    with crds_cache_locking.get_cache_lock():
        refs = crds.getreferences(
            crds_dict, reftypes=['flat'], context=crds_context,
        )
    return refs.get('flat')


def apply_flat_with_retry(model, flatfile, delays=(0, 3, 10)):
    """Apply flat in-memory, retrying on transient CRDS races."""
    from jwst.flatfield.flat_field import do_correction

    last_exc = None
    for delay in delays:
        if delay:
            sleep(delay)
        try:
            # Deep-copy the cached pristine flat: do_correction mutates the
            # flat it receives (see module docstring). An in-memory copy is
            # microseconds vs an ~50MB NFS re-read per exposure.
            flat = _get_flat(flatfile).copy()
            try:
                model, _ = do_correction(model, flat)
            finally:
                flat.close()
            return model
        except Exception as e:
            last_exc = e
    raise last_exc
