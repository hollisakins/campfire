"""
MIRI CFP_* provenance keys + thin wrappers around ``common.cfp`` operations.

Mirrors ``nircam/cfp.py``: the wrappers bake in MIRI's ``CFP_KEYS`` /
``CFP_COMMENTS`` so call sites in ``miri/`` write
``cfp.format(KEY=value)`` / ``cfp.clear_from(path, key)`` with no
signature noise.

The keys list is empty for now; entries get added as custom MIRI steps
land (persistence prepass, warm pixel mask, super-background, ...).
``CFP_DET1``, ``CFP_IMG2``, and ``CFP_OUT`` will be the first additions
when the stage-1/2/3 wrappers ship — they reuse the stock JWST pipeline
and the CFP names match NIRCam's for consistency across instruments.
"""

from campfire_pipeline.common import cfp as _cfp


# Ordered list of provenance keys, one per pipeline step. The order encodes
# the dependency chain: clearing key K should also clear every key after K.
# Populate as MIRI reduction steps are implemented (see
# docs/design-miri-reduction.md).
CFP_KEYS: list[str] = []

CFP_COMMENTS: dict[str, str] = {}


# Re-exports that don't need the keys list
iso_now = _cfp.iso_now
has_step = _cfp.has_step
should_skip = _cfp.should_skip


def format(**updates):
    """Validate and format MIRI CFP keyword updates. See ``common.cfp.format``."""
    return _cfp.format(updates, CFP_KEYS, CFP_COMMENTS)


def get_steps(path):
    """Return ``{key: value}`` for every MIRI CFP key present on ``path``."""
    return _cfp.get_steps(path, CFP_KEYS)


def clear_from(path, key):
    """Atomically remove ``key`` and every later MIRI CFP key from ``path``."""
    return _cfp.clear_from(path, key, CFP_KEYS)
