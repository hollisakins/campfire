"""
Choice of the source shape for the profile-fit extraction (prototype, extraction v2).

The profile fit (``profile_fit``) can fit a source's cross-dispersion shape freely,
but at low S/N a free shape follows the noise, and in crowded masks it follows
other sources' light. This module decides, per product (one source, one disperser),
which shape builds the extraction profile:

1. **own**: the source's own fit with recalibrated priors and tighter bounds, used
   when the source is detected above a gate and the fit is sane (no parameter on a
   bound, no disagreement among the planned positions).
2. **prism**: for a grating, the shape fitted to the PRISM spectrum of the same
   source in the same observation, mapped to the grating's wavelengths by its
   chromatic terms (``profile_fit.map_shape``), keeping its offset from the planned
   position. Only when the PRISM product took its own shape and the two were taken
   at the same telescope roll.
3. **default**: the star-measured PSF times a Gaussian of the prior median width (or
   ``default_shape_width``) at the planned position, with the pointing offsets fitted
   (``default_fit_offsets``). Optionally, between ``gate_partial_snr_*`` and the gate,
   an own fit of the width (or centre) only (``profile_fit_partial``).

Every candidate that runs is compared on the same pixels (a pixel is kept when any
candidate's outlier clipping keeps it), with the spectrum re-solved and without
prior terms, so the choice can be redone offline from the header.

The gate is the integrated detection S/N (``profile_fit.detection_snr``), computed
before any candidate is fitted.

With ``profile_fit_two_gauss`` an own fit that is chosen is refitted with a
two-Gaussian galaxy (``profile_fit.fit_two``) and that fit replaces it only when the
data ask for it (``profile_fit.two_gauss_test``); otherwise the own fit is kept as it
is. Its h and width ratio (PFHALO, PFHRATIO) travel with the shape.
"""

from dataclasses import dataclass, field

import numpy as np

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import profile_timing as TM


SHAPES = ('gated', 'free', 'fixed')
PRISM_MODES = ('unless_better', 'always')


