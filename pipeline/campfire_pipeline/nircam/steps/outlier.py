"""
outlier: cosmic-ray detection on canonical NIRCam exposures.

Two implementations live here, dispatched by the orchestrator based on
``[nircam.outlier].implementation``. The shipped default is ``"campfire"``
(``outlier_step_campfire``, below); ``"jwst"`` is the alternative.

- ``outlier_step`` (jwst) — per-visit pass using
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

- ``outlier_step_campfire`` (campfire, the default) — per-visit pass on the
  instrument-neutral outlier code, ``imaging/outlier.py::detect_outliers``,
  which the HST instruments run too (``nircam/outlier_detect.py`` is the
  NIRCam side of it). Same visit grouping, intra-program overlap padding
  and manifest conventions as ``outlier_step``. The visit's exposures are
  drizzled one by one onto a north-up grid at the native scale, the median
  (the minimum where two or three exposures overlap) is blotted back, and
  the two-pass test flags what disagrees. ``CFP_OUT`` is stamped per
  visit, as by ``outlier_step``.
"""

import os
import tempfile

import numpy as np
from astropy.io import fits
from matplotlib.path import Path

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam.manifest import (
    file_unchanged, input_entry, load_manifest, write_manifest,
)


# Extensions JWST datamodels don't know about — outlier_detection itself
# preserves them, but we capture/restore as a belt-and-suspenders measure
# in case the round-trip path through Image3Pipeline strips them on a
# future jwst release.
EXTRA_EXT_NAMES = ('SRCMASK', 'CFMASK')


def _release_unseeded_children(work, expanded, seeds, npixels=25,
                               nlevels=32, contrast=0.001, peak_ratio=2.0):
    """Deblend expanded components and release distinct-source children.

    Photutils multi-threshold watershed deblending on the (one-sign,
    smoothed) residual: a neighbouring galaxy whose isophotes touch the
    artifact's wings gets its own deblended child and is dropped from the
    mask, while the artifact — whose peak owns the seed — is kept.

    Watershed alone over-segments flat-topped elongated artifacts (noise
    dips along a ridge create saddles), so unseeded children are NOT
    released on segmentation alone: a child is released only when its peak
    also exceeds ``peak_ratio`` x the brightest seeded child of the same
    parent component. Artifact-wing chunks share the artifact's surface
    brightness and stay masked; a distinct galaxy sticks out well above it
    and is released. A galaxy fainter than the artifact stays masked — the
    conservative direction (it was buried in the artifact anyway).
    """
    from scipy import ndimage

    elab, en = ndimage.label(expanded)
    if en == 0:
        return expanded
    data = np.where(expanded, np.maximum(work, 0.0), 0.0)
    try:
        from photutils.segmentation import SegmentationImage, deblend_sources
        deb = deblend_sources(
            data, SegmentationImage(elab), npixels=npixels, nlevels=nlevels,
            contrast=contrast, mode='linear', progress_bar=False,
        )
    except Exception as exc:  # never let a deblend hiccup kill the step
        log(f"  outlier grow: deblend failed ({exc}); keeping full expansion")
        return expanded

    child_ids = np.arange(1, deb.max_label + 1)
    peaks = np.atleast_1d(ndimage.maximum(data, deb.data, child_ids))
    # children partition parents, so elab is constant over each child
    parent_of = np.atleast_1d(
        ndimage.maximum(elab, deb.data, child_ids)).astype(int)
    seeded = np.zeros(deb.max_label + 1, dtype=bool)
    seeded[np.unique(deb.data[seeds & (deb.data > 0)])] = True

    ref_peak = {}  # parent id -> brightest seeded-child peak
    for i, cid in enumerate(child_ids):
        if seeded[cid]:
            p = parent_of[i]
            ref_peak[p] = max(ref_peak.get(p, 0.0), peaks[i])

    keep = seeded.copy()
    for i, cid in enumerate(child_ids):
        if not seeded[cid] and \
                peaks[i] <= peak_ratio * ref_peak.get(parent_of[i], np.inf):
            keep[cid] = True
    return keep[deb.data]


