"""
One product's profile-fit extraction profile, extraction v2.

Per product (one source, one disperser), after Spec3 has written its crf:

1. load the source's exposures (``profile_fit.load_source``) and leave out of the nod
   differences the pixels Spec3's outlier detection flagged in the crf
   (``profile_fit.mask_crf_outliers``; the fits do not clip against their model);
2. predict other slitlets' light and mask or subtract it for the shape fit
   (``profile_contam``; ``contam_predict``): the members of the source's joint-pass
   neighbourhood from that solve (``joint_first_pass.add_joint_light``, subtracted,
   never masked), every other source from its own pass 1;
3. choose the shape (``profile_chain``; for gratings the PRISM shape of the same
   source is read from the PRISM product of this run or an earlier one);
4. scan for a same-slitlet neighbour on the residuals (``nod_scan``; ``scan_neighbours``);
5. build the product's pixel maps once from the crf (``drizzle_map.DrizzleMap``: the s2d's
   grid and each exposure's mapping onto it), drizzle through them the chosen model per
   unit reported flux (``PROF2D``), the predicted other-source light (``CONTAM2D``) and its
   uncertainty (``CONTERR2D``), and write ``{product}_pf.fits`` with every decision in the
   primary header;
6. with ``mbkg_spectrum``, the master-background product through the same maps
   (``mbkg_extraction.fit_product``: its 2-D, the fit at this shape, its ``PROF2D`` /
   ``CONTAM2D`` / ``CONTERR2D``).

Every 2-D image is drizzled here, before stage 3 extracts anything. Stage 3 then
extracts ``fnu_prof`` and ``fnu_contam`` from the s2d with ``PROF2D`` and
chooses the best spectrum ``fnu`` (``stage3._profile_fit_columns``), then deletes the
``_pf.fits``: what the ``_spec.fits`` does not keep (CONTERR2D) is reduced to its 1-D
columns and cards.
"""

import os
from dataclasses import dataclass

import numpy as np


MIN_FIT_PIXELS = 50     # fewer left after contamination masking: the masking is undone (flagged)


def stage_flags(sc):
    g = sc.get
    return dict(
        contam=bool(g('contam_predict', True)),
        kappa=float(g('contam_mask_kappa', 1.0)),
        scan=bool(g('scan_neighbours', True)),
        scan_nbins=int(g('scan_nbins', 16)),
        scan_width=g('scan_width', None),
        scan_dchi2=float(g('scan_dchi2', 80.0)),
        scan_min_snr=float(g('scan_min_snr', 5.0)),
        scan_drop_frac=float(g('scan_drop_frac', 0.2)),
        scan_null_ratio=float(g('scan_null_ratio', 1.5)),
        scan_max_masked=float(g('scan_max_masked', 0.3)),
        scan_close=float(g('scan_close', 0.9)),
        scan_xdisp_tol=float(g('scan_xdisp_tol', 0.2)),
    )


def scan_input(exps, chain_result, maps, source_id):
    """The same-slitlet scan's data, independent of the joint model: the nod differences
    as stage 2b made them (nothing subtracted or masked for other slitlets' light), less
    every pixel that another slitlet's source shutter lights in any frame of the
    difference (trace maps, ``profile_contam.geometry``), with the target refitted there
    at the chain's chosen shape. Returns (FitResult, share of the fitted pixels kept)."""
    import copy
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec import profile_fit as PF

    ex = [copy.copy(e) for e in exps]
    n0 = sum(int(m.sum()) for _, m in PF.fit_pixel_masks(ex))
    if maps:
        for e, m in zip(ex, CT.geometry(ex, source_id, maps).mask):
            e.ok = e.ok & ~m
    n1 = sum(int(m.sum()) for _, m in PF.fit_pixel_masks(ex))
    share = n1 / n0 if n0 else 0.0
    if n1 < MIN_FIT_PIXELS:
        return None, share
    return PF.refit_at_shape(chain_result, ex)[0], share


