"""
In-memory CFP_* status cache for imaging-arm orchestrators.

Built once at the top of ``run_process`` / ``run_combine`` / ``run_step``
by scanning the primary header of every canonical exposure file. Steps
then consult the cache via ``StepStatus.has(path, key)`` instead of
reopening each FITS for their skip check.

The cache collects any header keyword matching the ``CFP_*`` prefix, so
it works for any instrument's key set without being told what those keys
are. Validation of key names against an instrument's allowed list is the
caller's responsibility (e.g. via per-instrument ``cfp.format``).

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


def _scan_cfp_keys(path):
    """Return the set of ``CFP_*`` keys present in ``path``'s primary header."""
    try:
        with fits.open(path) as hdul:
            return {k for k in hdul[0].header if k.startswith('CFP_')}
    except (OSError, IOError):
        # Corrupt or unreadable: treat as no keys present so the step
        # itself can decide to fail loudly instead of being silently
        # skipped here.
        return set()


class StepStatus:
    """A path → set-of-CFP-keys snapshot, plus optional in-memory updates."""

    def __init__(self, present=None):
        self._present = dict(present) if present else {}

    @classmethod
    def scan(cls, paths):
        """Read primary headers once and record which CFP_* keys are present."""
        present = {}
        for p in paths:
            if not os.path.exists(p):
                present[p] = set()
                continue
            present[p] = _scan_cfp_keys(p)
        return cls(present)

    def has(self, path, key):
        """True if ``key`` is recorded on ``path``.

        Falls back to a live FITS read for paths not seen during the
        initial scan (e.g. files written between scan and the check).
        """
        if path in self._present:
            return key in self._present[path]
        if not os.path.exists(path):
            return False
        return key in _scan_cfp_keys(path)

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
            self._present[p] = _scan_cfp_keys(p)