def _expand_seeds(work, sig, seeds, expand_nsigma, max_factor,
                  fallback_radius, deblend):
    """One-sign waterfall expansion with deblend release and growth cap.

    Floods ``seeds`` through connected pixels of ``work`` above
    ``expand_nsigma``·``sig`` (callers pass ``-work`` for negative seeds),
    optionally releases deblended children that contain no seed pixels,
    then applies the per-component ``max_factor`` growth cap with the
    ``fallback_radius`` EDT-dilation fallback.
    """
    from scipy.ndimage import binary_propagation, distance_transform_edt, label

    above = (work > expand_nsigma * sig) | seeds
    expanded = binary_propagation(seeds, mask=above)
    if deblend:
        expanded = _release_unseeded_children(work, expanded, seeds)

    elab, en = label(expanded)
    final = np.zeros_like(expanded)
    for cid in range(1, en + 1):
        comp = elab == cid
        seed_area = np.count_nonzero(comp & seeds)
        if seed_area == 0:
            continue
        if np.count_nonzero(comp) > max_factor * seed_area:
            comp = distance_transform_edt(~(comp & seeds)) <= fallback_radius
        final |= comp
    return final


def grow_outlier_regions(sci, dq, min_area=100, expand_nsigma=1.5,
                         max_factor=40, fallback_radius=12.0, smooth=2.0,
                         skirt=2.0, negative=True, deblend=True,
                         preexisting_dnu=None, edge_buffer=50):
    """Waterfall (hysteresis) expansion of large OUTLIER DQ regions.

    Detector-fixed artifacts (scattered-light arcs, glints) move on-sky
    between dithers, so outlier detection flags their bright cores — but
    their wings survive below the per-pixel threshold and drizzle into the
    mosaic. Two-threshold ("waterfall") masking, SExtractor-isophote style:
    outlier detection provides the high-threshold seeds (connected OUTLIER
    components with area >= ``min_area`` px; cosmic-ray hits and galaxy-core
    speckles stay untouched), and each seed is flooded outward
    (morphological reconstruction) through connected pixels of the lightly
    smoothed science residual above ``expand_nsigma``·sigma — so the mask
    follows the artifact's actual morphology and stops at the background.
    Requires a flat background (the bkg step's conditioning/applied chain
    provides it), since the expansion threshold is global.

    Seeds are split by the sign of the smoothed residual they sit on (the
    base detection thresholds ``|sci - blot|``, so both signs seed): positive
    seeds (scattered light, glints) expand through pixels *above*
    +``expand_nsigma``·sigma, and — when ``negative`` is on — negative seeds
    (oversubtracted wisp/background regions) expand through pixels *below*
    −``expand_nsigma``·sigma. With ``negative=False`` all seeds expand on
    the positive side (the pre-sign-split behavior).

    Three guards:

    * **Deblend release** (``deblend``) — each expanded component is
      photutils-deblended on the smoothed residual; children that contain
      no seed pixels *and* peak clearly above the seeded artifact
      (neighbouring galaxies picked up by the flood) are released from the
      mask. See ``_release_unseeded_children``.
    * **Growth cap** — a seed on a bright star floods the star's own PSF
      halo. Any expanded component larger than ``max_factor`` x its seed
      area is discarded and replaced by a plain EDT dilation of the seed by
      ``fallback_radius`` px (artifact wings measured ~5-15x their seeds on
      rj0911; star halos above threshold run far beyond ``max_factor``).
    * **Skirt** — a final ``skirt`` px dilation covers the sub-threshold
      edge of the expansion.

    ``preexisting_dnu`` (boolean mask) marks pixels that were DO_NOT_USE
    *before* outlier detection ran (exposure-edge trim, bad pixels). Those
    pixels carry no weight in the drizzle, so flagging them as OUTLIER
    changes nothing in the mosaic — and they must not drive expansion:
    outlier detection flags near-contiguous strips along the trimmed
    exposure edges (blot/median mismatch at the frame boundary), and
    without this exclusion any strip over ``min_area`` seeds a flood into
    whatever real galaxy touches the frame edge. Seeds are therefore
    formed only from weight-carrying OUTLIER pixels; a genuine artifact
    overlapping the trim still seeds from its interior bulk, and its
    flood can re-enter the trimmed zone through above-threshold wings.

    ``edge_buffer`` extends the same idea geometrically: OUTLIER pixels
    within ``edge_buffer`` px of the frame edge never seed. Saturated
    star cores falling near the exposure edge get detection-flagged
    (saturation is unstable between dithers) on weight-carrying pixels
    the DNU exclusion can't catch, and would otherwise flood the star's
    own PSF wings. Exclusion is per-pixel, so a large artifact reaching
    the edge still seeds from the part of it deeper than the buffer.

    Grown pixels are flagged OUTLIER|DO_NOT_USE in ``dq`` (in place).
    Returns ``(n_large_seeds, n_added_px, added_mask)`` where ``added_mask``
    is the boolean array of newly flagged pixels (None when no large seeds).
    """
    from astropy.stats import mad_std
    from jwst.datamodels.dqflags import pixel as pf
    from scipy.ndimage import distance_transform_edt, gaussian_filter, label

    outlier = (dq & pf['OUTLIER']) != 0
    seedable = outlier if preexisting_dnu is None \
        else outlier & ~preexisting_dnu
    if edge_buffer > 0:
        b = int(edge_buffer)
        interior = np.zeros_like(outlier)
        interior[b:-b, b:-b] = True
        seedable = seedable & interior
    lab, nlab = label(seedable)
    if nlab == 0:
        return 0, 0, None
    areas = np.bincount(lab.ravel())[1:]
    large_ids = np.flatnonzero(areas >= min_area) + 1
    if large_ids.size == 0:
        return 0, 0, None

    work = gaussian_filter(
        np.nan_to_num(sci - np.nanmedian(sci), nan=0.0), smooth)
    sig = mad_std(work, ignore_nan=True)

    pos_seeds = np.zeros_like(outlier)
    neg_seeds = np.zeros_like(outlier)
    for cid in large_ids:
        comp = lab == cid
        if negative and np.median(work[comp]) < 0:
            neg_seeds |= comp
        else:
            pos_seeds |= comp

    final = np.zeros_like(outlier)
    for w, seeds in ((work, pos_seeds), (-work, neg_seeds)):
        if seeds.any():
            final |= _expand_seeds(w, sig, seeds, expand_nsigma,
                                   max_factor, fallback_radius, deblend)

    if skirt > 0:
        final = distance_transform_edt(~final) <= skirt

    added = final & ~outlier
    dq[added] |= (pf['OUTLIER'] | pf['DO_NOT_USE'])
    return int(large_ids.size), int(added.sum()), added


