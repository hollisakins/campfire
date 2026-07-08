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
# these keys. ``pool_modules`` is an orchestration key (it decides how detectors
# are pooled before the solve is called), so it is deliberately NOT a solve key.
_SOLVE_KEYS = ('coarse_searchrad', 'coarse_tolerance', 'coarse_separation',
               'refine_niter', 'fine_fitgeom', 'fine_min_general',
               'fine_min_rshift', 'fine_min_shift', 'tolerance', 'match_radius',
               'min_matched', 'min_coverage_arcsec', 'ref_border_arcmin',
               'nclip', 'sigma')
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


def _algn_rc(value):
    """The ``rc=`` refcat-hash token from a CFP_ALGN value string, or None."""
    for tok in str(value or '').split():
        if tok.startswith('rc='):
            return tok[3:]
    return None


def _format_algn_value(det, refcat_hash=None):
    # Per-detector provenance, kept short so the value + card comment fit one
    # 80-char FITS card. The coarse shift/rot is pool-level (same for every
    # detector) and is logged once per pool, not stamped on each card. The ``rc=``
    # token records which reference catalog produced the solve — read back by the
    # orchestration skip check to re-solve when the refcat changes.
    base = f'dof={det.dof} res={det.residual_arcsec:.3g} n={det.n_matched}'
    return f'{base} rc={refcat_hash}' if refcat_hash else base


def _exposure_mid_mjd(path):
    """Exposure mid-time as MJD from the primary header, or ``None``.

    Prefers ``EXPMID`` / ``MJD-AVG``; falls back to the mean of
    ``EXPSTART``/``EXPEND``. Read cheaply from the primary header (all detectors
    of one exposure share the same time), used to propagate refcat proper
    motions to the epoch the shutter was actually open.
    """
    with fits.open(path, memmap=False) as hdul:
        h = hdul[0].header
        for card in ('EXPMID', 'MJD-AVG'):
            if h.get(card) is not None:
                return float(h[card])
        start, end = h.get('EXPSTART'), h.get('EXPEND')
        if start is not None and end is not None:
            return 0.5 * (float(start) + float(end))
    return None


def _propagate_refcat(refcat, path, key):
    """Return *refcat* with positions moved to this exposure's mid-time.

    A no-op for a stationary (galaxy) refcat with no proper-motion columns.
    Fails open — aligns against catalog positions rather than rejecting the
    exposure — if the epoch can't be read or propagation errors.
    """
    from campfire_pipeline.nircam.refcat.io import has_proper_motion

    if not has_proper_motion(refcat):
        return refcat
    try:
        epoch = _exposure_mid_mjd(path)
        if epoch is None:
            log(f"align[{key}]: no exposure mid-time in header; using catalog "
                f"positions (no proper-motion propagation).")
            return refcat
        from campfire_pipeline.nircam.refcat.motion import propagate_to_epoch
        out = propagate_to_epoch(refcat, epoch)
        log(f"align[{key}]: propagated refcat proper motions to MJD {epoch:.4f}.")
        return out
    except Exception as e:  # noqa: BLE001 — align without propagation, don't reject
        log(f"align[{key}]: refcat epoch propagation failed "
            f"({type(e).__name__}: {e}); using catalog positions.")
        return refcat


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
    refcat_hash = config.get('_refcat_hash')   # provenance/staleness (orchestration)

    def _aligned_ok(path):
        # A detector counts as done only if it carries a *completed, non-rejected*
        # alignment produced by the *current* refcat. A NOT_ALIGNED exposure — or
        # one solved against a now-changed refcat (rc mismatch) — is re-attempted
        # on a normal re-run (no --overwrite) so the user can retune params /
        # swap the refcat without force-re-solving everything that succeeded.
        stamped = (status.has(path, 'CFP_ALGN') if status is not None
                   else cfp.has_step(path, 'CFP_ALGN'))
        if not stamped:
            return False
        value = cfp.step_value(path, 'CFP_ALGN')
        if value == NOT_ALIGNED_SENTINEL:
            return False
        return refcat_hash is None or _algn_rc(value) == refcat_hash

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

    # Reading + solving one exposure must never abort a whole field's align. Any
    # unexpected failure (a corrupt canonical, an unforeseen solver error the
    # in-solve guards missed) degrades this exposure to NOT_ALIGNED — surfaced
    # loudly by the align step and quarantined from combine — not crashing.
    try:
        detectors = []
        for m in members:
            member_cfg = dict(detect_cfg)
            member_cfg['fwhm'] = psf_by_filter.get(m.filter_name.lower(),
                                                   default_fwhm)
            detectors.append(_load_detector(m.path, m.detector, member_cfg,
                                            ImageModel))
        refcat = _propagate_refcat(refcat, members[0].path, key)
        solution = solve_exposure_group(detectors, refcat, key=key, **solve_cfg)
    except Exception as e:  # noqa: BLE001 — one bad exposure must not abort the field
        log(f"align[{key}]: FAILED — {type(e).__name__}: {e}; "
            f"stamping NOT_ALIGNED (WCS preserved, excluded from combine).")
        for m in members:
            try:
                _stamp_algn(m.path, NOT_ALIGNED_SENTINEL)
            except Exception as se:  # noqa: BLE001 — best-effort stamp
                log(f"align[{key}]: could not stamp NOT_ALIGNED on "
                    f"{os.path.basename(m.path)} ({type(se).__name__}).")
        return GroupSolution(key, 'NOT_ALIGNED', None, None, None, 0, [])

    by_detector = {d.detector: d for d in solution.detectors}
    for m in members:
        det_sol = by_detector.get(m.detector)
        if solution.status == 'SOLVED' and det_sol is not None:
            _write_solution(m.path, det_sol.wcs,
                            _format_algn_value(det_sol, refcat_hash),
                            ImageModel, update_fits_wcsinfo)
        else:
            _stamp_algn(m.path, NOT_ALIGNED_SENTINEL)

    log(f"align[{key}]: {solution.status} "
        f"({len(members)} detectors, n_matched={solution.n_matched})")
    return solution