def partner_overrides(exps, n_groups):
    """The background nods each exposure subtracts now, as override dicts (one per
    background group, {nod (str): [nods]}; [] for an excluded nod)."""
    from campfire_pipeline.nirspec import nod_scan as NS
    out = [dict() for _ in range(n_groups)]
    for e in exps:
        out[e.group][str(NS._nod(e))] = [] if e.partners is None else sorted(NS._nod(exps[j]) for j in e.partners)
    return out


def neighbour_preview(groups, raw, chain_result, maps, source_id, science, stage_config, proposals, crf_dq=None):
    """What a scan proposal would change, for the review figure (``plots.plot_neighbour_proposal``).

    The source is loaded twice from the frames before subtraction (``profile_fit.load_group``
    with ``pre_bkgsub``), with the background nods it subtracts now and with the proposed
    ones, and fitted at the chain's shape on the scan's pixels (``scan_input``), so the two
    spectra differ only by the choice of nods. Returns a dict (the two spectra, and per
    science exposure its canonical and its background nods now and proposed, as
    [(nod, canonical)]), or None when the frames before subtraction are not there (a
    canonical stage 2b never subtracted)."""
    from campfire_pipeline.nirspec import nod_scan as NS
    from campfire_pipeline.nirspec import profile_fit as PF

    now = partner_overrides(raw, len(groups))
    prop = [dict(d) for d in now]
    for e in raw:
        want = (proposals.get(NS._root(e)) or {}).get(str(NS._nod(e)))
        if want is not None:
            prop[e.group][str(NS._nod(e))] = list(want)
    fits_, loaded = {}, {}
    for key, ov in (('now', now), ('proposed', prop)):
        ex = PF.load_source(groups, bkg_overrides=ov, science=set(science), pre_bkgsub=True,
                            **PF.load_options(stage_config))
        if crf_dq is not None:
            PF.mask_crf_outliers(ex, crf_dq)          # as the product's own fit (analyse_product)
        if partner_overrides(ex, len(groups)) != ov:
            return None                    # no frames before subtraction: the nods could not be changed
        R, _ = scan_input(ex, chain_result, maps, source_id)
        if R is None:
            return None
        fits_[key], loaded[key] = R, ex

    def nods(ex, e):
        return [(NS._nod(ex[j]), ex[j].path) for j in e.partners]

    ex_now, ex_prop = loaded['now'], loaded['proposed']
    proposed = {e.name: nods(ex_prop, e) for e in ex_prop if e.partners is not None}
    frames = [dict(root=NS._root(e), nod=NS._nod(e), detector=e.detector, path=e.path, now=nods(ex_now, e),
                   proposed=proposed.get(e.name, []))
              for e in ex_now if e.use_data and e.has_source and e.partners is not None]
    R = fits_['now']
    return dict(lam=np.asarray(R.lam, np.float32),
                now=(np.asarray(R.flux, np.float32), np.asarray(R.flux_err, np.float32)),
                proposed=(np.asarray(fits_['proposed'].flux, np.float32),
                          np.asarray(fits_['proposed'].flux_err, np.float32)),
                frames=frames)


@dataclass
class Analysis:
    """Everything one product's profile fit decided (for the writer and for diagnostics)."""
    exps: list
    chain: object                     # profile_chain.ChainResult
    prediction: object = None         # profile_contam.Prediction
    clean: dict = None
    contam_state: str = 'off'
    scan: object = None               # nod_scan.ScanResult
    preview: dict = None              # neighbour_preview, when the scan proposes other background nods