def _grow_kwargs(step_config):
    """Waterfall-growth parameters from a ``[nircam.outlier]`` config dict."""
    return dict(
        min_area=int(step_config.get('grow_min_area', 100)),
        expand_nsigma=float(step_config.get('grow_expand_nsigma', 1.5)),
        max_factor=float(step_config.get('grow_max_factor', 40)),
        fallback_radius=float(step_config.get('grow_radius', 12)),
        negative=bool(step_config.get('grow_negative', True)),
        deblend=bool(step_config.get('grow_deblend', True)),
        edge_buffer=int(step_config.get('grow_edge_buffer', 50)),
    )


def _capture_extras(canonical):
    extras = []
    with fits.open(canonical, memmap=False) as hdul:
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


def _compute_overlap_inputs(visit_files, filter_files, sregions, cross_program):
    """Find spatially-overlapping exposures from other visits.

    Returns ``visit_files + overlap_files``. Default scopes overlap to the
    same JWST program (avoids redundantly drizzling each exposure once per
    overlapping program in COSMOS-area footprints); set ``cross_program=True``
    for the legacy all-programs behavior.

    The rule is the one every imaging instrument uses
    (``imaging/outlier_visits.py``); NIRCam supplies its footprints
    (``S_REGION``) and its program (the file name).
    """
    from campfire_pipeline.imaging.outlier_visits import overlap_inputs

    sregion_map = dict(zip(filter_files, sregions))
    return visit_files + overlap_inputs(
        visit_files, filter_files,
        lambda a, b: _polygon_overlap(sregion_map[a], sregion_map[b]),
        _program_id, cross_program)


