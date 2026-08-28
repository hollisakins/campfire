"""
resample: drizzle-combine canonical exposures into mosaic tiles.

Per-tile ensemble step. Selects exposures whose footprints intersect each
tile polygon, builds an ASN, and runs ``Image3Pipeline`` (resample only) to
produce ``_i2d.fits`` mosaic tiles in ``field.filter_dir(filter)`` — the
same flat directory that holds the canonical exposures, split-extension
files, and manifests for that (field, filter) pair.

Input source for the canonical-exposure layout is
``field.get_exposure_files(filter, with_step='CFP_OUT')`` — only exposures
that have completed outlier detection are eligible to be drizzled.

Mosaic outputs: ``CMPFRTIM`` / ``CMPFRVER`` stamping on the primary header,
optional 2D background subtraction via ``SubtractBackground``, and optional
extension splitting into ``_sci/_err/_wht/_srcmask`` files. The mosaic
basename is version-free (epic #261, N2 / D3) — one canonical name per
``(field, filter, tile, pixel_scale)`` — so there is no ``_latest_`` alias.

Parallelism
-----------
Tiles are independent: the per-tile pipeline reads only the (combine-frozen)
working-copy exposures and writes only tile-named outputs, so tiles are
dispatched across a forkserver pool bounded by ``-p``/``--processes``. ``-p``
is a **ceiling, not a target**: a memory scheduler picks the actual pool
width from a budget of ``MemAvailable`` (see :func:`_plan_tile_pool`), and a
weighted :class:`~campfire_pipeline.common.parallel.MemoryGate` admits the
drizzle and the ~2.5-3x-heavier bkgsub/split/plot stages separately, re-checking
live ``MemAvailable`` at every admission — these are shared nodes, and an OOM
kills a multi-hour combine. ``--processes 1`` (the default) runs the exact
serial tile loop, ordering-stable, with no gate and fail-fast errors.
"""

import math
import os
import shutil
import traceback
from contextlib import nullcontext
from datetime import datetime, timezone

import numpy as np
from astropy.io import fits
from shapely.geometry import Polygon

from campfire_pipeline.common.io import log, set_log_prefix
from campfire_pipeline.common.parallel import (
    MemoryGate, _RetryOnIOError, dispatch, mem_available_bytes,
)
from campfire_pipeline.nircam.geometry import (
    exposure_footprints, select_overlapping_files,
)


def _resolve_pixel_scale(value):
    """Return ``(scale_arcsec_float, scale_str)`` from a config value."""
    if isinstance(value, str):
        assert value.endswith('mas')
        return float(value[:-3]) / 1000, str(value)
    if value > 1:
        return float(value) / 1000, f'{int(value)}mas'
    return float(value), f'{int(value * 1000)}mas'


def _drizzle_tile_via_campfire(
    selected_files,
    output_path,
    *,
    crpix,
    crval,
    shape,
    rotation,
    pixel_scale,
    resample_cfg,
    reduction_version,
):
    """Drizzle ``selected_files`` to ``output_path`` via the campfire-native
    drizzle (issue #138).

    Same observable contract as ``_drizzle_tile_via_jwst`` — produces an i2d
    at ``output_path`` with SCI/ERR/WHT/CON extensions and ``CMPFRTIM`` /
    ``CMPFRVER`` stamped on the primary header.
    """
    from campfire_pipeline.nircam.drizzle import drizzle_tile

    drizzle_tile(
        selected_files,
        output_path,
        crpix=crpix,
        crval=crval,
        shape=shape,
        rotation=rotation,
        pixel_scale=pixel_scale,
        pixfrac=resample_cfg.get('pixfrac', 1.0),
        kernel=resample_cfg.get('kernel', 'square'),
        weight_type=resample_cfg.get('weight_type', 'ivm'),
        good_bits=resample_cfg.get('good_bits', '~DO_NOT_USE'),
        blendheaders=resample_cfg.get('blendheaders', True),
        reduction_version=reduction_version,
        compress_context=resample_cfg.get('compress_context', True),
        write_context=resample_cfg.get('write_context', True),
    )


