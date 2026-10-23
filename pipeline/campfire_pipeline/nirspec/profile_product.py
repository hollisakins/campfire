"""
One product's profile-fit extraction profile, extraction v2 (prototype).

Per product (one source, one disperser), after Spec3 has written its crf:

1. load the source's exposures (``profile_fit.load_source``);
2. predict other slitlets' light from the neighbours' pass-1 spectra and mask or
   subtract it for the shape fit (``profile_contam``; ``contam_predict``);
3. choose the shape (``profile_chain``; for gratings the PRISM shape of the same
   source is read from the PRISM product of this run or an earlier one);
4. scan for a same-slitlet neighbour on the residuals (``nod_scan``; ``scan_neighbours``);
5. drizzle the chosen model per unit reported flux through the crf (``PROF2D``) and
   the predicted other-source light (``CONTAM2D``), and write ``{product}_pf.fits``
   with every decision in the primary header.

Stage 3 then extracts ``fnu_pf`` from the s2d with ``PROF2D`` (``stage3._profile_fit_columns``).
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
        failed_open=str(g('contam_failed_open', 'flag')).lower(),
        beyond=str(g('contam_beyond_range', 'mask')).lower(),
        dominance_kappa=float(g('contam_dominance_kappa', 1.0)),
        scan=bool(g('scan_neighbours', True)),
        scan_nbins=int(g('scan_nbins', 16)),
        scan_width=g('scan_width', None),
        scan_dchi2=float(g('scan_dchi2', 100.0)),
        scan_min_snr=float(g('scan_min_snr', 5.0)),
        scan_drop_frac=float(g('scan_drop_frac', 0.2)),
        scan_null_ratio=float(g('scan_null_ratio', 1.5)),
        scan_max_masked=float(g('scan_max_masked', 0.3)),
        scan_noise=str(g('scan_noise', 'mean_square')).lower(),
        scan_close=float(g('scan_close', 0.9)),
        scan_xdisp_tol=float(g('scan_xdisp_tol', 0.2)),
    )


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
    from campfire_pipeline.nirspec import profile_fit as PF
    from campfire_pipeline.nirspec import profile_chain as PC
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec import nod_scan as NS

    log = log or (lambda *a: None)
    fl = stage_flags(stage_config)
    cfg = PC.ChainConfig.from_stage_config(stage_config)
    if exps is None:
        exps = PF.load_source(groups, bkg_overrides=overrides, science=set(science))
    P, clean, cstate = None, None, 'off'
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
            P = CT.predict(exps, source_id, maps, pass1_for, workspace_dir, own_pass1=pass1_for(source_id),
                           pair_ratio=fl['pair_ratio'], failed_open=fl['failed_open'], beyond=fl['beyond'])
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
    if prism is None and not is_prism and prism_product:
        prism = CT.read_prism_shape(workspace_dir, prism_product)
    ratio = (lambda R: CT.contam_light_ratio(P, R, exps)) if P is not None and clean is not None else None
    C = PC.choose_shape(exps, cfg, is_prism, prism=prism, clean=clean, log=log, contam_ratio=ratio)
    if P is not None:
        CT.pair_flag(P, C.result, exps, min_frac=fl['pair_min_frac'])
    S = None
    if fl['scan']:
        S = NS.run(C.result, nbins=fl['scan_nbins'], width=fl['scan_width'], dchi2_min=fl['scan_dchi2'],
                   min_snr=fl['scan_min_snr'], drop_frac=fl['scan_drop_frac'], null_ratio=fl['scan_null_ratio'],
                   noise=fl['scan_noise'], close=fl['scan_close'])
        # residuals of a target dominated by other slitlets' light are not a same-slitlet
        # neighbour's signature (seen on 5215): report, do not propose
        if clean is not None and clean['masked'] > fl['scan_max_masked']:
            S.suppress(f"other-slitlet light, masked {clean['masked']:.2f}")
        elif (C.bound_bits & PF.BOUND_BITS['width']) or C.result.params['width'] >= 0.99 * cfg.bounds.width_max:
            # the target's width is on its bound: extended or two-component; its
            # residuals read as a neighbour next to it (13113)
            S.suppress('target width on its bound')
        if not is_prism and prism_product and fl['scan_xdisp_tol'] >= 0:
            S.cross_check(read_scan(workspace_dir, prism_product), tol=fl['scan_xdisp_tol'])
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
            if 'PFNBR' in h:
                return bool(h['PFNBR']), float(h.get('PFNBRDEL', np.nan))
    return None


def run_product(product_name, workspace_dir, groups, overrides, science, source_id, stage_config,
                is_prism, prism_product='', pass1_products=None, cleanup_crfs=True):
    """Build ``{product_name}_pf.fits``. Returns a dict summary (for the driver), or
    None when the profile fit could not run. A failure is logged and leaves the
    standard products untouched.

    pass1_products : {source id: pass-1 product name} of the configuration (contamination)."""
    from astropy.io import fits
    from campfire_pipeline.common.io import log
    from campfire_pipeline.nirspec import profile_fit as PF
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec.stage3 import _cleanup_spec3_intermediates

    prev_cwd = os.getcwd()
    os.chdir(workspace_dir)
    try:
        crf = f'{product_name}_s{source_id:09d}_crf.fits'
        if not os.path.exists(crf):
            log(f"{product_name}: no Spec3 crf ({crf}); skipping profile fit")
            return None
        A = analyse_product(workspace_dir, groups, overrides, science, source_id, stage_config, is_prism,
                            prism_product=prism_product, pass1_products=pass1_products,
                            log=lambda m: log(f'{product_name}: {m}'))
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

        hdr = fits.Header()
        for k, v in C.header_cards().items():
            hdr[k] = v
        for k, (e, o) in enumerate(zip(exps, R.eoff)):
            hdr[f'PFOFF{k}'] = (float(o), f'Pointing offset {e.name[:31]}')
        hdr['PFNEXP'] = (len(exps), 'Exposures in the model (incl. subtracted partners)')
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
            hdr['PFCMISS'] = (len(P.missing), 'Of those, without a pass-1 spectrum (masked)')
            hdr['PFCUNREL'] = (len(P.unreliable), 'Of those, pass 1 not decontaminated (masked)')
            hdr['PFCMFRM'] = (len(P.missing_frame), 'Of those, a pass-1 frame missing (masked)')
            nb = sum(int(b.sum()) for b in (P.beyond or []))
            hdr['PFCBEYND'] = (nb, 'Pixels lit by others past their range (masked)')
            hdr['PFPAIR'] = (int(P.pair), 'Similar-brightness contaminant on the trace')
            hdr['PFPAIRID'] = (int(P.pair_id), 'Its source id (-1: none)')
            hdr['PFPAIRR'] = (_v(P.pair_ratio), 'Its pass-1 flux / the target pass-1 flux')
            hdr['PFPAIRF'] = (_v(P.pair_frac), 'Its light / target light in the extraction')
        if S is not None:
            for k, v in S.header_cards().items():
                hdr[k] = v
        out = fits.HDUList([fits.PrimaryHDU(header=hdr)] + hdus)
        path = os.path.join(workspace_dir, f'{product_name}_pf.fits')
        out.writeto(path, overwrite=True)
        return dict(product=product_name, source_id=int(source_id), shape=C.shape, reason=C.reason,
                    is_prism=bool(is_prism),
                    scan=(None if S is None else dict(detected=S.detected, delta=S.delta, dchi2=S.dchi2,
                                                      proposals=S.proposals, unresolved=S.unresolved,
                                                      xdisp=S.xdisp)))
    except Exception as e:                       # noqa: BLE001
        import traceback
        log(f"{product_name}: profile fit failed ({type(e).__name__}: {e}); standard extraction only")
        log(traceback.format_exc()[-1500:])
        return None
    finally:
        if cleanup_crfs:
            _cleanup_spec3_intermediates(list(science), product_name)
        os.chdir(prev_cwd)
