"""Align exposure I/O: read a canonical exposure group, solve, write back.

This is the FITS layer of the NIRCam ``align`` phase. For one exposure group it
reads each detector's gwcs and detects sources, runs the in-memory solve
(:func:`campfire_pipeline.nircam.align.solve.solve_exposure_group`), and writes
the corrected gwcs back onto each canonical with a ``CFP_ALGN`` provenance stamp
— or a ``NOT_ALIGNED`` sentinel when the exposure can't be tied to the reference
(WCS preserved, never retried).

Idempotency: the original (un-aligned) gwcs is stashed in a ``WCS_BAK``
extension on first apply; on ``overwrite`` the solve runs from that original, so
re-running never composes a second correction on top of the first. Mirrors the
``steps/wcs_shift.py`` write-back contract (the ``WCS_BAK`` helpers are
replicated here because ``align`` supersedes and will remove ``wcs_shift``).

The reference catalog and config knobs arrive already resolved — refcat
resolution / loading and ``[<field>.align]`` parsing live in the field-level
orchestration.
"""

import io
import os
import warnings

import numpy as np
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import atomic_save, log
from campfire_pipeline.nircam.align.detect import (
    DETECT_DQ_BITS,
    detect_star_centroids,
)
from campfire_pipeline.nircam.align.solve import (
    DetectorInput,
    GroupSolution,
    solve_exposure_group,
)
from campfire_pipeline.nircam.association import exposure_key

WCS_BAK_EXTNAME = 'WCS_BAK'
NOT_ALIGNED_SENTINEL = cfp.NOT_ALIGNED

# Solve/detection knobs threaded from [<field>.align]; the orchestration passes
# a resolved dict, these are the fallbacks. Per-filter PSF FWHM
# (``psf_fwhm_by_filter``) is resolved per member below, not passed through
# these keys. ``bootstrap_max`` and the ``refine_*`` knobs are solve keys (the
# triangle-vertex cap moved to the bootstrap; detection is no longer count-
# capped, so ``brightest`` is gone from the align path).
_SOLVE_KEYS = ('fitgeom', 'minobj', 'nclip', 'sigma', 'tolerance', 'adaptive',
               'adaptive_min_matches', 'match_radius', 'min_matched',
               'ref_border_arcmin', 'bootstrap_max', 'refine_searchrad',
               'refine_tolerance', 'refine_niter')
_DETECT_KEYS = ('fwhm', 'nsigma', 'edge', 'snr_min', 'objmag_lim',
                'sharplo', 'sharphi', 'roundlo', 'roundhi')


# --- WCS_BAK gwcs <-> ASDF-in-FITS (replicated from steps/wcs_shift.py) ------

def _serialize_gwcs_to_hdu(wcs, name=WCS_BAK_EXTNAME):
    import asdf
    af = asdf.AsdfFile({'wcs': wcs})
    buf = io.BytesIO()
    af.write_to(buf)
    data = np.frombuffer(buf.getvalue(), dtype=np.uint8).copy()
    return fits.ImageHDU(data=data, name=name)


def _deserialize_gwcs_from_hdu(hdu):
    import asdf
    buf = io.BytesIO(hdu.data.tobytes())
    with asdf.open(buf, lazy_load=False) as af:
        return af['wcs']


def _stamp_algn(path, value):
    """Atomically set CFP_ALGN=*value* (header only; WCS untouched)."""
    base, ext = os.path.splitext(path)
    tmp = f'{base}.tmp{ext}'
    with fits.open(path) as hdul:
        hdul[0].header['CFP_ALGN'] = (value, cfp.CFP_COMMENTS['CFP_ALGN'])
        hdul.writeto(tmp, overwrite=True)
    os.replace(tmp, path)


def _format_algn_value(det):
    # Per-detector provenance, kept short so the value + card comment fit one
    # 80-char FITS card. The shared shift/rot is group-level (same for every
    # detector) and is logged once per group, not stamped on each card.
    return f'dof={det.dof} res={det.residual_arcsec:.3g} n={det.n_matched}'


def _detect_mask(model):
    mask = ~np.isfinite(np.asarray(model.data, dtype=float))
    if getattr(model, 'err', None) is not None:
        mask |= ~np.isfinite(np.asarray(model.err, dtype=float))
    if getattr(model, 'dq', None) is not None:
        mask |= (np.asarray(model.dq).astype(np.uint32) & DETECT_DQ_BITS) != 0
    return mask


def _load_detector(path, detector, detect_cfg, ImageModel):
    """Open a canonical, pick the ORIGINAL gwcs (from WCS_BAK if this file was
    aligned before), detect sources, and return a :class:`DetectorInput`."""
    model = ImageModel(path, memmap=False)
    try:
        wi = model.meta.wcsinfo.instance
        wcsinfo = {'v2_ref': wi['v2_ref'], 'v3_ref': wi['v3_ref'],
                   'roll_ref': wi['roll_ref']}
        orig_wcs = model.meta.wcs
        cat = detect_star_centroids(np.asarray(model.data, dtype=float),
                                    mask=_detect_mask(model), **detect_cfg)
    finally:
        model.close()

    # A prior align stashed the un-aligned gwcs; solve from it so an overwrite
    # re-run corrects the original, never the already-corrected WCS.
    with fits.open(path, memmap=False) as hdul:
        if WCS_BAK_EXTNAME in hdul:
            orig_wcs = _deserialize_gwcs_from_hdu(hdul[WCS_BAK_EXTNAME])

    return DetectorInput(detector=detector, wcs=orig_wcs, wcsinfo=wcsinfo,
                         catalog=cat)