def analyse_product(workspace_dir, groups, overrides, science, source_id, stage_config, is_prism,
                    prism_product='', pass1_products=None, log=None, exps=None, pass1_for=None, maps=None,
                    prism=None, stuck_keys=None, shape_product=None, crf_dq=None):
    """Load, leave out Spec3's outliers, predict and apply other slitlets' light, choose the
    shape, scan for a same-slitlet neighbour. No files are written. ``stuck_keys``,
    ``shape_product``: as ``run_product``.

    crf_dq : {canonical name: crf DQ} of the product's Spec3 crf (``drizzle_map.crf_dq``): its
        outliers are left out of the nod differences (``profile_fit.mask_crf_outliers``; the fits
        no longer clip against their model). None: nothing is left out but the canonicals' own
        DO_NOT_USE, and the log says so (``run_product`` never gets here without a crf: it falls
        back to the 3-pixel boxcar, ``FALLBACK_NO_CRF``).

    For tests: ``exps`` (preloaded exposures, modified in place), ``pass1_for``
    (sid -> ``Pass1``), ``maps`` and ``prism`` (a ``PrismShape``) replace what is
    otherwise read from the workspace."""
    import copy
    from campfire_pipeline.nirspec import profile_fit as PF
    from campfire_pipeline.nirspec import profile_chain as PC
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec import nod_scan as NS
    from campfire_pipeline.nirspec import profile_timing as TM

    log = log or (lambda *a: None)
    fl = stage_flags(stage_config)
    cfg = PC.ChainConfig.from_stage_config(stage_config)
    if exps is None:
        with TM.step('load'):
            exps = PF.load_source(groups, bkg_overrides=overrides, science=set(science),
                                  **PF.load_options(stage_config))
    if crf_dq is None:
        log('no crf outlier flags: only the canonicals\' DO_NOT_USE pixels are left out')
    else:
        n_out, missing = PF.mask_crf_outliers(exps, crf_dq)
        log(f'{n_out} pixels of the nod differences left out as Spec3 outliers (crf)'
            + (f'; no crf entry for {missing}' if missing else ''))
    raw = [copy.copy(e) for e in exps]      # as stage 2b made them, Spec3's outliers left out: the scan's data
    P, clean, cstate = None, None, 'off'
    t_mark = TM.start()
    if fl['contam']:
        maps = CT.maps_for_exposures(workspace_dir, exps, stuck_keys) if maps is None else maps
        if not maps:
            cstate = 'no_maps'
        else:
            read_pass1, why_missing = CT.pass1_lookup(workspace_dir, pass1_products)
            pass1_for = pass1_for or read_pass1
            # the neighbours' light from the target's own joint solve (joint_first_pass), when
            # its pass 1 came from there: those members are left out of the prediction from
            # their own pass 1s and added from it
            jl = _own_joint_light(workspace_dir, pass1_products, source_id)
            members = {int(s) for s in jl['solved']} if jl is not None else set()
            P = CT.predict(exps, source_id, maps, pass1_for, workspace_dir, why_missing=why_missing, exclude=members)
            if jl is not None:
                from campfire_pipeline.nirspec import joint_first_pass as JF
                try:
                    JF.add_joint_light(P, exps, jl)
                except ValueError as e:
                    log(f'{e}; neighbours predicted from their own pass 1s')
                    P = CT.predict(exps, source_id, maps, pass1_for, workspace_dir, why_missing=why_missing)
            saved = [(e.ok, e.diff) for e in exps]
            clean = CT.apply(exps, P, kappa=fl['kappa'])
            cstate = 'subtracted' if P.have_maps else 'subtracted_partial_maps'
            npix = sum(int(m.sum()) for _, m in PF.fit_pixel_masks(exps))
            if npix < MIN_FIT_PIXELS:
                # masking left (almost) nothing to fit: fit the data as they are, flagged
                for e, (ok, d) in zip(exps, saved):
                    e.ok, e.diff = ok, d
                log(f'contamination masking left {npix} pixels; undone')
                cstate, clean = f'{cstate}_undone', None
    TM.stop('contam', t_mark)
    if prism is None and not is_prism and (shape_product or prism_product):
        prism = CT.read_prism_shape(workspace_dir, shape_product or prism_product)
    ratio = (lambda R: CT.contam_light_ratio(P, R, exps)) if P is not None and clean is not None else None
    with TM.step('chain'):
        C = PC.choose_shape(exps, cfg, is_prism, prism=prism, clean=clean, log=log, contam_ratio=ratio)
    S = None
    t_mark = TM.start()
    if fl['scan']:
        # the scan never sees other slitlets' light: their pixels are left out, not modelled
        with TM.step('scan_input'):
            if maps is None:
                maps = CT.maps_for_exposures(workspace_dir, exps, stuck_keys)
            R_scan, share = scan_input(raw, C.result, maps, source_id)
        S = NS.run(R_scan if R_scan is not None else C.result, nbins=fl['scan_nbins'], width=fl['scan_width'],
                   dchi2_min=fl['scan_dchi2'],
                   min_snr=fl['scan_min_snr'], drop_frac=fl['scan_drop_frac'], null_ratio=fl['scan_null_ratio'],
                   close=fl['scan_close'])
        # too few pixels free of other slitlets' light to tell a same-slitlet neighbour
        # (seen on 5215): report, do not propose
        if R_scan is None or 1.0 - share > fl['scan_max_masked']:
            S.suppress(f"other slitlets light {1.0 - share:.2f} of the pixels")
        elif (C.bound_bits & PF.BOUND_BITS['width']) or C.result.params['width'] >= 0.99 * cfg.bounds.width_max:
            # the target's width is on its bound: extended or two-component; its
            # residuals read as a neighbour next to it (13113)
            S.suppress('target width on its bound')
        if not is_prism and prism_product and fl['scan_xdisp_tol'] >= 0:
            S.cross_check(read_scan(workspace_dir, prism_product), tol=fl['scan_xdisp_tol'])
        if S.drop_in_force(raw):
            log('neighbour scan: proposals already in force left out')
    TM.stop('scan', t_mark)
    preview = None
    if S is not None and S.proposals and groups:
        with TM.step('scan_preview'):
            try:
                preview = neighbour_preview(groups, raw, C.result, maps, source_id, science, stage_config,
                                            S.proposals, crf_dq=crf_dq)
                if preview is None:
                    log('neighbour proposal: no preview (no frames before subtraction)')
            except Exception as e:                   # noqa: BLE001 - a review figure must not fail the product
                log(f'neighbour proposal preview failed ({type(e).__name__}: {e})')
    return Analysis(exps=exps, chain=C, prediction=P, clean=clean, contam_state=cstate, scan=S, preview=preview)


