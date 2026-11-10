"""
Choice of the source shape for the profile-fit extraction.

The profile fit (``profile_fit``) fits a source's cross-dispersion shape with
Gaussian priors on its centre and width; where the data say little, the priors
decide, and the fit settles on a compact source at the planned position. This
module decides, per product (one source, one disperser), which shape builds the
extraction profile:

1. **own**: the source's own fit, at every S/N (user, 2026-11-10: no detection gate).
   A fit that ends with a parameter on a bound is kept and flagged (``SHAPEWHY``
   ';bound:<names>', ``bound_bits``): falling back to the default shape reported about a
   quarter of the flux of sources of width 1 slit unit or more at every S/N, while the
   fit on its bound reported 0.95-1.10 of it (injections on zenith_p15, 2026-11-10). A
   grating with a PRISM shape takes its own fit only when the two differ, the own fit
   describes the data better by ``own_vs_prism_dchi2`` + k ln(N), and the detection S/N
   is at least ``own_vs_prism_min_gate``.
2. **prism**: for a grating, the shape fitted to the PRISM spectrum of the same
   source in the same observation, mapped to the grating's wavelengths by its
   chromatic terms (``profile_fit.map_shape``), keeping its offset from the planned
   position. Only when the PRISM product took its own shape and the two were taken
   at the same telescope roll. **grating**: where there is no PRISM shape, the same
   candidate from the source's other grating that took its own fit (its centre offset
   and width, held over the wavelengths; ``PrismShape``); stage 3 gives it to the
   products that fell to the default shape, once every grating has run
   (``stage3.grating_transfer_plan``).
3. **default**: the star-measured PSF times a Gaussian of the prior median width (or
   ``default_shape_width``) at the planned position. Only where there is no own fit:
   contamination masking left too little of the trace, the fit raised, or the planned
   positions disagree between exposures (``srcypos``).

Every candidate that runs is compared on the same pixels (a pixel is kept when any
candidate's outlier clipping keeps it), with the spectrum re-solved and without
prior terms, so the choice can be redone offline from the header.

The integrated detection S/N (``profile_fit.detection_snr``) is reported (``DETSNR``)
and decides nothing here except the PRISM comparison's ``own_vs_prism_min_gate``.
"""

from dataclasses import dataclass, field

import numpy as np

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import profile_timing as TM


SHAPES = ('gated', 'fixed')


