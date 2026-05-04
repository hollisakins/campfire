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

The translation from ``git describe`` to PEP 440:

    pipeline-v0.4.0                    -> 0.4.0
    pipeline-v0.4.0-3-g7f4e2c1         -> 0.4.1.dev3+g7f4e2c1
    pipeline-v0.4.0-3-g7f4e2c1-dirty   -> 0.4.1.dev3+g7f4e2c1.d20260504
    pipeline-v0.4.0-0-g7f4e2c1-dirty   -> 0.4.0+d20260504
    (no matching tag yet)              -> 0.0.0.dev0+g7f4e2c1[.d20260504]
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import campfire_pipeline


_DESCRIBE_RE = re.compile(
    r'^pipeline-v(?P<base>\d+\.\d+\.\d+)'
    r'(?:-(?P<distance>\d+)-g(?P<sha>[0-9a-f]+))?'
    r'(?P<dirty>-dirty)?$'
)


def _repo_root() -> Path:
    # campfire_pipeline/__init__.py lives at <repo>/pipeline/campfire_pipeline/
    return Path(campfire_pipeline.__file__).resolve().parent.parent.parent


def _run_git(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ['git', *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _bump_patch(base: str) -> str:
    major, minor, patch = base.split('.')
    return f"{major}.{minor}.{int(patch) + 1}"


def _today_local_segment() -> str:
    return datetime.now(timezone.utc).strftime('d%Y%m%d')


def _describe_to_pep440(described: str) -> str | None:
    m = _DESCRIBE_RE.match(described)
    if not m:
        return None

    base = m.group('base')
    distance = int(m.group('distance') or 0)
    sha = m.group('sha')
    dirty = bool(m.group('dirty'))

    if distance == 0:
        version = base
        local_parts: list[str] = []
    else:
        version = f"{_bump_patch(base)}.dev{distance}"
        local_parts = [f"g{sha}"]

    if dirty:
        local_parts.append(_today_local_segment())

    if local_parts:
        version = f"{version}+{'.'.join(local_parts)}"

    return version


def _git_version() -> str | None:
    repo = _repo_root()
    if not (repo / '.git').exists():
        return None

    described = _run_git(
        ['describe', '--tags', '--long', '--dirty', '--match', 'pipeline-v*'],
        repo,
    )
    if described:
        return _describe_to_pep440(described)

    # No matching tag yet — synthesize a 0.0.0.dev0 string from HEAD.
    sha = _run_git(['rev-parse', '--short=7', 'HEAD'], repo)
    if not sha:
        return None
    dirty_local = _today_local_segment() if _run_git(
        ['status', '--porcelain', '--', 'pipeline'], repo
    ) else None
    local = f"g{sha}" + (f".{dirty_local}" if dirty_local else "")
    return f"0.0.0.dev0+{local}"


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
