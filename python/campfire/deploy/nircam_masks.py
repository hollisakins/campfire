"""
Imaging mask round-trip — .reg files ↔ Supabase ``exposures.mask_regions``
(NIRCam, ACS/WFC, WFC3/IR: one table, told apart by ``instrument``).

The web admin UI ``/admin/nircam/[id]`` is the canonical editing surface for
per-exposure mask polygons. This module materializes that DB state back into
the reduction workspace:

``pull-masks``
    Materializes the DB polygons back into ``<rootname>.reg`` files in DS9
    ``image`` coord format, ready for ``apply_masks_step`` to consume. To
    keep hand-drawn legacy masks safe, the writer only touches files for
    which the matching exposure has a non-null ``mask_regions`` row: a
    .reg without any DB representation is treated as a local-only artifact
    and left alone. The ``_imported/`` archive (left behind by the retired
    one-time ``import-masks`` command, issue #371) is never modified.

Why pixel coords as the canonical DB representation:
    The web canvas is pixel-native and cannot reasonably ship a GWCS-aware
    JS WCS library. Polygons were converted FK5/ICRS → pixel once at import
    time using the actual exposure WCS (with distortion), so the in-browser
    editor can render and edit polygons with zero WCS round-trips, and
    ``apply_masks_step`` reads the materialized ``image``-coord ``.reg``
    as a no-op pixel polygon.
"""

from datetime import datetime, timezone


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pull: DB → .reg
# ---------------------------------------------------------------------------

def pull_masks(field, config, dry_run=False, instrument='nircam'):
    """Write ``mask_regions`` JSONB rows back out as DS9 ``image`` .reg files.

    One imaging instrument's exposures of the field, into
    ``reference/<instrument>/<field>/masks/<filter>/<filename>.reg``.

    Only writes files for exposures whose ``mask_regions`` is non-null.
    Legacy ``.reg`` files with no DB representation are left untouched —
    that's the safety net for hand-drawn masks that haven't been imported.

    Local-first: a ``.reg`` a person wrote or edited is never overwritten; one
    whose polygons differ from the DB is kept and reported
    (``local_first.write_if_absent``). The one file that gives way is a mask
    the pipeline generated and nobody has edited since (the HST ``trails``
    step's preliminary satellite-trail mask, recognized by its content hash):
    the deploy seeded the database from it, so the database row is that mask
    as a reviewer left it.
    """
    from campfire.deploy.local_first import (
        KEPT_LOCAL, REPLACED, WRITTEN, write_if_absent,
    )
    from campfire.deploy.nircam import _resolve_imaging_dirs
    from campfire.deploy.supabase import get_supabase_client

    dirs = _resolve_imaging_dirs(instrument, field)
    masks_root = dirs['masks']

    client = get_supabase_client(config)
    resp = (client.table('exposures')
            .select('filter,filename,mask_regions')
            .eq('instrument', instrument)
            .eq('field', field)
            .not_.is_('mask_regions', 'null')
            .execute())
    rows = resp.data or []
    # Reviewed, no mask: the deploy seeded a pipeline-found mask and a reviewer
    # deleted it. That decision has to reach the disk too, or the pipeline's
    # own file would keep being applied.
    resp = (client.table('exposures')
            .select('filter,filename')
            .eq('instrument', instrument)
            .eq('field', field)
            .is_('mask_regions', 'null')
            .not_.is_('mask_seed_hash', 'null')
            .execute())
    cleared = resp.data or []
    if not rows and not cleared:
        print(f"No mask_regions rows for field={field} ({instrument})")
        return

    print(f"Field: {field} ({instrument})")
    print(f"Exposures with DB masks: {len(rows)}")

    n_written = 0
    n_kept = 0
    n_empty = 0
    n_replaced = 0
    n_cleared = 0
    for row in cleared:
        filtname = row['filter']
        out_path = masks_root / filtname / f"{row['filename']}.reg"
        content = _serialize_regfile([], generated_at=_utcnow_iso())
        status = write_if_absent(out_path, content, dry_run=dry_run,
                                 replace_unedited=True)
        if status in (WRITTEN, REPLACED):
            n_cleared += 1
            print(f"  {'would clear' if dry_run else 'cleared'} {filtname}/{out_path.name}: "
                  f"a reviewer removed the pipeline's mask")
        elif status == KEPT_LOCAL:
            n_kept += 1
            print(f"  kept local {filtname}/{out_path.name} (the portal has no mask for it; "
                  f"not overwritten)")
    generated_at = _utcnow_iso()
    for row in rows:
        payload = row.get('mask_regions') or {}
        polygons = payload.get('polygons') or []
        if not polygons:
            n_empty += 1
            continue
        filtname = row['filter']
        basename = row['filename']
        out_dir = masks_root / filtname
        out_path = out_dir / f'{basename}.reg'

        content = _serialize_regfile(polygons, generated_at=generated_at)
        status = write_if_absent(out_path, content, dry_run=dry_run,
                                 replace_unedited=True)
        if status == WRITTEN:
            n_written += 1
            print(f"  {'would write' if dry_run else 'wrote'} {filtname}/{out_path.name} "
                  f"({len(polygons)} polygon{'s' if len(polygons) != 1 else ''})")
        elif status == REPLACED:
            n_replaced += 1
            print(f"  {'would replace' if dry_run else 'replaced'} the unedited pipeline mask "
                  f"{filtname}/{out_path.name} with the reviewed one "
                  f"({len(polygons)} polygon{'s' if len(polygons) != 1 else ''})")
        elif status == KEPT_LOCAL:
            n_kept += 1
            print(f"  kept local {filtname}/{out_path.name} (differs from the DB; not overwritten)")

    print(f"\n{'Would write' if dry_run else 'Wrote'} {n_written}, "
          f"{'would replace' if dry_run else 'replaced'} {n_replaced} unedited pipeline mask(s), "
          f"{'would clear' if dry_run else 'cleared'} {n_cleared} a reviewer removed, "
          f"kept {n_kept} local that differ from the DB, skipped {n_empty} empty")


