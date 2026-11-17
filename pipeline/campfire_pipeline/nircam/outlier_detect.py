"""
outlier_detect: per-visit outlier rejection for NIRCam, on the shared code.

A visit's exposures are compared with each other and with the exposures of
other visits that overlap them (``nircam/steps/outlier.py`` selects those).
The comparison itself is ``imaging/outlier.py::detect_outliers``, the
instrument-neutral routine the HST instruments run: every exposure drizzled
alone onto one north-up grid at the native pixel scale, a per-pixel median
(the minimum where two or three exposures overlap and the median stands above
it by more than the noise allows), the median blotted back onto each
exposure, and the two-pass test ``|pixel - blot| > scale * gradient + snr *
sigma``. NIRCam reaches it through :class:`~campfire_pipeline.nircam.exposure.NircamChip`.

What stays NIRCam's, because it is about a JWST file and not about the
comparison:

* the flags go into ``DQ`` as ``OUTLIER | DO_NOT_USE``, and the arrays are
  blanked where ``DO_NOT_USE`` is set, as the JWST pipeline leaves them;
* large flagged regions can be grown (``grow_large_regions``,
  ``nircam/steps/outlier.py``);
* the diagnostic ``<rootname>_outlier.pdf``;
* ``CFP_OUT`` is stamped through ``atomic_save``.

Until 2026-11 this module was a second implementation of the same method
(its own grid in the instrument frame, a plain median, a linear blot, the
exposure's ``ERR`` as the noise). The differences that matter to a NIRCam
user are listed in ``pipeline/CHANGELOG.md`` under the release that made the
move.
"""

import os
import tempfile

import numpy as np

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import atomic_save, log