def _drizzle_tile_via_jwst(
    selected_files,
    output_path,
    *,
    crpix,
    crval,
    shape,
    rotation,
    pixel_scale,
    resample_cfg,
    reduction_version,
):
    """Drizzle ``selected_files`` to ``output_path`` via JWST ``Image3Pipeline``.

    Builds an ASN next to ``output_path``, runs ``Image3Pipeline`` with
    every substep but ``resample`` skipped, and stamps ``CMPFRTIM`` /
    ``CMPFRVER`` on the primary header of the resulting i2d.

    ``[nircam.resample].compress_context`` is honored here too, but it costs
    more than on the campfire backend: the write happens inside jwst's own
    resample step, so the uncompressed CON hits the disk first and is then
    rewritten compressed (one extra read+write of the i2d). The campfire
    backend instead saves a placeholder and never materialises it. Peak RSS is
    unaffected either way — the rewrite streams the CON back through the memmap
    tile-by-tile rather than holding it resident.

    Parameters
    ----------
    selected_files : list of str
        CRF input paths.
    output_path : str
        Destination i2d path. Must end in ``_i2d.fits``.
    crpix, crval, shape, rotation : tile WCS parameters from
        ``Field.get_tile_wcs``.
    pixel_scale : float
        Output pixel scale in arcseconds.
    resample_cfg : dict
        ``[nircam.resample]`` config block (used for pixfrac/kernel etc.).
    reduction_version : str
        Stamped as ``CMPFRVER`` on the primary header.
    """
    from jwst.associations.lib.rules_level3_base import DMS_Level3_Base
    from jwst.associations import asn_from_list
    from jwst.pipeline import calwebb_image3

    mosaic_outdir = os.path.dirname(output_path)
    mosaic_name = os.path.basename(output_path).removesuffix('_i2d.fits')

    asn_file = os.path.join(mosaic_outdir, f'{mosaic_name}_asn.json')
    asn = asn_from_list.asn_from_list(
        selected_files, rule=DMS_Level3_Base, product_name=mosaic_name,
    )
    with open(asn_file, 'w') as fp:
        _, serialized = asn.dump(format='json')
        fp.write(serialized)

    params = {
        'assign_mtwcs': {'skip': True},
        'tweakreg': {'skip': True},
        'skymatch': {'skip': True},
        'outlier_detection': {'skip': True},
        'resample': {
            'pixfrac': resample_cfg.get('pixfrac', 1),
            'kernel': resample_cfg.get('kernel', 'square'),
            'pixel_scale': pixel_scale,
            'rotation': rotation,
            'output_shape': shape,
            'crpix': crpix,
            'crval': crval,
            'fillval': 'NaN',
            'weight_type': 'ivm',
            'single': False,
            'blendheaders': True,
            'save_results': True,
        },
        'source_catalog': {'skip': True},
    }

    calwebb_image3.Image3Pipeline.call(
        asn_file, output_dir=mosaic_outdir, steps=params,
        save_results=True,
    )

    if resample_cfg.get('compress_context', True):
        from campfire_pipeline.nircam.drizzle import compress_context_extension

        before = os.path.getsize(output_path)
        if compress_context_extension(output_path):
            after = os.path.getsize(output_path)
            log(f"  compressed CON: {before / 2**30:.1f} GiB → "
                f"{after / 2**30:.1f} GiB "
                f"({before / max(after, 1):.1f}x smaller)")

    with fits.open(output_path, mode='update') as hdul:
        hdul[0].header['CMPFRTIM'] = (
            datetime.now(timezone.utc).isoformat(),
            'UTC date/time of CAMPFIRE reduction (ISO 8601)',
        )
        hdul[0].header['CMPFRVER'] = (
            reduction_version,
            'CAMPFIRE git commit (or pinned version)',
        )


# ---------------------------------------------------------------------------
# Memory-aware tile scheduler
# ---------------------------------------------------------------------------
#
# Per-stage worst-case footprints are estimated from each tile's own geometry
# (output shape × bytes/pixel) rather than hardcoded totals, so the scheduler
# adapts to any field — COSMOS's 21 tiles and A2744's single 1.26 Gpx `full`
# mosaic alike. The calibrated constants live in config
# (`[nircam.resample].mem_*`) and are deliberately absent from the manifest
# config hash: they cannot change pixels, so tuning them never marks tiles
# stale.

# Gate for parallel tile workers. Populated in each pool worker by
# _init_tile_worker (multiprocessing sync primitives must ride the Pool
# initializer, never task arguments); stays None in the parent and on serial
# runs, where _gate_hold degrades to a no-op.
_TILE_GATE = None


