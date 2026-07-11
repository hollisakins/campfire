"""Push planning — the local→cloud differ.

The pull direction has always had a differ (``LocalStore.get_pending_objects``:
a cloud row is pending when no local hash matches it). This module is its
mirror image: given locally-discovered upload tasks and the server registry's
rows for those keys, classify each file as **new** (no active cloud object),
**changed** (content identity differs), or **unchanged** (skip the upload).

Content identity is per-product-type, not per-direction:

* NIRCam canonical exposures compare on the science-only ``sci_dq_hash`` —
  whole-file hashes churn on every pipeline re-save (header timestamps), so
  they cannot drive dedup (epic #261, D1).
* Everything else compares on the whole-file ``content_hash``. Only an
  authoritative ``sha256:`` value is comparable; a provisional ``etag:`` row
  (bucket-metadata backfill) always re-uploads.

The stat fast path: when the local mirror's ``pushed_*`` bookkeeping records
that this same file (mtime+size unchanged) was already confirmed against the
same server identity, the file is skipped **without reading it** — the
rsync-style short-circuit that keeps a no-op re-push at directory-walk cost
instead of a full re-hash of hundreds of GB. Files are only hashed when their
stat changed, and then only to answer "did the science change or was this a
re-save". The pipeline's own manifest logic (``file_unchanged``) uses the same
idiom at reduction time.

This module is pure bookkeeping — no Supabase, no network. The deploy-side
wrapper (``campfire.deploy.push``) fetches the server rows for the candidate
keys and applies the plan's bookkeeping to the local mirror.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .hashing import hash_files_parallel, sci_dq_hashes_parallel

# Product types whose push identity is the science-only SCI+DQ+CFMASK digest.
IDENTITY_SCI_DQ_TYPES = frozenset({'nircam_exposure'})

# mtime comparison tolerance (matches verify_local_objects).
_MTIME_TOL = 1e-3


def identity_kind_for_key(storage_key: str) -> str:
    """Return ``'sci_dq'`` or ``'whole_file'`` for a storage key.

    Unknown/unparseable keys fall back to whole-file identity (the
    conservative choice: whole-file mismatches always re-upload).
    """
    try:
        from campfire_layout import parse_key
        parsed = parse_key(storage_key)
        product_type = parsed.product_type
    except Exception:
        return 'whole_file'
    return 'sci_dq' if product_type in IDENTITY_SCI_DQ_TYPES else 'whole_file'


def server_identity_for(row: Optional[dict], kind: str) -> Optional[str]:
    """The comparable cloud-side identity for a registry row, or None.

    None means "no comparable identity" — the caller must treat the file as
    changed (always upload): a missing/inactive row, a provisional ``etag:``
    whole-file hash, or a legacy exposure row without a science digest.
    """
    if not row or row.get('status') != 'active':
        return None
    if kind == 'sci_dq':
        h = row.get('sci_dq_hash')
    else:
        h = row.get('content_hash')
    if h and h.startswith('sha256:'):
        return h
    return None


@dataclass
class PushPlan:
    """Classification of upload tasks against the cloud registry.

    ``to_upload`` preserves the input task order (new + changed interleaved).
    ``identities`` maps ``r2_key`` → the local content identity for every task
    that was hashed at plan time (uploads whose identity was needed for the
    decision); ``whole_file`` carries ``(sha256, size)`` for plan-hashed
    whole-file tasks so registration never re-reads them. ``confirmed``
    lists ``(r2_key, identity, mtime, size)`` for identity-verified unchanged
    files — the caller records these in the mirror so the next run takes the
    stat fast path.
    """

    to_upload: List = field(default_factory=list)
    new: List = field(default_factory=list)
    changed: List = field(default_factory=list)
    unchanged: List = field(default_factory=list)
    missing: List = field(default_factory=list)
    fast_skipped: int = 0
    identities: Dict[str, Optional[str]] = field(default_factory=dict)
    whole_file: Dict[str, Tuple[str, int]] = field(default_factory=dict)
    stats: Dict[str, Tuple[float, int]] = field(default_factory=dict)
    confirmed: List[Tuple[str, str, float, int]] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{len(self.new)} new",
            f"{len(self.changed)} changed",
            f"{len(self.unchanged)} unchanged",
        ]
        if self.fast_skipped:
            parts.append(f"{self.fast_skipped} via stat fast-path")
        if self.missing:
            parts.append(f"{len(self.missing)} missing locally")
        return ", ".join(parts)


def plan_push(
    tasks,
    server_rows: Dict[str, dict],
    *,
    local_rows: Optional[Dict[str, dict]] = None,
    max_workers: Optional[int] = None,
    progress: bool = True,
) -> PushPlan:
    """Classify ``tasks`` (UploadTask-shaped) against the server registry.

    Parameters
    ----------
    tasks
        Locally-discovered candidates (``.local_path`` / ``.r2_key`` /
        ``.content_type``).
    server_rows
        ``{storage_key: row}`` fetched from the **server** registry for these
        keys (authoritative — never a possibly-stale full mirror). Rows carry
        ``status`` / ``content_hash`` / ``sci_dq_hash``.
    local_rows
        ``{storage_key: row}`` from the local mirror, supplying the
        ``pushed_*`` stat fast-path bookkeeping. Optional: without it every
        candidate with a comparable server identity is hashed (still correct,
        just slower).
    """
    plan = PushPlan()
    local_rows = local_rows or {}

    # (task, kind, server_identity, stat) needing a local hash to decide.
    undecided: List[Tuple] = []

    for task in tasks:
        key = task.r2_key
        path = Path(task.local_path)
        try:
            st = os.stat(path)
        except OSError:
            plan.missing.append(task)
            continue
        plan.stats[key] = (st.st_mtime, st.st_size)

        kind = identity_kind_for_key(key)
        row = server_rows.get(key)
        identity = server_identity_for(row, kind)

        if identity is None:
            # No comparable cloud identity: brand-new object, inactive row, or
            # provisional/absent digest → always upload.
            if row and row.get('status') == 'active':
                plan.changed.append(task)
            else:
                plan.new.append(task)
            plan.to_upload.append(task)
            continue

        # Stat fast path: this machine already confirmed this exact file
        # (mtime+size) against this exact server identity — skip without
        # reading a byte.
        lrow = local_rows.get(key)
        if (
            lrow
            and lrow.get('pushed_identity') == identity
            and lrow.get('pushed_mtime') is not None
            and lrow.get('pushed_size') is not None
            and abs(st.st_mtime - lrow['pushed_mtime']) < _MTIME_TOL
            and st.st_size == lrow['pushed_size']
        ):
            plan.unchanged.append(task)
            plan.fast_skipped += 1
            continue

        undecided.append((task, kind, identity, st))

    # Hash the undecided files (stat changed, or no fast-path record), grouped
    # by identity kind so each file is read once with the right reader.
    sci_dq_paths = [Path(t.local_path) for t, k, _i, _s in undecided if k == 'sci_dq']
    whole_paths = [Path(t.local_path) for t, k, _i, _s in undecided if k == 'whole_file']

    sci_dq_local = sci_dq_hashes_parallel(
        sci_dq_paths, max_workers=max_workers,
        progress_desc='Hashing exposures' if progress else None,
    ) if sci_dq_paths else {}
    whole_local = hash_files_parallel(
        whole_paths, max_workers=max_workers) if whole_paths else {}

    for task, kind, identity, st in undecided:
        key = task.r2_key
        path = Path(task.local_path)
        if kind == 'sci_dq':
            local_identity = sci_dq_local.get(path)
        else:
            local_identity, size = whole_local[path]
            plan.whole_file[key] = (local_identity, size)
        plan.identities[key] = local_identity

        if local_identity is not None and local_identity == identity:
            # Science-identical (or byte-identical) — skip the upload and
            # record the fresh stat so the next run takes the fast path.
            plan.unchanged.append(task)
            plan.confirmed.append((key, local_identity, st.st_mtime, st.st_size))
        else:
            plan.changed.append(task)
            plan.to_upload.append(task)

    return plan