def _serialize_regfile(polygons, generated_at):
    """Render a list of polygon dicts as a DS9 image-coord .reg file."""
    lines = [
        '# Region file format: DS9 version 4.1',
        f'# Generated by campfire deploy pull-masks at {generated_at}.',
        '# Local edits are kept: a pull never overwrites an existing file.',
        ('global color=green dashlist=8 3 width=1 font="helvetica 10 normal '
         'roman" select=1 highlite=1 dash=0 fixed=0 edit=1 move=1 delete=1 '
         'include=1 source=1'),
        'image',
    ]
    for poly in polygons:
        verts = poly.get('vertices') or []
        if len(verts) < 3:
            continue
        flat = ','.join(f'{float(x):.4f},{float(y):.4f}' for x, y in verts)
        label = poly.get('label')
        suffix = f' # text={{{label}}}' if label else ''
        lines.append(f'polygon({flat}){suffix}')
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# .reg → polygons (the reverse of _serialize_regfile)
# ---------------------------------------------------------------------------

def polygons_from_regfile(text):
    """Read the ``image``-coordinate polygons of a DS9 region file.

    Returns ``(polygons, skipped)``: ``polygons`` in the ``mask_regions`` shape
    (``[{'vertices': [[x, y], ...], 'label': str or None}, ...]``, DS9 image
    coordinates, the numbers as written), and ``skipped`` the region lines that
    are not image polygons (a box, a circle, anything in sky coordinates). The
    database holds polygons in image coordinates only, so those cannot be
    stored and the caller should say so.

    This is what lets a mask that starts on disk reach the portal: the
    satellite trails cfpipe's HST ``trails`` step writes as a preliminary mask.
    """
    import re

    polygons, skipped = [], []
    frame = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or line.startswith('global'):
            continue
        if line.lower() in ('image', 'physical', 'fk5', 'icrs', 'fk4', 'galactic',
                            'ecliptic', 'wcs', 'linear', 'amplifier', 'detector'):
            frame = line.lower()
            continue
        body, _, comment = line.partition('#')
        m = re.match(r'^\s*(?:image\s*;\s*)?polygon\s*\(([^)]*)\)\s*$', body, re.I)
        in_image = frame == 'image' or body.strip().lower().startswith('image')
        if not m or not in_image:
            skipped.append(line)
            continue
        try:
            numbers = [float(v) for v in m.group(1).replace(',', ' ').split()]
        except ValueError:
            skipped.append(line)
            continue
        if len(numbers) < 6 or len(numbers) % 2:
            skipped.append(line)
            continue
        label = None
        t = re.search(r'text=\{([^}]*)\}', comment)
        if t:
            label = t.group(1)
        polygons.append({'vertices': [[numbers[i], numbers[i + 1]]
                                      for i in range(0, len(numbers), 2)],
                         'label': label})
    return polygons, skipped


