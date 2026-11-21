"""What a deploy will do, decided before it does any of it.

``campfire deploy --obs X`` (NIRSpec) and ``campfire deploy --field Y``
(NIRCam) each build one plan, print it, and then either stop (``--dry-run``)
or carry out that same plan. The file list a dry run prints is the list a
live run uploads, and the catalog section names every table the live run
writes, so the preview cannot fall behind the deploy as products are added
(it had: the NIRSpec 1-D spectrum sidecars, spectrum-exposure intermediates
and detector rate files were uploaded on every deploy but never shown, and
the NIRCam dry run was one sentence of counts).

This module holds the data model and the rendering only. Building a plan
(discovery, sidecar generation, the read-only registry check) and executing it
live in :mod:`campfire.deploy.deploy` (NIRSpec) and
:mod:`campfire.deploy.nircam` (NIRCam).
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


# ---------------------------------------------------------------------------
# Shared rendering
# ---------------------------------------------------------------------------

def _render_header(title: str, facts: list[str], *, dry_run: bool, draft: bool) -> list[str]:
    mode = []
    if dry_run:
        mode.append('dry run: nothing will be written')
    if draft:
        mode.append('draft: admin-only until published')
    out = [f"Deploy plan: {title}" + (f"  ({'; '.join(mode)})" if mode else '')]
    if facts:
        out.append('  ' + ' · '.join(facts))
    return out


def _render_warnings(warnings: list[str]) -> list[str]:
    if not warnings:
        return []
    out = ['']
    for w in warnings:
        lines = w.split('\n')
        out.append(f"  ! {lines[0]}")
        out.extend(f"    {line}" for line in lines[1:])
    return out


def _group_split(group: FileGroup, push) -> tuple[int, int]:
    """``(to upload, already in cloud)`` for one group under a push plan."""
    unchanged = {t.r2_key for t in push.unchanged}
    missing = {t.r2_key for t in push.missing}
    keys = [t.r2_key for t in group.tasks]
    n_skip = sum(1 for k in keys if k in unchanged)
    n_missing = sum(1 for k in keys if k in missing)
    return len(keys) - n_skip - n_missing, n_skip


def _render_files(groups: list[FileGroup], push, notes: list[str] = ()) -> list[str]:
    """The ``Files → OSN`` table: one row per non-empty group, a total, and
    (with a push plan) the upload / already-in-cloud split."""
    groups = [g for g in groups if g.tasks]
    out = ['', "Files → OSN"]
    width = max([len(g.label) for g in groups] + [len('Total')]) + 2
    head = f"  {'':<{width}}{'files':>7}{'size':>11}"
    if push is not None:
        head += f"{'upload':>9}{'in cloud':>10}"
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
    out.extend(f"  {n}" for n in notes)
    return out


def _render_rows(title: str, rows: list[tuple[str, str, str]]) -> list[str]:
    """A ``label  count   note`` table. A note may span lines (``\\n``)."""
    out = ['', title]
    lw = max(len(r[0]) for r in rows) + 2
    nw = max(len(r[1]) for r in rows)
    for label, n, note in rows:
        lines = note.split('\n') if note else ['']
        out.append(f"  {label:<{lw}}{n:>{nw}}" + (f"   {lines[0]}" if lines[0] else ''))
        out.extend(f"  {'':<{lw}}{'':>{nw}}   {line}" for line in lines[1:])
    return out


def _render_then(steps: list[str]) -> list[str]:
    return ['', "Then"] + [f"  {s}" for s in steps]


# ---------------------------------------------------------------------------
# NIRSpec: one observation
# ---------------------------------------------------------------------------

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


def render_nirspec_plan(plan: NirspecDeployPlan, *, dry_run: bool) -> str:
    """The NIRSpec plan as the block of text the CLI prints before acting on it."""
    facts = [f"program {plan.program_slug}", f"field {plan.field}"]
    if plan.cfpipe_version:
        facts.append(f"cfpipe {plan.cfpipe_version}")
    if plan.crds_context:
        facts.append(f"CRDS {plan.crds_context}")
    out = _render_header(plan.obs_name, facts, dry_run=dry_run, draft=plan.draft)
    out += _render_warnings(plan.warnings)

    if plan.supabase_only:
        out += ['', "Files → OSN: none (--supabase-only)"]
    else:
        out += _render_files(plan.file_groups, plan.push)

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
    out += _render_rows("Catalog → database", rows)

    steps = []
    if plan.defer_rebuild:
        steps.append(f"Reconcile objects in field {plan.field}: once, after every observation")
    else:
        steps.append(f"Reconcile objects in field {plan.field} (object counts known after this step)")
    if plan.photometry:
        steps.append(f"Photometry: {plan.photometry}")
    steps.append("Refresh portal caches; record the deployment")
    out += _render_then(steps)
    return '\n'.join(out)


# ---------------------------------------------------------------------------
# NIRCam: one field
# ---------------------------------------------------------------------------

@dataclass
class NircamDeployPlan:
    """Everything one ``campfire deploy --field`` will upload and write.

    ``existing_exposures`` and ``push`` are the read-only remote check
    (``None`` when it was not made).
    """

    field: str
    filters: list[str]
    file_groups: list[FileGroup]
    records: list[dict]          # one per exposure: the `nircam_exposures` table
    mosaics: list[dict]          # deployable mosaic products (one nircam_images row each)
    cfpipe_version: Optional[str] = None
    jwst_version: Optional[str] = None
    crds_context: Optional[str] = None
    in_fields_toml: bool = True  # whether the field registry row is written

    draft: bool = False
    # The `push im` product names in this deploy (default all three), and the
    # tiles the mosaics are narrowed to (empty: every tile)
    products: list[str] = field(default_factory=lambda: ['mosaics', 'exposures', 'expmaps'])
    tiles: list[str] = field(default_factory=list)

    warnings: list[str] = field(default_factory=list)

    # Execution inputs: the exposure FITS tasks (audit counts) and the mosaic keys
    fits_keys: set = field(default_factory=set, repr=False)

    # Remote check (read-only)
    existing_exposures: Optional[int] = None
    push: object = None  # campfire.storage.plan.PushPlan

    @property
    def upload_tasks(self) -> list:
        return [t for g in self.file_groups for t in g.tasks]

    @property
    def mosaic_keys(self) -> set[str]:
        return {m['storage_key'] for m in self.mosaics}

    def n_mosaic_rows(self) -> Optional[int]:
        """How many ``nircam_images`` rows the deploy will (re-)index.

        A published deploy indexes every mosaic present in the cloud after the
        upload; a draft only the new/changed ones (unchanged mosaics stay on
        their published deployment). ``None`` without a push plan for a draft.
        """
        keys = self.mosaic_keys
        if self.push is None:
            return None if self.draft else len(keys)
        if self.draft:
            return sum(1 for t in self.push.to_upload if t.r2_key in keys)
        missing = {t.r2_key for t in self.push.missing}
        return len(keys - missing)


def render_nircam_plan(plan: NircamDeployPlan, *, dry_run: bool) -> str:
    """The NIRCam plan as the block of text the CLI prints before acting on it."""
    from collections import Counter

    facts = [f"{len(plan.filters)} filter(s): {', '.join(plan.filters)}"]
    if plan.cfpipe_version:
        facts.append(f"cfpipe {plan.cfpipe_version}")
    if plan.crds_context:
        facts.append(f"CRDS {plan.crds_context}")
    out = _render_header(f"field {plan.field}", facts, dry_run=dry_run, draft=plan.draft)
    out += _render_warnings(plan.warnings)

    left_out = [p for p in ('mosaics', 'exposures', 'expmaps') if p not in plan.products]
    notes = [f"Not in this deploy: {', '.join(left_out)}"] if left_out else []
    if plan.tiles:
        notes.append(f"Mosaics narrowed to tile(s): {', '.join(plan.tiles)}")
    out += _render_files(plan.file_groups, plan.push, notes)

    rows: list[tuple[str, str, str]] = []
    if 'exposures' in plan.products:
        n = len(plan.records)
        if plan.existing_exposures is None:
            enote = "review state kept on existing rows"
        else:
            enote = (f"{n - plan.existing_exposures:,} new, "
                     f"{plan.existing_exposures:,} updated; review state kept")
        stages = Counter(r['stage'] for r in plan.records)
        if stages:
            enote += "\nlatest stage: " + ', '.join(
                f"{s} {c:,}" for s, c in sorted(stages.items(), key=lambda kv: -kv[1]))
        rows.append(("Exposures", f"{n:,}", enote))
    if plan.mosaics:
        n_rows = plan.n_mosaic_rows()
        mnote = "one per tile × filter × extension"
        if plan.draft:
            mnote += "; new/changed only (draft)"
        rows.append(("Mosaic index", '?' if n_rows is None else f"{n_rows:,}", mnote))
    if plan.in_fields_toml:
        rows.append(("Field registry", '1', "fields.toml config + survey area"))
    else:
        rows.append(("Field registry", '-', "not in fields.toml, skipped"))
    out += _render_rows("Catalog → database", rows)

    out += _render_then(["Record the deployment; flag a FitsGL map rebuild if the mosaics changed"])
    return '\n'.join(out)
