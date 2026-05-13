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


class StepStatus:
    """A path → set-of-CFP-keys snapshot, plus optional in-memory updates."""

    def __init__(self, present=None):
        self._present = dict(present) if present else {}

    @classmethod
    def scan(cls, paths):
        """Read primary headers once and record which CFP keys are present."""
        present = {}
        for p in paths:
            if not os.path.exists(p):
                present[p] = set()
                continue
            try:
                with fits.open(p) as hdul:
                    hdr = hdul[0].header
                    present[p] = {k for k in cfp.CFP_KEYS if k in hdr}
            except (OSError, IOError):
                # Corrupt or unreadable: treat as no keys present so the
                # step itself can decide to fail loudly instead of being
                # silently skipped here.
                present[p] = set()
        return cls(present)

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
                with fits.open(p) as hdul:
                    hdr = hdul[0].header
                    self._present[p] = {k for k in cfp.CFP_KEYS if k in hdr}
            except (OSError, IOError):
                self._present[p] = set()
