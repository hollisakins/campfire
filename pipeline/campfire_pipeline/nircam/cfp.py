"""
NIRCam CFP_* provenance keys + thin wrappers around ``common.cfp`` operations.

The order of ``CFP_KEYS`` matters: it defines the dependency chain used by
``clear_from()`` (e.g. ``cfpipe nircam reset --from sky`` clears CFP_SKY and
every later key, since the SCI mutations are not independent).

The wrappers below bake in ``CFP_KEYS`` / ``CFP_COMMENTS`` so call sites in
``nircam/`` can keep writing ``cfp.format(KEY=value)`` and
``cfp.clear_from(path, key)`` with no signature noise. ``has_step`` and
``should_skip`` are re-exported unchanged because they don't need the keys
list (typo guarding lives in ``format`` where the write happens).
"""

from campfire_pipeline.common import cfp as _cfp


# Ordered list of provenance keys, one per pipeline step. The order encodes
# the dependency chain: clearing key K should also clear every key after K.
# (FITS standard limits keyword names to 8 characters, hence the
# abbreviated forms — CFP_BPIX for bad_pixel, etc.)
CFP_KEYS = [
    'CFP_DET1',  # detector1
    'CFP_PERS',  # snowblind persistence
    'CFP_WISP',  # wisp template subtraction
    'CFP_1F',    # 1/f striping
    'CFP_IMG2',  # image2
    'CFP_EDGE',  # edge flagging
    'CFP_SKY',   # sky pedestal subtraction
    'CFP_DIAG',  # diagonal scattered-light striping (opt-in)
    'CFP_VAR',   # variance rescaling
    'CFP_SHFT',  # pre-jhat astrometric WCS shift (opt-in, rule-driven)
    'CFP_PREV',  # per-exposure preview PNG for web admin triage
    'CFP_JHAT',  # WCS alignment
    'CFP_MASK',  # user region masks
    'CFP_BPIX',  # bad pixel mask
    'CFP_OUT',   # outlier detection (per-visit ensemble)
]

CFP_COMMENTS = {
    'CFP_DET1': 'campfire: detector1 done',
    'CFP_PERS': 'campfire: persistence flagged',
    'CFP_WISP': 'campfire: wisp template, scale',
    'CFP_1F':   'campfire: 1/f striping params',
    'CFP_IMG2': 'campfire: image2 done',
    'CFP_EDGE': 'campfire: edges flagged',
    'CFP_SKY':  'campfire: sky pedestal value',
    'CFP_DIAG': 'campfire: diagonal stripe theta and search range',
    'CFP_VAR':  'campfire: variance correction factor',
    'CFP_SHFT': 'campfire: pre-jhat WCS shift (dra,ddec,droll,scale)',
    'CFP_PREV': 'campfire: preview PNG rendered',
    'CFP_JHAT': 'campfire: jhat refcat used',
    'CFP_MASK': 'campfire: user masks applied',
    'CFP_BPIX': 'campfire: bad pixel mask applied',
    'CFP_OUT':  'campfire: outlier detection done',
}


# Re-exports that don't need the keys list
iso_now = _cfp.iso_now
has_step = _cfp.has_step
should_skip = _cfp.should_skip


def format(**updates):
    """Validate and format NIRCam CFP keyword updates. See ``common.cfp.format``."""
    return _cfp.format(updates, CFP_KEYS, CFP_COMMENTS)


def get_steps(path):
    """Return ``{key: value}`` for every NIRCam CFP key present on ``path``."""
    return _cfp.get_steps(path, CFP_KEYS)


def clear_from(path, key):
    """Atomically remove ``key`` and every later NIRCam CFP key from ``path``."""
    return _cfp.clear_from(path, key, CFP_KEYS)