@dataclass
class ChainConfig:
    shape: str = 'gated'                 # gated | free (V0: old priors and bounds, no gate) | fixed (default shape)
    chromatic: bool = True
    prism_transfer: bool = True
    prism_mode: str = 'unless_better'    # unless_better | always
    gate_prism: float = 25.0             # own fit above these (round-3 realistic-mix injections,
    gate_grating: float = 70.0           # mean-square gate statistic; 2026-10-23)
    own_vs_prism_dchi2: float = 25.0     # plus k ln(N_eff), k = shape parameters
    own_vs_prism_min_gate: float = 100.0  # below: a grating keeps the PRISM shape (own fit not better there)
    priors: PF.Priors = field(default_factory=PF.Priors)
    bounds: PF.Bounds = field(default_factory=PF.Bounds)
    srcypos_tolerance: float = 0.05
    pa_tolerance: float = 1.0            # deg; PRISM shape transferred only at the same roll
    agree_centre: float = 0.08           # own and PRISM shapes "agree" within these (then PRISM is kept)
    agree_log_width: float = 0.3
    disagree_centre: float = 0.1         # PFSHDIS thresholds
    disagree_width_ratio: tuple = (0.6, 1.6)
    min_clean_fraction: float = 0.5      # contamination masking: below this no own fit
    min_clean_fraction_exposure: float = 0.3
    gate_noise: str = 'mean_square'      # noise scale of the gate statistic: mean_square | mad
    dominance: str = 'light'             # other sources dominate the trace -> no own fit: light | pixels | off
    dominance_max: float = 1.0           # light: their predicted light / the target's fitted light on the main shutter
    dominance_min_clean: float = 0.5     # pixels: main-shutter fraction below dominance_kappa x noise
    dominance_apply: str = 'mask_only'   # always | mask_only (only when contam_mode = mask) | flag (PFDOMIN only)
    contam_mode: str = 'hybrid'
    default_width: float = None          # width of the default shape (None: the prior median width)
    default_fit_offsets: bool = True     # the default shape also fits the per-exposure pointing offsets
    partial_fit: str = 'off'             # below the gate: off | width | centre (own fit of that parameter only)
    gate_partial_prism: float = 25.0     # ... from these gates up
    gate_partial_grating: float = 30.0
    two_gauss: bool = False              # own fits: try the two-Gaussian galaxy (profile_fit.fit_two)
    two_gauss_dchi2: float = 25.0        # adopted when dchi2 / c > this + k ln N_eff
    two_gauss_k: float = 2.0
    two_gauss_halo_max: float = 0.95     # bound of h
    two_gauss_sigma_max: float = 0.6     # bound of the second width at the pivot [slit units]
    two_gauss_flux_flag: float = 0.10    # PF2GFLAG when adopting it changes the reported flux by more

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
        mode = str(g('profile_fit_prism_mode', 'unless_better')).lower()
        if mode not in PRISM_MODES:
            raise ValueError(f'profile_fit_prism_mode must be one of {PRISM_MODES}, got {mode!r}')
        priors = PF.Priors(
            centre_sigma=float(g('prior_centre_sigma', 0.08)),
            drift_sigma=float(g('prior_drift_sigma', 0.03)),
            width_median=float(g('prior_width_median', 0.15)),
            width_log_sigma=float(g('prior_width_log_sigma', 0.6)),
            width_change_sigma=float(g('prior_width_change_sigma', 0.04)),
            offset_sigma=float(g('prior_offset_sigma', 0.015)))
        bounds = PF.Bounds(
            centre=float(g('bound_centre', 0.3)), drift=float(g('bound_drift', 0.3)),
            width_max=float(g('bound_width_max', 0.6)), width_change=float(g('bound_width_change', 0.3)),
            offset=float(g('bound_offset', 0.1)))
        dom = str(g('contam_dominance', 'light')).lower()
        if dom not in ('light', 'pixels', 'off'):
            raise ValueError(f"contam_dominance must be 'light', 'pixels' or 'off', got {dom!r}")
        dapply = str(g('contam_dominance_apply', 'mask_only')).lower()
        if dapply not in ('always', 'mask_only', 'flag'):
            raise ValueError(f"contam_dominance_apply must be 'always', 'mask_only' or 'flag', got {dapply!r}")
        partial = str(g('profile_fit_partial', 'off')).lower()
        if partial not in ('off',) + tuple(PF.PARTIAL_FREE):
            raise ValueError(f"profile_fit_partial must be 'off', 'width' or 'centre', got {partial!r}")
        dwidth = g('default_shape_width', None)
        return cls(shape=shape, chromatic=bool(g('profile_fit_chromatic', True)),
                   prism_transfer=bool(g('profile_fit_prism_transfer', True)), prism_mode=mode,
                   gate_prism=float(g('gate_snr_prism', 25.0)), gate_grating=float(g('gate_snr_grating', 70.0)),
                   own_vs_prism_dchi2=float(g('own_vs_prism_dchi2', 25.0)),
                   own_vs_prism_min_gate=float(g('own_vs_prism_min_gate', 100.0)),
                   agree_centre=float(g('own_vs_prism_centre', 0.08)),
                   agree_log_width=float(g('own_vs_prism_log_width', 0.3)), priors=priors, bounds=bounds,
                   srcypos_tolerance=float(g('srcypos_tolerance', 0.05)),
                   min_clean_fraction=float(g('contam_min_clean_fraction', 0.5)),
                   min_clean_fraction_exposure=float(g('contam_min_clean_fraction_exposure', 0.3)),
                   gate_noise=str(g('gate_noise', 'mean_square')).lower(), dominance=dom,
                   dominance_max=float(g('contam_dominance_max', 1.0)),
                   dominance_min_clean=float(g('contam_dominance_min_clean', 0.5)),
                   dominance_apply=dapply, contam_mode=str(g('contam_mode', 'hybrid')).lower(),
                   default_width=(None if dwidth is None else float(dwidth)),
                   default_fit_offsets=bool(g('default_fit_offsets', True)), partial_fit=partial,
                   gate_partial_prism=float(g('gate_partial_snr_prism', 25.0)),
                   gate_partial_grating=float(g('gate_partial_snr_grating', 30.0)),
                   two_gauss=bool(g('profile_fit_two_gauss', False)),
                   two_gauss_dchi2=float(g('two_gauss_dchi2', 25.0)),
                   two_gauss_k=float(g('two_gauss_dchi2_k', 2.0)),
                   two_gauss_halo_max=float(g('two_gauss_halo_max', 0.95)),
                   two_gauss_sigma_max=float(g('two_gauss_sigma_max', 0.6)),
                   two_gauss_flux_flag=float(g('two_gauss_flux_flag', 0.10)))

    def try_two_gauss(self, R0):
        """The two-Gaussian galaxy on top of own fit R0: (R2, adopt, stats) or None
        when the fit fails. ``stats``: dchi2 (/ c), thr, c, nu, h, ratio, sigma2,
        at_cap, dflux (reported flux of R2 over R0's, minus 1)."""
        try:
            R2 = PF.fit_two(R0, h_max=self.two_gauss_halo_max, sigma_max=self.two_gauss_sigma_max)
        except Exception:                               # noqa: BLE001 - the single Gaussian stays
            return None
        adopt, st = PF.two_gauss_test(R0, R2, self.two_gauss_dchi2, self.two_gauss_k)
        adopt = adopt and R2.params.get('halo', 0.0) > 0
        st.update(h=R2.halo_fit['h'], ratio=R2.halo_fit['ratio'], sigma2=R2.halo_fit['sigma2'],
                  at_cap=R2.halo_fit['at_cap'], dflux=PF.reported_flux_change(R0, R2),
                  x0=R0.chi2 / max(st['nu'], 1.0), x1=R2.chi2 / max(st['nu'], 1.0))
        return R2, adopt, st


