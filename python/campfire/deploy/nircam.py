"""
NIRCam exposure deployment — push canonical exposure state to OSN + Supabase.

Scans the per-filter flat directories (the canonical layout where each exposure
is a single ``{rootname}.fits`` mutated in place through the ``CFP_*`` provenance
keys), derives a ``stage`` value from the highest completed CFP key, and:

* uploads the **canonical exposure FITS** (and any field ``expmap`` coverage
  files) to **OSN** under ``campfire-layout`` canonical keys, content-hash-
  deduped so an unchanged exposure is not re-uploaded (epic #261, N1 / D1);
* records a **field-scoped deployment** (provenance + lifecycle anchor) and
  registers those objects in ``storage_objects`` (``nircam_exposure`` /
  ``nircam_expmap``) tagged with its ``deployment_id`` — visibility rides
  ``deployment.status`` via the storage gate: ``published`` is public to everyone
  (NIRCam fields span multiple programs, so there is no per-program scope),
  ``draft`` is admin-only for review;
* uploads ``*_preview.png`` (grid thumbnail) / ``*_full.png`` (native-res mask
  surface) to **OSN** under canonical keys and registers them too (epic #261,
  N5) — the admin UI serves both via short-lived presigned OSN GET URLs, so the
  R2 legacy keys + the ``/api/nircam-preview`` proxy are retired;
* upserts ``nircam_exposures`` while preserving web-triage fields
  (``review_status``, ``correction``, ``notes``).

A normal deploy publishes immediately; ``--draft`` holds the field admin-only for
portal inspection, then ``campfire deploy publish --field`` makes it public.

Excluded exposures flagged by reviewers in the admin UI round-trip back to the
pipeline via ``campfire deploy nircam pull`` (epic #261, N6): it materializes
``review_status='excluded'`` rows into
``$CAMPFIRE_ROOT/reference/nircam/<field>/exposures.json``
(``nircam_exclusions.pull_exclusions``), which ``combine`` reads to drop those
rootnames. The DB is the source of truth; the JSON file is a generated,
fully-overwritten artifact — don't hand-edit it.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from astropy.io import fits

from campfire_layout import KeyScheme, Scope, is_compressed_key, storage_key
from campfire.deploy.deploy_plan import (
    FileGroup, NircamDeployPlan, format_bytes, path_size, render_nircam_plan,
)
from campfire.deploy.r2 import UploadTask, upload_files_parallel
from campfire.modality import IMAGING
from campfire.deploy.supabase import (
    ScopeLease, deploy_event_metadata,
    get_supabase_client, get_user_id_from_token, insert_deployment, log_deploy_event,
)

# The OSN data backend NIRCam canonical intermediates are homed on (epic #210/#216,
# same as NIRSpec canonical spectrum-exposures). As of N5 the preview/full PNGs
# also land here under canonical keys (served via presigned OSN GET URLs).
_CANONICAL_BACKEND = 'osn'
# The `campfire push im` / `pull im` product names, in display order.
IMAGING_PRODUCTS = tuple(IMAGING.product_names(['nircam']))
_FITS_CONTENT_TYPE = 'application/fits'
_GZIP_CONTENT_TYPE = 'application/gzip'  # compressed mosaic FITS (stored as .fits.gz)
_PNG_CONTENT_TYPE = 'image/png'


# ---------------------------------------------------------------------------
# CFP key → stage name mapping
# ---------------------------------------------------------------------------
# Ordered from earliest to latest, matching campfire_pipeline.common.cfp.CFP_KEYS
# (kept duplicated here so the deploy package doesn't depend on the pipeline).
# Each entry: (CFP_* keyword, stage value to report).
_CFP_TO_STAGE = [
    ('CFP_DET1', 'detector1'),
    ('CFP_JACK', 'jackknife'),
    ('CFP_PERS', 'persistence'),
    ('CFP_WISP', 'wisp'),
    ('CFP_IMG2', 'image2'),
    ('CFP_EDGE', 'edge'),
    ('CFP_BKG',  'bkg'),  # unified striping + sky + variance
    ('CFP_DIAG', 'diag_striping'),
    ('CFP_SHFT', 'wcs_shift'),
    ('CFP_PREV', 'preview'),
    ('CFP_JHAT', 'jhat'),
    ('CFP_MASK', 'apply_mask'),
    ('CFP_BPIX', 'bad_pixel'),
    ('CFP_OUT',  'outlier'),
]


def _stage_from_header(header):
    """Return the highest-completed step name based on CFP_* keys in ``header``.

    Falls back to ``'detector1'`` if a canonical file exists but no CFP key is
    set — that's unusual but the file's mere existence implies detector1 ran.
    """
    stage = 'detector1'
    for key, name in _CFP_TO_STAGE:
        if key in header:
            stage = name
    return stage


# The science-only change-detection digest (SCI+DQ+CFMASK+CFSPIKE; epic #261, D1) lives
# in the shared storage core — it is the push-dedup identity for NIRCam
# exposures on every access point. Re-exported under the established name.
from campfire.storage.hashing import sci_dq_hash as _sci_dq_hash  # noqa: E402


def _compute_sci_dq_hashes(fits_tasks, *, max_workers=None):
    """Return ``{r2_key: sci_dq_hash}`` for *fits_tasks*, hashed in parallel.

    Computing the SCI+DQ+CFMASK+CFSPIKE digest for candidate exposures is the dominant
    local cost before any upload starts; the shared parallel hasher overlaps
    the I/O-bound reads. A per-file read failure records ``None`` (never dedup
    on it).
    """
    from campfire.storage.hashing import sci_dq_hashes_parallel

    if not fits_tasks:
        return {}
    by_path = sci_dq_hashes_parallel(
        [t.local_path for t in fits_tasks], max_workers=max_workers,
        progress_desc='Hashing exposures',
    )
    return {t.r2_key: by_path.get(Path(t.local_path)) for t in fits_tasks}


# Parallel upload streams (CAMPFIRE_DEPLOY_UPLOAD_WORKERS-tunable) — the shared
# push-side default; kept under the established local name.
from campfire.deploy.push import default_upload_workers as _upload_workers  # noqa: E402


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_campfire_root():
    root = os.environ.get('CAMPFIRE_ROOT')
    if not root:
        print("Error: $CAMPFIRE_ROOT is not set.")
        print("  export CAMPFIRE_ROOT=/path/to/campfire/data")
        sys.exit(1)
    return Path(root)


def _resolve_nircam_dirs(field):
    """Return a dict of NIRCam directory paths for a field."""
    root = _resolve_campfire_root()
    products = root / 'products' / 'nircam' / field
    return {
        'root': root,
        'products': products,
        'reference': root / 'reference' / 'nircam' / field,
        'masks': root / 'reference' / 'nircam' / field / 'masks',
    }


def _discover_filters(dirs):
    """List available filters by scanning the products directory.

    Each real filter is a subdirectory (``<field>/<filter>/``). A stray legacy
    ``expmaps/`` dir (expmaps now live per-filter) is skipped so it is never
    treated as a filter.
    """
    products = dirs['products']
    if not products.exists():
        return []
    return sorted(
        d.name for d in products.iterdir()
        if d.is_dir() and not d.name.startswith('.') and d.name != 'expmaps'
    )


# ---------------------------------------------------------------------------
# Exposure discovery
# ---------------------------------------------------------------------------

def discover_exposures(dirs, filters):
    """Discover canonical exposures and derive metadata from their headers.

    Scans ``products/nircam/{field}/{filter}/jw*.fits`` (excluding mosaics and
    transient files), reads the primary header for CFP_* keys and basic
    metadata. Mosaics are named ``mosaic_*.fits`` and naturally excluded by
    the ``jw*`` glob.

    Returns
    -------
    dict
        ``{(filter, basename): info}`` where info has keys ``basename``,
        ``filter``, ``path`` (the canonical FITS Path), ``stage``, ``visit``,
        ``detector``, ``date_obs``, ``ra_center``, ``dec_center``.
    """
    exposures = {}
    for filtname in filters:
        filter_dir = dirs['products'] / filtname
        if not filter_dir.exists():
            continue
        for path in sorted(filter_dir.glob('jw*.fits')):
            name = path.name
            if name.endswith('.tmp.fits') or name.endswith('_jump.fits'):
                continue
            basename = name.removesuffix('.fits')
            info = _read_exposure_metadata(path)
            info['basename'] = basename
            info['filter'] = filtname
            info['path'] = path
            exposures[(filtname, basename)] = info
    return exposures


def _read_exposure_metadata(path):
    """Read header → stage + metadata in a single open."""
    info = {
        'stage': 'detector1',
        'visit': None,
        'detector': None,
        'date_obs': None,
        'ra_center': None,
        'dec_center': None,
        'combine_stamped': False,
    }
    # JWST naming convention: {visit}_{activity}_{exposure}_{detector}.fits
    parts = path.stem.split('_')
    if len(parts) >= 1:
        info['visit'] = parts[0]
    if len(parts) >= 4:
        info['detector'] = parts[3]

    try:
        with fits.open(path, memmap=True) as hdul:
            hdr0 = hdul[0].header
            info['stage'] = _stage_from_header(hdr0)
            # A canonical exposure must be the frozen process output; a
            # combine-phase stamp means it was mutated in place (old pipeline).
            info['combine_stamped'] = ('CFP_BPIX' in hdr0 or 'CFP_OUT' in hdr0)
            info['date_obs'] = hdr0.get('DATE-OBS')
            info['detector'] = hdr0.get('DETECTOR', info['detector'])
            # Index HDU 1 directly rather than testing len(hdul): len() forces
            # astropy to enumerate *every* HDU, parsing the full structure of a
            # ~120 MB exposure just to learn whether a second one exists. That
            # dominated discover_exposures — 9-14x slower than this form on
            # COSMOS LW, measured cold-vs-cold on disjoint file slices. The
            # cost is header parsing, not I/O (a warm page cache barely helps),
            # so parallelising the scan would have masked it rather than fixed
            # it. Lazy loading stops at HDU 1.
            try:
                sci_hdr = hdul[1].header
            except IndexError:
                sci_hdr = None
            if sci_hdr is not None:
                ra = sci_hdr.get('CRVAL1')
                dec = sci_hdr.get('CRVAL2')
                if ra is not None and dec is not None:
                    info['ra_center'] = float(ra)
                    info['dec_center'] = float(dec)
    except Exception:
        pass
    return info


# ---------------------------------------------------------------------------
# Preview PNG discovery
# ---------------------------------------------------------------------------

def build_upload_tasks(dirs, field, filters):
    """Build OSN upload tasks for per-exposure preview PNGs.

    The ``preview`` pipeline step writes two PNGs per exposure:
    ``{rootname}_preview.png`` (downsampled thumbnail) and
    ``{rootname}_full.png`` (native resolution, used by the in-browser mask
    editor). Both are uploaded so the table view stays fast while the
    editor renders at exposure-pixel resolution.

    Keyed under the ``campfire-layout`` CANONICAL scheme (OSN,
    ``data/products/nircam/<field>/<filter>/<rootname>_{preview,full}.png``) —
    the same tier as the canonical FITS. The admin UI serves them via
    short-lived presigned OSN GET URLs (epic #261, N5), retiring the R2 legacy
    keys + the ``/api/nircam-preview`` proxy hop.

    Returns list of (UploadTask, basename, filter, kind) tuples where
    ``kind`` is ``'thumb'`` or ``'full'``.
    """
    tasks = []
    for filtname in filters:
        png_dir = dirs['products'] / filtname
        if not png_dir.exists():
            continue
        for png_path in sorted(png_dir.glob('jw*_preview.png')):
            basename = png_path.name.removesuffix('_preview.png')
            key = storage_key('nircam_exposure_preview', Scope(field=field, filt=filtname),
                              png_path.name, scheme=KeyScheme.CANONICAL)
            tasks.append((
                UploadTask(local_path=png_path, r2_key=key,
                           content_type='image/png'),
                basename, filtname, 'thumb',
            ))
        for png_path in sorted(png_dir.glob('jw*_full.png')):
            basename = png_path.name.removesuffix('_full.png')
            key = storage_key('nircam_exposure_full', Scope(field=field, filt=filtname),
                              png_path.name, scheme=KeyScheme.CANONICAL)
            tasks.append((
                UploadTask(local_path=png_path, r2_key=key,
                           content_type='image/png'),
                basename, filtname, 'full',
            ))
    return tasks


def _read_full_png_dimensions(png_path):
    """Return (width, height) of a PNG without decoding pixel data."""
    # PNG IHDR: 8-byte signature, 4-byte length, 4-byte 'IHDR',
    # then width (4 BE) and height (4 BE).
    import struct
    with open(png_path, 'rb') as f:
        f.seek(16)
        width, height = struct.unpack('>II', f.read(8))
    return width, height


# ---------------------------------------------------------------------------
# Canonical FITS + expmap discovery (epic #261, N1)
# ---------------------------------------------------------------------------

def build_fits_upload_tasks(field, exposures):
    """Build canonical-key OSN upload tasks for the exposure FITS.

    One ``nircam_exposure`` task per discovered exposure, keyed under the
    ``campfire-layout`` CANONICAL scheme (``data/products/nircam/<field>/<filter>/
    <rootname>.fits``). Returns a list of ``UploadTask``; content-hash dedup is
    applied by the caller (unchanged exposures are dropped before upload).

    Freeze guard (epic #261, N7): the canonical ``nircam_exposure`` must be the
    frozen process-phase output. An exposure carrying a combine-phase CFP stamp
    (``CFP_BPIX`` / ``CFP_OUT``) was mutated in place by an old-pipeline combine;
    uploading it would overwrite the pristine canonical in OSN with
    outlier-rejected bytes and poison a later restore. Refuse loudly rather than
    silently clobber — the fix is to regenerate the canonical with
    ``cfpipe nircam process``. ``CFP_MASK`` is allowed: apply_mask legitimately
    records the manual mask on the canonical.
    """
    stamped = sorted(
        f'{filtname}/{basename}'
        for (filtname, basename), info in exposures.items()
        if info.get('combine_stamped')
    )
    if stamped:
        raise RuntimeError(
            "Refusing to deploy combine-mutated canonical exposures — these carry "
            "CFP_BPIX/CFP_OUT (bad-pixel/outlier flags baked into the canonical by "
            "an old-pipeline combine). Re-run `cfpipe nircam process` to regenerate "
            "the frozen canonical, then redeploy:\n  " + "\n  ".join(stamped)
        )

    tasks = []
    for (filtname, basename), info in sorted(exposures.items()):
        path = info['path']
        key = storage_key('nircam_exposure', Scope(field=field, filt=filtname),
                          path.name, scheme=KeyScheme.CANONICAL)
        tasks.append(UploadTask(local_path=path, r2_key=key,
                                content_type=_FITS_CONTENT_TYPE))
    return tasks


# Stage-decorated expmap variants that are NOT deployed. The fiducial coverage map
# is the undecorated ``expmap_<field>_<filter>.fits``; ``_uncal`` is a reducer-only
# raw quick-look, and ``_canonical`` is the pre-rename legacy name for what is now
# the undecorated fiducial (a stale copy may still sit on a reducer's disk). Deploy
# ships only the fiducial map so users download a single, unqualified exposure map.
# Reducer-only quick-look / pre-rename leftovers, keyed on the filename *stem*
# so the check covers both the .fits map and its .png plot.
_EXPMAP_NONFIDUCIAL_STEMS = ('_uncal', '_canonical')

# The per-filter expmap products deployed together: the coverage FITS and the
# dark web plot PNG (NIRCam page redesign). product_type is key-derived at
# registration, so each just needs its canonical key + content type.
_EXPMAP_DEPLOY = (
    ('expmap_*.fits', 'nircam_expmap', _FITS_CONTENT_TYPE),
    ('expmap_*.png', 'nircam_expmap_plot', _PNG_CONTENT_TYPE),
)


def discover_expmap_tasks(dirs, field, filters):
    """Build canonical-key OSN upload tasks for the per-filter fiducial expmaps.

    Expmaps are per-``(field, filter)`` coverage maps written into the canonical
    filter directory (``products/nircam/<field>/<filter>/expmap_*``), alongside the
    mosaics/exposures. Deploys both the coverage **FITS** and its dark web **PNG**
    plot; only the **fiducial** map (``expmap_<field>_<filter>.{fits,png}``, no stage
    suffix) is shipped — the reducer-only ``_uncal`` quick-look (and any pre-rename
    ``_canonical`` leftover) is skipped so a user gets a single, unqualified map. The
    light-mode ``.pdf`` diagnostic stays local. Produced at combine time, so a
    process-only field may have none yet — deploy is idempotent and picks them up on
    a later run. Returns a list of ``UploadTask`` (empty if none found).
    """
    products = dirs['products']
    tasks = []
    for filtname in filters:
        filter_dir = products / filtname
        if not filter_dir.exists():
            continue
        for pattern, ptype, content_type in _EXPMAP_DEPLOY:
            for path in sorted(filter_dir.glob(pattern)):
                if path.stem.endswith(_EXPMAP_NONFIDUCIAL_STEMS):
                    continue
                key = storage_key(ptype, Scope(field=field, filt=filtname),
                                  path.name, scheme=KeyScheme.CANONICAL)
                tasks.append(UploadTask(local_path=path, r2_key=key,
                                        content_type=content_type))
    return tasks


def discover_layout_tasks(dirs, field):
    """Canonical-key OSN upload task for the field layout plot, if present.

    ``<field>_layout.png`` (stacked-filter coverage + tile outlines) lives at the
    per-field products root and is the landing-page card preview. Only the fiducial
    (undecorated) plot is deployed; the ``_uncal`` quick-look and the ``.json``
    coverage sidecar (read locally by the field-registry upsert) stay off OSN.
    """
    path = dirs['products'] / f'{field}_layout.png'
    if not path.exists():
        return []
    key = storage_key('nircam_layout', Scope(field=field), path.name,
                      scheme=KeyScheme.CANONICAL)
    return [UploadTask(local_path=path, r2_key=key,
                       content_type=_PNG_CONTENT_TYPE)]


# ---------------------------------------------------------------------------
# Deploy (push)
# ---------------------------------------------------------------------------

def _provenance_from_header(path):
    """``(CMPFRVER, CAL_VER, CRDS_CTX)`` from a FITS primary header, or a None
    triple on any read failure."""
    try:
        with fits.open(path, memmap=False) as hdul:
            hdr = hdul[0].header
            return hdr.get('CMPFRVER'), hdr.get('CAL_VER'), hdr.get('CRDS_CTX')
    except Exception:
        return None, None, None


def _read_field_provenance(dirs, field, filters, *, prefer_mosaic=True):
    """Best-effort ``(cfpipe_version, jwst_version, crds_context)`` for a field.

    Prefer the local ``i2d`` mosaic header (the combined science product) — the
    i2d is no longer uploaded to the cloud, but ``discover_mosaics`` still
    surfaces it locally, and it is the only mosaic product that carries the
    provenance cards (the split ``_sci/_err/_wht`` extensions inherit the SCI
    *extension* header and carry none). Fall back to any canonical exposure
    header. Both carry the CAMPFIRE ``CMPFRVER`` — the mosaic from
    ``resample.py``, the exposure from ``detector1`` at creation — plus the
    jwst-native ``CAL_VER`` / ``CRDS_CTX``. So a mid-reduction ``--draft``
    exposure deploy (no mosaic yet) now records real provenance instead of NULL
    (audit B2). Returns ``(None, None, None)`` only when the field has neither a
    readable i2d nor a readable exposure.

    ``prefer_mosaic=False`` skips the mosaic entirely and reads the exposures.
    Set it when the mosaics are not part of this deployment (``--no-mosaics``):
    an on-disk i2d left over from an earlier reduction would otherwise stamp the
    deployment with the provenance of a product it is not shipping, and the
    whole point of that mode is that the exposures have moved on from it.
    """
    try:
        mosaics = discover_mosaics(dirs, field, filters) if prefer_mosaic else []
    except Exception:
        mosaics = []
    if mosaics:
        chosen = next((m for m in mosaics if m.get('extension') == 'i2d'), mosaics[0])
        prov = _provenance_from_header(chosen['path'])
        if any(prov):
            return prov

    # No mosaic (or an unstamped one): read a representative exposure header.
    try:
        exposures = discover_exposures(dirs, filters)
    except Exception:
        return None, None, None
    for info in exposures.values():
        prov = _provenance_from_header(info['path'])
        if any(prov):
            return prov
    return None, None, None


def deploy_nircam(field, config, filters=None, dry_run=False, draft=False,
                  products=None, tiles=None, steal_lease=False, auto_approve=False,
                  explicit_products=False):
    """Deploy a NIRCam field: plan, confirm, execute (epic #261, N1/N2).

    Records a **field-scoped deployment** (provenance + the draft->published
    lifecycle), uploads the canonical exposure FITS, field expmaps, preview and
    layout PNGs and the field's mosaics (+ their thumbnails) to OSN under
    ``campfire-layout`` canonical keys (exposures content-hash deduped on
    ``sha256(SCI+DQ+CFMASK+CFSPIKE)`` and their WCS digest, everything else on
    the whole file), upserts ``nircam_exposures`` and the ``nircam_images``
    mosaic index, and registers every landed object in ``storage_objects``
    tagged with that ``deployment_id``.

    The plan (:class:`~campfire.deploy.deploy_plan.NircamDeployPlan`) is built
    first, from the products on disk plus a read-only check against the
    database and the storage registry, and printed. ``dry_run`` stops there;
    otherwise one confirmation (skipped by *auto_approve*) and the plan is
    executed as printed.

    Visibility rides the deployment (not a bespoke admin-only special case): a
    ``published`` field deployment is public to everyone (NIRCam fields span
    multiple programs, so there is no per-program scope); ``--draft`` keeps it
    admin-only for portal inspection until ``campfire deploy publish --field``.

    Parameters
    ----------
    field : str
        Field name.
    config : dict
        Deploy config (Supabase + R2/OSN credentials).
    filters : list of str, optional
        Filters to deploy. If None, discovers every directory under
        ``products/nircam/{field}/``.
    dry_run : bool
        If True, print the plan without making changes.
    draft : bool
        If True, record the deployment as ``draft`` (admin-only) for review;
        otherwise ``published`` (public) immediately.
    products : list of str, optional
        What to deploy, in the ``campfire push im`` vocabulary
        (:data:`campfire.modality.IMAGING`): ``exposures`` (the exposure FITS,
        their preview PNGs and the ``nircam_exposures`` rows), ``expmaps`` (the
        coverage maps and the field layout plot) and ``mosaics`` (the mosaic
        FITS, their thumbnails and the ``nircam_images`` index). ``None`` deploys
        all three. A product left out is not discovered at all: e.g. without
        ``mosaics`` (``--no-mosaics``, the inspect-then-rebuild loop, where the
        mosaics built before the masks exist are superseded by the post-mask
        re-combine) a stray on-disk tile cannot abort the deploy.
    tiles : list of str, optional
        Narrow the mosaics to these fields.toml tiles.
    explicit_products : bool
        *products* was named by the user (``--products``): a named product with
        nothing on disk is then an error, as for push and pull.
    auto_approve : bool
        Skip the confirmation prompt.
    """
    plan = _build_nircam_plan(field, filters, draft=draft, products=products, tiles=tiles,
                              explicit_products=explicit_products)
    if plan is None:
        return

    from campfire.deploy.push import open_reducer_store
    client = get_supabase_client(config)
    store = open_reducer_store()
    _check_nircam_plan_remote(client, plan, store)

    print()
    print(render_nircam_plan(plan, dry_run=dry_run))
    print()
    if dry_run:
        return
    # The one interactive gate, before the lease, the deployment row or any byte.
    if not auto_approve:
        resp = input("Proceed? [y/N]: ")
        if resp.strip().lower() != 'y':
            print("Aborted.")
            return
    _execute_nircam_plan(plan, client, store, config, steal_lease=steal_lease)


def _build_nircam_plan(field, filters, *, draft, products=None, tiles=None,
                       explicit_products=False):
    """The local half of a field's deploy plan, or None when there is nothing
    to deploy (the reason is printed). Reads headers and PNG sizes; touches no
    network. *products* / *tiles* as for :func:`deploy_nircam`."""
    products = list(products) if products else list(IMAGING_PRODUCTS)
    want = set(products)
    dirs = _resolve_nircam_dirs(field)
    if not dirs['products'].exists():
        print(f"Error: Products directory not found: {dirs['products']}")
        sys.exit(1)

    warnings = []
    available = _discover_filters(dirs)
    if filters:
        missing = [f for f in filters if f not in available]
        if missing:
            warnings.append(f"No products for filter(s): {', '.join(missing)}")
        filters = [f for f in filters if f in available]
    else:
        filters = available
    if not filters:
        print("No filters found to deploy.")
        return None

    # A product left out of the selection is not discovered at all.
    exposures = discover_exposures(dirs, filters) if 'exposures' in want else {}
    expmap_tasks, layout_tasks = [], []
    if 'expmaps' in want:
        expmap_tasks = discover_expmap_tasks(dirs, field, filters)
        layout_tasks = discover_layout_tasks(dirs, field)
    # Scope mosaic discovery to what the current fields.toml declares: a stray
    # on-disk tile/epoch from a former config must not ride into the cloud.
    # Without mosaics in the selection skip discovery too, not just the upload:
    # the fields.toml scope check would otherwise abort a good exposure deploy
    # over a mosaic we were never going to send.
    mosaics = []
    if 'mosaics' in want:
        try:
            mosaics = scope_mosaics_to_fields_toml(
                deployable_mosaics(discover_mosaics(dirs, field, filters)), field)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)
        if tiles:
            mosaics = [m for m in mosaics if m.get('tile') in set(tiles)]

    # A product the user named that matches nothing is an invalid selection
    # (the modality rule push and pull follow), never a silent no-op.
    if explicit_products:
        found = {'exposures': bool(exposures),
                 'expmaps': bool(expmap_tasks or layout_tasks),
                 'mosaics': bool(mosaics)}
        empty = [p for p in products if not found.get(p)]
        if empty:
            print(f"Error: nothing on disk for --products {', '.join(empty)} in field "
                  f"{field} (filters: {', '.join(filters)}"
                  + (f"; tiles: {', '.join(tiles)}" if tiles else '') + ").")
            sys.exit(1)
    # Deploy whatever the field has: mosaics/expmaps are independent of the
    # per-exposure FITS (a field can keep its science mosaics after the large cal
    # exposures are pruned). Only bail if there is genuinely nothing to ship.
    if not exposures and not expmap_tasks and not mosaics and not layout_tasks:
        print(f"Nothing to deploy for field {field}.")
        return None

    png_tasks = build_upload_tasks(dirs, field, filters) if 'exposures' in want else []
    thumb_r2_keys = {(f, b): t.r2_key for t, b, f, kind in png_tasks if kind == 'thumb'}
    full_r2_keys = {(f, b): t.r2_key for t, b, f, kind in png_tasks if kind == 'full'}
    # Image dimensions come from the full-res PNG (= native exposure shape), so
    # the web canvas can place mask polygons in DS9 image coords without ever
    # needing the FITS file.
    full_dims = {(f, b): _read_full_png_dimensions(t.local_path)
                 for t, b, f, kind in png_tasks if kind == 'full'}

    fits_tasks = build_fits_upload_tasks(field, exposures)
    records = []
    for (filtname, basename), info in sorted(exposures.items()):
        dims = full_dims.get((filtname, basename))
        records.append({
            'field': field,
            'filter': filtname,
            'detector': info['detector'] or 'unknown',
            'filename': basename,
            'visit': info['visit'],
            'date_obs': info['date_obs'],
            'ra_center': info['ra_center'],
            'dec_center': info['dec_center'],
            'stage': info['stage'],
            'png_path': thumb_r2_keys.get((filtname, basename)),
            'full_png_path': full_r2_keys.get((filtname, basename)),
            'image_width': dims[0] if dims else None,
            'image_height': dims[1] if dims else None,
        })

    mosaic_tasks = [UploadTask(local_path=m['path'], r2_key=m['storage_key'],
                               content_type=m['content_type']) for m in mosaics]
    # Per-mosaic science thumbnails ride the same plan/registration: they get a
    # nircam_mosaic_thumbnail storage_objects row (key-derived product_type) but
    # NO nircam_images row; the web shows them on the sci extension row.
    mosaic_thumb_tasks = discover_mosaic_thumbnail_tasks(mosaics, field)

    # Batch-level provenance (audit B2). Without mosaics in the selection they
    # are not part of this deployment, so a stale on-disk i2d must not supply
    # it: read the exposures instead.
    cfpipe_version, jwst_version, crds_context = _read_field_provenance(
        dirs, field, filters, prefer_mosaic='mosaics' in want)

    from campfire.deploy.fields import load_fields_toml
    try:
        in_fields_toml = field in load_fields_toml()
    except Exception:  # noqa: BLE001 - the registry upsert is best-effort too
        in_fields_toml = False

    return NircamDeployPlan(
        field=field,
        filters=filters,
        file_groups=[
            FileGroup('Exposures (FITS)', fits_tasks),
            FileGroup('Exposure previews (PNG)', [t[0] for t in png_tasks]),
            FileGroup('Exposure maps (FITS)', expmap_tasks),
            FileGroup('Layout plots', layout_tasks),
            FileGroup('Mosaics (FITS)', mosaic_tasks),
            FileGroup('Mosaic thumbnails (PNG)', mosaic_thumb_tasks),
        ],
        records=records,
        mosaics=mosaics,
        cfpipe_version=cfpipe_version,
        jwst_version=jwst_version,
        crds_context=crds_context,
        in_fields_toml=in_fields_toml,
        draft=draft,
        products=products,
        tiles=list(tiles) if tiles else [],
        warnings=warnings,
        fits_keys={t.r2_key for t in fits_tasks},
    )


def _check_nircam_plan_remote(client, plan, store, *, progress=True):
    """The remote half of the plan, read-only: which exposure rows exist and
    which files the cloud registry already holds (no wcs_hash backfill, so a
    dry run writes nothing to the server)."""
    from campfire.deploy.push import plan_remote_push
    plan.existing_exposures = len(_existing_exposure_keys(client, plan.records))
    plan.push, _ = plan_remote_push(client, store, plan.upload_tasks,
                                    backend=_CANONICAL_BACKEND, progress=progress)


def _execute_nircam_plan(plan, client, store, config, *, steal_lease=False):
    """Carry out a confirmed field plan: deployment, one upload, catalog, summary."""
    from campfire.deploy.push import plan_remote_push, registration_flusher
    from campfire.deploy.registry import set_active_deployment

    field, draft = plan.field, plan.draft
    user_id = get_user_id_from_token(config)

    # Deploy lease (data releases P1) + the B4 / D12 optimistic-concurrency
    # version: the lease makes this deploy visible while it runs (refusing —
    # DeployScopeBusy, surfaced by the CLI with --steal-lease — if another
    # deploy of this field is live; a lease with no heartbeat for 30 minutes is
    # taken over as stale), and the version is compare-and-set at finalize so a
    # concurrent same-field deploy is detected.
    lease = ScopeLease(client, 'field', field, actor=user_id, steal=steal_lease)
    lease.begin()

    # Re-check the upload set under the lease (the plan's check ran before the
    # prompt): cheap, since files the first pass confirmed take the stat fast
    # path. This pass writes the legacy exposure wcs_hash backfill a dry run
    # must not.
    push_plan, _ = plan_remote_push(client, store, plan.upload_tasks,
                                    backend=_CANONICAL_BACKEND, progress=False,
                                    apply_backfill=True)
    if plan.push is not None and (
            len(push_plan.to_upload), len(push_plan.unchanged)) != (
            len(plan.push.to_upload), len(plan.push.unchanged)):
        print(f"Note: the cloud registry changed since the plan was made; "
              f"now {push_plan.summary()}")

    # The field-scoped deployment is opened before any upload: it is the
    # provenance anchor and the draft/published visibility gate every
    # registered object hangs off, and each upload batch registers against it
    # as it lands (so an interrupted deploy resumes with its uploads attached).
    deployment_id = insert_deployment(
        client, field=field, deployed_by=user_id,
        status='draft' if draft else 'published', n_targets=len(plan.records),
        cfpipe_version=plan.cfpipe_version, jwst_version=plan.jwst_version,
        crds_context=plan.crds_context)
    status_word = 'draft, admin-only' if draft else 'published, public'
    print(f"Deployment #{deployment_id} opened ({status_word})")

    # ---- Upload: one pass, one byte bar ------------------------------------
    # Mosaic rows register with their stored (gzipped) size and no cfpipe
    # stamp; everything else with the field's cfpipe version — two flushers fed
    # from the one upload, routed by key.
    upload_workers = _upload_workers(_CANONICAL_BACKEND)
    mosaic_keys = plan.mosaic_keys
    stored_sizes: dict[str, int] = {}
    tree_flusher = registration_flusher(
        client, store, push_plan, backend=_CANONICAL_BACKEND,
        deployment_id=deployment_id, uploaded_by=user_id,
        cfpipe_version=plan.cfpipe_version, max_workers=upload_workers,
        heartbeat=lease.heartbeat)
    mosaic_flusher = registration_flusher(
        client, store, push_plan, backend=_CANONICAL_BACKEND,
        deployment_id=deployment_id, uploaded_by=user_id,
        max_workers=upload_workers, stored_sizes=stored_sizes,
        heartbeat=lease.heartbeat)

    def _register(task):
        (mosaic_flusher if task.r2_key in mosaic_keys else tree_flusher).add(task)

    uploaded: set[str] = set()
    n_failed = 0
    up_bytes = 0
    if push_plan.to_upload:
        up_bytes = sum(path_size(t.local_path) for t in push_plan.to_upload)
        print(f"\nUploading {len(push_plan.to_upload):,} file(s), "
              f"{format_bytes(up_bytes)} → OSN")
        _success, n_failed, failures = upload_files_parallel(
            config, push_plan.to_upload, desc='OSN upload', max_workers=upload_workers,
            succeeded_out=uploaded, backend=_CANONICAL_BACKEND,
            on_success=_register, stored_sizes_out=stored_sizes)
        tree_flusher.flush()
        mosaic_flusher.flush()
        if failures:
            print(f"  {n_failed} upload(s) failed:")
            for msg in failures[:10]:
                print(f"    - {msg}")
            if len(failures) > 10:
                print(f"    ... and {len(failures) - 10} more")
    else:
        print("\nNothing to upload: the cloud already holds every file")

    # ---- Catalog ------------------------------------------------------------
    print("\nWriting catalog...")
    n_new = _upsert_exposures(client, plan.records)
    print(f"  {len(plan.records):,} exposures ({n_new:,} new)")

    unchanged_keys = {t.r2_key for t in push_plan.unchanged}
    n_img = 0
    if plan.mosaics:
        # Local size for nircam_images.file_size: the plan stat()s every file.
        for m in plan.mosaics:
            m['size'] = push_plan.stats.get(m['storage_key'], (None, None))[1]
        # A failed upload must NOT get a nircam_images row (it would advertise a
        # file_path with no object and no registry row; the slot 404s). A re-run
        # heals. PUBLISHED: (re-)index every present mosaic under this
        # deployment. DRAFT is staging: index only the new/changed (uploaded)
        # mosaics, and leave unchanged, already-published ones on their live
        # deployment so the field doesn't go dark.
        present = uploaded | unchanged_keys
        img_status = 'draft' if draft else 'published'
        indexable = uploaded if draft else present
        img_rows = [{
            'field': field, 'tile': m['tile'], 'filter': m['filter'],
            'pixel_scale': m['pixel_scale'], 'extension': m['extension'],
            'epoch': m.get('epoch', ''),
            'file_path': m['storage_key'], 'file_size': m['size'],
            'deploy_status': img_status, 'deployment_id': deployment_id,
        } for m in plan.mosaics if m['storage_key'] in indexable]
        n_img = _upsert_nircam_images(client, img_rows)
        note = ''
        n_unindexed = len(mosaic_keys) - n_img if not draft else 0
        if n_unindexed:
            note = f"; {n_unindexed:,} not indexed (upload failed, re-run to retry)"
        print(f"  {n_img:,} mosaic index rows ({img_status}){note}")

    # Re-point dedup-skipped objects (unchanged bytes, not re-registered) to
    # this deployment so a PUBLISHED re-deploy flips the whole field
    # consistently. NOT on a --draft re-deploy: draft is staging, so
    # already-published unchanged objects stay on their published deployment
    # (only the new/changed ones stage as draft), else the live field goes dark.
    if deployment_id and not draft and unchanged_keys:
        set_active_deployment(client, sorted(unchanged_keys - uploaded), deployment_id)

    # Field registry: the fields.toml config + deploy-computed survey area
    # (issue #303), latest_deployment_id pointing here. Best-effort: a missing
    # fields.toml section or layout.json never fails the deploy.
    try:
        from campfire.deploy.fields import upsert_field_on_deploy
        upsert_field_on_deploy(client, _resolve_nircam_dirs(field)['products'], field,
                               deployment_id)
    except Exception as e:  # noqa: BLE001 - registry upsert must never sink a deploy
        print(f"  fields: registry upsert skipped ({e})")

    # ---- Finalize -----------------------------------------------------------
    claim = lease.claim()
    if claim.get('conflict'):
        print(f"⚠  CONCURRENT DEPLOY DETECTED for field '{field}': another reducer "
              f"advanced this field while this deploy was running. Re-run to reconcile.")
    # Audit counts keep their historical meaning: files outside the mosaic
    # stage (exposure FITS, expmaps, PNGs, layout); `skipped` the exposure FITS
    # the dedup left alone.
    n_tree_uploaded = len(uploaded - mosaic_keys)
    n_skipped = sum(1 for k in unchanged_keys if k in plan.fits_keys)
    log_deploy_event(
        client, action='upload', actor=user_id, deployment_id=deployment_id,
        observation=None, field=field, affected_count=n_tree_uploaded,
        metadata=deploy_event_metadata(
            'nircam', field=field, filters=plan.filters, planned=len(plan.records),
            succeeded=n_tree_uploaded, failed=n_failed, skipped=n_skipped,
            items=len(plan.records), draft=draft,
            fits_uploaded=n_tree_uploaded, fits_skipped=n_skipped))

    # If the field's FitsGL map pyramid is missing or its mosaics changed, print a
    # suggested `campfire fitsgl build/deploy` — a hint only, never an auto-build
    # (pyramid builds are expensive; epic #337, Phase 3). Best-effort + fully guarded
    # so a missing fitsgl extra / dataset table never touches the deploy result.
    try:
        from campfire.fitsgl.deploy import suggest_fitsgl_rebuild
        suggest_fitsgl_rebuild(client, field)
    except Exception:
        pass

    # Closing summary: one block stating what happened, after the stage output.
    print()
    print(f"Deploy complete: field {field} → deployment #{deployment_id} "
          f"({'draft, admin-only' if draft else 'published'})")
    print(f"  files:    {len(uploaded):,} uploaded ({format_bytes(up_bytes)}), "
          f"{len(unchanged_keys):,} already in the cloud"
          + (f", {n_failed:,} FAILED (re-run to retry)" if n_failed else ''))
    catalog = [f"{len(plan.records):,} exposures ({n_new:,} new)"]
    if plan.mosaics:
        catalog.append(f"{n_img:,} mosaic index rows")
    print(f"  catalog:  {', '.join(catalog)}")


def _existing_exposure_keys(client, records, batch_size=500):
    """``{(field, filter, filename)}`` of *records* already in ``nircam_exposures``."""
    filenames = [r['filename'] for r in records]
    existing = set()
    for i in range(0, len(filenames), batch_size):
        batch = filenames[i:i + batch_size]
        resp = (client.table('nircam_exposures')
                .select('filename, field, filter')
                .in_('filename', batch)
                .execute())
        for row in resp.data:
            existing.add((row['field'], row['filter'], row['filename']))
    return existing


def _upsert_exposures(client, records, batch_size=500):
    """Upsert exposure records, preserving web-triage fields for existing rows.

    New rows get default ``review_status='pending'``, ``correction='none'``.
    Existing rows: update pipeline-derived columns only (``stage``,
    ``png_path``, metadata). Preserve web-owned ``review_status``,
    ``correction``, ``mask_regions``, ``notes``. Returns the number of new rows.
    """
    if not records:
        return 0

    existing = _existing_exposure_keys(client, records, batch_size)

    new_records = []
    update_records = []
    now = datetime.now(timezone.utc).isoformat()

    for r in records:
        key = (r['field'], r['filter'], r['filename'])
        if key in existing:
            update = {
                'field': r['field'],
                'filter': r['filter'],
                'filename': r['filename'],
                'stage': r['stage'],
                'detector': r['detector'],
                'updated_at': now,
            }
            if r.get('png_path'):
                update['png_path'] = r['png_path']
            if r.get('full_png_path'):
                update['full_png_path'] = r['full_png_path']
            if r.get('image_width') is not None:
                update['image_width'] = r['image_width']
            if r.get('image_height') is not None:
                update['image_height'] = r['image_height']
            if r.get('visit'):
                update['visit'] = r['visit']
            if r.get('date_obs'):
                update['date_obs'] = r['date_obs']
            if r.get('ra_center') is not None:
                update['ra_center'] = r['ra_center']
            if r.get('dec_center') is not None:
                update['dec_center'] = r['dec_center']
            update_records.append(update)
        else:
            new = {
                **r,
                'review_status': 'pending',
                'correction': 'none',
                'created_at': now,
                'updated_at': now,
            }
            new_records.append(new)

    for i in range(0, len(new_records), batch_size):
        batch = new_records[i:i + batch_size]
        client.table('nircam_exposures').insert(batch).execute()

    for i in range(0, len(update_records), batch_size):
        batch = update_records[i:i + batch_size]
        client.table('nircam_exposures').upsert(
            batch,
            on_conflict='field,filter,filename',
        ).execute()
    return len(new_records)


# ---------------------------------------------------------------------------
# Mosaic deploy (epic #261, N2) — combined mosaics → OSN + nircam_images index
# ---------------------------------------------------------------------------

# Split-extension products the resample step emits, mapped to the nircam_images
# `extension` value. A slot deploys only the files that exist on disk.
_MOSAIC_EXTENSIONS = (
    ('_i2d.fits', 'i2d'),
    ('_sci.fits', 'sci'),
    ('_err.fits', 'err'),
    ('_wht.fits', 'wht'),
    ('_srcmask.fits', 'srcmask'),
    # Per-tile exposure map (EXPTIME + NEXP): the mosaic-scale member of the 'exp'
    # family whose field-wide, coarse member is the nircam_expmap product.
    ('_exp.fits', 'exp'),
)

# The i2d cube is the multi-extension science product the split _sci/_err/_wht
# extensions are carved from — heavy, and redundant for users, who download the
# split extensions. It stays *discovered* (discover_mosaics still returns it)
# because local consumers depend on it: the deployment provenance stamp reads its
# primary-header CMPFRVER/CAL_VER/CRDS_CTX cards (the split extensions inherit
# only the SCI *extension* header and carry none of them — see resample.py), and
# the FitsGL map pyramid falls back to it when a tile has no split sci. It is
# simply never uploaded to the cloud or indexed in nircam_images. Every
# cloud-bound path runs discovered mosaics through `deployable_mosaics` first.
_UNDEPLOYED_MOSAIC_EXTENSIONS = frozenset({'i2d'})


def deployable_mosaics(mosaics):
    """Discovered mosaics minus the extensions we don't ship to the cloud (i2d).

    ``discover_mosaics`` intentionally returns every extension on disk because
    local consumers need i2d (provenance stamping, the FitsGL pyramid fallback);
    the deploy and ``campfire deploy push`` paths run its output through this
    filter so i2d bytes never leave the reducer and no ``nircam_images`` i2d row
    is written.
    """
    return [m for m in mosaics
            if m['extension'] not in _UNDEPLOYED_MOSAIC_EXTENSIONS]


def discover_mosaics(dirs, field, filters):
    """Discover mosaic products via their manifests.

    Returns every extension present on disk, including the ``i2d`` cube — local
    consumers (deployment provenance, the FitsGL pyramid) read it. The cloud
    upload/index paths drop i2d via :func:`deployable_mosaics`; discovery itself
    stays complete.

    Each ``mosaic_*_manifest.json`` (written by the resample step) carries the
    authoritative ``(mosaic_name, filter, tile, pixel_scale, epoch)`` — read from
    the manifest rather than parsed from the filename, so a multi-underscore
    field name can't break a positional split. Returns one dict per existing
    mosaic file:
    ``{path, filter, tile, pixel_scale, epoch, extension, storage_key}`` under
    the version-free canonical key (epic #261, N2 / D3). ``epoch`` is ``''`` for
    a full-field mosaic and the epoch name for a subset mosaic; it rides in the
    filename so the storage key is unique without any layout change.
    """
    import json
    products = dirs['products']
    out = []
    for filtname in filters:
        filter_dir = products / filtname
        if not filter_dir.exists():
            continue
        for manifest_path in sorted(filter_dir.glob('mosaic_*_manifest.json')):
            try:
                m = json.loads(manifest_path.read_text())
            except Exception:
                continue
            base = m.get('mosaic_name')
            tile = m.get('tile')
            pixel_scale = m.get('pixel_scale')
            mfilter = m.get('filter') or filtname
            mfield = m.get('field') or field
            epoch = m.get('epoch') or ''
            if not (base and tile and pixel_scale):
                continue
            # Skip stale pre-N2 versioned / `_latest_` manifests: only the
            # canonical version-free name is deployable (epic #261, D3). A field
            # reduced before N2 keeps its old `..._v0_1_..._manifest.json` on
            # disk after re-combine; without this guard that stale slot would
            # both re-upload a version-bearing key to OSN AND collide with the
            # version-free row on the nircam_images (field,tile,filter,scale,
            # extension,epoch) conflict key, crashing the batch upsert. Reconstruct
            # the expected name deploy-side (deploy must not import the pipeline) —
            # the same version-free contract rgb._find_mosaic enforces. An epoch
            # mosaic appends a trailing ``_<epoch>`` segment (see
            # manifest.build_mosaic_name), so fold it into the expected name.
            expected = f'mosaic_nircam_{mfilter}_{mfield}_{pixel_scale}_{tile}'
            if epoch:
                expected += f'_{epoch}'
            if base != expected:
                continue
            for suffix, ext in _MOSAIC_EXTENSIONS:
                fpath = filter_dir / f'{base}{suffix}'
                if not fpath.exists():
                    continue
                key = storage_key('nircam_mosaic', Scope(field=field, filt=filtname),
                                  fpath.name, scheme=KeyScheme.CANONICAL)
                out.append({
                    'path': fpath, 'filter': mfilter, 'tile': tile,
                    'pixel_scale': pixel_scale, 'epoch': epoch,
                    'extension': ext, 'storage_key': key,
                    'content_type': (_GZIP_CONTENT_TYPE if is_compressed_key(key)
                                     else _FITS_CONTENT_TYPE),
                    'mosaic_name': base,
                })
    return out


def scope_mosaics_to_fields_toml(mosaics, field):
    """Drop discovered mosaics whose ``(tile, epoch)`` the current fields.toml
    does not declare for ``field``.

    ``discover_mosaics`` walks the products tree and returns whatever mosaic
    manifests are physically present — including stray products left over from a
    former config (a commented-out tile, a renamed or removed epoch). fields.toml
    is the source of truth for what the *current* config asks the pipeline to
    build, so the cloud-bound paths (``campfire deploy --field`` / ``campfire
    push``) run discovery through this filter: a mosaic whose tile isn't a declared
    tile — or whose epoch isn't a declared epoch — is skipped (logged, not silently
    dropped) and never uploaded to OSN or indexed in ``nircam_images``.

    Raises ``ValueError`` if the field has mosaics on disk but no ``[<field>]``
    section in fields.toml: we can't tell declared from stray, so refuse to deploy
    unscoped rather than blindly ship the tree (that is exactly the footgun this
    guards against).
    """
    if not mosaics:
        return mosaics
    from campfire.deploy.fields import declared_tiles_and_epochs, load_fields_toml
    section = load_fields_toml().get(field)
    if section is None:
        raise ValueError(
            f"field '{field}' has {len(mosaics)} mosaic product(s) on disk but no "
            f"[{field}] section in fields.toml — refusing to deploy unscoped. Add "
            f"the [{field}] section, or remove the stray products from the tree.")
    tiles, epochs = declared_tiles_and_epochs(section)
    kept, dropped = [], []
    for m in mosaics:
        tile_ok = m['tile'] in tiles
        epoch_ok = (not m.get('epoch')) or m['epoch'] in epochs
        (kept if tile_ok and epoch_ok else dropped).append(m)
    for tile, epoch in sorted({(m['tile'], m.get('epoch') or '') for m in dropped}):
        why = ("tile not declared" if tile not in tiles
               else f"epoch '{epoch}' not declared")
        label = f"tile '{tile}'" + (f" epoch '{epoch}'" if epoch else "")
        print(f"  mosaics: skipping {label} — {why} in fields.toml "
              f"(stray on-disk product, not deployed)")
    return kept


# The thumbnail pair the pipeline writes per mosaic base (derived from the
# i2d): a small table rendition + a large quick-look for the web popup.
_MOSAIC_PNG_RENDITIONS = (
    ('_thumb.png', 'nircam_mosaic_thumbnail'),
    ('_quicklook.png', 'nircam_mosaic_quicklook'),
)


def discover_mosaic_thumbnail_tasks(mosaics, field):
    """Upload tasks for each mosaic base's PNG renditions (thumbnail pair).

    The pipeline writes ``<base>_thumb.png`` (small, table) and
    ``<base>_quicklook.png`` (large, popup) per mosaic, so this dedups the
    per-extension ``mosaics`` list to one pass per base and emits a task per
    rendition that exists on disk. The scope filter is the mosaic's own
    directory name, so the keys sit beside their mosaic FITS. Fields reduced
    before a rendition existed simply skip it.
    """
    tasks = []
    seen = set()
    for m in mosaics:
        filt = m['path'].parent.name
        key_id = (filt, m['mosaic_name'])
        if key_id in seen:
            continue
        seen.add(key_id)
        for suffix, product in _MOSAIC_PNG_RENDITIONS:
            png_path = m['path'].parent / f"{m['mosaic_name']}{suffix}"
            if png_path.exists():
                tasks.append(UploadTask(
                    local_path=png_path,
                    r2_key=storage_key(product,
                                       Scope(field=field, filt=filt),
                                       png_path.name, scheme=KeyScheme.CANONICAL),
                    content_type=_PNG_CONTENT_TYPE))
    return tasks


def _upsert_nircam_images(client, rows, batch_size=500):
    """Upsert nircam_images rows keyed on
    (field, tile, filter, pixel_scale, extension, epoch)."""
    if not rows:
        return 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        client.table('nircam_images').upsert(
            batch, on_conflict='field,tile,filter,pixel_scale,extension,epoch',
        ).execute()
    return len(rows)