def _init_tile_worker(gate):
    global _TILE_GATE
    _TILE_GATE = gate


def _gate_hold(nbytes, label):
    if _TILE_GATE is None:
        return nullcontext()
    return _TILE_GATE.hold(nbytes, label=label)


def _estimate_drizzle_bytes(npix, n_inputs, step_config):
    """Estimated peak bytes for drizzling one ``npix``-pixel tile.

    Both backends hold four float32 full-tile output planes (SCI/WHT and two
    variance accumulators) plus an int32 context cube of ``ceil(n_inputs/32)``
    full-tile planes — for a deep tile the context cube dominates, and it is
    computable exactly, so only the residual scratch (per-input detector
    arrays, pixmaps, header blending) is the calibrated
    ``mem_drizzle_base_bytes_per_pixel`` constant.
    """
    base = float(step_config.get('mem_drizzle_base_bytes_per_pixel', 40.0))
    margin = float(step_config.get('mem_margin', 1.3))
    # With write_context = false no context cube is allocated at all (see
    # drizzle_tile), so charging for it would strand budget and needlessly
    # narrow the pool on exactly the deep tiles this option exists to rescue.
    if not step_config.get('write_context', True):
        return int(npix * base * margin)
    ctx_planes = max(1, math.ceil(max(int(n_inputs), 1) / 32))
    return int(npix * (base + 4 * ctx_planes) * margin)


def _estimate_bkgsub_bytes(npix, step_config):
    """Estimated peak bytes for one tile's bkgsub/split/plot tail.

    ``SubtractBackground`` stacks float64 intermediates (ring-median fill,
    per-tier gaussian convolutions, EDT dilations, Background2D meshes, the
    guard's equalized maps) on top of the float32 SCI/ERR/WHT inputs; the
    extension split then holds full SCI/ERR/WHT/SRCMASK copies. Measured
    51-66 GB per COSMOS LW tile — ~2.5-3x the drizzle — which is what the
    default ``mem_bkgsub_bytes_per_pixel`` is calibrated against.
    """
    bpp = float(step_config.get('mem_bkgsub_bytes_per_pixel', 130.0))
    margin = float(step_config.get('mem_margin', 1.3))
    return int(npix * bpp * margin)


