"""Push planning — the local→cloud differ.

The pull direction has always had a differ (``LocalStore.get_pending_objects``:
a cloud row is pending when no local hash matches it). This module is its
mirror image: given locally-discovered upload tasks and the server registry's
rows for those keys, classify each file as **new** (no active cloud object),
**changed** (content identity differs), or **unchanged** (skip the upload).

Content identity is per-product-type, not per-direction:

* NIRCam canonical exposures compare on the **exposure identity**: the
  science-only ``sci_dq_hash`` *and* the astrometric ``wcs_hash``, both of which
  must match to skip an upload. Whole-file hashes churn on every pipeline
  re-save (header timestamps), so they cannot drive dedup (epic #261, D1) — but
  the science digest alone is blind to a re-alignment, which rewrites an
  exposure's WCS without touching one SCI or DQ pixel. Pixels *and* sky
  position together are what makes an exposure the exposure it is.
* Everything else compares on the whole-file ``content_hash``. Only an
  authoritative ``sha256:`` value is comparable; a provisional ``etag:`` row
  (bucket-metadata backfill) always re-uploads.

The stat fast path: when the local mirror's ``pushed_*`` bookkeeping records
that this same file (mtime+size unchanged) was already confirmed against the
same server identity, the file is skipped **without reading it** — the
rsync-style short-circuit that keeps a no-op re-push at directory-walk cost
instead of a full re-hash of hundreds of GB. Files are only hashed when their
stat changed, and then only to answer "did the science or the astrometry change,
or was this a re-save". The pipeline's own manifest logic (``file_unchanged``)
uses the same idiom at reduction time.

**Legacy exposure rows.** Registry rows written before ``wcs_hash`` existed
carry a science digest and a NULL astrometric one, so their exposure identity
is not directly comparable. Rather than re-upload an entire NIRCam corpus over
a slow link, those rows are *reconciled*: when the science digest still matches,
the local whole-file sha256 is compared against the row's authoritative
``content_hash``, and a byte-for-byte match proves the cloud copy carries this
file's WCS — so the upload is skipped and the row's ``wcs_hash`` is backfilled
(``PushPlan.backfill``). Anything that fails that proof uploads, which is the
correct outcome: it is exactly the re-aligned exposure the old identity missed.

This module is pure bookkeeping — no Supabase, no network. The deploy-side
wrapper (``campfire.deploy.push``) fetches the server rows for the candidate
keys, applies the plan's bookkeeping to the local mirror, and flushes the
``wcs_hash`` backfill to the registry.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .hashing import exposure_identities_parallel, hash_files_parallel

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


def compose_identity(sci_dq: Optional[str],
                     wcs: Optional[str]) -> Optional[str]:
    """The exposure identity token: science digest + astrometric digest.

    A single opaque string, because that is what both sides of the comparison
    store in one column (``storage_objects.pushed_identity`` on the local
    mirror). ``wcs=None`` composes to the bare science digest, so a file with no
    WCS component — and every ``pushed_identity`` recorded before this existed —
    keeps its old token and its stat fast path. None in, None out: an exposure
    with no readable science digest has no identity at all.
    """
    if not sci_dq:
        return None
    return f'{sci_dq}+wcs={wcs}' if wcs else sci_dq


def server_identity_for(row: Optional[dict], kind: str) -> Optional[str]:
    """The comparable cloud-side identity for a registry row, or None.

    None means "no comparable identity" — the caller must treat the file as
    changed (always upload): a missing/inactive row, a provisional ``etag:``
    whole-file hash, or a legacy exposure row without a science digest.

    For ``sci_dq`` rows this is the composed exposure identity. A row with a
    science digest but a NULL ``wcs_hash`` (written before the astrometric
    component existed) therefore composes to the bare science digest, which will
    not equal a local identity that *does* carry a WCS — see the reconciliation
    pass in :func:`plan_push`, which resolves those without re-uploading files
    that are provably unchanged.
    """
    if not row or row.get('status') != 'active':
        return None
    if kind == 'sci_dq':
        h = row.get('sci_dq_hash')
        if h and h.startswith('sha256:'):
            return compose_identity(h, row.get('wcs_hash'))
        return None
    h = row.get('content_hash')
    if h and h.startswith('sha256:'):
        return h
    return None


@dataclass
class PushPlan:
    """Classification of upload tasks against the cloud registry.

    ``to_upload`` preserves the input task order (new + changed interleaved).
    ``identities`` maps ``r2_key`` → the local **registerable** digest for every
    task hashed at plan time (the science-only digest for exposures, the
    whole-file sha256 otherwise), and ``wcs_hashes`` the matching astrometric
    digest for exposures — the two go into their own registry columns, so they
    are kept apart here rather than composed. ``whole_file`` carries
    ``(sha256, size)`` for plan-hashed whole-file tasks so registration never
    re-reads them. ``confirmed`` lists ``(r2_key, identity, mtime, size)`` for
    identity-verified unchanged files — the caller records these in the mirror
    so the next run takes the stat fast path — where ``identity`` is the
    *composed* token. ``backfill`` lists ``(r2_key, wcs_hash)`` for legacy rows
    proven unchanged by the reconciliation pass, for the caller to write back to
    the registry.
    """

    to_upload: List = field(default_factory=list)
    new: List = field(default_factory=list)
    changed: List = field(default_factory=list)
    unchanged: List = field(default_factory=list)
    missing: List = field(default_factory=list)
    fast_skipped: int = 0
    identities: Dict[str, Optional[str]] = field(default_factory=dict)
    wcs_hashes: Dict[str, Optional[str]] = field(default_factory=dict)
    whole_file: Dict[str, Tuple[str, int]] = field(default_factory=dict)
    stats: Dict[str, Tuple[float, int]] = field(default_factory=dict)
    confirmed: List[Tuple[str, str, float, int]] = field(default_factory=list)
    backfill: List[Tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{len(self.new)} new",
            f"{len(self.changed)} changed",
            f"{len(self.unchanged)} unchanged",
        ]
        if self.fast_skipped:
            parts.append(f"{self.fast_skipped} via stat fast-path")
        if self.backfill:
            # "reconciled", not "backfilled": the plan only determines these —
            # whether the registry write happens is the caller's call (a dry run
            # never writes). The actual write count is printed by the backfill.
            parts.append(f"{len(self.backfill)} legacy wcs_hash reconciled")
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
        ``status`` / ``content_hash`` / ``sci_dq_hash`` / ``wcs_hash``. The last
        is read only from here, never from the local mirror, so a reducer whose
        mirror predates it still plans correctly.
    local_rows
        ``{storage_key: row}`` from the local mirror, supplying the
        ``pushed_*`` stat fast-path bookkeeping. Optional: without it every
        candidate with a comparable server identity is hashed (still correct,
        just slower).
    """
    plan = PushPlan()
    local_rows = local_rows or {}

    # Verdicts are recorded per task index and emitted in input order at the
    # end, so the multi-pass classification below (hash, then reconcile legacy
    # rows) cannot disturb ``to_upload``'s documented ordering.
    verdict: Dict[int, str] = {}
    # (index, task, kind, server_identity, row, stat) needing a local hash.
    undecided: List[Tuple] = []
    ordered: List[Tuple[int, object]] = []

    for idx, task in enumerate(tasks):
        key = task.r2_key
        path = Path(task.local_path)
        try:
            st = os.stat(path)
        except OSError:
            plan.missing.append(task)
            continue
        ordered.append((idx, task))
        plan.stats[key] = (st.st_mtime, st.st_size)

        kind = identity_kind_for_key(key)
        row = server_rows.get(key)
        identity = server_identity_for(row, kind)

        if identity is None:
            # No comparable cloud identity: brand-new object, inactive row, or
            # provisional/absent digest → always upload.
            verdict[idx] = 'changed' if (row and row.get('status') == 'active') else 'new'
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
            verdict[idx] = 'unchanged'
            plan.fast_skipped += 1
            continue

        undecided.append((idx, task, kind, identity, row, st))

    # Hash the undecided files (stat changed, or no fast-path record), grouped
    # by identity kind so each file is read once with the right reader.
    exp_paths = [Path(t.local_path) for _i, t, k, _id, _r, _s in undecided
                 if k == 'sci_dq']
    whole_paths = [Path(t.local_path) for _i, t, k, _id, _r, _s in undecided
                   if k == 'whole_file']

    exp_local = exposure_identities_parallel(
        exp_paths, max_workers=max_workers,
        progress_desc='Hashing exposures (SCI+DQ+WCS)' if progress else None,
    ) if exp_paths else {}
    whole_local = hash_files_parallel(
        whole_paths, max_workers=max_workers,
        progress_desc='Hashing changed candidates' if progress else None,
    ) if whole_paths else {}

    # Legacy exposure rows that survive the identity comparison and need the
    # whole-file proof: (index, task, path, sci_dq, wcs, row, stat).
    reconcile: List[Tuple] = []

    for idx, task, kind, identity, row, st in undecided:
        key = task.r2_key
        path = Path(task.local_path)
        sci = wcs = None
        if kind == 'sci_dq':
            sci, wcs = exp_local.get(path, (None, None))
            plan.identities[key] = sci
            plan.wcs_hashes[key] = wcs
            local_identity = compose_identity(sci, wcs)
        else:
            local_identity, size = whole_local[path]
            plan.whole_file[key] = (local_identity, size)
            plan.identities[key] = local_identity

        if local_identity is not None and local_identity == identity:
            # Identical on every comparable component — skip the upload and
            # record the fresh stat so the next run takes the fast path.
            verdict[idx] = 'unchanged'
            plan.confirmed.append((key, local_identity, st.st_mtime, st.st_size))
            continue

        # A legacy row (science digest matches, no astrometric digest recorded)
        # is not evidence of a change — only of a row written before the WCS
        # was part of the identity. Defer it to the reconciliation pass rather
        # than re-uploading a whole corpus on the first post-upgrade push.
        if (
            kind == 'sci_dq'
            and wcs is not None
            and row is not None
            and not row.get('wcs_hash')
            and row.get('sci_dq_hash') == sci
        ):
            reconcile.append((idx, task, path, sci, wcs, row, st))
            continue

        verdict[idx] = 'changed'

    # Reconciliation: the cloud object's bytes ARE this local file's bytes iff
    # the whole-file sha256 matches the row's authoritative content_hash — which
    # proves the cloud copy carries this WCS, so the row can simply learn its
    # wcs_hash. Anything else uploads: a byte difference with no recorded
    # astrometry is unprovable, and is precisely the re-aligned exposure the
    # science-only identity used to skip.
    if reconcile:
        legacy_hashes = hash_files_parallel(
            [p for _i, _t, p, _sci, _w, _r, _s in reconcile],
            max_workers=max_workers,
            progress_desc='Reconciling legacy exposure rows' if progress else None,
        )
        for idx, task, path, sci, wcs, row, st in reconcile:
            key = task.r2_key
            whole, size = legacy_hashes[path]
            plan.whole_file[key] = (whole, size)
            if whole == row.get('content_hash'):
                verdict[idx] = 'unchanged'
                identity = compose_identity(sci, wcs)
                plan.confirmed.append((key, identity, st.st_mtime, st.st_size))
                plan.backfill.append((key, wcs))
            else:
                verdict[idx] = 'changed'

    for idx, task in ordered:
        v = verdict[idx]
        if v == 'unchanged':
            plan.unchanged.append(task)
            continue
        (plan.new if v == 'new' else plan.changed).append(task)
        plan.to_upload.append(task)

    return plan
