"""
CFP_* provenance keywords for canonical exposure files.

Every campfire NIRCam pipeline step records its execution by setting a
``CFP_<step>`` keyword in the primary FITS header of the exposure file. The
keyword's *presence* drives skip-if-exists logic; its *value* is either an ISO
timestamp or a short parameter summary, depending on which is more useful for
provenance and debugging.

The order of ``CFP_KEYS`` matters: it defines the dependency chain used by
``clear_from()`` (e.g. ``cfpipe nircam reset --from sky`` clears CFP_SKY and
every later key, since the SCI mutations are not independent).
"""

import os
from datetime import datetime

from astropy.io import fits


# Ordered list of provenance keys, one per pipeline step. The order encodes
# the dependency chain: clearing key K should also clear every key after K.
CFP_KEYS = [
    'CFP_DET1',  # detector1
    'CFP_PERS',  # snowblind persistence
    'CFP_WISP',  # wisp template subtraction
    'CFP_1F',    # 1/f striping
    'CFP_IMG2',  # image2
    'CFP_EDGE',  # edge flagging
    'CFP_SKY',   # sky pedestal subtraction
    'CFP_VAR',   # variance rescaling
    'CFP_JHAT',  # WCS alignment
    'CFP_MASK',  # user region masks
    'CFP_BPIX',  # bad pixel mask
    'CFP_OUT',   # outlier detection (skymatch folded in)
]

CFP_COMMENTS = {
    'CFP_DET1': 'campfire: detector1 done',
    'CFP_PERS': 'campfire: persistence flagged',
    'CFP_WISP': 'campfire: wisp template, scale',
    'CFP_1F':   'campfire: 1/f striping params',
    'CFP_IMG2': 'campfire: image2 done',
    'CFP_EDGE': 'campfire: edges flagged',
    'CFP_SKY':  'campfire: sky pedestal value',
    'CFP_VAR':  'campfire: variance correction factor',
    'CFP_JHAT': 'campfire: jhat refcat used',
    'CFP_MASK': 'campfire: user masks applied',
    'CFP_BPIX': 'campfire: bad pixel mask applied',
    'CFP_OUT':  'campfire: outlier detection done',
}


def iso_now():
    """ISO-8601 timestamp string suitable as a default CFP keyword value."""
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def format(**updates):
    """Validate CFP keyword updates and pair them with their comments.

    Pass ``key=None`` to fill in an ISO timestamp automatically.

    Returns a dict ready to hand to ``atomic_save(..., header_updates=...)``.

    Raises
    ------
    ValueError
        If any key is not a known CFP key.
    """
    formatted = {}
    for key, val in updates.items():
        if key not in CFP_KEYS:
            raise ValueError(
                f"Unknown CFP key '{key}'. Known keys: {CFP_KEYS}"
            )
        if val is None:
            val = iso_now()
        formatted[key] = (val, CFP_COMMENTS[key])
    return formatted


def has_step(path_or_header, key):
    """Return True if ``key`` is recorded on the given exposure file/header.

    Accepts either a path or an already-open ``fits.Header`` so callers that
    already have a header in hand don't pay for a re-open.
    """
    if key not in CFP_KEYS:
        raise ValueError(f"Unknown CFP key: {key}")
    if isinstance(path_or_header, fits.Header):
        return key in path_or_header
    with fits.open(path_or_header) as hdul:
        return key in hdul[0].header


def get_steps(path):
    """Return ``{key: value}`` for every CFP_* keyword present on ``path``.

    Used by ``cfpipe nircam status`` to render a per-exposure completion table.
    """
    with fits.open(path) as hdul:
        hdr = hdul[0].header
        return {k: hdr[k] for k in CFP_KEYS if k in hdr}


def clear_from(path, key):
    """Atomically remove ``key`` and every later CFP keyword from ``path``.

    Used by ``cfpipe nircam reset --from <step>`` to mark an exposure as
    needing re-processing from the named step onward. Does not modify SCI/DQ
    arrays — the caller is responsible for actually re-running the upstream
    steps that produce the data state for ``key``.
    """
    if key not in CFP_KEYS:
        raise ValueError(f"Unknown CFP key: {key}")
    to_clear = CFP_KEYS[CFP_KEYS.index(key):]

    tmp = path + '.tmp'
    with fits.open(path) as hdul:
        for k in to_clear:
            if k in hdul[0].header:
                del hdul[0].header[k]
        hdul.writeto(tmp, overwrite=True)
    os.replace(tmp, path)
