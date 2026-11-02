"""
One product's profile-fit extraction profile, extraction v2 (prototype).

Per product (one source, one disperser), after Spec3 has written its crf:

1. load the source's exposures (``profile_fit.load_source``);
2. predict other slitlets' light and mask or subtract it for the shape fit
   (``profile_contam``; ``contam_predict``): the members of the source's joint-pass
   neighbourhood from that solve (``joint_first_pass.add_joint_light``, subtracted,
   never masked), every other source from its own pass 1;
3. choose the shape (``profile_chain``; for gratings the PRISM shape of the same
   source is read from the PRISM product of this run or an earlier one);
4. scan for a same-slitlet neighbour on the residuals (``nod_scan``; ``scan_neighbours``);
5. drizzle the chosen model per unit reported flux through the crf (``PROF2D``), the
   predicted other-source light (``CONTAM2D``) and its uncertainty (``CONTERR2D``), and
   write ``{product}_pf.fits`` with every decision in the primary header.

Stage 3 then extracts ``fnu_prof`` and ``fnu_contam`` from the s2d with ``PROF2D`` and
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
        contam_mode=str(g('contam_mode', 'hybrid')).lower(),
        kappa=float(g('contam_mask_kappa', 1.0)),
        pair_ratio=float(g('contam_pair_ratio', 3.0)),
        pair_min_frac=float(g('contam_pair_min_frac', 0.3)),
        dominance_kappa=float(g('contam_dominance_kappa', 1.0)),
        scan=bool(g('scan_neighbours', True)),
        scan_nbins=int(g('scan_nbins', 16)),
        scan_width=g('scan_width', None),
        scan_dchi2=float(g('scan_dchi2', 80.0)),
        scan_min_snr=float(g('scan_min_snr', 5.0)),
        scan_drop_frac=float(g('scan_drop_frac', 0.2)),
        scan_null_ratio=float(g('scan_null_ratio', 1.5)),
        scan_max_masked=float(g('scan_max_masked', 0.3)),
        scan_noise=str(g('scan_noise', 'mean_square')).lower(),
        scan_close=float(g('scan_close', 0.9)),
        scan_close_rule=str(g('scan_close_rule', 'all')).lower(),
        scan_gain_rule=bool(g('scan_gain_rule', False)),
        scan_flag_dchi2=float(g('scan_flag_dchi2', 1500.0)),
        scan_flag_dchi2_low=float(g('scan_flag_dchi2_low', 500.0)),
        scan_flag_null_ratio=float(g('scan_flag_null_ratio', 5.0)),
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
    M0 = chain_result.model
    M = PF.ProfileModel(ex, chromatic=M0.chromatic, priors=M0.priors, bounds=M0.bounds_cfg, c_prior=M0.c_prior,
                        grid=M0.lam_grid)
    M.pivot = M0.pivot                       # the chain's shape is defined at its own pivot
    p = chain_result.params
    shape = dict(centre_offset=p['centre'] - M0.c_prior, drift=p['drift'], width=p['width'],
                 width_change=p['width_change'])
    return PF.fit_fixed(model=M, shape=shape), share


@dataclass
class Analysis:
    """Everything one product's profile fit decided (for the writer and for diagnostics)."""
    exps: list
    chain: object                     # profile_chain.ChainResult
    prediction: object = None         # profile_contam.Prediction
    clean: dict = None
    contam_state: str = 'off'
    scan: object = None               # nod_scan.ScanResult