@dataclass
class ChainConfig:
    shape: str = 'gated'                 # gated | fixed (the default shape only)
    chromatic: bool = True
    prism_transfer: bool = True
    gate_prism: float = 25.0             # the joint first pass's member shapes only (joint_first_pass.shape_for);
    gate_grating: float = 70.0           # the chains themselves have no detection gate
    own_vs_prism_dchi2: float = 25.0     # plus k ln(N_eff), k = shape parameters
    own_vs_prism_min_gate: float = 100.0  # below: a grating keeps the PRISM shape (own fit not better there)
    priors: PF.Priors = field(default_factory=PF.Priors)
    bounds: PF.Bounds = field(default_factory=PF.Bounds)
    srcypos_tolerance: float = 0.05
    pa_tolerance: float = 1.0            # deg; PRISM shape transferred only at the same roll
    agree_centre: float = 0.08           # own and PRISM shapes "agree" within these (then PRISM is kept)
    agree_log_width: float = 0.3
    disagree_centre: float = 0.1         # thresholds of the PRISM-own disagreement (reason prism_kept)
    disagree_width_ratio: tuple = (0.6, 1.6)
    min_clean_fraction: float = 0.5      # contamination masking: below this no own fit
    min_clean_fraction_exposure: float = 0.3
    default_width: float = None          # width of the default shape (None: the prior median width)
    mbkg_scale_sigma: float = 0.01       # allowance of the master-background scale term (mbkg_extraction); 0: off
    mbkg_scale_cap: float = 0.03         # a fitted |scale| this large: set to zero, refitted without the term

    def default_shape(self):
        """Shape dict of the default candidate for ``profile_fit.fit_fixed`` (None: the prior median width)."""
        return None if self.default_width is None else dict(width=float(self.default_width))

    @classmethod
    def from_stage_config(cls, sc):
        """From the merged ``[nirspec.stage3]`` configuration."""
        g = sc.get
        shape = str(g('profile_fit_shape', 'gated')).lower()
        if shape not in SHAPES:
            raise ValueError(f'profile_fit_shape must be one of {SHAPES}, got {shape!r}')
        priors = PF.Priors(
            centre_sigma=float(g('prior_centre_sigma', 0.08)),
            drift_sigma=float(g('prior_drift_sigma', 0.03)),
            width_median=float(g('prior_width_median', 0.15)),
            width_log_sigma=float(g('prior_width_log_sigma', 0.6)),
            width_change_sigma=float(g('prior_width_change_sigma', 0.04)))
        bounds = PF.Bounds(
            centre=float(g('bound_centre', 0.3)), drift=float(g('bound_drift', 0.3)),
            width_max=float(g('bound_width_max', 1.5)), width_change=float(g('bound_width_change', 0.6)))
        dwidth = g('default_shape_width', None)
        return cls(shape=shape, chromatic=bool(g('profile_fit_chromatic', True)),
                   prism_transfer=bool(g('profile_fit_prism_transfer', True)),
                   gate_prism=float(g('gate_snr_prism', 25.0)), gate_grating=float(g('gate_snr_grating', 70.0)),
                   own_vs_prism_dchi2=float(g('own_vs_prism_dchi2', 25.0)),
                   own_vs_prism_min_gate=float(g('own_vs_prism_min_gate', 100.0)),
                   agree_centre=float(g('own_vs_prism_centre', 0.08)),
                   agree_log_width=float(g('own_vs_prism_log_width', 0.3)), priors=priors, bounds=bounds,
                   srcypos_tolerance=float(g('srcypos_tolerance', 0.05)),
                   min_clean_fraction=float(g('contam_min_clean_fraction', 0.5)),
                   min_clean_fraction_exposure=float(g('contam_min_clean_fraction_exposure', 0.3)),
                   default_width=(None if dwidth is None else float(dwidth)),
                   mbkg_scale_sigma=float(g('mbkg_scale_allowance', 0.01)),
                   mbkg_scale_cap=float(g('mbkg_scale_cap', 0.03)))


@dataclass
class PrismShape:
    """What a grating needs from another product of the same source to take its shape: the
    PRISM product (``chromatic``: its drift and width change are measured over the whole
    range, so they map to any grating), or, where there is no PRISM shape, the source's
    grating product that took its own fit (not chromatic: its centre offset and width at its
    pivot, held at every wavelength, since a grating's chromatic terms do not extrapolate an
    octave or more to another grating's range)."""
    product: str
    shape: str                      # SHAPE of that product
    params: dict                    # centre, centre_prior, drift, width, width_change, pivot
    pa_v3: float = np.nan
    chromatic: bool = True

    @classmethod
    def from_header(cls, product, h, chromatic=True):
        if 'SHAPE' not in h:
            return None
        params = dict(centre=float(h['SRCCEN']), centre_prior=float(h['SRCCENPL']), drift=float(h['SRCDRIFT']),
                      width=float(h['SRCWIDTH']), width_change=float(h['SRCDWID']), pivot=float(h['SRCPIVOT']))
        if not chromatic:
            params.update(drift=0.0, width_change=0.0)
        return cls(product=product, shape=str(h['SHAPE']), params=params, pa_v3=float(h.get('PA_V3', np.nan)),
                   chromatic=chromatic)