def _write_solution(path, corrected_wcs, cfp_value, ImageModel,
                    update_fits_wcsinfo):
    """Write *corrected_wcs* back onto the canonical + stamp CFP_ALGN, keeping
    SRCMASK and stashing the original gwcs in WCS_BAK."""
    existing_wcs_bak = None
    srcmask_hdu = None
    with fits.open(path, memmap=False) as hdul:
        if WCS_BAK_EXTNAME in hdul:
            wb = hdul[WCS_BAK_EXTNAME]
            existing_wcs_bak = fits.ImageHDU(data=wb.data.copy(),
                                             header=wb.header.copy(),
                                             name=WCS_BAK_EXTNAME)
        if 'SRCMASK' in hdul:
            sm = hdul['SRCMASK']
            srcmask_hdu = fits.ImageHDU(data=sm.data.copy(),
                                        header=sm.header.copy(), name='SRCMASK')

    model = ImageModel(path, memmap=False)
    try:
        # Preserve the true original: keep an existing WCS_BAK, else the current
        # (still un-aligned) WCS becomes the baseline.
        wcs_bak = (existing_wcs_bak if existing_wcs_bak is not None
                   else _serialize_gwcs_to_hdu(model.meta.wcs))
        model.meta.wcs = corrected_wcs
        try:
            update_fits_wcsinfo(model)
        except (ValueError, RuntimeError) as e:
            log(f"align: update_fits_wcsinfo failed on {os.path.basename(path)} "
                f"({type(e).__name__}); FITS SIP keywords not refreshed.")

        extra_hdus = [wcs_bak] + ([srcmask_hdu] if srcmask_hdu is not None else [])
        atomic_save(model, path,
                    header_updates=cfp.format(CFP_ALGN=cfp_value),
                    extra_hdus=extra_hdus)
    finally:
        model.close()


def align_exposure_group(members, refcat, *, key=None, config=None,
                         overwrite=False, status=None):
    """Align one exposure group and write the results to its canonicals.

    Parameters
    ----------
    members : list of association.ExposureMember
        The detectors of one exposure (each carries ``.path``, ``.detector``).
    refcat : astropy.table.Table
        Static reference catalog with ``RA``/``DEC`` (deg), already loaded.
    key : str, optional
        Exposure key; defaults to the shared exposure token.
    config : dict, optional
        Resolved ``[<field>.align]`` knobs (solve + detection).
    overwrite, status :
        Standard step idempotency controls.

    Returns
    -------
    GroupSolution
        ``status`` is ``'SOLVED'`` / ``'NOT_ALIGNED'`` / ``'SKIPPED'``.
    """
    members = list(members)
    if not members:
        return GroupSolution(key or '', 'SKIPPED', None, None, None, 0, [])
    if key is None:
        key = exposure_key(members[0].path)
    config = dict(config or {})

    def _aligned_ok(path):
        # A detector counts as done only if it carries a *completed, non-rejected*
        # alignment. A NOT_ALIGNED exposure is re-attempted on a normal re-run
        # (no --overwrite) so the user can retune [<field>.align] params and try
        # again without force-re-solving everything that already succeeded.
        stamped = (status.has(path, 'CFP_ALGN') if status is not None
                   else cfp.has_step(path, 'CFP_ALGN'))
        return stamped and cfp.step_value(path, 'CFP_ALGN') != NOT_ALIGNED_SENTINEL

    if not overwrite and all(_aligned_ok(m.path) for m in members):
        log(f"align[{key}]: all {len(members)} detectors already aligned; "
            f"skipping (use --overwrite to re-solve).")
        return GroupSolution(key, 'SKIPPED', None, None, None, 0, [])

    solve_cfg = {k: config[k] for k in _SOLVE_KEYS if k in config}
    detect_cfg = {k: config[k] for k in _DETECT_KEYS if k in config}

    # Per-filter detection PSF FWHM: NIRCam's core width runs from ~F070W to
    # ~F480M, so a single fwhm is wrong across the exposure's SW+LW channels.
    # Key it off each member's filter, falling back to the scalar `fwhm`.
    psf_by_filter = {str(k).lower(): float(v)
                     for k, v in (config.get('psf_fwhm_by_filter') or {}).items()}
    default_fwhm = detect_cfg.get('fwhm', 2.5)

    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        from jwst.datamodels import ImageModel
        from jwst.assign_wcs.util import update_fits_wcsinfo

    detectors = []
    for m in members:
        member_cfg = dict(detect_cfg)
        member_cfg['fwhm'] = psf_by_filter.get(m.filter_name.lower(),
                                               default_fwhm)
        detectors.append(_load_detector(m.path, m.detector, member_cfg,
                                        ImageModel))

    solution = solve_exposure_group(detectors, refcat, key=key, **solve_cfg)

    by_detector = {d.detector: d for d in solution.detectors}
    for m in members:
        det_sol = by_detector.get(m.detector)
        if solution.status == 'SOLVED' and det_sol is not None:
            _write_solution(m.path, det_sol.wcs,
                            _format_algn_value(det_sol),
                            ImageModel, update_fits_wcsinfo)
        else:
            _stamp_algn(m.path, NOT_ALIGNED_SENTINEL)

    log(f"align[{key}]: {solution.status} "
        f"({len(members)} detectors, n_matched={solution.n_matched})")
    return solution
