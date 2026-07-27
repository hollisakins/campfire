"""Align exposure I/O: read a canonical exposure group, solve, write back.

This is the FITS layer of the NIRCam ``align`` phase. For one exposure group it
reads each detector's gwcs and detects sources, runs the in-memory solve
(:func:`campfire_pipeline.nircam.align.solve.solve_exposure_group`), and writes
the corrected gwcs back onto each canonical with a ``CFP_ALGN`` provenance stamp
— or a ``NOT_ALIGNED`` sentinel when the exposure can't be tied to the reference
(WCS preserved, never retried).

Idempotency: the pre-align gwcs is stashed in an ``ALGN_BAK`` extension on first
apply; on ``overwrite`` the solve runs from that baseline, so re-running never
composes a second correction on top of the first. ``align`` uses its OWN backup
extension (not ``wcs_shift``'s ``WCS_BAK``) because both steps run in the process
loop: ``wcs_shift`` applies manual offsets first and backs up the pre-*shift* WCS
in ``WCS_BAK``, so ``align`` must solve from the wcs_shift-corrected WCS and leave
``WCS_BAK`` intact. The gwcs<->ASDF-in-FITS backup technique is shared with
``steps/wcs_shift.py``.

The reference catalog and config knobs arrive already resolved — refcat
resolution / loading and ``[<field>.align]`` parsing live in the field-level
orchestration.

**Diagnostics on disk.** align's self-reported residual is measured on the very
sample it matched, so it cannot expose a confident lock onto the wrong sources.
Two outputs exist for INDEPENDENT validation: an ``ALGNCAT`` binary table per
detector (every detection, its sky position under the WRITTEN WCS, and whether /
how far it matched the reference — so overlapping exposures can be cross-checked
against each other without drizzling anything), and the ``ALGN*`` scalar header
keywords (detection / match / refcat-density counts plus the matcher's peak
contrasts, which DO distinguish a decisive lock from an ambiguous one). Neither
is read by the pipeline; they exist to be audited.
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
    ab_zeropoint_from_sci_header,
    detect_sources,
)
from campfire_pipeline.nircam.align.solve import (
    DetectorInput,
    GroupSolution,
    solve_exposure_group,
)
from campfire_pipeline.nircam.association import exposure_key

# align's OWN gwcs backup — the pre-align input WCS. Deliberately distinct from
# wcs_shift's ``WCS_BAK`` (which holds the pre-*shift* WCS): wcs_shift runs before
# align in the process loop, so if align solved from ``WCS_BAK`` it would discard
# the manual offset wcs_shift applied. align solves from the current (post-shift)
# WCS, backs it up here, and preserves wcs_shift's ``WCS_BAK`` untouched.
ALGN_BAK_EXTNAME = 'ALGN_BAK'
WCS_BAK_EXTNAME = 'WCS_BAK'          # wcs_shift's backup — preserved, never solved from
# Per-source align diagnostics (one row per detection). Replaced — never
# duplicated — on a re-solve, like the backup extensions above.
ALGNCAT_EXTNAME = 'ALGNCAT'
NOT_ALIGNED_SENTINEL = cfp.NOT_ALIGNED

# Columns of the ALGNCAT table, in order: the detection catalog as the solve saw
# it, plus the sky position under the WCS this file now carries and the match
# outcome. ``ra``/``dec``/``ref_ra``/``ref_dec`` stay float64 (degrees at
# milliarcsecond precision needs it); everything else is float32/int to keep the
# extension small — it is written to every aligned canonical.
_ALGNCAT_COLUMNS = (
    ('x', 'f4'), ('y', 'f4'), ('ra', 'f8'), ('dec', 'f8'),
    ('flux', 'f4'), ('fluxerr', 'f4'), ('mag', 'f4'), ('snr', 'f4'),
    ('npix', 'i4'), ('id', 'i8'), ('matched', 'bool'),
    ('sep_arcsec', 'f4'), ('ref_ra', 'f8'), ('ref_dec', 'f8'),
)

# Scalar align diagnostics, stamped on the primary header. Deliberately NOT
# added to ``cfp.NIRCAM``: that keyset is the step-provenance *chain* — its order
# drives ``reset --from`` slicing and it renders the ``status`` completion table —
# and these keywords record how WELL the solve did, not that it ran. They follow
# the same convention otherwise (a ``{key: comment}`` map, applied as
# ``{key: (value, comment)}`` through ``atomic_save(header_updates=...)``).
#
# Every key is written on every solve, carrying ``ALGN_UNDEF`` when the quantity
# was not measured — never omitted, so a re-solve can never leave a previous
# solve's number standing (FITS headers forbid NaN and ``atomic_save`` sets
# keywords rather than deleting them, so silence would mean staleness).
ALGN_UNDEF = 'UNDEF'          # not measured (see :func:`_hdr_num`)
ALGN_DIAG_COMMENTS = {
    'ALGNNDET': 'align: sources detected on this detector',
    'ALGNNMAT': 'align: sources matched 1-to-1 to the refcat',
    'ALGNNREF': 'align: refcat sources in exposure footprint',
    'ALGNGPK':  'align: gross 2D offset-hist peak height',
    'ALGNGRU':  'align: gross runner-up peak height',
    'ALGNGCON': 'align: gross peak contrast (peak/runner-up)',
    'ALGNGMAS': 'align: gross peak neighbourhood mass (pairs)',
    'ALGNXPK':  'align: dx consensus peak height',
    'ALGNXFWH': 'align: dx consensus peak FWHM (arcsec)',
    'ALGNXCON': 'align: dx peak contrast (peak/runner-up)',
    'ALGNYPK':  'align: dy consensus peak height',
    'ALGNYFWH': 'align: dy consensus peak FWHM (arcsec)',
    'ALGNYCON': 'align: dy peak contrast (peak/runner-up)',
    'ALGNHBIN': 'align: consensus histogram bin (arcsec)',
    'ALGNSTAL': 'align: ALGNCAT does not match the current WCS',
}

# GroupSolution.diagnostics key -> FITS keyword (see histmatch's ``diag``).
_ALGN_DIAG_KEYWORDS = {
    'gross_peak': 'ALGNGPK',
    'gross_runner_up': 'ALGNGRU',
    'gross_contrast': 'ALGNGCON',
    'gross_mass': 'ALGNGMAS',
    'dx_peak': 'ALGNXPK',
    'dx_fwhm_arcsec': 'ALGNXFWH',
    'dx_contrast': 'ALGNXCON',
    'dy_peak': 'ALGNYPK',
    'dy_fwhm_arcsec': 'ALGNYFWH',
    'dy_contrast': 'ALGNYCON',
    'hist_binsize_arcsec': 'ALGNHBIN',
}

# Solve/detection knobs threaded from [<field>.align]; the orchestration passes
# a resolved dict, these are the fallbacks. Per-filter PSF FWHM
# (``psf_fwhm_by_filter``) is resolved per member below, not passed through
# these keys. ``pool_modules`` is an orchestration key (it decides how detectors
# are pooled before the solve is called), so it is deliberately NOT a solve key.
_SOLVE_KEYS = ('coarse_searchrad', 'refine_niter',
               'd2d_max', 'binsize_px', 'gaussian_sigma_px',
               'rough_cut_px_min', 'rough_cut_px_max', 'nfwhm', 'hist_nsigma',
               'histocut_order', 'slope_max', 'slope_nsteps', 'delta_mag_lim',
               'fine_fitgeom', 'fine_min_general',
               'fine_min_rshift', 'fine_min_shift', 'tolerance', 'match_radius',
               'min_matched', 'min_coverage_arcsec', 'ref_border_arcmin',
               'nclip', 'sigma', 'max_residual_arcsec',
               'dva_repivot', 'dva_pivot', 'fine_min_significance')
_DETECT_KEYS = ('fwhm', 'snr_thresh', 'minarea', 'deblend_nthresh',
                'deblend_cont', 'edge', 'snr_min', 'objmag_lim')


# --- gwcs <-> ASDF-in-FITS backup (technique shared with steps/wcs_shift.py) --

def _serialize_gwcs_to_hdu(wcs, name=ALGN_BAK_EXTNAME):
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
    """Atomically set CFP_ALGN=*value* (header only; WCS untouched).

    Also marks any ``ALGNCAT`` from an earlier successful solve as **stale**
    (``ALGNSTAL``), because this path leaves the extension in place: the WCS it
    was computed against is not the one the file now carries. Marking is
    deliberate rather than deleting the HDU — a datamodel round-trip registers
    ``ALGNCAT`` in the embedded ASDF's ``extra_fits``, and dropping the HDU with
    plain ``astropy`` (as here) would leave that reference dangling and break the
    next datamodel load (see ``common.io.atomic_save``'s note). Consumers must
    treat ``ALGNSTAL = T`` as "no per-source diagnostics for this WCS"; a later
    successful solve rewrites both the extension and the flag.
    """
    base, ext = os.path.splitext(path)
    tmp = f'{base}.tmp{ext}'
    with fits.open(path) as hdul:
        hdul[0].header['CFP_ALGN'] = (value, cfp.CFP_COMMENTS['CFP_ALGN'])
        # Every scalar diagnostic describes ONE solve. A file re-solved to
        # NOT_ALIGNED must not keep the counts and peak contrasts of an earlier
        # successful run standing beside the rejection — that would read as
        # provenance for the rejected attempt. Reset them all to UNDEF; the
        # all-keys-on-every-solve contract is what makes staleness impossible.
        for key in ALGN_DIAG_COMMENTS:
            if key != 'ALGNSTAL':
                hdul[0].header[key] = (ALGN_UNDEF, ALGN_DIAG_COMMENTS[key])
        hdul[0].header['ALGNSTAL'] = (True, ALGN_DIAG_COMMENTS['ALGNSTAL'])
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


def _hdr_num(value):
    """A FITS-safe header value: the float/int itself, else :data:`ALGN_UNDEF`.

    FITS headers cannot hold NaN/Inf, and an *undefined* (valueless) card is not
    an option either: a datamodel round-trip pulls it into ``extra_fits`` as an
    ``astropy.io.fits.card.Undefined``, which the embedded ASDF cannot serialize
    — the next save raises. A short string is legal in both, so unmeasured
    quantities are stamped rather than skipped, which is what keeps a re-solve
    from leaving a previous solve's number standing.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ALGN_UNDEF
    if not np.isfinite(v):
        return ALGN_UNDEF
    return int(value) if isinstance(value, (int, np.integer)) else v


def _diag_header_updates(solution, det):
    """``{keyword: (value, comment)}`` for the scalar align diagnostics.

    Every :data:`ALGN_DIAG_COMMENTS` key is present (undefined where unmeasured)
    so a re-solve overwrites the previous solve's numbers wholesale. ``det`` is
    this detector's :class:`~...solve.DetectorSolution`, *solution* its pool.
    """
    diag = solution.diagnostics or {}
    values = {kw: _hdr_num(diag.get(k))
              for k, kw in _ALGN_DIAG_KEYWORDS.items()}
    values['ALGNNDET'] = _hdr_num(getattr(det, 'n_detected', None))
    values['ALGNNMAT'] = _hdr_num(det.n_matched)
    values['ALGNNREF'] = _hdr_num(solution.n_refcat_in_footprint)
    # This solve wrote a matching ALGNCAT, so nothing is stale.
    values['ALGNSTAL'] = False
    return {kw: (values[kw], ALGN_DIAG_COMMENTS[kw])
            for kw in ALGN_DIAG_COMMENTS}


def _algncat_hdu(catalog, det):
    """The per-source ``ALGNCAT`` table for one detector, or ``None``.

    One row per detection — the catalog the solve worked from, its sky position
    under *det*'s (final, accepted) WCS, and the match outcome — which is what
    makes an independent check possible: two overlapping exposures can be
    compared source-by-source without resampling either of them. Returns ``None``
    (and logs) when the solve carried no per-source diagnostics or they are not
    row-aligned with *catalog*, rather than writing a misleading table.
    """
    n = len(catalog)
    matched = getattr(det, 'matched', None)
    if matched is None or len(matched) != n:
        log(f"align: no per-source diagnostics for {det.detector} "
            f"({ALGNCAT_EXTNAME} not written).")
        return None

    ra, dec = det.wcs(np.asarray(catalog['x'], dtype=float),
                      np.asarray(catalog['y'], dtype=float))
    source = dict(catalog.columns)
    source.update(ra=ra, dec=dec, matched=matched,
                  sep_arcsec=det.sep_arcsec,
                  ref_ra=det.ref_ra, ref_dec=det.ref_dec)

    cols = {}
    for name, dtype in _ALGNCAT_COLUMNS:
        col = source.get(name)
        fill = False if dtype == 'bool' else (0 if dtype[0] == 'i' else np.nan)
        cols[name] = (np.full(n, fill, dtype=dtype) if col is None
                      else np.asarray(col, dtype=dtype))
    from astropy.table import Table
    hdu = fits.BinTableHDU(Table(cols), name=ALGNCAT_EXTNAME)
    hdu.header['ALGNDOF'] = (det.dof, 'align: geometry this WCS was fit with')
    return hdu


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
    """Open a canonical, pick the pre-align gwcs (from ALGN_BAK if this file was
    aligned before), detect sources, and return a :class:`DetectorInput`."""
    # AB zeropoint for calibrated detection magnitudes (None -> uncalibrated
    # fallback, objmag_lim skipped loudly) — read cheaply before the datamodel.
    with fits.open(path, memmap=False) as hdul:
        zeropoint = ab_zeropoint_from_sci_header(hdul['SCI'].header)

    model = ImageModel(path, memmap=False)
    try:
        wi = model.meta.wcsinfo.instance
        wcsinfo = {'v2_ref': wi['v2_ref'], 'v3_ref': wi['v3_ref'],
                   'roll_ref': wi['roll_ref']}
        orig_wcs = model.meta.wcs
        err = (np.asarray(model.err, dtype=float)
               if getattr(model, 'err', None) is not None else None)
        cat = detect_sources(np.asarray(model.data, dtype=float), err,
                             mask=_detect_mask(model),
                             zeropoint=zeropoint, **detect_cfg)
    finally:
        model.close()

    # A prior ALIGN stashed the pre-align gwcs in ALGN_BAK; solve from it so an
    # overwrite re-run corrects the original input WCS, never the already-aligned
    # one. First-time solve uses the current meta.wcs above — which, when a field
    # also runs wcs_shift, is the wcs_shift-CORRECTED WCS (we deliberately do NOT
    # read wcs_shift's WCS_BAK here, or the manual offset would be discarded).
    with fits.open(path, memmap=False) as hdul:
        if ALGN_BAK_EXTNAME in hdul:
            orig_wcs = _deserialize_gwcs_from_hdu(hdul[ALGN_BAK_EXTNAME])

    return DetectorInput(detector=detector, wcs=orig_wcs, wcsinfo=wcsinfo,
                         catalog=cat)


def _write_solution(path, corrected_wcs, cfp_value, ImageModel,
                    update_fits_wcsinfo, *, algncat=None, diag_updates=None):
    """Write *corrected_wcs* back onto the canonical + stamp CFP_ALGN, stashing
    the pre-align gwcs in ALGN_BAK and preserving SRCMASK and any wcs_shift
    ``WCS_BAK``.

    *algncat* (a ``BinTableHDU``) and *diag_updates* are the align diagnostics for
    this detector. The table rides the same ``extra_hdus`` replace-or-append path
    as the backup extensions, so a re-solve overwrites it in place rather than
    accumulating copies; the keywords ride ``header_updates`` alongside CFP_ALGN,
    in the one atomic write."""
    existing_algn_bak = None
    wcs_shift_bak = None
    srcmask_hdu = None
    with fits.open(path, memmap=False) as hdul:
        if ALGN_BAK_EXTNAME in hdul:
            ab = hdul[ALGN_BAK_EXTNAME]
            existing_algn_bak = fits.ImageHDU(data=ab.data.copy(),
                                              header=ab.header.copy(),
                                              name=ALGN_BAK_EXTNAME)
        if WCS_BAK_EXTNAME in hdul:            # wcs_shift's backup — keep intact
            wb = hdul[WCS_BAK_EXTNAME]
            wcs_shift_bak = fits.ImageHDU(data=wb.data.copy(),
                                          header=wb.header.copy(),
                                          name=WCS_BAK_EXTNAME)
        if 'SRCMASK' in hdul:
            sm = hdul['SRCMASK']
            srcmask_hdu = fits.ImageHDU(data=sm.data.copy(),
                                        header=sm.header.copy(), name='SRCMASK')

    model = ImageModel(path, memmap=False)
    try:
        # Preserve the pre-align baseline: keep an existing ALGN_BAK across
        # re-solves, else the current (pre-align, wcs_shift-corrected) WCS
        # becomes it.
        algn_bak = (existing_algn_bak if existing_algn_bak is not None
                    else _serialize_gwcs_to_hdu(model.meta.wcs))
        model.meta.wcs = corrected_wcs
        try:
            update_fits_wcsinfo(model)
        except (ValueError, RuntimeError) as e:
            log(f"align: update_fits_wcsinfo failed on {os.path.basename(path)} "
                f"({type(e).__name__}); FITS SIP keywords not refreshed.")

        extra_hdus = [algn_bak]
        if wcs_shift_bak is not None:
            extra_hdus.append(wcs_shift_bak)
        if srcmask_hdu is not None:
            extra_hdus.append(srcmask_hdu)
        if algncat is not None:
            extra_hdus.append(algncat)
        header_updates = cfp.format(CFP_ALGN=cfp_value)
        header_updates.update(diag_updates or {})
        atomic_save(model, path, header_updates=header_updates,
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

    # Per-filter matched-filter kernel FWHM: the kernel should track the PSF
    # core, whose width runs from ~F070W to ~F480M, so a single fwhm is wrong
    # across the exposure's SW+LW channels. Key it off each member's filter,
    # falling back to the scalar `fwhm`.
    psf_by_filter = {str(k).lower(): float(v)
                     for k, v in (config.get('psf_fwhm_by_filter') or {}).items()}
    default_fwhm = detect_cfg.get('fwhm', 1.5)

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
    # The detection catalogs are still live here (the solve read, never replaced,
    # them), row-aligned with each solution's per-source diagnostic arrays — this
    # is what ALGNCAT is built from.
    by_input = {d.detector: d for d in detectors}
    for m in members:
        det_sol = by_detector.get(m.detector)
        # ``aligned=False`` marks a per-detector residual-gate reject inside an
        # otherwise SOLVED pool — that detector is stamped NOT_ALIGNED (WCS
        # preserved, quarantined from combine) like a failed pool.
        if (solution.status == 'SOLVED' and det_sol is not None
                and getattr(det_sol, 'aligned', True)):
            det_in = by_input.get(m.detector)
            algncat = (_algncat_hdu(det_in.catalog, det_sol)
                       if det_in is not None else None)
            _write_solution(m.path, det_sol.wcs,
                            _format_algn_value(det_sol, refcat_hash),
                            ImageModel, update_fits_wcsinfo,
                            algncat=algncat,
                            diag_updates=_diag_header_updates(solution,
                                                              det_sol))
        else:
            _stamp_algn(m.path, NOT_ALIGNED_SENTINEL)

    log(f"align[{key}]: {solution.status} "
        f"({len(members)} detectors, n_matched={solution.n_matched})")
    return solution