@dataclass
class ChainResult:
    result: object                  # FitResult of the chosen shape (its model carries the exposures)
    shape: str                      # own | prism | default
    chain: str                      # gated | fixed
    reason: str
    snr_det: float = np.nan
    detection: dict = field(default_factory=dict)
    chi2: dict = field(default_factory=dict)        # candidate -> chi2 on the common pixels
    npix_common: int = 0
    bound_bits: int = 0
    srcy_flag: int = 0
    shdis: int = 0
    prism_product: str = ''
    transfer: str = 'none'          # used | none | no_prism | prism_not_own | pa_mismatch | off
    pa_v3: float = np.nan
    candidates: dict = field(default_factory=dict, repr=False)
    dominance: float = np.nan       # other sources' predicted light / the target's on the main shutter (recorded)

    def header_cards(self):
        """{card: value} of the chain (``extraction_cards``; written to the intermediate,
        copied to ``_spec.fits``). Values that do not apply are left out by ``put``."""
        p = self.result.params
        exps = self.result.model.exps
        c = {
            'EXTRVER': PF.FIT_VERSION,
            'OPTICVER': PF.optics_version(),
            'FLUXAPER': PF.REPORT_APERTURE,
            'SHAPE': self.shape,
            'SHAPEWHY': self.reason[:40],
            'SHAPESRC': self.prism_product[:40],
            'PA_V3': float(self.pa_v3),
            'DETSNR': float(self.snr_det),
            'SRCCEN': p['centre'],
            'SRCCENPL': p['centre_prior'],
            'SRCDRIFT': p['drift'],
            'SRCWIDTH': p['width'],
            'SRCDWID': p['width_change'],
            'SRCPIVOT': p['pivot'],
            'SRCCHROM': bool(self.result.chromatic),
            'SRCCHI2': float(self.result.chi2_red),
            'BAROFF': float(np.median([e.bar_offset for e in exps])) if exps else np.nan,
            # the master-background scale term (master-background spectrum only; NaN, left out, otherwise)
            'MBKGSCL': float(getattr(self.result, 'mbkg_scale', np.nan)),
            'MBKGSCLE': float(getattr(self.result, 'mbkg_scale_err', np.nan)),
            'MBKGSCLC': float(getattr(self.result, 'mbkg_scale_capped', np.nan)),   # only when the cap applied
        }
        sl = exps[0].slit_loss if exps else None
        if sl is not None:
            c['SLOSSY'] = sl.y
            c['SLOSSX'] = sl.x
        return c


def _exp_pa(exps):
    v = [getattr(e, 'pa_v3', np.nan) for e in exps if e.use_data]
    v = [x for x in v if np.isfinite(x)]
    return float(np.median(v)) if v else np.nan


def _bit_names(bits):
    return ','.join(n for n, b in PF.BOUND_BITS.items() if bits & b) or 'none'


def _threshold(cfg, M, R):
    k = 4 if M.chromatic else 2
    return cfg.own_vs_prism_dchi2 + k * np.log(max(R.npix, 2))


