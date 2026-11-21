"""What a NIRSpec observation deploy will do, decided before it does any of it.

``campfire deploy --obs X`` builds one :class:`NirspecDeployPlan`, prints it,
and then either stops (``--dry-run``) or carries out that same plan. The file
list a dry run prints is the list a live run uploads, and the catalog section
names every table the live run writes, so the preview cannot fall behind the
deploy as products are added (it had: the 1-D spectrum sidecars, the
spectrum-exposure intermediates and the detector rate files were uploaded on
every deploy but never shown by ``--dry-run``).

This module holds the data model and the rendering only. Building a plan
(discovery, sidecar generation, the read-only registry check) and executing it
live in :mod:`campfire.deploy.deploy`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def format_bytes(n: int) -> str:
    """``1536`` -> ``'1.5 KB'``; binary multiples, as the upload bar counts them."""
    size = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f"{size:.0f} {unit}" if unit == 'B' else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # unreachable


def path_size(path: Path) -> int:
    """A file's size in bytes, 0 if it is gone."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


@dataclass
class FileGroup:
    """One kind of product the deploy uploads, with its upload tasks."""

    label: str
    tasks: list  # list[UploadTask]

    @property
    def nbytes(self) -> int:
        return sum(path_size(t.local_path) for t in self.tasks)


@dataclass
class NirspecDeployPlan:
    """Everything one ``campfire deploy --obs`` will upload and write.

    The local half is filled by the builder before anything touches the
    network; ``existing_targets``, ``push`` and ``n_published_redraft`` are the
    read-only remote check (``None`` when it was not made).
    """

    obs_name: str
    field: str
    program_slug: str
    targets: list[dict]  # one per (observation, source): the `targets` table
    spectra: list[dict]  # one per (target, grating): the `spectra` table
    file_groups: list[FileGroup]
    cfpipe_version: Optional[str] = None
    crds_context: Optional[str] = None

    # Catalog extras
    pointings: Optional[list[dict]] = None   # None: no pointings ECSV
    shutters: Optional[list[dict]] = None    # None: no shutters ECSV (or skipped)
    shutters_skipped: bool = False           # --no-shutters
    astrometry: str = ''                     # how shutter positions are corrected
    n_line_fits: int = 0
    n_rate_rows: int = 0                     # one triage row per detector rate file
    n_grid_rows: int = 0                     # one grid row per spectrum-exposure
    photometry: str = ''                     # what happens to photometry, in words

    # Modes
    supabase_only: bool = False
    force_overwrite: bool = False
    draft: bool = False
    defer_rebuild: bool = False

    warnings: list[str] = field(default_factory=list)

    # Execution inputs: the products behind the catalog rows above
    lines_paths: list = field(default_factory=list, repr=False)
    exposure_files: list = field(default_factory=list, repr=False)
    rate_files: list = field(default_factory=list, repr=False)

    # Remote check (read-only)
    existing_targets: Optional[int] = None
    n_published_redraft: Optional[int] = None
    push: object = None  # campfire.storage.plan.PushPlan

    @property
    def upload_tasks(self) -> list:
        return [t for g in self.file_groups for t in g.tasks]

    @property
    def n_new_targets(self) -> Optional[int]:
        if self.existing_targets is None:
            return None
        return len(self.targets) - self.existing_targets


def _group_split(group: FileGroup, push) -> tuple[int, int]:
    """``(to upload, already in cloud)`` for one group under a push plan."""
    unchanged = {t.r2_key for t in push.unchanged}
    missing = {t.r2_key for t in push.missing}
    keys = [t.r2_key for t in group.tasks]
    n_skip = sum(1 for k in keys if k in unchanged)
    n_missing = sum(1 for k in keys if k in missing)
    return len(keys) - n_skip - n_missing, n_skip