def _star_protection(step_config, all_inputs, visit_files, refcat):
    """Masks around catalog stars that moved between the exposures compared
    (``nircam/outlier_protect.py``), or ``{}``.

    The gate is the motion alone, whichever exposures are compared: a visit
    whose comparison set was taken within days protects nothing and runs
    exactly as before; one program returning to a field a year later, or the
    cross-program comparison, protects the stars that moved
    ``protect_min_motion`` native pixels or more. ``protect_stars = false``
    turns the protection off; without a reference catalog there is nothing to
    go by.
    """
    if not step_config.get('protect_stars', True) or refcat is None:
        return {}
    from campfire_pipeline.nircam.outlier_protect import protection_for_visit

    min_motion = float(step_config.get('protect_min_motion', 0.15))
    masks, info = protection_for_visit(
        all_inputs, visit_files, refcat,
        radius=float(step_config.get('protect_radius', 0.4)),
        min_motion=min_motion)
    if info['n_protected']:
        log(f"  outlier: exposures span {info['span_years']:.2f} yr; "
            f"{info['n_protected']} catalog star(s) moved {min_motion:g} px "
            f"or more and are protected")
    return masks


def _visit_is_up_to_date(visit, visit_files, all_inputs, manifest_path, status):
    """Decide whether a visit can skip outlier detection.

    Returns True only if every visit file has ``CFP_OUT`` stamped and the
    input set + content hashes match the on-disk manifest. Logs the reason
    when a re-run is needed.
    """
    if status is not None:
        all_done = all(status.has(f, 'CFP_OUT') for f in visit_files)
    else:
        all_done = all(cfp.has_step(f, 'CFP_OUT') for f in visit_files)
    if not all_done:
        log(f"  CFP_OUT missing on some visit members; running")
        return False

    manifest = load_manifest(manifest_path)
    if manifest is None:
        log(f"  no manifest for visit {visit}; running")
        return False

    new_basenames = sorted(os.path.basename(f) for f in all_inputs)
    old_basenames = sorted(inp['filename'] for inp in manifest['inputs'])
    if new_basenames != old_basenames:
        log(f"  input set changed for visit {visit}")
        return False

    old_by_name = {inp['filename']: inp for inp in manifest['inputs']}
    content_changed = []
    for f in all_inputs:
        bn = os.path.basename(f)
        if bn in old_by_name and not file_unchanged(f, old_by_name[bn]):
            content_changed.append(bn)
    if content_changed:
        log(f"  inputs changed: {', '.join(content_changed)}")
        return False

    log(f"  visit {visit} up-to-date; skipping")
    return True


def _write_outlier_manifest(visit, field, filtname, all_inputs, manifest_path):
    """Write the per-visit outlier manifest (input filenames + content hashes)."""
    write_manifest({
        'visit': visit,
        'field': field.name,
        'filter': filtname,
        'inputs': [input_entry(f) for f in sorted(all_inputs)],
    }, manifest_path)


