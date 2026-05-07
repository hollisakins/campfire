"""
outlier: cosmic-ray detection on canonical NIRCam exposures.

Two implementations live here, dispatched by the orchestrator based on
``[nircam.outlier].implementation``:

- ``outlier_step`` (jwst, default) — per-visit pass using
  ``jwst.pipeline.calwebb_image3.Image3Pipeline``. For each visit, finds
  every spatially overlapping exposure **from the same program** (so the
  cross-visit median has full intra-program statistics on overlap
  regions), assembles an ASN, and runs Image3 in outlier-only mode
  inside a private scratch directory. Each visit's own scratch outputs
  are then atomically promoted back to their canonical paths with
  ``CFP_OUT`` stamped. Manifests are written alongside the canonical
  files at ``products/nircam/<field>/<filter>/outlier_<visit>_manifest.json``.

  Cross-program overlap padding (the legacy behavior, where any
  spatially-overlapping exposure regardless of program was included)
  is gated behind ``[nircam.outlier].cross_program_overlap = true``.
  Intra-program is the new default: it removes the redundant-drizzle
  scaling problem in heavily-observed footprints (e.g. COSMOS center)
  where a single CRF would otherwise be drizzled once for its own
  visit plus once for every other program's visit it overlaps.

- ``outlier_step_campfire`` (campfire, opt-in) — per-visit pass using
  the campfire-native drizzle primitive (issue #138 Phase 2 v2). Same
  visit grouping, intra-program overlap padding, and manifest
  conventions as ``outlier_step``. Differs in the drizzle/median/blot
  path: builds a per-visit intermediate WCS via ``wcs_from_sregions``
  (input native pscale, ref-input rotation — same convention jwst
  uses internally), routes the per-input drizzle through
  ``drizzle.drizzle_tile_singles`` (bbox-sliced, variance-trick-ready),
  streams sci+err rasters through ``MedianComputer``, and flags CRs
  via ``flag_resampled_model_crs``. CFP_OUT stamping is per-visit
  (same semantic as ``outlier_step``).
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


def _program_id(path):
    """Extract 5-digit JWST program id from a CRF filename.

    JWST filenames follow the convention ``jw<PPPPP><OOO><VVV>_...`` where
    ``PPPPP`` is the 5-digit program id (chars 2-7 of the basename).
    """
    return os.path.basename(path)[2:7]


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

    do_plot = step_config.get('plot', True)
    filtname = visit_files[0].split('/')[-2]
    log(f"Outlier detection on visit {visit} ({len(visit_files)} exposures)")

    manifest_path = os.path.join(
        field.filter_dir(filtname), f'outlier_{visit}_manifest.json',
    )

    # Cross-visit overlap padding scope. Default scopes overlap to the
    # same JWST program — avoids redundantly drizzling the same exposure
    # on behalf of every other program that happens to overlap, which is
    # the dominant cost driver in COSMOS-area outlier runs. Flip
    # ``cross_program_overlap = true`` to restore the legacy all-programs
    # behavior (slower, slightly stronger CR statistics for tiles where
    # multiple programs dither over the same area).
    cross_program = step_config.get('cross_program_overlap', False)
    visit_program = _program_id(visit_files[0])

    # Find spatially overlapping exposures from other visits
    overlap_files = []
    visit_set = set(visit_files)
    for visit_file, region_a in zip(visit_files,
                                    [sregions[filter_files.index(f)]
                                     for f in visit_files]):
        for other_file, region_b in zip(filter_files, sregions):
            if other_file in visit_set or other_file in overlap_files:
                continue
            if not cross_program and _program_id(other_file) != visit_program:
                continue
            if _polygon_overlap(region_a, region_b):
                overlap_files.append(other_file)

    all_inputs = visit_files + overlap_files
    log(f"  including {len(all_inputs)} files (visit + overlap"
        f"{', intra-program' if not cross_program else ''})")

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

        if do_plot:
            from jwst.datamodels.dqflags import pixel as pixel_flags
            OUTLIER_BIT = pixel_flags['OUTLIER']

        for canonical in visit_files:
            rootname = os.path.basename(canonical).removesuffix('.fits')
            matches = [f for f in scratch_files if f.startswith(rootname)]
            if not matches:
                log(f"  outlier: no scratch output for {rootname}")
                continue
            if len(matches) > 1:
                matches.sort(key=len)  # shortest = closest to canonical
            scratch_out = os.path.join(scratch, matches[0])

            if do_plot:
                with fits.open(canonical) as hdul:
                    sci_before = hdul['SCI'].data.copy()
                    dq_before = hdul['DQ'].data.copy()

            with ImageModel(scratch_out) as model:
                atomic_save(
                    model, canonical,
                    header_updates=cfp.format(CFP_OUT=None),
                    extra_hdus=saved_extras.get(rootname),
                )
            log(f"  outlier promoted: {rootname}")

            if do_plot:
                from campfire_pipeline.nircam.steps._plots import plot_outlier
                with fits.open(canonical) as hdul:
                    dq_after = hdul['DQ'].data.copy()
                new_outlier = (
                    ((dq_after & OUTLIER_BIT) != 0)
                    & ~((dq_before & OUTLIER_BIT) != 0)
                )
                out_pdf = os.path.join(
                    os.path.dirname(canonical), f'{rootname}_outlier.pdf',
                )
                plot_outlier(
                    sci_before, new_outlier,
                    save_file=out_pdf,
                    title=f'{rootname}: outlier (jwst)',
                )
                log(f"  saved {os.path.basename(out_pdf)}")

    # Manifest records ALL inputs (visit + overlap) so we can detect changes
    # in either next time
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


def outlier_step_campfire(visit, visit_files, filter_files, sregions,
                          field, step_config, overwrite=False, status=None):
    """Per-visit outlier using campfire-native drizzle (issue #138 Phase 2 v2).

    Same orchestration as ``outlier_step`` (intra-program overlap
    padding, manifest staleness check, CFP_OUT stamping); the
    drizzle/median/blot path runs through campfire's
    ``outlier_detect_for_visit`` instead of ``Image3Pipeline``. Builds
    a per-visit intermediate WCS via ``wcs_from_sregions`` (jwst-style
    auto-derivation of pscale/rotation/shape) and uses the bbox-sliced
    ``drizzle_tile_singles`` primitive. CFP_OUT stamping is performed
    inside ``outlier_detect_for_visit`` via ``atomic_save``.
    """
    if not visit_files:
        return

    do_plot = step_config.get('plot', True)
    filtname = visit_files[0].split('/')[-2]
    log(f"Outlier (campfire) on visit {visit} ({len(visit_files)} exposures)")

    manifest_path = os.path.join(
        field.filter_dir(filtname), f'outlier_{visit}_manifest.json',
    )

    cross_program = step_config.get('cross_program_overlap', False)
    visit_program = _program_id(visit_files[0])

    overlap_files = []
    visit_set = set(visit_files)
    for visit_file, region_a in zip(visit_files,
                                    [sregions[filter_files.index(f)]
                                     for f in visit_files]):
        for other_file, region_b in zip(filter_files, sregions):
            if other_file in visit_set or other_file in overlap_files:
                continue
            if not cross_program and _program_id(other_file) != visit_program:
                continue
            if _polygon_overlap(region_a, region_b):
                overlap_files.append(other_file)

    all_inputs = visit_files + overlap_files
    log(f"  including {len(all_inputs)} files (visit + overlap"
        f"{', intra-program' if not cross_program else ''})")

    # Manifest staleness — same logic as outlier_step
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

    # Capture extras for the visit's own files (only those get saved back)
    saved_extras = {
        os.path.basename(f).removesuffix('.fits'): _capture_extras(f)
        for f in visit_files
    }

    from campfire_pipeline.nircam.outlier_detect import outlier_detect_for_visit

    snr_str = step_config.get('snr', '3.0 2.0')
    scale_str = step_config.get('scale', '1.2 0.7')
    snr = tuple(float(x) for x in snr_str.split())
    scale = tuple(float(x) for x in scale_str.split())

    outlier_detect_for_visit(
        all_inputs, visit_files,
        snr=snr, scale=scale,
        backg=float(step_config.get('backg', 0.0)),
        pixfrac=float(step_config.get('pixfrac', 1.0)),
        kernel=step_config.get('kernel', 'square'),
        weight_type=step_config.get('weight_type', 'ivm'),
        good_bits=step_config.get('good_bits', '~DO_NOT_USE'),
        in_memory=bool(step_config.get('in_memory', False)),
        extras_per_visit=saved_extras,
        plot=do_plot,
    )

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
