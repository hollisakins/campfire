"""
outlier: cosmic-ray detection on canonical NIRCam exposures.

Two implementations live here, dispatched by the orchestrator based on
``[nircam.outlier].implementation``:

- ``outlier_step`` (jwst, default) — per-visit pass using
  ``jwst.pipeline.calwebb_image3.Image3Pipeline``. For each visit, finds
  every spatially overlapping exposure from any other visit (so the
  cross-visit median has full statistics on overlap regions), assembles
  an ASN, and runs Image3 in outlier-only mode inside a private scratch
  directory. Each visit's own scratch outputs are then atomically
  promoted back to their canonical paths with ``CFP_OUT`` stamped.
  Manifests: ``exposures/<filter>/manifests/outlier_<visit>_manifest.json``.

- ``outlier_step_per_tile`` (campfire, opt-in) — per-tile pass using
  the campfire-native ``outlier_detect_for_tile`` (issue #138, Phase 2).
  All exposures overlapping a tile are drizzled into the tile WCS,
  medianed via streaming ``MedianComputer``, blotted back via
  ``gwcs_blot``, and DQ-flagged via the standard two-pass SNR scheme.
  Drops visit grouping entirely; drops the cross-visit overlap-padding
  ASN gymnastics. Manifests:
  ``exposures/<filter>/manifests/outlier_<tile>_manifest.json``. The
  per-exposure ``CFP_OUT`` stamp is stamped by the orchestrator only
  after every tile the exposure participates in has completed
  (deferred-completion semantic — preserves resample's
  ``with_step='CFP_OUT'`` input query).
"""

import json
import os
import tempfile

import numpy as np
from astropy.io import fits
from matplotlib.path import Path

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam.manifest import (
    compute_file_hash, load_manifest, write_manifest,
)


# Extensions JWST datamodels don't know about — outlier_detection itself
# preserves them, but we capture/restore as a belt-and-suspenders measure
# in case the round-trip path through Image3Pipeline strips them on a
# future jwst release.
EXTRA_EXT_NAMES = ('SRCMASK', 'CFMASK')


def _capture_extras(canonical):
    extras = []
    with fits.open(canonical) as hdul:
        for name in EXTRA_EXT_NAMES:
            if name not in hdul:
                continue
            hdu = hdul[name]
            extras.append(fits.ImageHDU(
                hdu.data.copy(), header=hdu.header.copy(), name=name,
            ))
    return extras


def _polygon_overlap(region_a, region_b):
    """True if either polygon's vertices fall inside the other."""
    ra_a = [float(s) for s in region_a.split()[2::2]]
    dec_a = [float(s) for s in region_a.split()[3::2]]
    poly_a = Path(np.array([ra_a, dec_a]).T, closed=True)

    ra_b = [float(s) for s in region_b.split()[2::2]]
    dec_b = [float(s) for s in region_b.split()[3::2]]
    poly_b = Path(np.array([ra_b, dec_b]).T, closed=True)

    for p in poly_a.vertices:
        if poly_b.contains_point(p):
            return True
    for p in poly_b.vertices:
        if poly_a.contains_point(p):
            return True
    return False


