"""Generic manifest-driven fetch + cache engine for large reference data.

Extracted from ``nircam/wisp_cache.py`` (M1 of the spike-masking build plan,
``docs/design-nircam-spike-masking.md`` §3.4) so the wisp templates and the
spike footprint models share one engine instead of forked copies.
``wisp_cache`` is now a thin wrapper over this module.

A dataset plugs in three things: a packaged TOML manifest (``base_url`` +
per-file ``sha256``/``bytes``), a registered ``campfire_layout`` cache kind,
and an error class. The engine provides the shared semantics the wisp layer
established:

* fetch by plain anonymous-HTTPS ``urllib`` GET of ``{base_url}/{name}`` —
  no login, no cloud credentials, no storage keys;
* stream to a sibling ``.part`` temp file, verify size + sha256 against the
  manifest, then ``os.replace`` into place — a file present in the cache is
  always a complete, verified download, so present files are trusted and
  never re-hashed;
* per-file advisory lock so concurrent runs don't double-fetch or read a
  half-written file;
* **fail loud**: listed-but-unfetchable raises; a name missing from the
  manifest is the *caller's* decision to surface (the engine skips it with a
  visible log line — what "required" means is dataset policy, not engine
  policy).
"""

import hashlib
import os
import tempfile
import time
import urllib.error
import urllib.request

from campfire_layout import cache_path

from campfire_pipeline.common.io import log

_DOWNLOAD_RETRIES = 3
_HTTP_TIMEOUT = 120  # seconds per read


class RefCacheError(RuntimeError):
    """A manifest-listed reference file is missing and could not be fetched."""


def load_manifest(path, entry_table):
    """Load a manifest TOML: ``(base_url, {name: (sha256, bytes)})``.

    ``entry_table`` is the array-of-tables key (``"template"`` for wisps,
    ``"model"`` for spike models). A malformed or unconfigured manifest (empty
    ``base_url`` / no entries) loads fine — the emptiness is surfaced later,
    loudly, only if a fetch is actually needed.
    """
    import toml
    data = toml.load(path)
    base_url = (data.get('base_url') or '').rstrip('/')
    entries = {}
    for entry in data.get(entry_table, []):
        entries[entry['name']] = (entry.get('sha256'), entry.get('bytes'))
    return base_url, entries


class RefCache:
    """One dataset's fetch+cache policy bound to the shared engine.

    ``manifest_fn`` is a zero-arg callable returning
    ``(base_url, {name: (sha256, bytes)})`` — a callable rather than a value
    so wrappers can keep a monkeypatchable module-level loader and the
    manifest is only read when actually consulted.
    """

    def __init__(self, cache_kind, manifest_fn, *, error_cls=RefCacheError,
                 log_prefix='ref', log_noun='reference file',
                 no_base_url_hint=''):
        self._cache_kind = cache_kind
        self._manifest_fn = manifest_fn
        self._error_cls = error_cls
        self._log_prefix = log_prefix
        self._log_noun = log_noun
        self._no_base_url_hint = no_base_url_hint

    def cache_dir(self):
        """``$CAMPFIRE_ROOT/cache/<kind>/``, created on demand."""
        d = str(cache_path(self._cache_kind))
        os.makedirs(d, exist_ok=True)
        return d

    def resolve(self, name, legacy_dir=None):
        """Absolute path to ``name`` if already present, else ``None``.

        Resolution order: the fetch cache first, then a legacy user-supplied
        directory if given — machines with files already copied there keep
        working with zero disruption and no fetch.
        """
        cached = os.path.join(self.cache_dir(), name)
        if os.path.exists(cached):
            return cached
        if legacy_dir:
            legacy = os.path.join(legacy_dir, name)
            if os.path.exists(legacy):
                return legacy
        return None

    def ensure(self, names, legacy_dir=None):
        """Ensure every file in ``names`` is present locally, fetching if needed.

        Present files (cache or legacy) are trusted and skipped — the atomic,
        verified write in ``_download_one`` means a cached file was already
        checked. Names not in the manifest are skipped with a warning rather
        than erroring here — the caller's "required" gate decides what is
        genuinely required; this only fulfills a given list.

        Returns the number of files actually downloaded. Raises the dataset's
        error class if a manifest-listed file can't be fetched.
        """
        base_url, entries = self._manifest_fn()
        cdir = self.cache_dir()
        fetched = 0
        for name in names:
            if self.resolve(name, legacy_dir) is not None:
                continue
            if name not in entries:
                log(f"  {self._log_prefix}: {name} is not in the manifest; "
                    f"cannot fetch, skipping")
                continue
            sha, nbytes = entries[name]
            dest = os.path.join(cdir, name)
            with _file_lock(dest + '.lock'):
                # Double-check under the lock: another process may have just
                # fetched it.
                if os.path.exists(dest):
                    continue
                log(f"  fetching {self._log_noun} {name} …")
                self._download_one(name, dest, base_url, sha, nbytes)
                fetched += 1
        return fetched

    def _download_one(self, name, dest, base_url, expected_sha, expected_bytes):
        """Stream one file to ``dest`` atomically, verifying size + sha256.

        Retries transient HTTP/URL errors; raises on exhaustion or a checksum
        mismatch (the latter is not retried by url — a mismatch is
        deterministic).
        """
        if not base_url:
            hint = f" {self._no_base_url_hint}" if self._no_base_url_hint else ""
            raise self._error_cls(
                f"cannot fetch {name}: {self._log_prefix} manifest has no "
                f"base_url set.{hint}")

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
                    raise self._error_cls(
                        f"size mismatch for {name}: got {total} bytes, "
                        f"manifest says {expected_bytes}")
                digest = h.hexdigest()
                if expected_sha and digest != expected_sha:
                    raise self._error_cls(
                        f"checksum mismatch for {name}: got {digest}, "
                        f"manifest says {expected_sha}")
                os.replace(tmp, dest)
                return
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
                _unlink_quiet(tmp)
                if attempt < _DOWNLOAD_RETRIES:
                    log(f"  {self._log_prefix} fetch {name}: {e} (attempt "
                        f"{attempt}/{_DOWNLOAD_RETRIES}); retrying")
                    time.sleep(2 * attempt)
            except BaseException:
                _unlink_quiet(tmp)
                raise
        raise self._error_cls(
            f"failed to fetch {name} from {url} after {_DOWNLOAD_RETRIES} "
            f"attempts: {last_err}")


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


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