def choose_shape(exps, cfg, is_prism, prism=None, clean=None, log=None, contam_ratio=None):
    """Run the shape chain for one product. ``prism``: the ``PrismShape`` of the same
    source (gratings), ``clean``: dict(overall, per_exposure[, clean_pred]) of the
    fraction of the main-shutter pixels left by contamination masking (None: no
    masking), ``contam_ratio``: callable(FitResult) -> other sources' predicted light
    over the target's fitted light on the main shutter (``profile_contam.contam_light_ratio``),
    kept on the result as ``dominance`` (the others' light is subtracted, so it decides nothing). The
    masked-fraction rule only sees the pixels the subtraction could not handle."""
    log = log or (lambda *a: None)
    srcy = PF.planned_position(exps, cfg.srcypos_tolerance)[1]
    pa = _exp_pa(exps)

    M = PF.ProfileModel(exps, chromatic=cfg.chromatic, priors=cfg.priors, bounds=cfg.bounds)
    snr, det = PF.detection_snr(M)                    # reported (DETSNR)
    with TM.step('chain_fixed'):
        cands = {'default': PF.fit_fixed(model=M, shape=cfg.default_shape())}
    if cfg.shape == 'fixed':
        R = cands['default']
        return ChainResult(result=R, shape='default', chain='fixed', reason='fixed', snr_det=snr,
                           detection=det, chi2={'default': R.chi2}, npix_common=R.npix, srcy_flag=srcy, pa_v3=pa,
                           candidates=cands)

    # PRISM-transferred candidate (gratings)
    transfer, xshape = 'none', None
    if not is_prism:
        if not cfg.prism_transfer:
            transfer = 'off'
        elif prism is None:
            transfer = 'no_prism'
        elif prism.shape != 'own':
            transfer = 'prism_not_own'
        elif np.isfinite(prism.pa_v3) and np.isfinite(pa) and abs(((prism.pa_v3 - pa) + 180) % 360 - 180) > cfg.pa_tolerance:
            transfer = 'pa_mismatch'
        else:
            xshape = PF.map_shape(prism.params, M.pivot)
            with TM.step('chain_fixed'):
                cands['prism'] = PF.fit_fixed(model=M, shape=xshape)
            transfer = 'used'

    # how much of the trace's light is other sources' (recorded only)
    dom = np.nan
    if clean is not None and contam_ratio is not None:
        dom = float(contam_ratio(cands['prism'] if 'prism' in cands else cands['default']))

    # own fit, at every S/N: where the data say little, its priors decide
    reason = None
    if clean is not None and (clean['overall'] < cfg.min_clean_fraction
                              or min(clean['per_exposure'] or [1.0]) < cfg.min_clean_fraction_exposure):
        reason = f"masked:{clean['overall']:.2f}"
    else:
        try:
            with TM.step('chain_own'):
                cands['own'] = PF.fit(exps, model=M)
        except Exception as e:                         # noqa: BLE001 - the chain falls back
            log(f'own fit failed ({type(e).__name__}: {e})')
            reason = 'fit_failed'

    # common pixels: kept where any candidate's clipping keeps them
    keep = np.zeros(M.npix, bool)
    for R in cands.values():
        keep |= R.use
    chi2 = {}
    for name, R in cands.items():
        p, eoff = PF._qvec(R)
        chi2[name] = M.chi2_at(p, eoff, keep)
    npix_common = int(round(float(np.sum(M.dof_weight()[keep]))))
    fallback = 'prism' if 'prism' in cands else 'default'

    shdis = 0
    if 'own' in cands and xshape is not None:
        o = cands['own'].params
        dc = (o['centre'] - o['centre_prior']) - xshape['centre_offset']
        ratio = o['width'] / xshape['width']
        lo, hi = cfg.disagree_width_ratio
        shdis = int(abs(dc) > cfg.disagree_centre or not (lo <= ratio <= hi))

    # a parameter on its bound is a flag, not a switch: the fit on its bound is kept (it
    # reported 0.95-1.10 of injected wide sources' flux, the default shape about 0.25;
    # user, 2026-11-10), and the bits and SHAPEWHY say so
    bits = cands['own'].bound_bits if 'own' in cands else 0
    if 'own' not in cands:
        choice = fallback
    elif srcy == 2:
        choice, reason = 'default', 'srcypos'
    elif 'prism' in cands:
        o = cands['own'].params
        dc = (o['centre'] - o['centre_prior']) - xshape['centre_offset']
        dlw = np.log(o['width'] / xshape['width'])
        differ = abs(dc) > cfg.agree_centre or abs(dlw) > cfg.agree_log_width
        better = chi2['prism'] - chi2['own'] > _threshold(cfg, M, cands['own'])
        if better and differ and snr >= cfg.own_vs_prism_min_gate:
            choice, reason = 'own', 'own_better'
        elif better and differ:
            choice, reason = 'prism', 'prism_kept:gate'
        else:
            choice, reason = 'prism', 'prism_kept' if differ else 'shapes_agree'
    else:
        choice, reason = 'own', 'own_fit'
    if choice == 'own' and bits:
        reason = f'{reason};bound:{_bit_names(bits)}'
    if choice == 'prism' and (reason == 'fit_failed' or reason.startswith('masked')):
        reason = f'{reason};prism'
    R = cands[choice]
    # a shape transferred from a sibling grating is labelled apart from one from PRISM
    shape = 'grating' if (choice == 'prism' and prism is not None and not prism.chromatic) else choice
    return ChainResult(result=R, shape=shape, chain='gated', reason=reason, snr_det=snr, detection=det,
                       chi2=chi2, npix_common=npix_common, bound_bits=bits, srcy_flag=srcy, shdis=shdis,
                       prism_product=prism.product if (prism is not None and transfer == 'used') else '',
                       transfer=transfer, pa_v3=pa, candidates=cands, dominance=dom)