# ---------------------------------------------------------------------------
# Drift report (epic #261, N6, D8): local .reg vs DB masks
# ---------------------------------------------------------------------------

def mask_drift_report(field, config, instrument='nircam'):
    """Diff local ``masks/<filter>/*.reg`` against DB ``mask_regions``; warn.

    One imaging instrument's exposures of the field.

    Advisory (exit 0). Returns a dict for tests with three categories:

    * ``stale`` — a pull-generated ``.reg`` whose DB row now has no polygons
      (the portal cleared the mask, but ``pull-masks`` only writes non-null rows
      so the file lingers and ``apply_masks`` still applies it).
    * ``local_only`` — a non-generated ``.reg`` for an exposure with no DB mask
      (hand-drawn, never imported — the portal can't see it). Never told to
      delete; it's the safety net for hand-drawn masks.
    * ``not_materialized`` — DB has polygons but no local ``.reg`` (``pull-masks``
      not run yet).
    """
    from campfire.deploy.local_first import is_unedited_generated
    from campfire.deploy.nircam import _resolve_imaging_dirs
    from campfire.deploy.supabase import get_supabase_client

    dirs = _resolve_imaging_dirs(instrument, field)
    masks_root = dirs['masks']

    client = get_supabase_client(config)
    resp = (client.table('exposures')
            .select('filter,filename,mask_regions')
            .eq('instrument', instrument)
            .eq('field', field)
            .execute())
    rows = resp.data or []
    db_has_mask = {}
    for row in rows:
        payload = row.get('mask_regions') or {}
        db_has_mask[(row['filter'], row['filename'])] = bool(payload.get('polygons'))

    stale, local_only, pipeline_pending = [], [], []
    seen_on_disk = set()
    if masks_root.exists():
        for filt_dir in sorted(masks_root.iterdir()):
            if not filt_dir.is_dir() or filt_dir.name == '_imported':
                continue
            filtname = filt_dir.name
            for reg_path in sorted(filt_dir.glob('*.reg')):
                rootname = reg_path.stem
                seen_on_disk.add((filtname, rootname))
                try:
                    text = reg_path.read_text()
                except OSError:
                    text = ''
                head = ''.join(text.splitlines(keepends=True)[:4])
                is_generated = 'Generated by campfire deploy' in head
                has_db = db_has_mask.get((filtname, rootname), False)
                if has_db:
                    continue
                if not polygons_from_regfile(text)[0] and not polygons_from_regfile(text)[1]:
                    # No region in it: "reviewed, no mask", which is what the
                    # database says too.
                    continue
                if is_unedited_generated(text):
                    # The pipeline's own mask, not edited: the next deploy
                    # sends it to the portal.
                    pipeline_pending.append(str(reg_path))
                elif is_generated:
                    stale.append(str(reg_path))
                else:
                    local_only.append(str(reg_path))

    not_materialized = [
        f"{filt}/{root}" for (filt, root), has in sorted(db_has_mask.items())
        if has and (filt, root) not in seen_on_disk
    ]

    print(f"\nMask drift report for field={field} ({instrument}):")
    if not (stale or local_only or not_materialized or pipeline_pending):
        print("  clean — local .reg files and DB masks agree.")
    for p in pipeline_pending:
        print(f"  PIPELINE MASK not in the portal yet: {p}\n"
              f"        the next deploy of the field sends it for review.")
    for p in stale:
        print(f"  STALE (portal cleared, combine still applies): {p}\n"
              f"        remove it, or re-import if the mask is intentional.")
    for p in local_only:
        print(f"  LOCAL-ONLY (not in DB, portal can't see it): {p}\n"
              f"        redraw it in the portal mask editor to track it in the DB.")
    for k in not_materialized:
        print(f"  DB MASK not on disk (pull not run): {k}\n"
              f"        run: campfire pull --field {field}")

    return {'stale': stale, 'local_only': local_only,
            'pipeline_pending': pipeline_pending,
            'not_materialized': not_materialized}

