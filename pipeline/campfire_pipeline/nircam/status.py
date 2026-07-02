"""
In-memory CFP_* status cache for the NIRCam orchestrator.

Built once at the top of ``run_process`` / ``run_combine`` / ``run_step``
by scanning the primary header of every canonical exposure file. Steps
then consult the cache via ``StepStatus.has(path, key)`` instead of
reopening each FITS for their skip check.

Cache freshness: each step has its own CFP_* key, and that key is only
ever set by that step. So per-step skip checks remain correct without
in-flight updates. The orchestrator still calls ``mark_all`` after each
step finishes so that ``Field.get_exposure_files(..., with_step=...)``
sees freshly-stamped keys (the only place within-phase changes are
observed — resample reads ``CFP_OUT`` written by outlier earlier in the
same combine phase).
"""

import os

from astropy.io import fits

from campfire_pipeline.common import cfp


# CFP keys whose *value* (not just presence) is cached during the scan, so
# consumers can branch on it without re-opening the FITS. Kept tiny: only
# CFP_JHAT, whose value is a refcat name on success or a failure sentinel that
# ``resample`` reads to exclude unaligned exposures from the mosaic.
_VALUE_KEYS = ('CFP_JHAT',)


class StepStatus:
    """A path → set-of-CFP-keys snapshot, plus optional in-memory updates.

    Also caches the *value* of a few keys (``_VALUE_KEYS``) read during the
    same single header scan, exposed via ``value``.
    """

    def __init__(self, present=None, values=None):
        self._present = dict(present) if present else {}
        self._values = dict(values) if values else {}

    @classmethod
    def scan(cls, paths):
        """Read primary headers once and record which CFP keys are present."""
        present = {}
        values = {}
        for p in paths:
            if not os.path.exists(p):
                present[p] = set()
                continue
            try:
                with fits.open(p, memmap=False) as hdul:
                    hdr = hdul[0].header
                    present[p] = {k for k in cfp.CFP_KEYS if k in hdr}
                    values[p] = {k: hdr[k] for k in _VALUE_KEYS if k in hdr}
            except (OSError, IOError):
                # Corrupt or unreadable: treat as no keys present so the
                # step itself can decide to fail loudly instead of being
                # silently skipped here.
                present[p] = set()
        return cls(present, values)

    def has(self, path, key):
        """True if ``key`` is recorded on ``path``.

        Falls back to a live FITS read for paths not seen during the
        initial scan (e.g. files written between scan and the check).
        """
        if key not in cfp.CFP_KEYS:
            raise ValueError(f"Unknown CFP key: {key}")
        if path in self._present:
            return key in self._present[path]
        if not os.path.exists(path):
            return False
        return cfp.has_step(path, key)

    def value(self, path, key, default=None):
        """Return the cached value of ``key`` on ``path``.

        Falls back to a live header read for paths not seen during the scan
        (or scanned before this key was written). Only ``_VALUE_KEYS`` are
        cached; other keys always take the live path.
        """
        if key in _VALUE_KEYS and path in self._values:
            return self._values[path].get(key, default)
        if not os.path.exists(path):
            return default
        return cfp.get_value(path, key, default)

    def mark(self, path, key):
        self._present.setdefault(path, set()).add(key)

    def mark_all(self, paths, key):
        for p in paths:
            self.mark(p, key)

    def add_paths(self, paths):
        """Scan additional paths into the cache (used when new files appear)."""
        for p in paths:
            if p in self._present:
                continue
            if not os.path.exists(p):
                self._present[p] = set()
                continue
            try:
                with fits.open(p, memmap=False) as hdul:
                    hdr = hdul[0].header
                    self._present[p] = {k for k in cfp.CFP_KEYS if k in hdr}
                    self._values[p] = {k: hdr[k] for k in _VALUE_KEYS if k in hdr}
            except (OSError, IOError):
                self._present[p] = set()