def _plan_tile_pool(tiles, field, pixel_scale_str, step_config, n_processes):
    """Pick the tile-pool width from the memory budget; ``-p`` is a ceiling.

    The pool is sized so the *lightest* possible stage reservation (the
    smallest tile's single-context-plane drizzle) could fill the budget — the
    widest pool that could ever be useful. The per-stage gate reservations do
    the real throttling at runtime, so oversizing here only idles workers,
    while undersizing would strand budget.

    Returns ``(n_workers, budget_bytes)``. ``budget_bytes`` is ``None`` when
    ``MemAvailable`` is unreadable (non-Linux dev boxes without psutil): the
    pool is then capped only by ``-p``/tile count and runs ungated.
    """
    avail = mem_available_bytes()
    if avail is None:
        n_workers = max(1, min(n_processes, len(tiles)))
        log(f"resample: tile pool: {n_workers} workers, UNGATED "
            f"(MemAvailable unreadable on this platform; "
            f"ceiling -p {n_processes})")
        return n_workers, None
    budget = int(avail * float(step_config.get('mem_fraction', 0.65)))
    min_npix = min(
        int(shape[0]) * int(shape[1])
        for shape in (field.get_tile_wcs(t, pixel_scale=pixel_scale_str)[2]
                      for t in tiles))
    floor_est = _estimate_drizzle_bytes(min_npix, 1, step_config)
    n_workers = max(1, min(n_processes, len(tiles),
                           budget // max(floor_est, 1)))
    log(f"resample: tile pool: {n_workers} workers "
        f"(ceiling -p {n_processes}, budget {budget / 2**30:.1f} GiB of "
        f"{avail / 2**30:.1f} GiB available, lightest stage estimate "
        f"{floor_est / 2**30:.1f} GiB)")
    return n_workers, budget


def _heavy_work_expected(mosaic_file, needs_rebuild, step_config):
    """Cheap, conservative predicate: will this tile run bkgsub, extension
    splitting, or plotting (the heavy tail)?

    Errs on True — holding the gate for a few header reads is cheap, running
    a bkgsub outside it is not. Mirrors the decision logic in
    :func:`_resample_tile` *without* its order-sensitive side effects (the
    stamp backfill, and the SRCMASK re-check that must run after bkgsub),
    which stay inside the gated region.
    """
    if needs_rebuild:
        return True   # bkgsub + split + plot all follow a fresh drizzle
    if not os.path.exists(mosaic_file):
        return True
    from campfire_pipeline.nircam.manifest import MOSAIC_BKGSUB_KEY

    if step_config.get('background_subtract', True):
        with fits.open(mosaic_file) as hdul:
            if MOSAIC_BKGSUB_KEY not in hdul[0].header:
                return True
    if step_config.get('split_extensions', True):
        outdir = os.path.dirname(mosaic_file)
        base = os.path.basename(mosaic_file)
        for suffix in ('_sci.fits', '_err.fits', '_wht.fits'):
            if not os.path.exists(
                    os.path.join(outdir, base.replace('_i2d.fits', suffix))):
                return True
        srcmask_path = os.path.join(
            outdir, base.replace('_i2d.fits', '_srcmask.fits'))
        if not os.path.exists(srcmask_path):
            with fits.open(mosaic_file) as hdul:
                if 'SRCMASK' in hdul:
                    return True
    return False


def resample_step(filtname, exposure_files, field, step_config,
                  reduction_version, overwrite=False, tiles=None, epoch=None,
                  n_processes=1):
    """Drizzle-combine canonical exposure files into mosaic tiles.

    Parameters
    ----------
    filtname : str
    exposure_files : list of str
        Canonical exposure paths (``CFP_OUT`` already stamped). When ``epoch``
        is set these have already been narrowed to the epoch's subset by the
        caller (see :meth:`Field.get_exposure_files`).
    field : Field
    step_config : dict
        ``[nircam.resample]`` (legacy ``[nircam.stage3.resample]``).
    reduction_version : str
        Campfire reduction version stamped onto each mosaic primary header
        as ``CMPFRVER``.
    overwrite : bool
    tiles : str, list of str, or None
        Tile name(s) to drizzle. ``None`` (the default) resamples every tile
        in the field. Tile selection is a runtime CLI parameter (``--tiles``),
        not a config key — passing a subset only limits which mosaics are
        built; each tile is drizzled from the same exposure set it would use
        in a whole-field run.
    epoch : str, optional
        Epoch name (fields.toml ``[<field>.epochs.<name>]``) for a subset
        mosaic. Appended as a trailing filename segment and recorded in the
        manifest. ``None`` (the default) builds the full-field mosaics with no
        epoch segment.
    n_processes : int
        **Ceiling** on concurrent tile workers (the CLI ``-p``), not a
        target: :func:`_plan_tile_pool` picks the actual pool width from the
        memory budget, and the per-stage memory gate throttles admissions
        below even that when live ``MemAvailable`` is tight. ``1`` (the
        default) runs the tile loop strictly serially and ordering-stable,
        with exceptions propagating fail-fast; in parallel mode a failing
        tile does not stop its siblings — failures are collected and raised
        together at the end.
    """
    pixel_scale, pixel_scale_str = _resolve_pixel_scale(
        step_config.get('pixel_scale', '60mas'),
    )
    mode = step_config.get('mode', 'tile')
    if mode != 'tile':
        raise NotImplementedError(f"resample mode {mode!r} not supported")

    if tiles is None:
        tiles = list(field.tiles.keys())
    elif isinstance(tiles, str):
        tiles = [tiles]

    worker_kwargs = dict(
        filtname=filtname, exposure_files=exposure_files, field=field,
        step_config=step_config, reduction_version=reduction_version,
        pixel_scale=pixel_scale, pixel_scale_str=pixel_scale_str,
        overwrite=overwrite, epoch=epoch,
    )

    use_pool = (n_processes > 1 and len(tiles) > 1
                and step_config.get('parallel_tiles', True))
    if use_pool:
        # One pass over the exposures reads every footprint; per-tile
        # selection is then pure polygon math. That both spares each worker
        # a full re-read of the exposure headers (selection previously ran
        # once per tile, each opening every file) and lets the pool be
        # sized on the tiles that actually have work instead of the raw
        # tile list.
        footprints = exposure_footprints(exposure_files)
        tasks = []
        for tile in tiles:
            selected = select_overlapping_files(
                exposure_files, Polygon(field.get_tile_corners(tile)),
                footprints=footprints,
            )
            if selected:
                tasks.append((tile, selected))
            else:
                log(f"resample: no exposures overlap {tile}; skipping")
        if not tasks:
            return
        n_workers, budget = _plan_tile_pool(
            [tile for tile, _ in tasks], field, pixel_scale_str,
            step_config, n_processes,
        )
        use_pool = n_workers > 1

    if not use_pool:
        for tile in tiles:
            _process_tile(tile, **worker_kwargs)
        return

    gate = MemoryGate(budget) if budget is not None else None
    results = dispatch(
        _process_tile, tasks, n_processes=n_workers, use_starmap=True,
        initializer=_init_tile_worker, initargs=(gate,),
        capture_errors=True, tag_logs=True, retry_crds=True,
        **worker_kwargs,
    )

    failures = [r for r in results if r.get('error')]
    for f in failures:
        log(f"resample: tile {f['tile']} FAILED:\n{f['error']}")
    if failures:
        raise RuntimeError(
            f"resample: {len(failures)}/{len(results)} tile(s) failed for "
            f"{filtname}: {', '.join(sorted(f['tile'] for f in failures))}"
        )


def _process_tile(tile, selected=None, *, capture_errors=False,
                  tag_logs=False, **kwargs):
    """Pool-worker wrapper around :func:`_resample_tile`.

    Runs identically in-process (serial: exceptions propagate, untagged logs)
    and as a forkserver pool worker (``capture_errors=True`` returns
    exceptions as ``{'tile', 'error'}`` so one bad tile doesn't kill its
    siblings; ``tag_logs=True`` prefixes every log line — including those
    from the drizzle/bkgsub modules this calls into — with the tile name).
    ``selected`` carries the tile's input list when the parent already did
    the selection (parallel mode); ``None`` makes the worker select for
    itself (serial mode).
    """
    if tag_logs:
        set_log_prefix(f'[{tile}]')
    try:
        _resample_tile(tile, selected=selected, **kwargs)
        return {'tile': tile, 'error': None}
    except Exception:
        if not capture_errors:
            raise
        return {'tile': tile, 'error': traceback.format_exc()}
    finally:
        if tag_logs:
            set_log_prefix('')


def _resample_tile(tile, *, filtname, exposure_files, field, step_config,
                   reduction_version, pixel_scale, pixel_scale_str,
                   overwrite, epoch, selected=None, retry_crds=False):
    """Drizzle + background-subtract + split + plot one mosaic tile.

    The whole per-tile pipeline: staleness check, drizzle, optional
    background subtraction, extension splitting, plots. Reads only the
    combine-frozen working-copy exposures and writes only tile-named outputs
    (i2d, manifest, ASN, bkgsub snapshot, split extensions, PNGs), so
    concurrent tiles never collide.

    When the memory gate is armed (parallel mode), the drizzle and the
    bkgsub/split/plot tail each reserve their own estimated footprint —
    the tail is the ~2.5-3x heavier stage, so per-stage reservations let
    drizzles run wide while bkgsubs throttle, and stagger the bkgsub peaks
    instead of aligning them.
    """
    from campfire_pipeline.nircam.manifest import (
        BKGSUB_PIXEL_DEFAULTS, MOSAIC_BKGSUB_KEY, bkgsub_stamp_value,
        build_mosaic_name, check_config_changed, check_inputs_changed,
        create_manifest, write_manifest,
    )

    log(f"resample: tile {tile}, {filtname}, {pixel_scale_str}")

    mosaic_name = build_mosaic_name(
        filtname, field.name, pixel_scale_str, tile, epoch=epoch,
        template=step_config.get('mosaic_name'),
    )
    mosaic_outdir = field.filter_dir(filtname)
    mosaic_file = os.path.join(mosaic_outdir, f'{mosaic_name}_i2d.fits')
    manifest_path = os.path.join(
        mosaic_outdir, f'{mosaic_name}_manifest.json',
    )

    log(f"  mosaic → {mosaic_file}")

    if selected is None:
        tile_polygon = Polygon(field.get_tile_corners(tile))
        selected = select_overlapping_files(exposure_files, tile_polygon)
    if not selected:
        log(f"  no exposures overlap {tile}; skipping")
        return

    # Decide if we need to rebuild
    needs_rebuild = overwrite
    if not needs_rebuild and not os.path.exists(mosaic_file):
        needs_rebuild = True
        log(f"  mosaic does not exist; building")
    if not needs_rebuild:
        inputs_changed, reasons = check_inputs_changed(
            manifest_path, selected,
        )
        cfg_changed = check_config_changed(
            manifest_path, {'resample': step_config}, pixel_scale_str,
        )
        if inputs_changed or cfg_changed:
            needs_rebuild = True
            all_reasons = list(reasons) if inputs_changed else []
            if cfg_changed:
                all_reasons.append('processing config changed')
            log(f"  tile {tile} stale: {'; '.join(all_reasons)}")
        else:
            log(f"  tile {tile} up-to-date "
                f"({len(selected)} inputs unchanged); skipping")

    if needs_rebuild:
        log(f"  drizzling {len(selected)} exposures")

        crpix, crval, shape, rotation = field.get_tile_wcs(
            tile, pixel_scale=pixel_scale_str,
        )
        npix = int(shape[0]) * int(shape[1])

        implementation = step_config.get('implementation', 'jwst')
        if implementation == 'campfire':
            drizzle_fn = _drizzle_tile_via_campfire
        elif implementation == 'jwst':
            drizzle_fn = _drizzle_tile_via_jwst
        else:
            raise ValueError(
                f"Unknown resample.implementation {implementation!r}; "
                f"expected 'jwst' or 'campfire'"
            )
        if retry_crds:
            # Parallel workers race each other on the shared CRDS cache
            # (jwst backend); same retry the other parallel stages use.
            drizzle_fn = _RetryOnIOError(drizzle_fn)

        with _gate_hold(
                _estimate_drizzle_bytes(npix, len(selected), step_config),
                f'{tile} drizzle'):
            drizzle_fn(
                selected,
                mosaic_file,
                crpix=crpix,
                crval=crval,
                shape=shape,
                rotation=rotation,
                pixel_scale=pixel_scale,
                resample_cfg=step_config,
                reduction_version=reduction_version,
            )

        # Stamp epoch provenance onto the drizzled i2d (both drizzle
        # implementations already stamp CMPFRVER/CMPFRTIM). Empty for a
        # full-field mosaic so normal outputs are unaffected.
        if epoch:
            with fits.open(mosaic_file, mode='update') as hdul:
                hdul[0].header['CFEPOCH'] = (
                    epoch, 'CAMPFIRE epoch (exposure subset) name',
                )

        manifest = create_manifest(
            mosaic_name, field, filtname, tile, pixel_scale_str,
            selected, {'resample': step_config}, epoch=epoch,
        )
        write_manifest(manifest, manifest_path)

    # The bkgsub/split/plot tail dominates memory, so it takes its own,
    # larger gate reservation. The precheck is conservative and only
    # consulted when a gate is armed: holding the gate for a few header
    # reads is cheap, running a bkgsub outside it is not. On serial
    # (ungated) runs nothing here is evaluated — including get_tile_wcs,
    # which up-to-date tiles have historically never needed.
    heavy = (_TILE_GATE is not None
             and _heavy_work_expected(mosaic_file, needs_rebuild,
                                      step_config))
    if heavy:
        _, _, shape, _ = field.get_tile_wcs(
            tile, pixel_scale=pixel_scale_str,
        )
        npix = int(shape[0]) * int(shape[1])
        gate_ctx = _gate_hold(_estimate_bkgsub_bytes(npix, step_config),
                              f'{tile} bkgsub')
    else:
        gate_ctx = nullcontext()
    with gate_ctx:
        if step_config.get('background_subtract', True):
            from campfire_pipeline.nircam.bkgsub import SubtractBackground

            pre_bkg = mosaic_file.replace('_i2d.fits', '_i2d_before_bkgsub.fits')
            stamp_card = (
                bkgsub_stamp_value(step_config),
                'campfire: mosaic bkgsub (alg ver, params hash)',
            )

            # The CFP_BKGS primary-header stamp on the i2d — not the existence
            # of the _i2d_before_bkgsub.fits snapshot — is the record that the
            # on-disk pixels are already background-subtracted (issue #427):
            # deriving bkgsub_done from the snapshot made a deleted snapshot
            # silently subtract the background a second time. The snapshot is
            # a rollback convenience copy, deletable once its mosaic carries
            # the stamp.
            if needs_rebuild:
                bkgsub_done = False  # freshly drizzled, stamp gone with it
            else:
                with fits.open(mosaic_file) as hdul:
                    bkgsub_done = MOSAIC_BKGSUB_KEY in hdul[0].header
                    has_srcmask = 'SRCMASK' in hdul
                if not bkgsub_done and os.path.exists(pre_bkg):
                    # No stamp but a snapshot on disk: either a legacy mosaic
                    # subtracted before the stamp existed, or a rollback where
                    # the snapshot was copied over the i2d (restoring
                    # unsubtracted pixels) with the snapshot left in place.
                    # SubtractBackground always appends a SRCMASK extension
                    # and the snapshot (copied from the pre-subtraction
                    # drizzle output) never carries one, so SRCMASK presence
                    # tells the two apart.
                    if has_srcmask:
                        # Legacy subtracted mosaic. The manifest config check
                        # passed to get here, so the current bkgsub settings
                        # are the ones that produced it — backfill the stamp
                        # so the snapshot becomes deletable from now on.
                        bkgsub_done = True
                        log(f"  backfilling {MOSAIC_BKGSUB_KEY} stamp on "
                            f"pre-stamp mosaic {os.path.basename(mosaic_file)}")
                        with fits.open(mosaic_file, mode='update') as hdul:
                            hdul[0].header[MOSAIC_BKGSUB_KEY] = stamp_card
                    else:
                        log("  snapshot present but i2d has no SRCMASK — "
                            "restored pre-bkgsub data; re-running bkgsub")

            if not bkgsub_done:
                if os.path.exists(pre_bkg):
                    os.remove(pre_bkg)

                # Pixel-affecting settings and their defaults come from the
                # manifest's BKGSUB_PIXEL_DEFAULTS — the same dict the tile
                # config hash iterates — so nothing applied here can change
                # mosaic pixels without also marking existing tiles stale.
                bkg = SubtractBackground(
                    **{k: step_config.get(k, d)
                       for k, d in BKGSUB_PIXEL_DEFAULTS.items()},
                    wht_aware=step_config.get('wht_aware', True),
                    suffix='bkgsub',
                    replace_sci=True,
                )
                bkg.call(mosaic_file)

                # Stamp the subtracted output *before* it is renamed into
                # place, so stamp and pixels land together atomically and the
                # pre-bkgsub snapshot (copied from the un-stamped input) never
                # carries the stamp.
                with fits.open(bkg.outfile, mode='update') as hdul:
                    hdul[0].header[MOSAIC_BKGSUB_KEY] = stamp_card

                if step_config.get('keep_pre_bkgsub', True):
                    log(f"  copying input → {os.path.basename(pre_bkg)}")
                    shutil.copy2(mosaic_file, pre_bkg)
                else:
                    log("  keep_pre_bkgsub = false; "
                        "not snapshotting the pre-bkgsub mosaic")

                log(f"  renaming {os.path.basename(bkg.outfile)} → "
                    f"{os.path.basename(mosaic_file)}")
                shutil.move(bkg.outfile, mosaic_file)
            else:
                log(f"  skipping background subtraction "
                    f"for {os.path.basename(mosaic_file)}")

        # No post-drizzle "SCI=NaN where WHT=0" pass is needed: both drizzle
        # backends use fillval='NaN', so uncovered / fully-masked pixels are
        # already NaN, and bkgsub preserves that (it masks isnan(sci) before
        # fitting and subtracts elementwise, so NaN - background = NaN).

        split_enabled = step_config.get('split_extensions', True)

        # Recover from a prior run that produced the i2d but failed (or was
        # interrupted) before writing the separated extension files. Since
        # splitting only reads from the existing i2d, we can re-split without
        # re-drizzling. needs_rebuild stays False, so bkgsub and the NaN-fill
        # (both already baked into the on-disk i2d) are not redone.
        missing_extensions = False
        if split_enabled and not needs_rebuild and os.path.exists(mosaic_file):
            base = os.path.basename(mosaic_file)
            required = ('_sci.fits', '_err.fits', '_wht.fits')
            missing_extensions = any(
                not os.path.exists(os.path.join(
                    mosaic_outdir, base.replace('_i2d.fits', suffix)))
                for suffix in required
            )
            if not missing_extensions:
                # SRCMASK is only expected when the i2d carries that extension.
                srcmask_path = os.path.join(
                    mosaic_outdir, base.replace('_i2d.fits', '_srcmask.fits'),
                )
                if not os.path.exists(srcmask_path):
                    with fits.open(mosaic_file) as hdul:
                        missing_extensions = 'SRCMASK' in hdul
            if missing_extensions:
                log(f"  {mosaic_name} i2d present but split extensions "
                    f"missing; re-splitting")

        do_split = (needs_rebuild or missing_extensions) and split_enabled
        do_plot = needs_rebuild and step_config.get('plot', True)

        if do_split or do_plot:
            with fits.open(mosaic_file) as hdul:
                sci = hdul['SCI'].data.copy()
                hdr = hdul['SCI'].header.copy()
                err = hdul['ERR'].data.copy() if do_split else None
                wht = hdul['WHT'].data.copy() if do_split else None
                srcmask = (hdul['SRCMASK'].data.copy()
                           if do_split and 'SRCMASK' in hdul else None)

        if do_split:
            log("  splitting extensions")
            base = os.path.basename(mosaic_file)
            fits.PrimaryHDU(data=sci, header=hdr).writeto(
                os.path.join(mosaic_outdir,
                             base.replace('_i2d.fits', '_sci.fits')),
                overwrite=True,
            )
            hdr.update({'EXTNAME': 'ERR'})
            fits.PrimaryHDU(data=err, header=hdr).writeto(
                os.path.join(mosaic_outdir,
                             base.replace('_i2d.fits', '_err.fits')),
                overwrite=True,
            )
            hdr.update({'EXTNAME': 'WHT'})
            fits.PrimaryHDU(data=wht, header=hdr).writeto(
                os.path.join(mosaic_outdir,
                             base.replace('_i2d.fits', '_wht.fits')),
                overwrite=True,
            )

            if srcmask is not None:
                hdr.update({'EXTNAME': 'SRCMASK'})
                fits.PrimaryHDU(data=srcmask, header=hdr).writeto(
                    os.path.join(
                        mosaic_outdir,
                        base.replace('_i2d.fits', '_srcmask.fits'),
                    ),
                    overwrite=True,
                )
            else:
                log(f"  {mosaic_name} has no SRCMASK extension")

        if do_plot:
            from campfire_pipeline.nircam.steps._plots import (
                plot_mosaic_bkgsub, plot_mosaic_thumbnail,
            )
            downsample = int(step_config.get('plot_downsample', 4))

            # Thumbnail pair: a small table rendition + a large quick-look
            # for the web popup, both size-capped (see plot_mosaic_thumbnail).
            thumb_png = mosaic_file.replace('_i2d.fits', '_thumb.png')
            plot_mosaic_thumbnail(
                sci, thumb_png,
                max_dim=int(step_config.get('thumbnail_max_dim', 500)),
            )
            log(f"  saved {os.path.basename(thumb_png)}")

            quicklook_png = mosaic_file.replace('_i2d.fits', '_quicklook.png')
            plot_mosaic_thumbnail(
                sci, quicklook_png,
                max_dim=int(step_config.get('quicklook_max_dim', 4096)),
            )
            log(f"  saved {os.path.basename(quicklook_png)}")

            if step_config.get('background_subtract', True):
                pre_bkg_path = mosaic_file.replace(
                    '_i2d.fits', '_i2d_before_bkgsub.fits',
                )
                if os.path.exists(pre_bkg_path):
                    sci_before_arr = fits.getdata(pre_bkg_path, extname='SCI')
                    bg_model = sci_before_arr - sci
                    bkg_png = mosaic_file.replace('_i2d.fits', '_bkgsub.png')
                    plot_mosaic_bkgsub(
                        sci_before_arr, sci, bg_model,
                        save_file=bkg_png, downsample=downsample,
                        title=mosaic_name,
                    )
                    log(f"  saved {os.path.basename(bkg_png)}")

        # The `version` axis is retired (epic #261, N2 / D3): the mosaic basename
        # is now the single canonical name per (field, filter, tile, pixel_scale),
        # so the old `_latest_` symlink farm that aliased a versioned output has no
        # target to point at and is gone. Readers (rgb, refcat, deploy) resolve the
        # direct version-free name.
