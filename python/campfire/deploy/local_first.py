"""Local-first writes for pulled review annotations.

The files under ``$CAMPFIRE_ROOT/reference/`` are local configuration: they are
hand-edited as well as materialized from the web review tables. A pull therefore
never overwrites or deletes what is already on disk. It creates files and entries
that are missing locally, leaves identical ones alone, and keeps (and reports) a
local file or entry that disagrees with the DB. Local config reaches the DB the
other way, on deploy (``annotation_push``).
"""
from __future__ import annotations

from pathlib import Path

WRITTEN, UNCHANGED, KEPT_LOCAL = 'written', 'unchanged', 'kept_local'


def body_lines(text: str) -> list[str]:
    """Content lines of a text config: comment (``#``) and blank lines dropped, so
    regenerated headers and timestamps do not count as a difference."""
    return [ln.rstrip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith('#')]


def write_if_absent(path: Path, content: str, *, dry_run: bool = False) -> str:
    """Write ``content`` to ``path`` only when no file is there.

    Returns ``WRITTEN`` for a new file, ``UNCHANGED`` when the existing file has the
    same content lines, and ``KEPT_LOCAL`` when it differs (the local file wins)."""
    path = Path(path)
    if path.exists():
        same = body_lines(path.read_text()) == body_lines(content)
        return UNCHANGED if same else KEPT_LOCAL
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(content)
        tmp.replace(path)
    return WRITTEN