def render_plan(plan: NirspecDeployPlan, *, dry_run: bool) -> str:
    """The plan as the block of text the CLI prints before acting on it."""
    out: list[str] = []
    mode = []
    if dry_run:
        mode.append('dry run: nothing will be written')
    if plan.draft:
        mode.append('draft: admin-only until published')
    out.append(f"Deploy plan: {plan.obs_name}" + (f"  ({'; '.join(mode)})" if mode else ''))
    facts = [f"program {plan.program_slug}", f"field {plan.field}"]
    if plan.cfpipe_version:
        facts.append(f"cfpipe {plan.cfpipe_version}")
    if plan.crds_context:
        facts.append(f"CRDS {plan.crds_context}")
    out.append('  ' + ' · '.join(facts))

    if plan.warnings:
        out.append('')
        for w in plan.warnings:
            lines = w.split('\n')
            out.append(f"  ! {lines[0]}")
            out.extend(f"    {line}" for line in lines[1:])

    # ---- Files ------------------------------------------------------------
    out.append('')
    if plan.supabase_only:
        out.append("Files → OSN: none (--supabase-only)")
    else:
        groups = [g for g in plan.file_groups if g.tasks]
        push = plan.push
        width = max([len(g.label) for g in groups] + [len('Total')]) + 2
        head = f"  {'':<{width}}{'files':>7}{'size':>11}"
        if push is not None:
            head += f"{'upload':>9}{'in cloud':>10}"
        out.append("Files → OSN")
        out.append(head)
        tot_n = tot_b = tot_up = tot_skip = 0
        for g in groups:
            nbytes = g.nbytes
            row = f"  {g.label:<{width}}{len(g.tasks):>7,}{format_bytes(nbytes):>11}"
            tot_n += len(g.tasks)
            tot_b += nbytes
            if push is not None:
                up, skip = _group_split(g, push)
                tot_up += up
                tot_skip += skip
                row += f"{up:>9,}{skip:>10,}"
            out.append(row)
        row = f"  {'Total':<{width}}{tot_n:>7,}{format_bytes(tot_b):>11}"
        if push is not None:
            row += f"{tot_up:>9,}{tot_skip:>10,}"
        out.append(row)
        if push is not None:
            up_bytes = sum(path_size(t.local_path) for t in push.to_upload)
            out.append(f"  → {len(push.to_upload):,} file(s) to upload, {format_bytes(up_bytes)} "
                       f"({len(push.new):,} new, {len(push.changed):,} changed)")
            if push.missing:
                out.append(f"  ! {len(push.missing):,} file(s) missing locally, not uploaded")
        else:
            out.append("  (not compared with the cloud registry)")

    # ---- Catalog ----------------------------------------------------------
    out.append('')
    out.append("Catalog → database")
    rows: list[tuple[str, str, str]] = []
    rows.append(("Program + observation", '', "config from programs.toml / observations.toml"))
    if plan.existing_targets is None:
        tnote = ("inspection data RESET" if plan.force_overwrite
                 else "existing: pipeline fields only, inspection kept")
    else:
        tnote = f"{plan.n_new_targets:,} new, {plan.existing_targets:,} updated"
        tnote += ("; inspection data RESET" if plan.force_overwrite
                  else "; inspection kept")
    rows.append(("Targets (source × observation)", f"{len(plan.targets):,}", tnote))
    rows.append(("Spectra (target × grating)", f"{len(plan.spectra):,}", ''))
    if plan.n_line_fits:
        rows.append(("Line fits", f"{plan.n_line_fits:,}", "inspected-redshift fits only"))
    if plan.pointings is not None:
        rows.append(("MSA pointings", f"{len(plan.pointings):,}",
                     "footprints, stored on the observation row"))
    else:
        rows.append(("MSA pointings", '-', "no pointings ECSV, skipped"))
    if plan.shutters_skipped:
        rows.append(("MSA shutters", '-', "skipped (--no-shutters)"))
    elif plan.shutters is not None:
        note = "replace this observation's rows"
        if plan.astrometry:
            note += f"; {plan.astrometry}"
        rows.append(("MSA shutters", f"{len(plan.shutters):,}", note))
    else:
        rows.append(("MSA shutters", '-', "no shutters ECSV, skipped"))
    n_review = plan.n_rate_rows + plan.n_grid_rows
    if n_review:
        parts = []
        if plan.n_rate_rows:
            parts.append(f"{plan.n_rate_rows:,} rate-mask triage")
        if plan.n_grid_rows:
            parts.append(f"{plan.n_grid_rows:,} exposure grid")
        rows.append(("Review rows", f"{n_review:,}", ', '.join(parts)))
    lw = max(len(r[0]) for r in rows) + 2
    nw = max(len(r[1]) for r in rows)
    for label, n, note in rows:
        out.append(f"  {label:<{lw}}{n:>{nw}}" + (f"   {note}" if note else ''))

    # ---- Afterwards -------------------------------------------------------
    out.append('')
    out.append("Then")
    if plan.defer_rebuild:
        out.append(f"  Reconcile objects in field {plan.field}: once, after every observation")
    else:
        out.append(f"  Reconcile objects in field {plan.field} (object counts known after this step)")
    if plan.photometry:
        out.append(f"  Photometry: {plan.photometry}")
    out.append("  Refresh portal caches; record the deployment")
    return '\n'.join(out)