@dataclass
class PrismShape:
    """What a grating needs from the PRISM product of the same source."""
    product: str
    shape: str                      # PFSHAPE of the PRISM product
    params: dict                    # centre, centre_prior, drift, width, width_change, pivot
    pa_v3: float = np.nan

    @classmethod
    def from_header(cls, product, h):
        if 'PFSHAPE' not in h:
            return None
        params = dict(centre=float(h['PFCEN']), centre_prior=float(h['PFCENPR']), drift=float(h['PFDRIFT']),
                      width=float(h['PFWIDTH']), width_change=float(h['PFDWIDTH']), pivot=float(h['PFPIVOT']))
        params.update(halo_from_header(h))
        return cls(product=product, shape=str(h['PFSHAPE']), params=params, pa_v3=float(h.get('PFPAV3', np.nan)))


def halo_from_header(h):
    """{halo, halo_ratio} of a two-Gaussian galaxy from a product header (PFHALO,
    PFHRATIO), or {} for the single Gaussian."""
    if float(h.get('PFHALO', 0.0)) > 0:
        return dict(halo=float(h['PFHALO']), halo_ratio=float(h['PFHRATIO']))
    return {}


def halo_cards(params):
    """PFHALO / PFHRATIO of a fitted shape with a two-Gaussian galaxy, else nothing."""
    if params.get('halo', 0.0) > 0:
        return {'PFHALO': (float(params['halo']), 'Second galaxy Gaussian: fraction of the light'),
                'PFHRATIO': (float(params['halo_ratio']), 'Its width / the core width (PFWIDTH)')}
    return {}


