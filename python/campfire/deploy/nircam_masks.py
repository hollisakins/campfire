"""
NIRCam mask round-trip — .reg files ↔ Supabase ``nircam_exposures.mask_regions``.

The web admin UI ``/admin/nircam/[id]`` is the canonical editing surface for
per-exposure mask polygons. This module backs that workflow on the reduction
side with two CLI commands:

``import-masks``
    Walks ``$CAMPFIRE_ROOT/reference/nircam/<field>/masks/<filter>/*.reg``,
    converts every region into DS9 ``image`` pixel coordinates using the
    canonical exposure's FITS WCS (so distortion is baked in once, not at
    every apply_masks run), and upserts into ``mask_regions`` with
    ``source='imported'``. After a successful import the source ``.reg``
    is **moved** to ``<masks>/<filter>/_imported/<rootname>.reg`` so the
    original (typically FK5) file is preserved untouched while the
    canonical ``<rootname>.reg`` path is freed for pull-masks to own.

``pull-masks``
    Materializes the DB polygons back into ``<rootname>.reg`` files in DS9
    ``image`` coord format, ready for ``apply_masks_step`` to consume. To
    keep hand-drawn legacy masks safe, the writer only touches files for
    which the matching exposure has a non-null ``mask_regions`` row: a
    .reg without any DB representation is treated as a local-only artifact
    and left alone. The ``_imported/`` archive is never modified by pull.

Why pixel coords as the canonical DB representation:
    The web canvas is pixel-native and cannot reasonably ship a GWCS-aware
    JS WCS library. Converting FK5/ICRS → pixel once at import time using
    the actual exposure WCS (with distortion) means the in-browser editor
    can render and edit polygons with zero WCS round-trips, and
    ``apply_masks_step`` reads the materialized ``image``-coord ``.reg``
    as a no-op pixel polygon.
"""

import os
import sys
import uuid
from datetime import datetime, timezone
from glob import glob
from pathlib import Path


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Import: .reg → DB
# ---------------------------------------------------------------------------

def import_masks(field, config, dry_run=False):
    """Import legacy ``.reg`` files into ``nircam_exposures.mask_regions``.

    For each ``reference/nircam/<field>/masks/<filter>/<rootname>.reg``:

    1. Locate the canonical exposure FITS at
       ``products/nircam/<field>/<filter>/<rootname>.fits``.
    2. Read ``model.get_fits_wcs()`` and convert every region to pixel
       coordinates via ``regions.Region.to_pixel(wcs)`` — the same WCS
       handle ``apply_masks_step`` uses, so import-time pixels match
       apply-time pixels exactly for the WCS state at import.
    3. Convert to DS9 ``image`` 1-indexed FITS coords (``regions``
       PixCoord is 0-indexed center-of-pixel, so add 1.0 per axis).
    4. Upsert the polygon list into ``mask_regions`` with provenance
       (``source='imported'``, ``original_frame``, ``imported_from``,
       ``imported_at``).

    Skips regions that aren't polygons (with a warning); the editor only
    supports polygons today, and ``apply_masks_step`` can keep reading
    legacy ``.reg`` files for any region types we don't import.
    """
    from campfire.deploy.nircam import (
        _resolve_nircam_dirs, _discover_filters,
    )
    from campfire.deploy.supabase import get_supabase_client

    dirs = _resolve_nircam_dirs(field)
    masks_root = dirs['masks']
    products = dirs['products']

    if not masks_root.exists():
        print(f"No masks directory: {masks_root}")
        return
    if not products.exists():
        print(f"No products directory: {products}")
        sys.exit(1)

    available = _discover_filters(dirs)
    if not available:
        print(f"No filters under {products}")
        return

    print(f"Field: {field}")
    print(f"Filters available: {', '.join(available)}")

    # Defer regions/jwst imports: regions pulls in astropy which is heavy,
    # and jwst is even worse. Don't pay for them if no .reg files exist.
    # Skip the _imported/ archive directory if it already exists from a
    # previous run — those .reg files have already been processed. Only
    # files directly in <masks>/<filter>/ are candidates.
    reg_paths = []
    for filtname in available:
        filt_masks = masks_root / filtname
        if not filt_masks.exists():
            continue
        for reg_path in sorted(filt_masks.glob('*.reg')):
            if reg_path.parent.name == '_imported':
                continue
            reg_paths.append((filtname, reg_path))

    if not reg_paths:
        print(f"No .reg files found under {masks_root}")
        return

    print(f"Found {len(reg_paths)} .reg file(s) to import")
    if dry_run:
        for filtname, reg_path in reg_paths:
            print(f"  {filtname}/{reg_path.name}")
        print("\nDry run — no changes made.")
        return

    from regions import Regions, PolygonSkyRegion, PolygonPixelRegion
    from jwst.datamodels import ImageModel
    import warnings

    client = get_supabase_client(config)

    imported_at = _utcnow_iso()
    n_ok = 0
    n_skipped = 0
    for filtname, reg_path in reg_paths:
        basename = reg_path.stem
        exposure_file = products / filtname / f'{basename}.fits'
        if not exposure_file.exists():
            print(f"  skip {filtname}/{reg_path.name}: no canonical exposure")
            n_skipped += 1
            continue

        try:
            regs = Regions.read(str(reg_path))
        except Exception as e:
            print(f"  skip {filtname}/{reg_path.name}: parse error: {e}")
            n_skipped += 1
            continue

        with ImageModel(str(exposure_file)) as model:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                wcs = model.get_fits_wcs()

        polygons = []
        for reg in regs:
            try:
                pix_reg = reg if isinstance(reg, PolygonPixelRegion) \
                    else reg.to_pixel(wcs)
            except (ValueError, TypeError) as e:
                print(f"  warn {filtname}/{reg_path.name}: cannot project "
                      f"region: {e}")
                continue
            if not isinstance(pix_reg, PolygonPixelRegion):
                print(f"  warn {filtname}/{reg_path.name}: non-polygon "
                      f"({type(pix_reg).__name__}) skipped")
                continue
            # regions PixCoord is 0-indexed center-of-pixel; DS9 image
            # coords are 1-indexed. +1 lands them on the canonical form.
            xs = pix_reg.vertices.x + 1.0
            ys = pix_reg.vertices.y + 1.0
            vertices = [[float(x), float(y)] for x, y in zip(xs, ys)]
            polygons.append({
                'id': str(uuid.uuid4()),
                'vertices': vertices,
                'source': 'imported',
                'original_frame': _detect_frame(reg, isinstance(
                    reg, PolygonPixelRegion)),
                'imported_from': reg_path.name,
                'imported_at': imported_at,
                'created_at': imported_at,
                'modified_at': imported_at,
            })

        if not polygons:
            print(f"  skip {filtname}/{reg_path.name}: no usable polygons")
            n_skipped += 1
            continue

        payload = {'version': 1, 'polygons': polygons}
        resp = (client.table('nircam_exposures')
                .update({'mask_regions': payload, 'masking': 'done'})
                .eq('field', field)
                .eq('filter', filtname)
                .eq('filename', basename)
                .execute())
        if not resp.data:
            print(f"  warn {filtname}/{reg_path.name}: no matching exposure "
                  f"row (deploy nircam first?)")
            n_skipped += 1
            continue
        # Move the source .reg into the _imported/ archive so the
        # canonical <rootname>.reg path is owned by pull-masks from now on.
        # The original FK5/ICRS file is preserved verbatim for audit.
        archive_dir = reg_path.parent / '_imported'
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / reg_path.name
        if archive_path.exists():
            # Don't clobber a prior archive — bump with a counter.
            i = 1
            while True:
                cand = archive_dir / f'{reg_path.stem}.{i}.reg'
                if not cand.exists():
                    archive_path = cand
                    break
                i += 1
        reg_path.rename(archive_path)

        n_ok += 1
        print(f"  imported {filtname}/{reg_path.name} ({len(polygons)} "
              f"polygon{'s' if len(polygons) != 1 else ''}); "
              f"original archived to _imported/{archive_path.name}")

    print(f"\nImported {n_ok}, skipped {n_skipped}")


