"""Resolve the reduction version string embedded in pipeline outputs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import campfire_pipeline


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


def _repo_root() -> Path:
    # campfire_pipeline/__init__.py lives at <repo>/pipeline/campfire_pipeline/
    return Path(campfire_pipeline.__file__).resolve().parent.parent.parent


def _git_version() -> str | None:
    repo = _repo_root()
    if not (repo / '.git').exists():
        return None
    commit = _run_git(['rev-parse', '--short=12', 'HEAD'], repo)
    if not commit:
        return None
    dirty = _run_git(['status', '--porcelain'], repo)
    return f'{commit}-dirty' if dirty else commit


def get_reduction_version(config: dict | None = None) -> str:
    """Return the reduction version string to embed in pipeline outputs.

    Resolution order:
    1. ``config['pipeline']['version']`` if explicitly set (escape hatch).
    2. Short git commit hash of the campfire repo, with ``-dirty`` suffix
       when the working tree has uncommitted changes.
    3. Fallback ``v{__version__}-nogit`` when git metadata is unavailable
       (e.g., installed from an sdist).
    """
    if config is not None:
        override = config.get('pipeline', {}).get('version')
        if override:
            return override

    git_ver = _git_version()
    if git_ver:
        return git_ver

    return f'v{campfire_pipeline.__version__}-nogit'
