"""
CFP_* provenance keywords for canonical exposure files.

Every campfire imaging pipeline step records its execution by setting a
``CFP_<step>`` keyword in the primary FITS header of the exposure file.
The keyword's *presence* drives skip-if-exists logic; its *value* is
either an ISO timestamp or a short parameter summary, depending on
which is more useful for provenance and debugging.

This module provides the *operations* on CFP keys. The keys themselves
(the ordered list that drives ``clear_from``'s dependency cascade, and
the comment-string map that ``format`` writes to FITS) live per-instrument
in ``<instrument>/cfp.py``: NIRCam has its own chain, MIRI has its own,
and the two can evolve independently.

Per-instrument modules typically expose thin wrappers that bake in
``CFP_KEYS`` and ``CFP_COMMENTS`` so call sites can keep writing
``cfp.format(KEY=value)`` with no signature noise — see
``nircam/cfp.py`` for the pattern.
"""

import os
from datetime import datetime

from astropy.io import fits

from campfire_pipeline.common.io import log


def iso_now():
    """ISO-8601 timestamp string suitable as a default CFP keyword value."""
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def format(updates, keys_list, comments):
    """Validate CFP keyword updates and pair them with their comments.

    Parameters
    ----------
    updates : dict
        ``{key: value}`` updates. Pass ``value=None`` to fill in an ISO
        timestamp automatically.
    keys_list : list of str
        The instrument's ordered list of allowed CFP_* keys. Used for
        typo guarding only.
    comments : dict
        ``{key: comment_string}`` for the FITS comment column.

    Returns
    -------
    dict
        Ready to hand to ``atomic_save(..., header_updates=...)``.

    Raises
    ------
    ValueError
        If any key is not in ``keys_list``.
    """
    formatted = {}
    for key, val in updates.items():
        if key not in keys_list:
            raise ValueError(
                f"Unknown CFP key '{key}'. Known keys: {keys_list}"
            )
        if val is None:
            val = iso_now()
        formatted[key] = (val, comments[key])
    return formatted


def has_step(path_or_header, key):
    """Return True if ``key`` is recorded on the given exposure file/header.

    Accepts either a path or an already-open ``fits.Header`` so callers that
    already have a header in hand don't pay for a re-open. The key name is
    not validated — keep typo guarding in ``format()`` where the write
    happens, not at the read side.
    """
    if isinstance(path_or_header, fits.Header):
        return key in path_or_header
    with fits.open(path_or_header) as hdul:
        return key in hdul[0].header


def should_skip(exposure_file, key, rootname, step_name, status, overwrite):
    """Skip-check shared across per-exposure step modules.

    Returns True (and logs) when the step is already recorded on the file
    and ``overwrite`` is False. ``status`` may be a pre-scanned StepStatus
    cache (preferred) or None (falls back to opening the FITS file).
    """
    if overwrite:
        return False
    done = (status.has(exposure_file, key) if status is not None
            else has_step(exposure_file, key))
    if done:
        log(f"Skipping {step_name} on {rootname}: {key} already set")
    return done


def get_steps(path, keys_list):
    """Return ``{key: value}`` for every CFP_* keyword in ``keys_list`` present on ``path``.

    Iterates ``keys_list`` so the result preserves the canonical instrument
    order — useful for rendering completion tables.
    """
    with fits.open(path) as hdul:
        hdr = hdul[0].header
        return {k: hdr[k] for k in keys_list if k in hdr}


def clear_from(path, key, keys_list):
    """Atomically remove ``key`` and every later CFP keyword in ``keys_list`` from ``path``.

    Used by ``cfpipe <instrument> reset --from <step>`` to mark an exposure
    as needing re-processing from the named step onward. Does not modify
    SCI/DQ arrays — the caller is responsible for actually re-running the
    upstream steps that produce the data state for ``key``.
    """
    if key not in keys_list:
        raise ValueError(f"Unknown CFP key: {key}")
    to_clear = keys_list[keys_list.index(key):]

    base, ext = os.path.splitext(path)
    tmp = f'{base}.tmp{ext}' if ext else f'{path}.tmp'
    with fits.open(path) as hdul:
        for k in to_clear:
            if k in hdul[0].header:
                del hdul[0].header[k]
        hdul.writeto(tmp, overwrite=True)
    os.replace(tmp, path)
