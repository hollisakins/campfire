"""Resolve the campfire-pipeline version string for output provenance.

Resolution order (first that succeeds wins):

1. ``config['pipeline']['version']`` override — escape hatch for ad-hoc
   tagged runs (e.g. ``"experimental-bkg-tweak"``).
2. Live ``git describe`` scoped to ``pipeline-v*`` tags, translated into
   PEP 440. Used during local development from a checkout.
3. ``campfire_pipeline._version.version`` — written at install time by
   setuptools-scm. Used when installed without git metadata available.
4. ``importlib.metadata.version("campfire-pipeline")`` — last-resort
   lookup against installed package metadata.
5. ``"0.0.0+unknown"`` — sentinel.

Both the **distance** (``.devN``) and the **dirty flag** are scoped to the
``pipeline/`` subtree. ``git describe --long``'s commit count and ``git
describe --dirty``'s working-tree check both span the whole monorepo, so
unrelated ``web/``, ``python/``, ``supabase/``, and ``scripts/`` activity
would otherwise bump the version of bit-identical pipeline code. We instead
use ``git describe --abbrev=0`` to get just the tag, ``git rev-list
<tag>..HEAD --count -- pipeline`` for distance, and ``git status
--porcelain -- pipeline`` for the dirty check.

    tag pipeline-v0.4.0, 0 pipeline commits since, clean -> 0.4.0
    tag pipeline-v0.4.0, 3 pipeline commits since, clean -> 0.4.1.dev3+g7f4e2c1
    tag pipeline-v0.4.0, 3 pipeline commits since, dirty -> 0.4.1.dev3+g7f4e2c1.d20260504
    tag pipeline-v0.4.0, 0 pipeline commits since, dirty -> 0.4.0+d20260504
    (no matching tag yet)                                -> 0.0.0.dev0+g7f4e2c1[.d20260504]

**Failure is not an answer** (#463). All git probes of one resolution share
a single wall-clock budget (``_GIT_TIME_BUDGET`` — a deadline, so a hung git
stalls startup once, not once per probe), and under NFS contention any of
them can time out. A timeout (or a missing
``git`` binary) must never be mistaken for a real answer — historically a
timed-out ``rev-list`` collapsed the distance to 0 and stamped a bare release
string (e.g. ``0.5.1``) onto data actually built 178 commits past the tag,
which ``campfire deploy``'s release check then waved through silently. Now any
call that *cannot answer* (as opposed to answering "no") marks the resolution
unresolved: the result keeps whatever was reliably determined, always carries
a local segment ending in ``unresolved``, and therefore always fails deploy's
``_is_release_version()`` and trips its warn-and-confirm path.

    tag ok, distance timed out                 -> 0.4.0+g7f4e2c1.unresolved
    describe timed out                         -> 0.0.0.dev0+g7f4e2c1.unresolved
    everything timed out                       -> 0.0.0.dev0+unresolved
    tag+distance ok, dirty check timed out     -> 0.4.1.dev3+g7f4e2c1.unresolved

Resolution is cached per process (``lru_cache``), so every product a process
writes carries the same string and the git cost is paid once.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import campfire_pipeline

logger = logging.getLogger(__name__)


_DESCRIBE_RE = re.compile(
    r'^pipeline-v(?P<base>\d+\.\d+\.\d+)'
    r'(?:-(?P<distance>\d+)-g(?P<sha>[0-9a-f]+))?$'
)
_TAG_RE = re.compile(r'^pipeline-v(?P<base>\d+\.\d+\.\d+)$')


def _repo_root() -> Path:
    # campfire_pipeline/__init__.py lives at <repo>/pipeline/campfire_pipeline/
    return Path(campfire_pipeline.__file__).resolve().parent.parent.parent


class _GitUnavailable(Exception):
    """git could not answer — timed out or the binary is missing.

    Distinct from a nonzero exit (git *answering* "no", e.g. ``describe``
    finding no matching tag), which ``_run_git`` reports as ``None``.
    """


# Total wall-clock budget shared by ALL git probes of one resolution — a
# deadline, not a per-call timeout, so a hung git costs at most this much
# even though up to four probes run. Generous because the result is cached
# per process, so this is a once-per-run cap, and blowing it permanently
# degrades the CMPFRVER provenance for everything the process writes
# (#463: git on contended NFS blew a 5 s budget).
_GIT_TIME_BUDGET = 30


def _run_git(args: list[str], cwd: Path, deadline: float | None = None) -> str | None:
    if deadline is None:
        timeout: float = _GIT_TIME_BUDGET
    else:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise _GitUnavailable(f"git {args[0]}: shared git deadline exhausted")
    try:
        result = subprocess.run(
            ['git', *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError:
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise _GitUnavailable(f"git {args[0]}: {exc}") from exc
    return result.stdout.strip() or None


def _bump_patch(base: str) -> str:
    major, minor, patch = base.split('.')
    return f"{major}.{minor}.{int(patch) + 1}"


def _today_local_segment() -> str:
    return datetime.now(timezone.utc).strftime('d%Y%m%d')


def _build_pep440(
    base: str,
    distance: int | None,
    sha: str | None,
    dirty: bool,
    unresolved: bool = False,
) -> str:
    if distance:
        version = f"{_bump_patch(base)}.dev{distance}"
    else:
        # distance 0 (exactly at the tag) or None (git could not answer;
        # only reachable with unresolved=True — never bump on a guess).
        version = base

    # The sha is normally only stamped alongside a nonzero distance, but an
    # unresolved version needs it regardless: it is the one field that still
    # pins the code exactly when distance/dirtiness are unknown.
    local_parts = [f"g{sha}"] if sha and (distance or unresolved) else []

    if dirty:
        local_parts.append(_today_local_segment())

    if unresolved:
        local_parts.append('unresolved')

    if local_parts:
        version = f"{version}+{'.'.join(local_parts)}"

    return version


def _describe_to_pep440(described: str, dirty: bool) -> str | None:
    """Translate a ``git describe --long`` string into a PEP 440 version.

    Retained as a pure parser/formatter for unit tests. The runtime path in
    :func:`_git_version` does not call this — distance there is recounted
    scoped to ``pipeline/``, which the raw ``describe --long`` count cannot
    express.
    """
    m = _DESCRIBE_RE.match(described)
    if not m:
        return None
    return _build_pep440(
        m.group('base'),
        int(m.group('distance') or 0),
        m.group('sha'),
        dirty,
    )


def _pipeline_dirty(repo: Path, deadline: float | None = None) -> bool:
    # `git describe --dirty` checks the entire working tree; we only care
    # about edits to pipeline/ since that's what the version string scopes.
    # Raises _GitUnavailable on timeout — a tree of unknown state must not
    # be stamped clean.
    return bool(_run_git(['status', '--porcelain', '--', 'pipeline'], repo, deadline))


def _pipeline_distance(repo: Path, tag: str, deadline: float | None = None) -> int | None:
    """Commits between *tag* and HEAD that touched ``pipeline/``.

    ``git describe --long`` reports the whole-monorepo count, which would
    bump ``.devN`` for any ``web/`` or ``python/`` commit even when pipeline
    code is byte-identical to *tag*. ``rev-list -- pipeline`` reports the
    count we actually want stamped onto reduced data.

    Returns ``None`` when git answered but the output is not a count;
    raises :class:`_GitUnavailable` when git could not answer at all.
    Neither must ever be collapsed to 0 — that is the false-release bug
    of #463.
    """
    out = _run_git(
        ['rev-list', f'{tag}..HEAD', '--count', '--', 'pipeline'], repo, deadline,
    )
    if out is None or not out.isdigit():
        return None
    return int(out)


def _git_version() -> str | None:
    repo = _repo_root()
    if not (repo / '.git').exists():
        return None

    # Each git query distinguishes "answered no" (proceed on that answer) from
    # "could not answer" (_GitUnavailable: timeout, missing binary). The latter
    # marks the whole resolution unresolved — we keep whatever was reliably
    # determined, but the result must never look like a clean release (#463).
    # All probes share one deadline: a hung git costs _GIT_TIME_BUDGET total,
    # not per probe — once the budget is gone, remaining probes fail instantly.
    unresolved = False
    deadline = time.monotonic() + _GIT_TIME_BUDGET

    try:
        tag = _run_git(
            ['describe', '--tags', '--abbrev=0', '--match', 'pipeline-v*'],
            repo,
            deadline,
        )
    except _GitUnavailable:
        tag = None
        unresolved = True

    base = None
    if tag is not None:
        m = _TAG_RE.match(tag)
        if not m:
            return None
        base = m.group('base')

    distance: int | None = None
    if base is not None:
        try:
            distance = _pipeline_distance(repo, tag, deadline)
        except _GitUnavailable:
            distance = None
        if distance is None:
            # Timed out, or rev-list answered garbage — either way the
            # distance is unknown, not zero.
            unresolved = True

    try:
        dirty = _pipeline_dirty(repo, deadline)
    except _GitUnavailable:
        dirty = False
        unresolved = True

    # The sha is needed for a nonzero distance, for the no-tag synthetic
    # version, and for any unresolved result (it alone still pins the code).
    sha = None
    if distance or unresolved or base is None:
        try:
            sha = _run_git(['rev-parse', '--short=7', 'HEAD'], repo, deadline)
        except _GitUnavailable:
            sha = None
            unresolved = True

    if base is not None:
        version = _build_pep440(base, distance, sha, dirty, unresolved=unresolved)
    elif not sha and not unresolved:
        # Genuinely no matching tag and no sha to synthesize from — fall
        # through to the packaged version.
        return None
    else:
        # No matching tag yet (or describe couldn't answer) — synthesize a
        # 0.0.0.dev0 string from whatever survived.
        local_parts = [f"g{sha}"] if sha else []
        if dirty:
            local_parts.append(_today_local_segment())
        if unresolved:
            local_parts.append('unresolved')
        version = '0.0.0.dev0'
        if local_parts:
            version = f"{version}+{'.'.join(local_parts)}"

    if unresolved:
        logger.warning(
            "git could not fully resolve the pipeline version (timeout or "
            "missing git); stamping degraded provenance %r", version,
        )
    return version


def _packaged_version() -> str | None:
    # setuptools-scm writes this at install time; absent in editable installs
    # without a build step.
    try:
        from campfire_pipeline._version import version as scm_version  # type: ignore[import-not-found]
        return scm_version
    except ImportError:
        pass

    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("campfire-pipeline")
        except PackageNotFoundError:
            return None
    except ImportError:
        return None


@lru_cache(maxsize=1)
def _resolved_version() -> str:
    return _git_version() or _packaged_version() or "0.0.0+unknown"


def get_reduction_version(config: dict | None = None) -> str:
    """Return the pipeline version string to embed in output FITS headers.

    See module docstring for the full resolution order.

    Parameters
    ----------
    config : dict, optional
        Pipeline config. If ``config['pipeline']['version']`` is set,
        that string is returned verbatim (escape hatch for ad-hoc tagged runs).
    """
    if config is not None:
        override = (config.get('pipeline') or {}).get('version')
        if override:
            return override
    return _resolved_version()