def read_scan(workspace_dir, product):
    """(detected, Delta) of a product's neighbour scan from its ``_pf.fits`` (this run) or
    ``_spec.fits`` header, or None."""
    from astropy.io import fits
    for s in ('_pf.fits', '_spec.fits'):
        p = os.path.join(workspace_dir, f'{product}{s}')
        if os.path.exists(p):
            try:
                h = fits.getheader(p)
            except OSError:
                continue
            if 'NBRDET' in h:
                return bool(h['NBRDET']), float(h.get('NBRDELTA', np.nan))
    return None


FALLBACK_NO_CRF = 'no_crf'      # Spec3 wrote no crf for the source (nothing to drizzle the model through)


def write_fallback(path, reason):
    """A ``_pf.fits`` holding only the fallback decision: the product keeps the 3-pixel
    boxcar as its best spectrum (``stage3._profile_fit_columns``)."""
    from astropy.io import fits
    from campfire_pipeline.nirspec.stage3 import extraction_cards
    fits.HDUList([fits.PrimaryHDU(header=extraction_cards('boxcar3', reason, 'off'))]).writeto(path, overwrite=True)


def _own_joint_light(workspace_dir, pass1_products, source_id):
    """The neighbours' light from the source's own joint solve
    (``joint_first_pass.read_joint_light``), or None: no joint pass 1, or the file is
    missing or of another solve."""
    from campfire_pipeline.nirspec import joint_first_pass as JF
    from campfire_pipeline.nirspec import profile_contam as CT
    name = (pass1_products or {}).get(int(source_id))
    path = CT.pass1_current(workspace_dir, name) if name else None
    if not path or not os.path.exists(path):
        return None
    z = np.load(path)
    if 'why' not in z or str(z['why']) != 'joint':
        return None
    return JF.read_joint_light(JF.joint_light_path(path), str(z['sig']))