def analyse_product(workspace_dir, groups, overrides, science, source_id, stage_config, is_prism,
                    prism_product='', pass1_products=None, log=None, exps=None, pass1_for=None, maps=None,
                    prism=None):
    """Load, predict and apply other slitlets' light, choose the shape, scan for a
    same-slitlet neighbour. No files are written.

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
            exps = PF.load_source(groups, bkg_overrides=overrides, science=set(science))
    raw = [copy.copy(e) for e in exps]      # as stage 2b made them: the same-slitlet scan's data
    P, clean, cstate = None, None, 'off'
    t_mark = TM.start()
    if fl['contam'] and cfg.shape != 'free':
        maps = CT.maps_for_exposures(workspace_dir, exps) if maps is None else maps
        if not maps:
            cstate = 'no_maps'
        else:
            names = pass1_products or {}
            if pass1_for is None:
                def pass1_for(sid):
                    name = names.get(int(sid))
                    path = CT.pass1_path(workspace_dir, name) if name else None
                    return CT.Pass1.read(path) if path and os.path.exists(path) else None

            def why_missing(sid):
                name = names.get(int(sid))
                if name is None:
                    # pass 1 runs for every source with a stage-3 science exposure
                    return ('no science exposure of it reaches stage 3 in this disperser (every nod '
                            'excluded, e.g. by a background override), so it has no s2d either')
                return CT.missing_reason(workspace_dir, name)
            # the neighbours' light from the target's own joint solve (joint_first_pass), when
            # its pass 1 came from there: those members are left out of the prediction from
            # their own pass 1s and added from it
            jl = _own_joint_light(workspace_dir, names, source_id)
            members = {int(s) for s in jl['members']} if jl is not None else set()
            P = CT.predict(exps, source_id, maps, pass1_for, workspace_dir, own_pass1=pass1_for(source_id),
                           pair_ratio=fl['pair_ratio'], why_missing=why_missing, exclude=members)
            if jl is not None:
                from campfire_pipeline.nirspec import joint_first_pass as JF
                try:
                    JF.add_joint_light(P, exps, jl)
                except ValueError as e:
                    log(f'{e}; neighbours predicted from their own pass 1s')
                    P = CT.predict(exps, source_id, maps, pass1_for, workspace_dir, own_pass1=pass1_for(source_id),
                                   pair_ratio=fl['pair_ratio'], why_missing=why_missing)
            saved = [(e.ok, e.diff) for e in exps]
            clean = CT.apply(exps, P, mode=fl['contam_mode'], kappa=fl['kappa'], dominance_kappa=fl['dominance_kappa'])
            cstate = fl['contam_mode'] if P.have_maps else f"{fl['contam_mode']}_partial_maps"
            npix = sum(int(m.sum()) for _, m in PF.fit_pixel_masks(exps))
            if npix < MIN_FIT_PIXELS:
                # masking left (almost) nothing to fit: fit the data as they are, flagged
                for e, (ok, d) in zip(exps, saved):
                    e.ok, e.diff = ok, d
                log(f'contamination masking left {npix} pixels; undone')
                cstate, clean = f'{cstate}_undone', None
    TM.stop('contam', t_mark)
    if prism is None and not is_prism and prism_product:
        prism = CT.read_prism_shape(workspace_dir, prism_product)
    ratio = (lambda R: CT.contam_light_ratio(P, R, exps)) if P is not None and clean is not None else None
    with TM.step('chain'):
        C = PC.choose_shape(exps, cfg, is_prism, prism=prism, clean=clean, log=log, contam_ratio=ratio)
    if P is not None:
        CT.pair_flag(P, C.result, exps, min_frac=fl['pair_min_frac'])
    S = None
    t_mark = TM.start()
    if fl['scan']:
        # the scan never sees other slitlets' light: their pixels are left out, not modelled
        with TM.step('scan_input'):
            if maps is None:
                maps = CT.maps_for_exposures(workspace_dir, exps)
            R_scan, share = scan_input(raw, C.result, maps, source_id)
        S = NS.run(R_scan if R_scan is not None else C.result, nbins=fl['scan_nbins'], width=fl['scan_width'],
                   dchi2_min=fl['scan_dchi2'],
                   min_snr=fl['scan_min_snr'], drop_frac=fl['scan_drop_frac'], null_ratio=fl['scan_null_ratio'],
                   noise=fl['scan_noise'], close=fl['scan_close'], close_rule=fl['scan_close_rule'],
                   gain_rule=fl['scan_gain_rule'], flag_dchi2=fl['scan_flag_dchi2'],
                   flag_low=fl['scan_flag_dchi2_low'], flag_null_ratio=fl['scan_flag_null_ratio'])
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
    TM.stop('scan', t_mark)
    return Analysis(exps=exps, chain=C, prediction=P, clean=clean, contam_state=cstate, scan=S)


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
            if 'PFNBRD' in h:            # detection at the proposal level (PFNBR is the stricter flag)
                return bool(h['PFNBRD']), float(h.get('PFNBRDEL', np.nan))
            if 'PFNBR' in h:
                return bool(h['PFNBR']), float(h.get('PFNBRDEL', np.nan))
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
    path = CT.pass1_path(workspace_dir, name) if name else None
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
    path = CT.pass1_path(workspace_dir, name) if name else None
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


def shape_header(C, exps):
    """The shape cards of a product's ``_pf.fits`` (the chain's cards, the per-exposure
    pointing offsets and their count), in the order ``run_product`` writes them."""
    from astropy.io import fits
    hdr = fits.Header()
    for k, v in C.header_cards().items():
        hdr[k] = v
    for k, (e, o) in enumerate(zip(exps, C.result.eoff)):
        hdr[f'PFOFF{k}'] = (float(o), f'Pointing offset {e.name[:31]}')
    hdr['PFNEXP'] = (len(exps), 'Exposures in the model (incl. partners)')
    return hdr


def run_product(product_name, workspace_dir, groups, overrides, science, source_id, stage_config,
                is_prism, prism_product='', pass1_products=None, cleanup_crfs=True):
    """Build ``{product_name}_pf.fits``. Returns a dict summary (for the driver).

    Named conditions (``FALLBACK_NO_CRF`` and the ``profile_fit.ProfileFitFallback``
    subclasses: an old-layout observation, no usable pixel, too few wavelengths) write
    a ``_pf.fits`` that holds only the reason; the product's best spectrum is then the
    3-pixel boxcar and the summary's ``fallback`` names the reason. Any other
    exception propagates: the stage fails.

    pass1_products : {source id: pass-1 product name} of the configuration (contamination)."""
    from astropy.io import fits
    from campfire_pipeline.common.io import log
    from campfire_pipeline.nirspec import profile_fit as PF
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec.stage3 import _cleanup_spec3_intermediates
    from campfire_pipeline.nirspec import profile_timing as TM

    prev_cwd = os.getcwd()
    os.chdir(workspace_dir)
    TM.reset()
    path = os.path.join(workspace_dir, f'{product_name}_pf.fits')
    try:
        crf = f'{product_name}_s{source_id:09d}_crf.fits'
        if not os.path.exists(crf):
            log(f"{product_name}: no Spec3 crf ({crf}); 3-pixel boxcar")
            write_fallback(path, FALLBACK_NO_CRF)
            return _fallback_summary(product_name, source_id, is_prism, FALLBACK_NO_CRF)
        try:
            A = analyse_product(workspace_dir, groups, overrides, science, source_id, stage_config, is_prism,
                                prism_product=prism_product, pass1_products=pass1_products,
                                log=lambda m: log(f'{product_name}: {m}'))
        except PF.ProfileFitFallback as e:
            log(f"{product_name}: {e}; 3-pixel boxcar ({e.reason})")
            write_fallback(path, e.reason)
            return _fallback_summary(product_name, source_id, is_prism, e.reason)
        exps, C, P, clean, cstate, S = A.exps, A.chain, A.prediction, A.clean, A.contam_state, A.scan
        R = C.result
        p = R.params
        log(f"{product_name}: shape {C.shape} ({C.reason}), gate {C.snr_det:.1f}/{C.gate:.0f}, "
            f"centre {p['centre']:+.3f} (plan {p['centre_prior']:+.3f}), width {p['width']:.3f}")
        if S is not None:
            if S.error:
                log(f'{product_name}: neighbour scan failed ({S.error})')
            elif S.detected:
                log(f'{product_name}: same-slitlet neighbour at Delta {S.delta:+.2f}, dchi2 {S.dchi2:.0f}, '
                    f'proposals {S.proposals}, unresolved {S.unresolved}')

        t_mark = TM.start()
        prof = PF.drizzle_frames(R, crf, unit=True)
        hdus = [fits.ImageHDU(np.asarray(prof.data, float) / 1e12, name='PROF2D')]
        prof.close()
        if P is not None:
            live = CT.live_frames(exps, P)
            c2d = PF.drizzle_arrays(lambda name: live.get(name), crf)
            h = fits.ImageHDU(np.asarray(c2d.data, float), name='CONTAM2D')
            h.header['BUNIT'] = ('MJy', 'Predicted other-source light, same units as SCI')
            hdus.append(h)
            c2d.close()
            live = CT.live_frames(exps, P, what='dpred')
            e2d = PF.drizzle_arrays(lambda name: live.get(name), crf)
            h = fits.ImageHDU(np.asarray(e2d.data, float), name='CONTERR2D')
            h.header['BUNIT'] = ('MJy', 'Uncertainty of CONTAM2D (systematic), as SCI')
            hdus.append(h)
            e2d.close()
            ns = _own_notsep(workspace_dir, pass1_products, source_id)
            if ns is not None:
                hdus.append(ns)
        TM.stop('drizzle', t_mark)

        hdr = shape_header(C, exps)
        hdr['PFNSRCF'] = (int(sum(not e.has_source for e in exps)), 'Exposures without the source (SRCFLUX=F)')
        hdr['PFCMODE'] = (cstate, 'Other-slitlet light: off/mask/hybrid/no_maps/..._undone')
        if P is not None:
            def _v(x):
                return float(x) if np.isfinite(x) else -999.0
            hdr['PFMASKF'] = (float(clean['masked']) if clean else 0.0, 'Fraction of main-shutter pixels dropped')
            hdr['PFCLEANP'] = (_v(clean['clean_pred']) if clean else -999.0,
                               'Main-shutter fraction with other light < noise')
            hdr['PFFOPEN'] = (_v(clean['failed_open']) if clean else -999.0,
                              'Main-shutter fraction in failed-open shutters')
            hdr['PFNCONT'] = (len(P.contributors), 'Other sources predicted on the trace')
            # not a subset of PFNCONT: an unreliable source is counted here whether or not
            # its light reaches the noise
            hdr['PFCUNREL'] = (len(P.unreliable), 'Other sources, pass 1 unreliable (masked)')
            # their own pass 1 fell back: predicted from their stage-3 3-pixel boxcar (subtracted)
            hdr['PFCBOX'] = (len(getattr(P, 'boxcar', ()) or ()), 'Other sources predicted from a 3px boxcar')
            hdr['PFPAIR'] = (int(P.pair), 'Similar-brightness contaminant on the trace')
            hdr['PFPAIRID'] = (int(P.pair_id), 'Its source id (-1: none)')
            hdr['PFPAIRR'] = (_v(P.pair_ratio), 'Its pass-1 flux / the target pass-1 flux')
            hdr['PFPAIRF'] = (_v(P.pair_frac), 'Its light / target light in the extraction')
        if S is not None:
            for k, v in S.header_cards().items():
                hdr[k] = v
        t_mark = TM.start()
        out = fits.HDUList([fits.PrimaryHDU(header=hdr)] + hdus)
        out.writeto(path, overwrite=True)
        TM.stop('write', t_mark)
        TM.report(f'product {product_name}', log=log)
        return dict(product=product_name, source_id=int(source_id), shape=C.shape, reason=C.reason,
                    is_prism=bool(is_prism),
                    scan=(None if S is None else dict(detected=S.detected, candidate=S.candidate, delta=S.delta,
                                                      dchi2=S.dchi2, proposals=S.proposals,
                                                      unresolved=S.unresolved, xdisp=S.xdisp)),
                    fallback=None)
    finally:
        if cleanup_crfs:
            _cleanup_spec3_intermediates(list(science), product_name)
        os.chdir(prev_cwd)