def outlier_step(visit, visit_files, filter_files, sregions,
                 field, step_config, overwrite=False, status=None):
    """Run outlier detection on one visit's exposures.

    Parameters
    ----------
    visit : str
        Visit identifier (e.g. ``'jw01727028001'``).
    visit_files : list of str
        Canonical exposure paths for this visit.
    filter_files : list of str
        All canonical exposure paths for this filter (used to find spatially
        overlapping exposures from other visits).
    sregions : list of str
        ``S_REGION`` header strings parallel to ``filter_files``.
    field : Field
    step_config : dict
        ``[nircam.outlier]`` (legacy ``[nircam.stage3.outlier]``).
    overwrite : bool
    status : StepStatus, optional
        Pre-scanned CFP_* status cache.
    """
    if not visit_files:
        return

    filtname = visit_files[0].split('/')[-2]
    log(f"Outlier detection on visit {visit} ({len(visit_files)} exposures)")

    manifest_dir = os.path.join(field.exposures_dir, filtname, 'manifests')
    manifest_path = os.path.join(
        manifest_dir, f'outlier_{visit}_manifest.json',
    )

    # Find spatially overlapping exposures from other visits
    overlap_files = []
    visit_set = set(visit_files)
    for visit_file, region_a in zip(visit_files,
                                    [sregions[filter_files.index(f)]
                                     for f in visit_files]):
        for other_file, region_b in zip(filter_files, sregions):
            if other_file in visit_set or other_file in overlap_files:
                continue
            if _polygon_overlap(region_a, region_b):
                overlap_files.append(other_file)

    all_inputs = visit_files + overlap_files
    log(f"  including {len(all_inputs)} files (visit + overlap)")

    # Manifest-based skip
    if not overwrite:
        if status is not None:
            all_done = all(status.has(f, 'CFP_OUT') for f in visit_files)
        else:
            all_done = all(cfp.has_step(f, 'CFP_OUT') for f in visit_files)
        if all_done:
            new_basenames = sorted(os.path.basename(f) for f in all_inputs)
            manifest = load_manifest(manifest_path)
            if manifest is not None:
                old_basenames = sorted(
                    inp['filename'] for inp in manifest['inputs']
                )
                if new_basenames == old_basenames:
                    old_hashes = {
                        inp['filename']: inp['file_hash']
                        for inp in manifest['inputs']
                    }
                    content_changed = []
                    for f in all_inputs:
                        bn = os.path.basename(f)
                        if bn in old_hashes:
                            if compute_file_hash(f) != old_hashes[bn]:
                                content_changed.append(bn)
                    if not content_changed:
                        log(f"  visit {visit} up-to-date; skipping")
                        return
                    log(f"  inputs changed: {', '.join(content_changed)}")
                else:
                    log(f"  input set changed for visit {visit}")
            else:
                log(f"  no manifest for visit {visit}; running")
        else:
            log(f"  CFP_OUT missing on some visit members; running")
    else:
        log(f"  --overwrite; running unconditionally")

    # Capture extras for the visit's own files (we only promote those back)
    saved_extras = {
        os.path.basename(f).removesuffix('.fits'): _capture_extras(f)
        for f in visit_files
    }

    params = {
        'assign_mtwcs': {'skip': True},
        'tweakreg': {'skip': True},
        'skymatch': {'skip': True},
        'resample': {'skip': True},
        'source_catalog': {'skip': True},
        'outlier_detection': {
            'weight_type': step_config.get('weight_type', 'ivm'),
            'pixfrac': step_config.get('pixfrac', 1.0),
            'kernel': step_config.get('kernel', 'square'),
            'fillval': step_config.get('fillval', 'INDEF'),
            'maskpt': step_config.get('maskpt', 0.1),
            'snr': step_config.get('snr', '3.0 2.0'),
            'scale': step_config.get('scale', '1.2 0.7'),
            'backg': step_config.get('backg', 0.0),
            'resample_data': step_config.get('resample_data', True),
            'good_bits': step_config.get('good_bits', '~DO_NOT_USE'),
            'save_intermediate_results': True,
            'save_results': True,
        },
    }

    from jwst.associations.lib.rules_level3_base import DMS_Level3_Base
    from jwst.associations import asn_from_list
    from jwst.pipeline import calwebb_image3
    from jwst.datamodels import ImageModel

    with tempfile.TemporaryDirectory(prefix='outlier-') as scratch:
        asn_file = os.path.join(scratch, f'outlier_{visit}_asn.json')
        asn = asn_from_list.asn_from_list(
            all_inputs, rule=DMS_Level3_Base,
            product_name=f'outlier_{visit}',
        )
        with open(asn_file, 'w') as fp:
            _, serialized = asn.dump(format='json')
            fp.write(serialized)

        try:
            calwebb_image3.Image3Pipeline.call(
                asn_file, output_dir=scratch, steps=params,
                save_results=True,
            )
        except Exception:
            log(f"outlier failed on visit {visit}")
            raise

        # Promote only the visit's own outputs back to canonical
        scratch_files = sorted(
            f for f in os.listdir(scratch)
            if f.endswith('.fits') and not f.endswith('_asn.json')
        )

        for canonical in visit_files:
            rootname = os.path.basename(canonical).removesuffix('.fits')
            matches = [f for f in scratch_files if f.startswith(rootname)]
            if not matches:
                log(f"  outlier: no scratch output for {rootname}")
                continue
            if len(matches) > 1:
                matches.sort(key=len)  # shortest = closest to canonical
            scratch_out = os.path.join(scratch, matches[0])

            with ImageModel(scratch_out) as model:
                atomic_save(
                    model, canonical,
                    header_updates=cfp.format(CFP_OUT=None),
                    extra_hdus=saved_extras.get(rootname),
                )
            log(f"  outlier promoted: {rootname}")

    # Manifest records ALL inputs (visit + overlap) so we can detect changes
    # in either next time
    os.makedirs(manifest_dir, exist_ok=True)
    manifest_data = {
        'visit': visit,
        'field': field.name,
        'filter': filtname,
        'inputs': [
            {
                'filename': os.path.basename(f),
                'file_hash': compute_file_hash(f),
            }
            for f in sorted(all_inputs)
        ],
    }
    write_manifest(manifest_data, manifest_path)