def _own_notsep(workspace_dir, pass1_products, source_id):
    """``NOTSEP`` table of the source's own joint pass 1 (``joint_first_pass``): its
    wavelengths and whether its light there cannot be told apart from a neighbour's.
    Stage 3 sets ``fnu_contam`` to NaN there. None for any other pass 1."""
    from astropy.io import fits
    from campfire_pipeline.nirspec import profile_contam as CT
    name = (pass1_products or {}).get(int(source_id))
    path = CT.pass1_current(workspace_dir, name) if name else None
    if not path or not os.path.exists(path):
        return None
    z = np.load(path)
    if 'nosep' not in z:
        return None
    return fits.BinTableHDU.from_columns(
        [fits.Column(name='lam', format='D', unit='um', array=np.asarray(z['lam'], float)),
         fits.Column(name='notsep', format='L', array=np.asarray(z['nosep'], bool))], name='NOTSEP')


def _fallback_summary(product_name, source_id, is_prism, reason):
    return dict(product=product_name, source_id=int(source_id), shape=None, reason=None, is_prism=bool(is_prism),
                scan=None, fallback=reason)


def shape_header(C, exps=None):
    """The shape cards of a product's ``_pf.fits`` (the chain's ``extraction_cards``), in
    the order ``run_product`` writes them."""
    from astropy.io import fits
    from campfire_pipeline.nirspec import extraction_cards as XC
    hdr = fits.Header()
    for k, v in C.header_cards().items():
        XC.put(hdr, k, v)
    return hdr