def _detect_frame(reg, is_pixel):
    """Best-effort serialization of the original DS9 frame name."""
    if is_pixel:
        return 'image'
    # SkyRegion: try to read the frame name from the SkyCoord
    try:
        return reg.center.frame.name  # 'fk5', 'icrs', ...
    except Exception:
        try:
            return reg.vertices.frame.name
        except Exception:
            return 'sky'


# ---------------------------------------------------------------------------
# Pull: DB → .reg
# ---------------------------------------------------------------------------

def pull_masks(field, config, dry_run=False):
    """Write ``mask_regions`` JSONB rows back out as DS9 ``image`` .reg files.

    Only writes files for exposures whose ``mask_regions`` is non-null.
    Legacy ``.reg`` files with no DB representation are left untouched —
    that's the safety net for hand-drawn masks that haven't been imported.

    The materialized file always carries a ``# Generated by ...`` header so
    a human opening the file knows it's a build artifact.
    """
    from campfire.deploy.nircam import _resolve_nircam_dirs
    from campfire.deploy.supabase import get_supabase_client

    dirs = _resolve_nircam_dirs(field)
    masks_root = dirs['masks']

    client = get_supabase_client(config)
    resp = (client.table('nircam_exposures')
            .select('filter,filename,mask_regions')
            .eq('field', field)
            .not_.is_('mask_regions', 'null')
            .execute())
    rows = resp.data or []
    if not rows:
        print(f"No mask_regions rows for field={field}")
        return

    print(f"Field: {field}")
    print(f"Exposures with DB masks: {len(rows)}")

    n_written = 0
    n_empty = 0
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
        if dry_run:
            print(f"  would write {filtname}/{out_path.name} "
                  f"({len(polygons)} polygon"
                  f"{'s' if len(polygons) != 1 else ''})")
            continue

        out_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_suffix(out_path.suffix + '.tmp')
        tmp_path.write_text(content)
        tmp_path.replace(out_path)
        n_written += 1
        print(f"  wrote {filtname}/{out_path.name} ({len(polygons)} "
              f"polygon{'s' if len(polygons) != 1 else ''})")

    if dry_run:
        print(f"\nDry run — no files written. {len(rows) - n_empty} would "
              f"be written, {n_empty} empty.")
    else:
        print(f"\nWrote {n_written}, skipped {n_empty} empty")


def _serialize_regfile(polygons, generated_at):
    """Render a list of polygon dicts as a DS9 image-coord .reg file."""
    lines = [
        '# Region file format: DS9 version 4.1',
        f'# Generated by campfire deploy pull-masks at {generated_at}.',
        '# Manual edits will be overwritten on the next pull.',
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