def outlier_step_per_tile(tile, tile_files, filtname, field, step_config,
                          overwrite=False):
    """Run per-tile outlier detection across all CRFs overlapping the tile.

    Parameters
    ----------
    tile : str
        Tile name (e.g. ``'venus'`` or ``'A1'``).
    tile_files : list of str
        Canonical CRF paths for this filter that overlap the tile.
        Caller (orchestrator) is expected to have run
        ``select_overlapping_files`` already.
    filtname : str
    field : Field
    step_config : dict
        ``[nircam.outlier]`` section.
    overwrite : bool

    Notes
    -----
    Does not stamp ``CFP_OUT`` — the orchestrator stamps that once every
    tile an exposure participates in has completed (deferred-completion
    semantic). DQ updates land via ``atomic_save`` inside
    ``outlier_detect_for_tile``.

    Manifest staleness check: skip if the manifest exists, all input
    hashes match, and ``--overwrite`` is not set.
    """
    if not tile_files:
        return

    from campfire_pipeline.nircam.outlier_detect import outlier_detect_for_tile

    log(f"Outlier (per-tile) on {tile} ({len(tile_files)} exposures, {filtname})")

    manifest_dir = os.path.join(field.exposures_dir, filtname, 'manifests')
    manifest_path = os.path.join(
        manifest_dir, f'outlier_{tile}_manifest.json',
    )

    if not overwrite:
        manifest = load_manifest(manifest_path)
        if manifest is not None:
            new_basenames = sorted(os.path.basename(f) for f in tile_files)
            old_basenames = sorted(inp['filename'] for inp in manifest['inputs'])
            if new_basenames == old_basenames:
                old_hashes = {
                    inp['filename']: inp['file_hash']
                    for inp in manifest['inputs']
                }
                content_changed = []
                for f in tile_files:
                    bn = os.path.basename(f)
                    if compute_file_hash(f) != old_hashes.get(bn):
                        content_changed.append(bn)
                if not content_changed:
                    log(f"  tile {tile} up-to-date; skipping")
                    return
                log(f"  inputs changed: {', '.join(content_changed)}")
            else:
                log(f"  input set changed for tile {tile}")
        else:
            log(f"  no manifest for tile {tile}; running")

    pixel_scale_str = step_config.get('pixel_scale', '60mas')
    if not pixel_scale_str.endswith('mas'):
        raise ValueError(
            f"[nircam.outlier].pixel_scale must look like 'NNmas' "
            f"(got {pixel_scale_str!r})"
        )
    pixel_scale = float(pixel_scale_str[:-3]) / 1000  # arcsec

    crpix, crval, shape, rotation = field.get_tile_wcs(
        tile, pixel_scale=pixel_scale_str,
    )

    snr_str = step_config.get('snr', '3.0 2.0')
    scale_str = step_config.get('scale', '1.2 0.7')
    snr = tuple(float(x) for x in snr_str.split())
    scale = tuple(float(x) for x in scale_str.split())

    outlier_detect_for_tile(
        tile_files,
        crpix=crpix, crval=crval, shape=shape, rotation=rotation,
        pixel_scale=pixel_scale,
        snr=snr, scale=scale,
        backg=float(step_config.get('backg', 0.0)),
        pixfrac=float(step_config.get('pixfrac', 1.0)),
        kernel=step_config.get('kernel', 'square'),
        weight_type=step_config.get('weight_type', 'ivm'),
        good_bits=step_config.get('good_bits', '~DO_NOT_USE'),
        in_memory=bool(step_config.get('in_memory', False)),
    )

    os.makedirs(manifest_dir, exist_ok=True)
    manifest_data = {
        'tile': tile,
        'field': field.name,
        'filter': filtname,
        'pixel_scale': pixel_scale_str,
        'inputs': [
            {
                'filename': os.path.basename(f),
                'file_hash': compute_file_hash(f),
            }
            for f in sorted(tile_files)
        ],
    }
    write_manifest(manifest_data, manifest_path)