@dataclass
class ChainResult:
    result: object                  # FitResult of the chosen shape (its model carries the exposures)
    shape: str                      # own | prism | default
    chain: str                      # gated | free | fixed
    reason: str
    snr_det: float = np.nan
    gate: float = np.nan
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
    dominance: float = np.nan       # other-source light / own light (light rule) or clean fraction (pixels rule)
    two: dict = field(default_factory=dict)   # the two-Gaussian test of the own fit (empty: not tried)

    def header_cards(self):
        """Primary-header cards of the chain (written to ``_pf.fits``, copied to ``_spec.fits``)."""
        p = self.result.params
        c = {
            'PFVER': ('2', 'Profile-fit model version'),
            'PFSHAPE': (self.shape, 'Shape used: own / prism / default'),
            'PFCHAIN': (self.chain, 'Shape chain: gated / free / fixed'),
            'PFREASON': (self.reason[:68], 'Why this shape'),
            'PFGATE': (_f(self.snr_det), 'Detection S/N (gate statistic)'),
            'PFGATETH': (_f(self.gate), 'Gate threshold'),
            'PFGATEC': (_f(self.detection.get('c', np.nan)), 'Noise scale used by the gate'),
            'PFGATEM': (_f(self.detection.get('c_mad', np.nan)), 'Noise scale from the MAD (old gate)'),
            'PFDOMIN': (_f(self.dominance), 'Other-source dominance of the trace'),
            'PFCHI2D': (_f(self.chi2.get('default', np.nan)), 'chi2, default shape, common pixels'),
            'PFCHI2P': (_f(self.chi2.get('prism', np.nan)), 'chi2, PRISM shape, common pixels'),
            'PFCHI2O': (_f(self.chi2.get('own', np.nan)), 'chi2, own fit, common pixels'),
            'PFNPIXC': (int(self.npix_common), 'Independent pixels behind the chi2 values'),
            'PFBOUND': (int(self.bound_bits), 'Own fit on a bound: 1 cen 2 wid 4 drift 8 dwid 16 off'),
            'PFSRCY': (int(self.srcy_flag), 'SRCYPOS: 0 ok, 1 placeholder ignored, 2 disagree'),
            'PFSHDIS': (int(self.shdis), 'PRISM and own grating shapes disagree'),
            'PFPRISM': (self.prism_product[:68], 'PRISM product the shape came from'),
            'PFXFER': (self.transfer, 'PRISM transfer: used/none/no_prism/...'),
            'PFPAV3': (_f(self.pa_v3), 'PA_V3 of the science exposures [deg]'),
            'PFCEN': (p['centre'], 'Source centre at the pivot [slit units]'),
            'PFCENPR': (p['centre_prior'], 'Planned source position (prior centre)'),
            'PFDRIFT': (p['drift'], 'Centre change per octave [slit units]'),
            'PFWIDTH': (p['width'], 'Galaxy Gaussian sigma at the pivot [slit units]'),
            'PFDWIDTH': (p['width_change'], 'Width change per octave [slit units]'),
            'PFPIVOT': (p['pivot'], 'Pivot wavelength [um]'),
            'PFCHROM': (bool(self.result.chromatic), 'Centre and width vary with wavelength'),
            'PFCHI2': (float(self.result.chi2_red), 'Reduced chi2 of the chosen fit'),
            'PFNPIX': (int(self.result.npix), 'Pixels in the fit'),
        }
        c.update(halo_cards(p))
        t = self.two
        if t:
            c.update({
                'PF2G': (bool(t.get('adopt', False)), 'Two-Gaussian galaxy adopted'),
                'PF2GDX2': (_f(t.get('dchi2', np.nan)), 'Its chi2 gain / c over the single Gaussian'),
                'PF2GTHR': (_f(t.get('thr', np.nan)), 'Adopted above: dchi2 + k ln N_eff'),
                'PF2GC': (_f(t.get('c', np.nan)), 'c = max(chi2 / N_eff, 1), single Gaussian'),
                'PF2GX0': (_f(t.get('x0', np.nan)), 'chi2 / N_eff, single Gaussian'),
                'PF2GX1': (_f(t.get('x1', np.nan)), 'chi2 / N_eff, two Gaussians'),
                'PF2GH': (_f(t.get('h', np.nan)), 'Its second-Gaussian light fraction'),
                'PF2GR': (_f(t.get('ratio', np.nan)), 'Its second / core width'),
                'PF2GDF': (_f(t.get('dflux', np.nan)), 'Its reported flux / single Gaussian - 1'),
                'PF2GFLAG': (bool(t.get('flag', False)), 'Adopted and |PF2GDF| > PF2GFTH'),
                'PF2GFTH': (_f(t.get('flag_thr', np.nan)), 'Flux-change flag threshold'),
            })
        return c