def outlier_detect_for_visit(
    all_inputs,
    visit_files,
    *,
    snr=(3.0, 2.0),
    scale=(1.2, 0.7),
    pixfrac=1.0,
    kernel='square',
    good_bits='~DO_NOT_USE',
    minmed_nsigma=(4.0, 3.0),
    minmed_max_n=3,
    min_contributors=2,
    maskpt=0.7,
    tempdir=None,
    extras_per_visit=None,
    plot=False,
    grow=None,
    protect=None,
):
    """Flag outliers on ``visit_files`` against ``all_inputs``.

    Parameters
    ----------
    all_inputs : list of str
        The exposures compared: the visit's own files and the ones of other
        visits that overlap them.
    visit_files : list of str
        The visit's files, a subset of ``all_inputs``: the only ones flagged
        and saved.
    snr, scale : pairs of float
        Thresholds of the two-pass test (the second pair applies next to the
        pixels the first pair flags).
    pixfrac, kernel : single-drizzle settings.
    good_bits : str
        DQ bits that do not make a pixel bad (``[nircam.outlier].good_bits``).
    minmed_nsigma, minmed_max_n, min_contributors, maskpt :
        Passed to :func:`campfire_pipeline.imaging.outlier.detect_outliers`,
        which documents them.
    tempdir : str, optional
        Where the single drizzles are kept while the median is built
        (default: the system temporary directory).
    extras_per_visit : dict, optional
        ``{rootname: [HDU, ...]}`` of extensions the JWST datamodel does not
        know (SRCMASK, CFMASK), captured before the run and written back.
    plot : bool
        Write ``<rootname>_outlier.pdf`` next to each visit exposure.
    grow : dict, optional
        Keyword arguments of ``grow_outlier_regions``; ``None`` turns the
        growth off.
    protect : dict, optional
        ``{path: bool array}`` from
        ``outlier_protect.protection_for_visit``: pixels around catalog stars
        that moved between the exposures compared. They are not tested, so
        they are never flagged and never lower the threshold of a neighbor.

    ``CFP_OUT`` is stamped on each visit file as it is saved.
    """
    from jwst.datamodels.dqflags import pixel as pixel_flags
    from jwst.lib.pipe_utils import match_nans_and_flags
    from stdatamodels.jwst.datamodels import ImageModel

    from campfire_pipeline.imaging.outlier import detect_outliers, warn_if_suspect
    from campfire_pipeline.nircam.exposure import open_exposure

    if not all_inputs:
        log("  outlier (per-visit): no inputs")
        return

    chips = {f: open_exposure(f, good_bits=good_bits) for f in all_inputs}
    missing = [f for f in visit_files if f not in chips]
    if missing:
        raise ValueError(
            f'outlier: visit files that are not among the inputs: {missing}')
    star_masks = {chips[f].key: mask for f, mask in (protect or {}).items()
                  if f in chips}

    result = detect_outliers(
        list(chips.values()),
        snr=snr, scale=scale, pixfrac=pixfrac, kernel=kernel,
        minmed_nsigma=minmed_nsigma, minmed_max_n=minmed_max_n,
        min_contributors=min_contributors, maskpt=maskpt,
        workdir=tempdir or tempfile.gettempdir(),
        protect=star_masks or None,
        flag_keys=[chips[f].key for f in visit_files],
    )

    outlier_bit = np.uint32(pixel_flags['OUTLIER'])
    dnu_bit = np.uint32(pixel_flags['DO_NOT_USE'])
    extras_per_visit = extras_per_visit or {}

    n_flagged_total = 0
    for crf in visit_files:
        key = chips[crf].key
        mask = result['flags'][key]
        stats = result['stats'][key]
        rootname = os.path.basename(crf).removesuffix('.fits')
        with ImageModel(crf, memmap=False) as model:
            dq_before = (model.dq & outlier_bit) != 0
            # weight-carrying state before detection: grow seeds must not
            # form on pixels that were already DO_NOT_USE (edge trim)
            dnu_before = (model.dq & dnu_bit) != 0
            sci_for_plot = model.data.copy() if plot else None

            model.dq |= mask.astype(np.uint32) * (dnu_bit | outlier_bit)
            # every array NaN where DO_NOT_USE is set, as the JWST outlier
            # step leaves a file
            match_nans_and_flags(model)

            dq_after = (model.dq & outlier_bit) != 0
            n_new = int((dq_after & ~dq_before).sum())
            n_flagged_total += n_new
            warn_if_suspect('outlier', rootname, n_new / max(int((~dnu_before).sum()), 1), log)
            cfp_val = None
            if key in star_masks:
                cfp_val = f"star_protect: {stats['n_protected']} px not tested"
                if stats['n_protected']:
                    log(f"  outlier: {rootname} — {stats['n_protected']} px "
                        f"around catalog stars that moved were not tested")

            grow_added = None
            if grow is not None:
                # lazy import: steps.outlier imports this module
                from campfire_pipeline.nircam.steps.outlier import (
                    grow_outlier_regions,
                )
                n_large, n_added, grow_added = grow_outlier_regions(
                    model.data, model.dq, preexisting_dnu=dnu_before, **grow)
                grown = (f'grow_large_regions: {n_large} regions, '
                         f'+{n_added} px')
                cfp_val = f'{cfp_val}; {grown}' if cfp_val else grown
                if n_large:
                    log(f"  outlier grow: {rootname} — {n_large} large "
                        f"regions, +{n_added} px flagged")
            atomic_save(
                model, crf,
                header_updates=cfp.format(CFP_OUT=cfp_val),
                extra_hdus=extras_per_visit.get(rootname),
            )

            if plot:
                from campfire_pipeline.nircam.steps._plots import plot_outlier
                # dq_after was taken before the grow, so new_outlier is the
                # detections only; grown pixels ride the second overlay
                new_outlier = dq_after & ~dq_before
                out_pdf = os.path.join(
                    os.path.dirname(crf), f'{rootname}_outlier.pdf',
                )
                plot_outlier(
                    sci_for_plot, new_outlier, grown=grow_added,
                    save_file=out_pdf,
                    title=f'{rootname}: outlier',
                )
                log(f"  saved {os.path.basename(out_pdf)}")

    log(f"  outlier: flagged {n_flagged_total} new pixels across "
        f"{len(visit_files)} visit exposures")
