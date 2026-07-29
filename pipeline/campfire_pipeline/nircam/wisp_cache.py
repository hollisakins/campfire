"""Fetch + cache NIRCam wisp templates from a public HTTPS endpoint.

Wisp templates are large (~2.5 GB total) reference data that used to live in the
user-supplied ``$CAMPFIRE_ROOT`` reference tree. Forgetting to copy them onto a
new reduction machine silently produced mosaics with *no* wisp subtraction (step
enabled, no templates found, quiet skip). They now travel via a checksummed
manifest shipped with the package (``data/wisp_manifest.toml``): the pipeline
fetches each needed template once from a public, anonymous-HTTPS endpoint into
``$CAMPFIRE_ROOT/cache/wisps/``, verifies its sha256, and hard-fails if a
listed template is missing or unfetchable.

The generic fetch/verify/cache machinery lives in ``common/ref_cache.py``
(extracted from here — M1 of the spike-masking build plan); this module is the
wisp-specific policy: template naming, the wisp detector set, and what
"required" means.

Manifest semantics (mirrored in ``data/wisp_manifest.toml``):
  * A ``(detector, filter)`` whose 4 templates ARE listed but are missing on
    disk and cannot be fetched  -> ``WispTemplateError`` (never a silent skip).
  * A ``(detector, filter)`` NOT listed  -> "no wisp template characterized";
    ``required_templates`` returns ``[]`` and the step stamps a visible
    ``CFP_WISP='skipped (no template)'`` rather than pretending it subtracted.
"""

import functools
from pathlib import Path

from campfire_pipeline.common import ref_cache


# Detectors that carry wisps and the four smoothing variants each must have.
# Kept in lockstep with steps/wisp.py:WISP_DETECTORS and scripts/build_wisp_manifest.py.
WISP_DETECTORS = {'nrca3', 'nrca4', 'nrcb3', 'nrcb4'}
_VARIANT_SUFFIXES = (
    'masked',
    'masked_smoothed_1x1',
    'masked_smoothed_2x2',
    'masked_smoothed_3x3',
)

_MANIFEST_PATH = Path(__file__).resolve().parent.parent / 'data' / 'wisp_manifest.toml'


class WispTemplateError(ref_cache.RefCacheError):
    """A required wisp template is missing and could not be fetched."""


@functools.lru_cache(maxsize=1)
def _manifest():
    """Load and index the shipped manifest once: ``(base_url, {name: (sha, bytes)})``."""
    return ref_cache.load_manifest(_MANIFEST_PATH, 'template')


# The lambda resolves ``_manifest`` from module globals at call time, so tests
# that monkeypatch ``wisp_cache._manifest`` keep working.
_ENGINE = ref_cache.RefCache(
    'wisps', lambda: _manifest(),
    error_cls=WispTemplateError,
    log_prefix='wisp',
    log_noun='wisp template',
    no_base_url_hint=(
        f"The manifest ({_MANIFEST_PATH.name}) is unconfigured — publish "
        "templates and regenerate it with scripts/build_wisp_manifest.py."),
)


def build_names(detector, filtname):
    """The 4 canonical template filenames for a ``(detector, filter)`` pair.

    Constructed identically to steps/wisp.py so the two never drift. Returns the
    names regardless of whether they exist in the manifest.
    """
    det = detector.upper()
    filt = filtname.upper()
    return [f'WISP_{det}_{filt}_CLEAR_{suffix}.fits' for suffix in _VARIANT_SUFFIXES]


def required_templates(detector, filtname):
    """Template filenames the manifest says SHOULD exist for this pair.

    Returns the 4 canonical names if the pair is characterized in the manifest,
    else ``[]`` (a legitimate "no template for this filter" — the step stamps a
    visible skip). The builder guarantees all-4-or-none, so testing the first
    name is sufficient.
    """
    _, templates = _manifest()
    names = build_names(detector, filtname)
    return names if names[0] in templates else []


def cache_dir():
    """``$CAMPFIRE_ROOT/cache/wisps/``, created on demand."""
    return _ENGINE.cache_dir()


def resolve(name, legacy_dir=None):
    """Absolute path to template ``name`` if already present, else ``None``.

    Resolution order: the fetch cache first, then the legacy user-supplied
    reference directory (``field.wisp_dir``) if given — so machines that already
    have templates copied there keep working with zero disruption and no fetch.
    """
    return _ENGINE.resolve(name, legacy_dir)


def ensure(names, legacy_dir=None):
    """Ensure every template in ``names`` is present locally, fetching if needed.

    See ``ref_cache.RefCache.ensure`` for the shared semantics (trust present
    files, per-file locking, loud failure on listed-but-unfetchable). Raises
    ``WispTemplateError``; returns the number of files actually downloaded.
    """
    return _ENGINE.ensure(names, legacy_dir=legacy_dir)


def ensure_for_pairs(pairs, legacy_dir=None):
    """Fetch all manifest-listed templates for a set of ``(detector, filter)``.

    ``pairs`` is any iterable of ``(detector, filter)`` tuples (case-insensitive).
    Pairs on non-wisp detectors, or not characterized in the manifest, contribute
    nothing. Returns the number of files actually downloaded.
    """
    names = []
    seen = set()
    for detector, filtname in pairs:
        if detector.lower() not in WISP_DETECTORS:
            continue
        for name in required_templates(detector, filtname):
            if name not in seen:
                seen.add(name)
                names.append(name)
    if not names:
        return 0
    return ensure(names, legacy_dir=legacy_dir)