def _f(x):
    x = float(x)
    return x if np.isfinite(x) else -999.0


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
    over the target's fitted light on the main shutter (``profile_contam.contam_light_ratio``).

    Other sources dominate the trace (``cfg.dominance``): 'light' when that ratio,
    at the fallback shape, exceeds ``dominance_max``; 'pixels' when less than
    ``dominance_min_clean`` of the main shutter has predicted light below the
    noise threshold. Then no own fit is made (reason ``dominated:<value>``). The
    masked-fraction rule stays: in hybrid mode it only sees the pixels the
    subtraction could not handle, which a smoothed pass 1 makes few."""
    log = log or (lambda *a: None)
    srcy = PF.planned_position(exps, cfg.srcypos_tolerance)[1]
    pa = _exp_pa(exps)

    if cfg.shape == 'free':
        M = PF.ProfileModel(exps, chromatic=cfg.chromatic)            # V0: original priors and bounds
        snr, det = PF.detection_snr(M, scale=cfg.gate_noise)
        R = PF.fit(exps, model=M)
        return ChainResult(result=R, shape='own', chain='free', reason='free', snr_det=snr, detection=det,
                           chi2={'own': R.chi2}, npix_common=R.npix, bound_bits=R.bound_bits, srcy_flag=srcy,
                           pa_v3=pa, candidates={'own': R})

    M = PF.ProfileModel(exps, chromatic=cfg.chromatic, priors=cfg.priors, bounds=cfg.bounds)
    snr, det = PF.detection_snr(M, scale=cfg.gate_noise)
    gate = cfg.gate_prism if is_prism else cfg.gate_grating
    with TM.step('chain_fixed'):
        cands = {'default': PF.fit_fixed(model=M, shape=cfg.default_shape(), fit_eoff=cfg.default_fit_offsets)}
    if cfg.shape == 'fixed':
        R = cands['default']
        return ChainResult(result=R, shape='default', chain='fixed', reason='fixed', snr_det=snr, gate=gate,
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
                cands['prism'] = PF.fit_fixed(model=M, shape=xshape, fit_eoff=True)
            transfer = 'used'

    # do other sources dominate the trace?
    dom, dominated = np.nan, False
    if clean is not None and cfg.dominance == 'light' and contam_ratio is not None:
        dom = float(contam_ratio(cands['prism'] if 'prism' in cands else cands['default']))
        dominated = dom > cfg.dominance_max
    elif clean is not None and cfg.dominance == 'pixels' and 'clean_pred' in clean:
        dom = float(clean['clean_pred'])
        dominated = dom < cfg.dominance_min_clean
    # applied only where the others' light is not subtracted (mask mode), or never (flag)
    if cfg.dominance_apply == 'flag' or (cfg.dominance_apply == 'mask_only' and cfg.contam_mode != 'mask'):
        dominated = False

    # own fit
    reason = None
    if clean is not None and (clean['overall'] < cfg.min_clean_fraction
                              or min(clean['per_exposure'] or [1.0]) < cfg.min_clean_fraction_exposure):
        reason = f"masked:{clean['overall']:.2f}"
    elif dominated:
        reason = f'dominated:{dom:.2f}'
    elif snr < gate:
        reason = 'below_gate'
        gate_part = cfg.gate_partial_prism if is_prism else cfg.gate_partial_grating
        if cfg.partial_fit != 'off' and 'prism' not in cands and snr >= gate_part:
            # intermediate S/N: fit only one shape parameter (the PRISM shape, where there is
            # one, is a better description than a partial own fit)
            try:
                with TM.step('chain_own'):
                    cands['own'] = PF.fit_partial(exps, free=PF.PARTIAL_FREE[cfg.partial_fit],
                                                  base=cfg.default_shape(), model=M)
                reason = f'gate_partial:{cfg.partial_fit}'
            except Exception as e:                     # noqa: BLE001 - the chain falls back
                log(f'partial own fit failed ({type(e).__name__}: {e})')
                reason = 'fit_failed'
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

    bits = cands['own'].bound_bits if 'own' in cands else 0
    if 'own' not in cands:
        choice = fallback
    elif srcy == 2:
        choice, reason = 'default', 'srcypos'
    elif bits:
        # the own fit ended on a bound: fall back only if the fallback describes the data
        # about as well; a real large offset keeps its own fit (flagged)
        R = cands['own']
        if chi2[fallback] - chi2['own'] > _threshold(cfg, M, R):
            choice, reason = 'own', f'bound_kept:{_bit_names(bits)}'
        else:
            choice, reason = fallback, f'bound:{_bit_names(bits)}'
    elif 'prism' in cands:
        if cfg.prism_mode == 'always':
            choice, reason = 'prism', 'prism_always'
        else:
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
        choice, reason = 'own', (reason if reason and reason.startswith('gate_partial') else 'gate_passed')
    if reason is None:
        reason = 'gate_passed'
    if choice == 'prism' and (reason in ('below_gate', 'fit_failed') or reason.startswith(('masked', 'dominated'))):
        reason = f'{reason};prism'
    R = cands[choice]
    shape = 'own' if choice == 'own' else choice
    two = {}
    if cfg.two_gauss and choice == 'own' and not reason.startswith('gate_partial'):
        # the two-Gaussian galaxy, on top of the finished own fit; kept only when the data ask for it
        with TM.step('chain_two'):
            out = cfg.try_two_gauss(R)
        if out is None:
            two = dict(adopt=False, why='fit_failed')
        else:
            R2, adopt, two = out
            two.update(adopt=adopt, flag_thr=cfg.two_gauss_flux_flag,
                       flag=bool(adopt and np.isfinite(two['dflux']) and abs(two['dflux']) > cfg.two_gauss_flux_flag))
            cands['own2'] = R2
            if adopt:
                R = R2
    return ChainResult(result=R, shape=shape, chain='gated', reason=reason, snr_det=snr, gate=gate, detection=det,
                       chi2=chi2, npix_common=npix_common, bound_bits=bits, srcy_flag=srcy, shdis=shdis,
                       prism_product=prism.product if (prism is not None and transfer == 'used') else '',
                       transfer=transfer, pa_v3=pa, candidates=cands, dominance=dom, two=two)