def outlier_step(visit, visit_files, filter_files, sregions,
                 field, step_config, overwrite=False, status=None,
                 refcat=None):
    """Run outlier detection on one visit's exposures.

    ``refcat`` is the field's reference catalog; its stars that moved between
    the exposures compared are protected (see ``_star_protection``).

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

    cross_program = step_config.get('cross_program_overlap', False)
    all_inputs = _compute_overlap_inputs(
        visit_files, filter_files, sregions, cross_program,
    )
    log(f"  including {len(all_inputs)} files (visit + overlap"
        f"{', intra-program' if not cross_program else ''})")

    if not overwrite:
        if _visit_is_up_to_date(visit, visit_files, all_inputs, manifest_path,
                                status):
            return
    else:
        log(f"  --overwrite; running unconditionally")

    saved_extras = {
        os.path.basename(f).removesuffix('.fits'): _capture_extras(f)
        for f in visit_files
    }
    protect = _star_protection(step_config, all_inputs, visit_files, refcat)

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

        # Image3Pipeline's outlier step uses stcal MedianComputer, which
        # creates its on-disk scratch via tempfile.TemporaryDirectory(dir='')
        # — that resolves against CWD, not $TMPDIR (see
        # stcal/outlier_detection/median.py:269). On networked-FS clusters
        # CWD is the user's home dir, filling the home quota with `N.bin`
        # files. chdir into our scratch (which lives under $TMPDIR and is
        # auto-cleaned) so the median scratch piggybacks on it.
        orig_cwd = os.getcwd()
        try:
            os.chdir(scratch)
            try:
                calwebb_image3.Image3Pipeline.call(
                    asn_file, output_dir=scratch, steps=params,
                    save_results=True,
                )
            except Exception:
                log(f"outlier failed on visit {visit}")
                raise
        finally:
            os.chdir(orig_cwd)

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
                with fits.open(canonical, memmap=False) as hdul:
                    sci_before = hdul['SCI'].data.copy()
                    dq_before = hdul['DQ'].data.copy()

            star_mask = protect.get(canonical)
            kept = None
            if star_mask is not None:
                # canonical on disk is still pre-outlier here
                from campfire_pipeline.nircam import outlier_protect
                with ImageModel(canonical) as before:
                    kept = outlier_protect.snapshot(before, star_mask)

            with ImageModel(scratch_out) as model:
                cfp_val = None
                grow_added = None
                if kept is not None:
                    from jwst.datamodels.dqflags import pixel as pf
                    n_released = outlier_protect.release(
                        model, star_mask, kept, pf['OUTLIER'])
                    cfp_val = f'star_protect: {n_released} px released'
                    if n_released:
                        log(f"  outlier: {rootname} — {n_released} px "
                            f"released around catalog stars that moved")
                if step_config.get('grow_large_regions', False):
                    # canonical on disk is still pre-outlier here: its DQ
                    # gives the weight-carrying state before detection, so
                    # grow seeds skip pixels already DO_NOT_USE (edge trim)
                    from jwst.datamodels.dqflags import pixel as pf
                    dnu_before = (
                        (fits.getdata(canonical, 'DQ') & pf['DO_NOT_USE'])
                        != 0
                    )
                    n_large, n_added, grow_added = grow_outlier_regions(
                        model.data, model.dq, preexisting_dnu=dnu_before,
                        **_grow_kwargs(step_config))
                    grown = (f'grow_large_regions: {n_large} regions, '
                             f'+{n_added} px')
                    cfp_val = f'{cfp_val}; {grown}' if cfp_val else grown
                    if n_large:
                        log(f"  outlier grow: {rootname} — {n_large} large "
                            f"regions, +{n_added} px flagged")
                atomic_save(
                    model, canonical,
                    header_updates=cfp.format(CFP_OUT=cfp_val),
                    extra_hdus=saved_extras.get(rootname),
                )
            log(f"  outlier promoted: {rootname}")

            if do_plot:
                from campfire_pipeline.nircam.steps._plots import plot_outlier
                with fits.open(canonical, memmap=False) as hdul:
                    dq_after = hdul['DQ'].data.copy()
                new_outlier = (
                    ((dq_after & OUTLIER_BIT) != 0)
                    & ~((dq_before & OUTLIER_BIT) != 0)
                )
                if grow_added is not None:
                    new_outlier &= ~grow_added
                out_pdf = os.path.join(
                    os.path.dirname(canonical), f'{rootname}_outlier.pdf',
                )
                plot_outlier(
                    sci_before, new_outlier, grown=grow_added,
                    save_file=out_pdf,
                    title=f'{rootname}: outlier (jwst)',
                )
                log(f"  saved {os.path.basename(out_pdf)}")

    _write_outlier_manifest(visit, field, filtname, all_inputs, manifest_path)


def outlier_step_campfire(visit, visit_files, filter_files, sregions,
                          field, step_config, overwrite=False, status=None,
                          refcat=None):
    """Per-visit outlier pass on the instrument-neutral code.

    Same orchestration as ``outlier_step`` (intra-program overlap
    padding, manifest staleness check, CFP_OUT stamping); the comparison
    is ``imaging/outlier.py::detect_outliers`` through
    ``outlier_detect_for_visit``, instead of ``Image3Pipeline``. CFP_OUT
    stamping is performed inside ``outlier_detect_for_visit`` via
    ``atomic_save``.

    The shared comparison weights the single drizzles by the inverse
    background variance and expects background-subtracted exposures (the
    ``bkg`` step's output), so ``weight_type`` other than ``"ivm"`` and a
    non-zero ``backg`` are refused here; both remain settings of the
    ``"jwst"`` implementation.
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
    all_inputs = _compute_overlap_inputs(
        visit_files, filter_files, sregions, cross_program,
    )
    log(f"  including {len(all_inputs)} files (visit + overlap"
        f"{', intra-program' if not cross_program else ''})")

    if not overwrite:
        if _visit_is_up_to_date(visit, visit_files, all_inputs, manifest_path,
                                status):
            return
    else:
        log(f"  --overwrite; running unconditionally")

    saved_extras = {
        os.path.basename(f).removesuffix('.fits'): _capture_extras(f)
        for f in visit_files
    }

    from campfire_pipeline.nircam.outlier_detect import outlier_detect_for_visit

    def pair(name, default):
        value = step_config.get(name, default)
        if isinstance(value, str):
            value = value.split()
        first, second = (float(x) for x in value)
        return first, second

    if step_config.get('weight_type', 'ivm') != 'ivm':
        raise ValueError(
            f"[nircam.outlier].weight_type = {step_config['weight_type']!r}: "
            f"the campfire implementation weights the single drizzles by the "
            f"inverse background variance ('ivm') only")
    if float(step_config.get('backg', 0.0)) != 0.0:
        raise ValueError(
            "[nircam.outlier].backg is not zero: the campfire implementation "
            "compares background-subtracted exposures and adds no level")

    grow = (_grow_kwargs(step_config)
            if step_config.get('grow_large_regions', False) else None)

    outlier_detect_for_visit(
        all_inputs, visit_files,
        snr=pair('snr', '3.0 2.0'), scale=pair('scale', '1.2 0.7'),
        pixfrac=float(step_config.get('pixfrac', 1.0)),
        kernel=step_config.get('kernel', 'square'),
        good_bits=step_config.get('good_bits', '~DO_NOT_USE'),
        minmed_nsigma=pair('minmed_nsigma', '4.0 3.0'),
        minmed_max_n=int(step_config.get('minmed_max_n', 3)),
        min_contributors=int(step_config.get('min_contributors', 2)),
        maskpt=float(step_config.get('maskpt', 0.7)),
        extras_per_visit=saved_extras,
        plot=do_plot,
        grow=grow,
        protect=_star_protection(step_config, all_inputs, visit_files,
                                 refcat),
    )

    _write_outlier_manifest(visit, field, filtname, all_inputs, manifest_path)
