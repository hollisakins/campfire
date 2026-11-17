"""Local-first writes for pulled review annotations.

The files under ``$CAMPFIRE_ROOT/reference/`` are local configuration: they are
hand-edited as well as materialized from the web review tables. A pull therefore
never overwrites or deletes what is already on disk. It creates files and entries
that are missing locally, leaves identical ones alone, and keeps (and reports) a
local file or entry that disagrees with the DB. Local config reaches the DB the
other way, on deploy (``annotation_push``).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

WRITTEN, UNCHANGED, KEPT_LOCAL, REPLACED = 'written', 'unchanged', 'kept_local', 'replaced'

# A region file the pipeline wrote records a hash of its own content lines in
# this header line (cfpipe's HST `trails` step, campfire_pipeline/hst/steps/
# trails.py, which writes satellite trails as a preliminary mask). The hash is
# sha256 over the content lines joined by newlines, first 16 hex digits; both
# sides must compute it the same way.
GENERATED_HASH_MARK = '# trails-body-sha256:'


def body_lines(text: str) -> list[str]:
    """Content lines of a text config: comment (``#``) and blank lines dropped, so
    regenerated headers and timestamps do not count as a difference."""
    return [ln.rstrip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith('#')]


def is_unedited_generated(text: str) -> bool:
    """Whether a file is one the pipeline generated and nobody has edited since:
    it carries the pipeline's hash line, and its content lines still hash to it.

    Such a file is not local configuration yet. It is the pipeline's first guess
    (a satellite trail it found), stored in the same place as a reviewer's masks
    so that it is applied and uploaded like one; once a reviewer changes the mask
    in the portal, that version is the one to use."""
    for line in text.splitlines():
        if line.startswith(GENERATED_HASH_MARK):
            digest = hashlib.sha256('\n'.join(body_lines(text)).encode()).hexdigest()[:16]
            return line[len(GENERATED_HASH_MARK):].strip() == digest
    return False


def write_if_absent(path: Path, content: str, *, dry_run: bool = False,
                    replace_unedited: bool = False) -> str:
    """Write ``content`` to ``path`` only when no file is there.

    Returns ``WRITTEN`` for a new file, ``UNCHANGED`` when the existing file has the
    same content lines, and ``KEPT_LOCAL`` when it differs (the local file wins).

    With ``replace_unedited``, a differing file that the pipeline generated and
    nobody edited (:func:`is_unedited_generated`) is replaced instead, and the
    result is ``REPLACED``. A file anyone has touched by hand is still kept."""
    path = Path(path)
    if path.exists():
        existing = path.read_text()
        if body_lines(existing) == body_lines(content):
            return UNCHANGED
        if not (replace_unedited and is_unedited_generated(existing)):
            return KEPT_LOCAL
        if not dry_run:
            tmp = path.with_suffix(path.suffix + '.tmp')
            tmp.write_text(content)
            tmp.replace(path)
        return REPLACED
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(content)
        tmp.replace(path)
    return WRITTEN
