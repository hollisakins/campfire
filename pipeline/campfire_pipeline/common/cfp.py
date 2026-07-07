"""
CFP_* provenance keywords for canonical pipeline files.

Every campfire pipeline step records its execution by setting a ``CFP_<step>``
keyword in the primary FITS header of the canonical file. The keyword's
*presence* drives skip-if-exists logic; its *value* is either an ISO timestamp
or a short parameter summary, depending on which is more useful for provenance
and debugging.

The two instruments keep **separate key sets** (``NIRCAM`` and ``NIRSPEC``)
that share these mechanics. They are deliberately distinct namespaces: the same
physical FITS keyword (e.g. ``CFP_BKG``, ``CFP_MASK``) carries different
semantics per instrument, which is safe only because a NIRCam canonical file
and a NIRSpec canonical file are never the same file. Every function takes a
keyword-only ``keyset`` argument that defaults to :data:`NIRCAM`, so existing
NIRCam call sites are unchanged; NIRSpec callers pass ``keyset=cfp.NIRSPEC``.

The order of a key set's ``keys`` matters: it defines the dependency chain used
by :func:`clear_from` (e.g. ``cfpipe nircam reset --from bkg`` clears
``CFP_BKG`` and every later key, since the SCI mutations are not independent).
``clear_from`` only ever slices within the selected key set, so a reset on one
instrument never touches the other's keywords.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime

from astropy.io import fits

from campfire_pipeline.common.io import log


@dataclass
class Keyset:
    """An ordered provenance-key chain for one instrument.

    ``keys`` is the execution/dependency order (FITS keyword names, <=8 chars);
    ``comments`` maps each key to its FITS card comment. ``clear_from`` slices
    ``keys`` from a given key onward, so the order is load-bearing.
    """
    name: str
    keys: list
    comments: dict = field(default_factory=dict)

    def __post_init__(self):
        missing = [k for k in self.keys if k not in self.comments]
        if missing:
            raise ValueError(
                f"Keyset '{self.name}' missing comments for: {missing}"
            )

    def validate(self, key):
        if key not in self.keys:
            raise ValueError(
                f"Unknown CFP key '{key}' for instrument '{self.name}'. "
                f"Known keys: {self.keys}"
            )


# --- NIRCam canonical-exposure chain ---------------------------------------
# One canonical FITS per exposure, mutated in place; the order is the process
# order (detector1 -> persistence -> wisp -> image2 -> ... -> outlier).
# (FITS limits keyword names to 8 characters, hence the abbreviated forms —
# CFP_BPIX for bad_pixel, CFP_BKG for background, etc.)
NIRCAM = Keyset(
    name='nircam',
    keys=[
        'CFP_DET1',  # detector1
        'CFP_PERS',  # snowblind persistence
        'CFP_WISP',  # wisp template subtraction
        'CFP_IMG2',  # image2
        'CFP_EDGE',  # edge flagging
        'CFP_BKG',   # unified background: per-amp pedestal + 1/f + variance
        'CFP_DIAG',  # diagonal scattered-light striping (opt-in)
        'CFP_SHFT',  # pre-jhat astrometric WCS shift (opt-in, rule-driven)
        'CFP_PREV',  # per-exposure preview PNG for web admin triage
        'CFP_JHAT',  # WCS alignment
        'CFP_ALGN',  # adaptive astrometric align (replaces jhat/wcs_shift)
        'CFP_MASK',  # user region masks
        'CFP_BPIX',  # bad pixel mask
        'CFP_OUT',   # outlier detection (per-visit ensemble)
    ],
    comments={
        'CFP_DET1': 'campfire: detector1 done',
        'CFP_PERS': 'campfire: persistence flagged',
        'CFP_WISP': 'campfire: wisp template, scale',
        'CFP_IMG2': 'campfire: image2 done',
        'CFP_EDGE': 'campfire: edges flagged',
        'CFP_BKG':  'campfire: background (pedestal, 1/f, variance)',
        'CFP_DIAG': 'campfire: diagonal stripe theta and search range',
        'CFP_SHFT': 'campfire: pre-jhat WCS shift (dra,ddec,droll,scale)',
        'CFP_PREV': 'campfire: preview PNG rendered',
        'CFP_JHAT': 'campfire: jhat refcat used',
        'CFP_ALGN': 'campfire: align solve',
        'CFP_MASK': 'campfire: user masks applied',
        'CFP_BPIX': 'campfire: bad pixel mask applied',
        'CFP_OUT':  'campfire: outlier detection done',
    },
)


# --- NIRSpec canonical spectrum-exposure chain -----------------------------
# One canonical FITS per (exposure, detector, source) — the _cal MultiSlitModel
# mutated in place (issue #212). The order is the stage2->3 process order. The
# per-(exposure,detector) _rate tier keeps its own CFBKGSUB sentinel (stage1);
# it is a separate tier and not part of this chain.
NIRSPEC = Keyset(
    name='nirspec',
    keys=[
        'CFP_CAL',   # spec2 cal: wcs/photom/fix_units (the canonical anchor)
        'CFP_MASK',  # nirspec manual mask applied (reserved; round-trip is open)
        'CFP_BKG',   # nodded background subtraction state (in-place)
        'CFP_S2D',   # rectified s2d views appended as S2D_* HDUs
    ],
    comments={
        'CFP_CAL':  'campfire: spec2 cal done (wcs/photom/fix_units)',
        'CFP_MASK': 'campfire: nirspec mask applied',
        'CFP_BKG':  'campfire: nodded bkgsub state',
        'CFP_S2D':  'campfire: s2d rectified views appended',
    },
)


# Backward-compatible module-level aliases: legacy NIRCam call sites and
# nircam/status.py reference ``cfp.CFP_KEYS`` / ``cfp.CFP_COMMENTS`` directly.
CFP_KEYS = NIRCAM.keys
CFP_COMMENTS = NIRCAM.comments


def iso_now():
    """ISO-8601 timestamp string suitable as a default CFP keyword value."""
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def format(*, keyset=NIRCAM, **updates):
    """Validate CFP keyword updates and pair them with their comments.

    Pass ``key=None`` to fill in an ISO timestamp automatically. ``keyset``
    selects the instrument namespace (default :data:`NIRCAM`).

    Returns a dict ready to hand to ``atomic_save(..., header_updates=...)``.

    Raises
    ------
    ValueError
        If any key is not a known CFP key for ``keyset``.
    """
    formatted = {}
    for key, val in updates.items():
        keyset.validate(key)
        if val is None:
            val = iso_now()
        formatted[key] = (val, keyset.comments[key])
    return formatted


def has_step(path_or_header, key, *, keyset=NIRCAM):
    """Return True if ``key`` is recorded on the given canonical file/header.

    Accepts either a path or an already-open ``fits.Header`` so callers that
    already have a header in hand don't pay for a re-open.
    """
    keyset.validate(key)
    if isinstance(path_or_header, fits.Header):
        return key in path_or_header
    with fits.open(path_or_header, memmap=False) as hdul:
        return key in hdul[0].header


def should_skip(exposure_file, key, rootname, step_name, status, overwrite,
                *, keyset=NIRCAM):
    """Skip-check shared across per-exposure/per-source step modules.

    Returns True (and logs) when the step is already recorded on the file
    and ``overwrite`` is False. ``status`` may be a pre-scanned StepStatus
    cache (preferred, NIRCam) or None (falls back to opening the FITS file).
    """
    if overwrite:
        return False
    done = (status.has(exposure_file, key) if status is not None
            else has_step(exposure_file, key, keyset=keyset))
    if done:
        log(f"Skipping {step_name} on {rootname}: {key} already set")
    return done


def get_steps(path, *, keyset=NIRCAM):
    """Return ``{key: value}`` for every CFP_* keyword present on ``path``.

    Ordered by ``keyset.keys``. Used by the ``status`` commands to render a
    per-file completion table.
    """
    with fits.open(path, memmap=False) as hdul:
        hdr = hdul[0].header
        return {k: hdr[k] for k in keyset.keys if k in hdr}


def clear_from(path, key, *, keyset=NIRCAM):
    """Atomically remove ``key`` and every later CFP keyword from ``path``.

    Used by ``reset --from <step>`` to mark a file as needing re-processing
    from the named step onward. Does not modify SCI/DQ arrays — the caller is
    responsible for actually re-running the upstream steps that produce the
    data state for ``key``. Only slices within ``keyset``, so a reset on one
    instrument never clears the other's keywords.
    """
    keyset.validate(key)
    to_clear = keyset.keys[keyset.keys.index(key):]

    base, ext = os.path.splitext(path)
    tmp = f'{base}.tmp{ext}' if ext else f'{path}.tmp'
    with fits.open(path, memmap=False) as hdul:
        for k in to_clear:
            if k in hdul[0].header:
                del hdul[0].header[k]
        hdul.writeto(tmp, overwrite=True)
    os.replace(tmp, path)
