"""Fetch + cache NIRCam wisp templates from a public HTTPS endpoint.

Wisp templates are large (~2.5 GB total) reference data that used to live in the
user-supplied ``$CAMPFIRE_ROOT`` reference tree. Forgetting to copy them onto a
new reduction machine silently produced mosaics with *no* wisp subtraction (step
enabled, no templates found, quiet skip). They now travel via a checksummed
manifest shipped with the package (``data/wisp_manifest.toml``): the pipeline
fetches each needed template once from a public, anonymous-HTTPS endpoint into
``$CAMPFIRE_ROOT/cache/wisps/``, verifies its sha256, and hard-fails if a
listed template is missing or unfetchable.

Deliberately independent of the campfire CLI / auth / storage-key machinery:
downloads are plain ``urllib`` GETs against ``base_url`` from the manifest — no
login, no cloud credentials, no ``campfire_layout`` storage key. The only new
runtime requirement is outbound HTTPS.

Manifest semantics (mirrored in ``data/wisp_manifest.toml``):
  * A ``(detector, filter)`` whose 4 templates ARE listed but are missing on
    disk and cannot be fetched  -> ``WispTemplateError`` (never a silent skip).
  * A ``(detector, filter)`` NOT listed  -> "no wisp template characterized";
    ``required_templates`` returns ``[]`` and the step stamps a visible
    ``CFP_WISP='skipped (no template)'`` rather than pretending it subtracted.
"""

import functools
import hashlib
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from campfire_layout import cache_path

from campfire_pipeline.common.io import log


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

_DOWNLOAD_RETRIES = 3
_HTTP_TIMEOUT = 120  # seconds per read; templates are ~16 MB each


class WispTemplateError(RuntimeError):
    """A required wisp template is missing and could not be fetched."""


@functools.lru_cache(maxsize=1)
def _manifest():
    """Load and index the shipped manifest once.

    Returns ``(base_url, {name: (sha256, bytes)})``. A malformed or unconfigured
    manifest (empty ``base_url`` / no templates) loads fine — the emptiness is
    surfaced later, loudly, only if a fetch is actually needed.
    """
    import toml
    data = toml.load(_MANIFEST_PATH)
    base_url = (data.get('base_url') or '').rstrip('/')
    templates = {}
    for entry in data.get('template', []):
        name = entry['name']
        templates[name] = (entry.get('sha256'), entry.get('bytes'))
    return base_url, templates


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
    d = str(cache_path('wisps'))
    os.makedirs(d, exist_ok=True)
    return d


def resolve(name, legacy_dir=None):
    """Absolute path to template ``name`` if already present, else ``None``.

    Resolution order: the fetch cache first, then the legacy user-supplied
    reference directory (``field.wisp_dir``) if given — so machines that already
    have templates copied there keep working with zero disruption and no fetch.
    """
    cached = os.path.join(cache_dir(), name)
    if os.path.exists(cached):
        return cached
    if legacy_dir:
        legacy = os.path.join(legacy_dir, name)
        if os.path.exists(legacy):
            return legacy
    return None


def _download_one(name, dest, base_url, expected_sha, expected_bytes):
    """Stream one template to ``dest`` atomically, verifying size + sha256.

    Downloads to a sibling ``.part`` temp file, checks byte count and sha256
    against the manifest, then ``os.replace`` into place — so a file that exists
    in the cache is always a complete, verified download. Retries transient
    HTTP/URL errors; raises ``WispTemplateError`` on exhaustion or a checksum
    mismatch (the latter is not retried by url — a mismatch is deterministic).
    """
    if not base_url:
        raise WispTemplateError(
            f"cannot fetch {name}: wisp manifest has no base_url set. The "
            f"manifest ({_MANIFEST_PATH.name}) is unconfigured — publish "
            "templates and regenerate it with scripts/build_wisp_manifest.py.")

    url = f'{base_url}/{name}'
    last_err = None
    for attempt in range(1, _DOWNLOAD_RETRIES + 1):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix='.part')
        h = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(fd, 'wb') as out:
                req = urllib.request.Request(
                    url, headers={'User-Agent': 'campfire-pipeline'})
                with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        h.update(chunk)
                        total += len(chunk)
                out.flush()
                os.fsync(out.fileno())

            if expected_bytes is not None and total != expected_bytes:
                raise WispTemplateError(
                    f"size mismatch for {name}: got {total} bytes, "
                    f"manifest says {expected_bytes}")
            digest = h.hexdigest()
            if expected_sha and digest != expected_sha:
                raise WispTemplateError(
                    f"checksum mismatch for {name}: got {digest}, "
                    f"manifest says {expected_sha}")
            os.replace(tmp, dest)
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            _unlink_quiet(tmp)
            if attempt < _DOWNLOAD_RETRIES:
                log(f"  wisp fetch {name}: {e} (attempt {attempt}/"
                    f"{_DOWNLOAD_RETRIES}); retrying")
                time.sleep(2 * attempt)
        except BaseException:
            _unlink_quiet(tmp)
            raise
    raise WispTemplateError(
        f"failed to fetch {name} from {url} after {_DOWNLOAD_RETRIES} "
        f"attempts: {last_err}")


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def ensure(names, legacy_dir=None):
    """Ensure every template in ``names`` is present locally, fetching if needed.

    Present files (cache or legacy) are trusted and skipped — the atomic,
    verified write in ``_download_one`` means a cached file was already checked,
    so re-hashing 2.5 GB every run is avoided. Missing ones are downloaded under
    a per-file lock so two concurrent ``cfpipe`` runs don't fetch the same file
    twice or read a half-written one.

    Names not present in the manifest are skipped with a warning rather than
    erroring here — ``required_templates`` is the gate that decides what is
    genuinely required; this function only fulfills a given list.

    Raises ``WispTemplateError`` if a manifest-listed template can't be fetched.
    """
    base_url, templates = _manifest()
    cdir = cache_dir()
    fetched = 0
    for name in names:
        if resolve(name, legacy_dir) is not None:
            continue
        if name not in templates:
            log(f"  wisp: {name} is not in the manifest; cannot fetch, skipping")
            continue
        sha, nbytes = templates[name]
        dest = os.path.join(cdir, name)
        with _file_lock(dest + '.lock'):
            # Double-check under the lock: another process may have just fetched it.
            if os.path.exists(dest):
                continue
            log(f"  fetching wisp template {name} …")
            _download_one(name, dest, base_url, sha, nbytes)
            fetched += 1
    return fetched


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


class _file_lock:
    """Minimal advisory lock via ``fcntl.flock`` on a lock file (POSIX)."""

    def __init__(self, path):
        self._path = path
        self._fd = None

    def __enter__(self):
        import fcntl
        self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