def run_product(product_name, workspace_dir, groups, overrides, science, source_id, stage_config,
                is_prism, prism_product='', pass1_products=None, cleanup_crfs=True, stuck_keys=None,
                shape_product=None):
    """Build ``{product_name}_pf.fits`` and, with ``mbkg_spectrum``, the master-background
    files (module doc). Returns a dict summary (for the driver).

    Named conditions (``FALLBACK_NO_CRF`` and the ``profile_fit.ProfileFitFallback``
    subclasses: an old-layout observation, no usable pixel, too few wavelengths) write
    a ``_pf.fits`` that holds only the reason; the product's best spectrum is then the
    3-pixel boxcar and the summary's ``fallback`` names the reason (the
    master-background spectrum too; without a crf there is none). Any other
    exception propagates: the stage fails.

    pass1_products : {source id: pass-1 product name} of the configuration (contamination).
    stuck_keys : the observation's stuck physical shutters by root, left out of the trace
    maps (``profile_contam.trace_maps``).
    shape_product : the product whose shape a grating takes, when not ``prism_product``
    (``stage3.grating_transfer_plan``: another grating of the source); the neighbour
    scan's cross-check stays with ``prism_product``."""
    from astropy.io import fits
    from campfire_pipeline.common.io import log
    from campfire_pipeline.nirspec import drizzle_map as DM
    from campfire_pipeline.nirspec import profile_fit as PF
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec.stage3 import _cleanup_spec3_intermediates
    from campfire_pipeline.nirspec import profile_timing as TM

    from campfire_pipeline.nirspec import mbkg_extraction as MX
    from campfire_pipeline.nirspec import extraction_cards as XC

    prev_cwd = os.getcwd()
    os.chdir(workspace_dir)
    TM.reset()
    path = os.path.join(workspace_dir, f'{product_name}_pf.fits')
    mbkg = bool(stage_config.get('mbkg_spectrum', True))
    try:
        crf = DM.crf_path(product_name, source_id)
        if not os.path.exists(crf):
            # nothing to drizzle through: no master-background 2-D either
            log(f"{product_name}: no Spec3 crf ({crf}); 3-pixel boxcar, no master-background spectrum")
            write_fallback(path, FALLBACK_NO_CRF)
            return _fallback_summary(product_name, source_id, is_prism, FALLBACK_NO_CRF)

        def pixel_maps():
            # the product's grid and every exposure's pixel map onto it, once (drizzle_map)
            with TM.step('map'):
                return DM.DrizzleMap.from_crf(crf, DM.step_settings(f'{product_name}_s2d.fits'))
        try:
            A = analyse_product(workspace_dir, groups, overrides, science, source_id, stage_config, is_prism,
                                prism_product=prism_product, pass1_products=pass1_products,
                                log=lambda m: log(f'{product_name}: {m}'), stuck_keys=stuck_keys,
                                shape_product=shape_product, crf_dq=DM.crf_dq(crf))
        except PF.ProfileFitFallback as e:
            log(f"{product_name}: {e}; 3-pixel boxcar ({e.reason})")
            write_fallback(path, e.reason)
            if mbkg:
                with TM.step('mbkg'):
                    MX.write_fallback(product_name, workspace_dir, source_id, e.reason, pixel_maps(), log=log)
            return _fallback_summary(product_name, source_id, is_prism, e.reason)
        exps, C, P, clean, cstate, S = A.exps, A.chain, A.prediction, A.clean, A.contam_state, A.scan
        R = C.result
        p = R.params
        log(f"{product_name}: shape {C.shape} ({C.reason}), detection S/N {C.snr_det:.1f}, "
            f"centre {p['centre']:+.3f} (plan {p['centre_prior']:+.3f}), width {p['width']:.3f}")
        if S is not None:
            if S.error:
                log(f'{product_name}: neighbour scan failed ({S.error})')
            elif S.detected:
                log(f'{product_name}: same-slitlet neighbour at Delta {S.delta:+.2f}, dchi2 {S.dchi2:.0f}, '
                    f'proposals {S.proposals}, unresolved {S.unresolved}')

        dmap = pixel_maps()
        t_mark = TM.start()
        prof = PF.drizzle_frames(R, dmap, unit=True)
        hdus = [fits.ImageHDU(np.asarray(prof, float) / 1e12, name='PROF2D')]
        if P is not None:
            live = CT.live_frames(exps, P)
            c2d = PF.drizzle_arrays(lambda name: live.get(name), dmap)
            h = fits.ImageHDU(np.asarray(c2d, float), name='CONTAM2D')
            h.header['BUNIT'] = ('MJy', 'Predicted other-source light, same units as SCI')
            hdus.append(h)
            live = CT.live_frames(exps, P, what='dpred')
            e2d = PF.drizzle_arrays(lambda name: live.get(name), dmap)
            h = fits.ImageHDU(np.asarray(e2d, float), name='CONTERR2D')
            h.header['BUNIT'] = ('MJy', 'Uncertainty of CONTAM2D (systematic), as SCI')
            hdus.append(h)
            ns = _own_notsep(workspace_dir, pass1_products, source_id)
            if ns is not None:
                hdus.append(ns)
        TM.stop('drizzle', t_mark)

        hdr = shape_header(C)
        XC.put(hdr, 'SELFSUB', float(MX.self_subtraction(R, exps)))
        if P is not None:
            XC.put(hdr, 'NCONTAM', len(P.contributors))
            # their own pass 1 fell back: predicted from their stage-3 3-pixel boxcar
            XC.put(hdr, 'CONTBOX', len(getattr(P, 'boxcar', ()) or ()))
        if S is not None:
            for k, v in S.header_cards().items():
                XC.put(hdr, k, v)
        t_mark = TM.start()
        out = fits.HDUList([fits.PrimaryHDU(header=hdr)] + hdus)
        out.writeto(path, overwrite=True)
        TM.stop('write', t_mark)
        if mbkg:
            # the master-background 2-D and spectrum at the shape this chain chose, through
            # the same pixel maps (mbkg_extraction)
            with TM.step('mbkg'):
                MX.fit_product(product_name, workspace_dir, exps, science, source_id, C, stage_config, dmap,
                               pass1_products=pass1_products, log=log, stuck_keys=stuck_keys)
        TM.report(f'product {product_name}', log=log)
        return dict(product=product_name, source_id=int(source_id), shape=C.shape, reason=C.reason,
                    is_prism=bool(is_prism), snr_det=float(C.snr_det), transfer=C.transfer,
                    scan=(None if S is None else dict(detected=S.detected, delta=S.delta,
                                                      dchi2=S.dchi2, proposals=S.proposals,
                                                      unresolved=S.unresolved, xdisp=S.xdisp,
                                                      preview=A.preview)),
                    fallback=None)
    finally:
        if cleanup_crfs:
            _cleanup_spec3_intermediates(list(science), product_name)
        os.chdir(prev_cwd)
