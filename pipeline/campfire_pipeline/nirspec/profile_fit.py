"""
Forward-model ("profile fit") extraction for nodded NIRSpec MSA spectra.

One true spectrum f(lambda) per source is fitted jointly to every nod difference
of the source. Each exposure k sees the source through its own throughput and its
own slitlet geometry:

    exposure k's nod difference at pixel p =
        tau_k(lam_k) f(lam_k) S_k(y_k)  -  mean over k's partners j of  tau_j(lam_j) f(lam_j) S_j(y_j)

where y_k / lam_k are exposure k's slit coordinate and wavelength at the pixel
(jwst WCS), and the partners are exactly the frames stage 2b subtracted
(``stage2.nod_background_partners``). The data are those differences with the
path-loss correction removed again, i.e. the frames jwst's background step
subtracted in ``stage2.run_stage2b_single_slitlet``.

S_k is the source's cross-dispersion image per unit total flux: a Gaussian galaxy
along the slit (centre and width, each optionally linear in log2 wavelength)
convolved with the star-measured pre-shutter PSF, clipped by the slitlet's open
shutters, blurred by the post-shutter PSF and integrated over each pixel.

Optionally the galaxy is two Gaussians with one centre, (1 - h) G(sigma) + h G(r sigma):
a compact core and a broader second component holding a fraction h of the light,
its width a fixed multiple r of the core's at every wavelength (``fit_two``). A
shape carries them as ``halo`` (h) and ``halo_ratio`` (r); without them (or h = 0)
every function below computes exactly the single Gaussian.

tau_k is the along-dispersion throughput: the point-source path loss jwst computed
for exposure k (``pathloss_point``, which covers a 3-shutter aperture centred on the
source, MOS1X3) divided by the model's own cross-slit transmission of a point source
through that aperture. Cross-slit losses are modelled for the actual source width;
the along-dispersion loss is the point-source one.

Shape parameters have Gaussian priors (width: log-normal on the bright-galaxy
population), so at low S/N the fit settles on a compact source at the planned
position and the extraction becomes a fixed-template optimal extraction.
"""

from dataclasses import dataclass, field

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import splu
from scipy.optimize import least_squares
from scipy.special import ndtr

from campfire_pipeline.nirspec.constants import MSA_SLIT_MARGIN, MSA_SLIT_PITCH

# ---------------------------------------------------------------------------
# Geometry and PSF
# ---------------------------------------------------------------------------
# Open half-height of a shutter in slit_frame units. Star profiles near shutter
# edges fit best with a hard clip at 0.50 (2026-10-17 calibration-field test,
# 154 PRISM star groups); jwst's bounding boxes use 0.55.
OPEN_HALF = 0.5
# jwst's MOS1X3 path-loss aperture: three shutters centred on the source shutter.
REF_APERTURE = (-1, 0, 1)
# Pixels used in the fit: inside an open shutter, or within this margin of one.
FIT_MARGIN = 0.2
DO_NOT_USE = 1

# Cross-dispersion PSF (PRISM): core sigma in native rows and wing fraction per
# detector, wing sigma = PSF_K * core. Lower quartile of per-star core widths from
# 135 calibration-field stars (programs 12497, 6642), joint wing fraction.
# NOTE: derived from nod differences built with the path-loss correction applied;
# to be re-derived through this module (bright stars, small effect expected).
PSF_LAM = np.array([0.85, 1.25, 1.75, 2.30, 2.95, 3.70, 4.70])
PSF_TABLES = {
    'NRS1': dict(S_rows=np.array([0.390, 0.400, 0.419, 0.427, 0.429, 0.479, 0.597]),
                 f=np.array([0.234, 0.190, 0.187, 0.250, 0.299, 0.232, 0.118])),
    'NRS2': dict(S_rows=np.array([0.333, 0.360, 0.393, 0.384, 0.399, 0.480, 0.621]),
                 f=np.array([0.325, 0.259, 0.249, 0.337, 0.402, 0.295, 0.134])),
}
PSF_K = 2.2

# Trace-centre law from the same stars: centre(lambda) = c + D(lambda) + alpha(lambda) * c,
# D per MSA quadrant and detector (falling back to the same quadrant on the other
# detector, then to the detector), alpha per detector.
CENTRE_LAW = {
    'NRS1': dict(D=np.array([+0.0104, +0.0051, +0.0001, -0.0064, -0.0166, -0.0256, -0.0383]),
                 alpha=np.array([+0.0106, +0.0048, +0.0007, -0.0133, -0.0398, -0.0743, -0.1066])),
    'NRS2': dict(D=np.array([+0.0109, +0.0060, +0.0015, -0.0088, -0.0178, -0.0305, -0.0457]),
                 alpha=np.array([+0.0092, +0.0017, +0.0021, -0.0076, -0.0141, -0.0319, -0.0691])),
    'Q1_NRS1': dict(D=np.array([+0.0060, +0.0018, +0.0001, -0.0053, -0.0104, -0.0130, -0.0204])),
    'Q1_NRS2': dict(D=np.array([+0.0037, +0.0018, +0.0018, -0.0036, -0.0072, -0.0135, -0.0204])),
    'Q2_NRS2': dict(D=np.array([+0.0140, +0.0073, +0.0014, -0.0108, -0.0219, -0.0392, -0.0573])),
    'Q3_NRS1': dict(D=np.array([+0.0071, +0.0032, +0.0001, -0.0036, -0.0085, -0.0149, -0.0225])),
    'Q4_NRS1': dict(D=np.array([+0.0149, +0.0075, +0.0002, -0.0087, -0.0220, -0.0339, -0.0484])),
}

_GLX, _GLW = np.polynomial.legendre.leggauss(4)   # pixel integral, < 1e-4 of the peak
W_FLOOR = 0.003


def open_shutters(shutter_state, slit_ymin, slit_ymax):
    """Open shutters of a slitlet, as offsets from the source shutter (+1 = next
    shutter towards +y in slit_frame).

    The extent comes from jwst's slit bounds (``slit_ymin`` / ``slit_ymax``, which sit
    MSA_SLIT_MARGIN beyond the outermost shutter centres); closed shutters inside it
    come from the shutter state string, read with jwst's convention that the
    character after 'x' is the shutter above (``jwst.pathloss.shutter_above_is_closed``).
    Stuck-closed shutters were removed from the metafile in stage 2a, so they already
    appear here as closed or outside the slit.
    """
    lo = int(round((slit_ymin + MSA_SLIT_MARGIN) / MSA_SLIT_PITCH))
    hi = int(round((slit_ymax - MSA_SLIT_MARGIN) / MSA_SLIT_PITCH))
    state = str(shutter_state or '')
    ix = state.find('x')
    closed = set()
    if ix >= 0:
        closed = {i - ix for i, ch in enumerate(state) if ch == '0'}
    return tuple(k for k in range(lo, hi + 1) if k not in closed)


def open_intervals(open_k):
    return [(k * MSA_SLIT_PITCH - OPEN_HALF, k * MSA_SLIT_PITCH + OPEN_HALF) for k in open_k]


def psf_params(lam, det):
    """Core sigma (native rows) and wing fraction at each wavelength."""
    tab = PSF_TABLES[det]
    return np.interp(lam, PSF_LAM, tab['S_rows']), np.interp(lam, PSF_LAM, tab['f'])


def centre_law(lam, det, quad=None):
    """(D, alpha) per wavelength for a detector and MSA quadrant."""
    other = 'NRS2' if det == 'NRS1' else 'NRS1'
    D = CENTRE_LAW[det]['D']
    for key in (f'Q{quad}_{det}', f'Q{quad}_{other}'):
        if key in CENTRE_LAW:
            D = CENTRE_LAW[key]['D']
            break
    return np.interp(lam, PSF_LAM, D), np.interp(lam, PSF_LAM, CENTRE_LAW[det]['alpha'])


def _transmitted(o, s, intervals):
    """Fraction of N(o, s) inside the intervals."""
    return sum(ndtr((hi - o) / s) - ndtr((lo - o) / s) for lo, hi in intervals)


def _clipblur(y, o, s1, s2, intervals):
    """Density at y of N(o, s1) clipped to the intervals, then blurred by N(0, s2).
    Integrates to the transmitted fraction."""
    S2 = s1 ** 2 + s2 ** 2
    S = np.sqrt(S2)
    m = (o * s2 ** 2 + y * s1 ** 2) / S2
    sm = s1 * s2 / S
    core = np.exp(-0.5 * (y - o) ** 2 / S2) / (np.sqrt(2 * np.pi) * S)
    clip = sum(ndtr((hi - m) / sm) - ndtr((lo - m) / sm) for lo, hi in intervals)
    return core * clip


def galaxy_components(p):
    """The galaxy's Gaussians as (fraction of the light, width / core width), or None
    for the single Gaussian. A shape vector of six entries (centre, drift, ln width,
    width change, h, r) with h > 0 is the two-Gaussian galaxy (``fit_two``)."""
    if len(p) > 4 and p[4] > 0:
        h, r = float(p[4]), float(p[5])
        return ((1.0 - h, 1.0), (h, r))
    return None


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@dataclass
class Exposure:
    """One exposure of a source on its background group's common detector frame.

    Arrays are (ny, nx) on the common frame; NaN where the exposure has no data.
    ``diff`` is the nod difference with the path-loss correction removed; ``var`` is
    this exposure's own frame variance (jwst's background subtraction does not
    propagate the subtracted frames' variance, ``jwst.background.subtract_images``;
    the model adds the partners' share, ``ProfileModel.diff_variance``). ``pl_applied`` is the correction stage 2b re-applied (point or
    uniform, per exposure), ``pl_point`` jwst's point-source path loss.

    ``has_source`` is False for a frame that does not contain the source (stage 2a's
    ``SRCFLUX = F``: jwst found the source outside the slitlet in that nod and wrote a
    placeholder slit frame centred on a shutter the source is not in). Such a frame
    still enters other nods' differences as a background partner, but it adds no
    source light to them: its terms in the model are zero.
    """
    name: str
    group: int
    detector: str
    grating: str
    quadrant: int
    diff: np.ndarray
    var: np.ndarray
    ok: np.ndarray
    wav: np.ndarray
    y: np.ndarray
    pl_applied: np.ndarray
    pl_point: np.ndarray
    src_y: float
    open_k: tuple
    partners: list = None          # indices into the source's exposure list; None = not subtracted
    use_data: bool = True          # fitted (a stage-3 science member)
    cutout: tuple = None           # (row0, col0, ny, nx) of the canonical cutout in the common frame
    path: str = ''
    origin: tuple = None           # (ystart, xstart) of the common frame on the detector, 1-based like jwst
    has_source: bool = True        # the source is in this frame (SRCFLUX != F)
    pa_v3: float = np.nan          # telescope roll [deg]

    @property
    def dy(self):
        """Pixel height in slit_frame units."""
        return np.abs(np.gradient(self.y, axis=0))


def _common_frame(slits):
    """Bounding box covering every cutout (jwst xstart/ystart are 1-based), as in
    ``stage2.pad_to_common_detector_region``."""
    y0 = min(s.ystart for s in slits)
    x0 = min(s.xstart for s in slits)
    y1 = max(s.ystart + s.ysize for s in slits)
    x1 = max(s.xstart + s.xsize for s in slits)
    return y0, x0, (y1 - y0, x1 - x0)


def _place(a, s, y0, x0, shape, fill=np.nan):
    out = np.full(shape, fill, dtype=float)
    r, c = s.ystart - y0, s.xstart - x0
    out[r:r + a.shape[0], c:c + a.shape[1]] = a
    return out


def _pa_v3(path):
    from astropy.io import fits
    try:
        v = fits.getheader(path, 1).get('PA_V3')
        return float(v) if v is not None else np.nan
    except (OSError, IndexError, ValueError, TypeError):
        return np.nan


def load_group(cal_files, bkg_overrides=None, science=None, group=0):
    """Exposures of one stage-2b background group, from the canonical files.

    The live SCI is stage 2b's nod difference with the path loss re-applied; this
    removes the applied correction again (the inverse of jwst's ``do_correction_mos``),
    which recovers the frame jwst's background step produced.

    science : set of file names that stage 3 combines (default: all with a partner list).
    """
    from astropy.io import fits
    from jwst.datamodels import MultiSlitModel
    from campfire_pipeline.nirspec.stage2 import nod_background_partners

    models = [MultiSlitModel(f) for f in cal_files]
    heads = [fits.getheader(f) for f in cal_files]
    srcflux = [str(h.get('SRCFLUX', 'T')).strip().upper() != 'F' for h in heads]
    slits = [m.slits[0] for m in models]
    y0, x0, shape = _common_frame(slits)
    partners = partners_from_headers(cal_files, heads)
    if partners is None:
        partners = nod_background_partners(cal_files, bkg_overrides)
    exps = []
    for f, m, s, part, has_src in zip(cal_files, models, slits, partners, srcflux):
        if part is not None and 'COMPLETE' not in str(m.meta.cal_step.bkg_subtract).upper():
            raise ValueError(f'profile fit: {f} is not background-subtracted (stage 2b has not run on it)')
        ny, nx = s.data.shape
        yy, xx = np.mgrid[:ny, :nx]
        _, ys, _ = s.meta.wcs.get_transform('detector', 'slit_frame')(xx, yy)
        pl_type = str(getattr(s, 'pathloss_correction_type', '') or '').upper()
        applied = 'COMPLETE' in str(m.meta.cal_step.pathloss).upper()
        if applied and pl_type == 'UNIFORM':
            pl = np.asarray(s.pathloss_uniform, float)
        elif applied:
            pl = np.asarray(s.pathloss_point, float)
        else:
            pl = np.ones((ny, nx))
        var = (np.asarray(s.var_poisson, float) + np.asarray(s.var_rnoise, float)
               + (np.asarray(s.var_flat, float) if s.var_flat is not None and np.size(s.var_flat) else 0.0))
        dq_bad = (np.asarray(s.dq) & DO_NOT_USE) > 0
        diff = np.asarray(s.data, float) * pl
        ok = np.isfinite(diff) & np.isfinite(var) & (var > 0) & ~dq_bad & np.isfinite(ys) & np.isfinite(s.wavelength) & np.isfinite(pl) & (pl > 0)
        name = str(f).split('/')[-1]
        exps.append(Exposure(
            name=name, group=group, detector=str(m.meta.instrument.detector).upper(),
            grating=f'{m.meta.instrument.grating}/{m.meta.instrument.filter}'.upper(),
            quadrant=int(s.quadrant) if s.quadrant is not None else None,
            diff=_place(diff, s, y0, x0, shape), var=_place(var * pl ** 2, s, y0, x0, shape),
            ok=_place(ok.astype(float), s, y0, x0, shape, fill=0.0) > 0,
            wav=_place(np.asarray(s.wavelength, float), s, y0, x0, shape), y=_place(ys, s, y0, x0, shape),
            pl_applied=_place(pl, s, y0, x0, shape), pl_point=_place(np.asarray(s.pathloss_point, float), s, y0, x0, shape),
            src_y=float(s.source_ypos), open_k=open_shutters(s.shutter_state, s.slit_ymin, s.slit_ymax),
            partners=part, use_data=(part is not None) and (science is None or name in science),
            cutout=(s.ystart - y0, s.xstart - x0, ny, nx), path=str(f), origin=(y0, x0),
            has_source=bool(has_src), pa_v3=_pa_v3(f)))
    for m in models:
        m.close()
    return exps


def partners_from_headers(cal_files, heads):
    """Partner lists as stage 2b recorded them (``BKGNODS``: the nods it subtracted, or
    'excluded'), so the model follows what was actually subtracted whatever override
    file (hand or automatic) chose them. None when any file of the group lacks the
    card (written before stage 2b recorded it): then the override file decides."""
    from campfire_pipeline.nirspec.stage2 import _nod_number
    nods = [_nod_number(f) for f in cal_files]
    out = []
    for h in heads:
        v = str(h.get('BKGNODS', '')).strip()
        if not v or v.upper() == 'N/A':
            return None
        if v == 'excluded':
            out.append(None)
            continue
        try:
            want = [int(x) for x in v.split(',')]
        except ValueError:
            return None
        if any(n not in nods for n in want):
            return None
        out.append([nods.index(n) for n in want])
    return out


def load_source(groups, bkg_overrides=None, science=None):
    """All exposures of a source: ``groups`` is a list of canonical-file lists, one per
    stage-2b background group, and ``bkg_overrides`` the matching list of that group's
    nod overrides (or None). Partner indices are made global."""
    out = []
    overrides = bkg_overrides if isinstance(bkg_overrides, (list, tuple)) else [bkg_overrides] * len(groups)
    for g, files in enumerate(groups):
        exps = load_group(files, bkg_overrides=overrides[g], science=science, group=g)
        base = len(out)
        for e in exps:
            if e.partners is not None:
                e.partners = [base + j for j in e.partners]
        out.extend(exps)
    if len({e.grating for e in out}) > 1:
        raise ValueError(f'profile fit: one disperser per fit, got {sorted({e.grating for e in out})}')
    return out


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
@dataclass
class Priors:
    """Gaussian priors of the shape. The defaults are the original (V0) values; the
    gated chain (``profile_chain``) passes the recalibrated ones from the config."""
    centre_sigma: float = 0.15        # centre vs the planned position (SRCYPOS), slit units
    drift_sigma: float = 0.05         # centre change per octave of wavelength
    width_median: float = 0.15        # galaxy Gaussian sigma, slit units (bright-galaxy population)
    width_log_sigma: float = 0.7      # natural-log scatter of the width prior
    width_change_sigma: float = 0.05  # width change per octave, slit units
    offset_sigma: float = 0.015       # per-exposure pointing offset (stars: median 0.01, 95 % < 0.02)


@dataclass
class Bounds:
    """Hard limits of the shape fit (half-ranges; width: upper limit). Defaults are
    the original (V0) values."""
    centre: float = 0.6               # centre within +- this of the planned position
    drift: float = 0.5                # per octave
    width_max: float = 1.5
    width_change: float = 0.4         # per octave
    offset: float = 0.3               # per-exposure pointing offset


# bit of each shape parameter in a bound-hit mask (header PFBOUND)
BOUND_BITS = dict(centre=1, width=2, drift=4, width_change=8, offset=16)


@dataclass
class FitResult:
    params: dict
    eoff: np.ndarray
    lam: np.ndarray                   # spectrum grid
    flux: np.ndarray                  # true spectrum (total flux, MJy), NaN where unconstrained
    flux_err: np.ndarray
    chi2: float
    npix: int                         # independent data values (fitted pixels x (N-1)/N)
    nfree: int
    chromatic: bool
    model: 'ProfileModel' = field(repr=False, default=None)

    @property
    def chi2_red(self):
        return self.chi2 / max(self.npix - self.nfree, 1)


def fit_pixel_masks(exps):
    """(exposure index, pixel mask) of the pixels a fit uses: usable data inside an open
    shutter (plus ``FIT_MARGIN``), covered by every subtracted frame."""
    rows = []
    for k, e in enumerate(exps):
        if not e.use_data:
            continue
        m = np.zeros(e.y.shape, bool)
        for lo, hi in open_intervals(e.open_k):
            m |= (e.y > lo - FIT_MARGIN) & (e.y < hi + FIT_MARGIN)
        m &= e.ok
        for j in e.partners:      # every subtracted frame must cover the pixel
            p = exps[j]
            m &= np.isfinite(p.y) & np.isfinite(p.wav) & np.isfinite(p.pl_point) & np.isfinite(p.var) & (p.var > 0)
        rows.append((k, m))
    return rows


def term_weights(exps, k):
    """(exposure, weight) of the terms of exposure k's nod difference: +1 for k's own
    frame and -1/n for each of its n subtracted partners, leaving out frames that do
    not contain the source (``Exposure.has_source``). Those were subtracted, so the
    others' weights are unchanged, but they put no source light on the difference."""
    e = exps[k]
    parts = list(e.partners or [])
    out = [(k, 1.0)] if e.has_source else []
    out += [(j, -1.0 / len(parts)) for j in parts if exps[j].has_source]
    return out


class ProfileModel:
    """Pixel table, spectrum grid and the linear model for a set of exposures."""

    def __init__(self, exps, chromatic=True, priors=None, grid=None, bounds=None, c_prior=None):
        self.exps = exps
        self.chromatic = chromatic
        self.priors = priors or Priors()
        self.bounds_cfg = bounds or Bounds()
        self.c_prior, self.srcy_flag = planned_position(exps)
        if c_prior is not None:
            self.c_prior = float(c_prior)
        self._build_pixels()
        self.lam_grid = self._grid() if grid is None else np.asarray(grid, float)
        lam = self.pix['lam']
        self.pivot = float(np.exp(np.median(np.log(lam))))
        self.lam_rng = (float(np.percentile(lam, 2)), float(np.percentile(lam, 98)))
        self._interp_tables()
        self.n_eoff = len(exps) - 1

    # -- pixels --------------------------------------------------------------
    def _terms(self, k, mask):
        """(exposure, y, lam, dy, sign/weight) for the source term and each partner term
        of exposure k's pixels selected by mask (frames without the source omitted,
        ``term_weights``)."""
        out = []
        for j, w in term_weights(self.exps, k):
            p = self.exps[j]
            out.append((j, p.y[mask], p.wav[mask], p.dy[mask], w))
        return out

    def _region(self, e):
        m = np.zeros(e.y.shape, bool)
        for lo, hi in open_intervals(e.open_k):
            m |= (e.y > lo - FIT_MARGIN) & (e.y < hi + FIT_MARGIN)
        return m

    def _build_pixels(self):
        rows = fit_pixel_masks(self.exps)
        if sum(int(m.sum()) for _, m in rows) == 0:
            raise ValueError('profile fit: no usable pixels')
        self.pix_masks = rows
        d, v, e_idx, lam = [], [], [], []
        for k, m in rows:
            e = self.exps[k]
            d.append(e.diff[m]); v.append(self.diff_variance(k)[m]); e_idx.append(np.full(m.sum(), k)); lam.append(e.wav[m])
        self.pix = dict(d=np.concatenate(d), v=np.concatenate(v), e=np.concatenate(e_idx), lam=np.concatenate(lam))
        self.npix = self.pix['d'].size
        self.use = np.ones(self.npix, bool)       # outlier mask

    def diff_variance(self, k):
        """Variance to weight exposure k's nod difference with.

        The N = n + 1 differences of a nod set share their frames' noise and carry
        only N - 1 independent values per pixel: D_i = N/(N-1) (F_i - mean F). For
        equal frame variances s2, treating them as independent with variance
        (N/(N-1))**2 * s2 gives exactly the frame-level chi2 (the sky profiled out),
        hence the correct spectrum and its errors; s2 is the mean frame variance
        of the set. Degrees of freedom are counted as (N-1)/N per pixel
        (``dof_weight``)."""
        e = self.exps[k]
        n = len(e.partners)
        s2 = (e.var + sum(self.exps[j].var for j in e.partners)) / (n + 1)
        return ((n + 1) / n) ** 2 * s2

    def dof_weight(self):
        """Independent values per fitted pixel, (N-1)/N for a set of N nods."""
        n = np.array([len(self.exps[k].partners) for k in self.pix['e']])
        return n / (n + 1.0)

    def _grid(self):
        """Spectrum grid: native sampling at the source row of the first fitted
        exposure on each detector, merged across detectors."""
        lams = []
        for det in sorted({e.detector for e in self.exps if e.use_data}):
            cand = [x for x in self.exps if x.use_data and x.detector == det]
            e = next((x for x in cand if x.has_source), cand[0])
            yy = np.where(np.isfinite(e.y), e.y, np.inf)
            r = np.argmin(np.abs(yy - e.src_y), axis=0)
            lam = e.wav[r, np.arange(e.wav.shape[1])]
            lams.append(lam[np.isfinite(lam)])
        g = np.unique(np.concatenate(lams))
        if g.size < 3:
            raise ValueError('profile fit: too few wavelength samples')
        step = np.median(np.diff(g))
        keep = [g[0]]
        for x in g[1:]:
            if x - keep[-1] > 0.5 * step:
                keep.append(x)
        return np.array(keep)

    def _interp_tables(self):
        """Precompute, per term, the spectrum-grid interpolation and the throughput.
        Everything that does not depend on the shape parameters."""
        self.terms = []
        g = self.lam_grid
        n = 0
        term_masks = []
        for k, m in self.pix_masks:
            rows = np.arange(n, n + int(m.sum()))      # term rows map onto the pixel table in order
            n += int(m.sum())
            for (j, y, lam, dy, w) in self._terms(k, m):
                idx = np.clip(np.searchsorted(g, lam) - 1, 0, g.size - 2)
                t = np.clip((lam - g[idx]) / (g[idx + 1] - g[idx]), 0.0, 1.0)
                inside = (lam >= g[0]) & (lam <= g[-1])
                self.terms.append(dict(k=k, j=j, y=y, lam=lam, dy=dy, w=w, idx=idx, t=t, inside=inside, rows=rows))
                term_masks.append(m)
        # each exposure's image is needed at the same detector pixels by every set it
        # appears in (as the source term or a partner), so it is evaluated once per
        # exposure on the union of those pixels and looked up per term
        union = {}
        for T, m in zip(self.terms, term_masks):
            union[T['j']] = union.get(T['j'], np.zeros(m.shape, bool)) | m
        self.image_pix = {}
        for j, U in union.items():
            e = self.exps[j]
            pos = np.full(U.shape, -1)
            pos[U] = np.arange(U.sum())
            self.image_pix[j] = dict(y=e.y[U], lam=e.wav[U], dy=e.dy[U], pos=pos)
        for T, mask in zip(self.terms, term_masks):
            T['upos'] = self.image_pix[T['j']]['pos'][mask]
        # along-dispersion throughput per exposure: jwst point-source path loss along
        # the trace, as a function of wavelength
        self.pl_curve = []
        for e in self.exps:
            yy = np.where(np.isfinite(e.y), e.y, np.inf)
            r = np.argmin(np.abs(yy - e.src_y), axis=0)
            cols = np.arange(e.wav.shape[1])
            lam, pl = e.wav[r, cols], e.pl_point[r, cols]
            ok = np.isfinite(lam) & np.isfinite(pl) & (pl > 0)
            o = np.argsort(lam[ok])
            self.pl_curve.append((lam[ok][o], pl[ok][o]))
        good = [c for c in self.pl_curve if c[0].size > 3]
        for i, c in enumerate(self.pl_curve):     # source off the shutter in this exposure: borrow
            if c[0].size <= 3 and good:
                self.pl_curve[i] = good[0]

    # -- shape ---------------------------------------------------------------
    def unpack(self, q):
        if self.chromatic:
            c0, dc, lnw, dw = q[:4]; eo = q[4:]
        else:
            c0, lnw = q[:2]; dc = dw = 0.0; eo = q[2:]
        return np.array([c0, dc, lnw, dw]), np.concatenate([[0.0], eo])

    def _x(self, lam):
        lo, hi = np.log2(self.lam_rng[0] / self.pivot), np.log2(self.lam_rng[1] / self.pivot)
        return np.clip(np.log2(lam / self.pivot), lo, hi)

    def centre(self, p, eoff, k, lam):
        e = self.exps[k]
        cg = p[0] + p[1] * self._x(lam)
        D, a = centre_law(lam, e.detector, e.quadrant)
        return cg + eoff[k] + D + a * cg

    def width(self, p, lam):
        return np.maximum(np.exp(p[2]) + p[3] * self._x(lam), W_FLOOR)

    def _split(self, k, lam, dy):
        S_rows, f = psf_params(lam, self.exps[k].detector)
        S = S_rows * dy
        a = S / np.sqrt(2.0)
        return a, a, f

    def image(self, p, eoff, k, y, lam, dy):
        """Exposure k's source image at slit positions y (pixel-integrated, per unit
        total flux, before the along-dispersion throughput)."""
        c = self.centre(p, eoff, k, lam)
        w = self.width(p, lam)
        a, b, f = self._split(k, lam, dy)
        iv = open_intervals(self.exps[k].open_k)
        gal = galaxy_components(p)
        if gal is None:
            comps = [(1 - f, np.sqrt(a ** 2 + w ** 2), b), (f, np.sqrt((PSF_K * a) ** 2 + w ** 2), PSF_K * b)]
        else:
            comps = []
            for ag, sg in gal:
                ws = sg * w
                comps += [(ag * (1 - f), np.sqrt(a ** 2 + ws ** 2), b),
                          (ag * f, np.sqrt((PSF_K * a) ** 2 + ws ** 2), PSF_K * b)]
        acc = 0.0
        for xi, wi in zip(_GLX, _GLW):
            yy = y + 0.5 * dy * xi
            acc = acc + wi * sum(fr * _clipblur(yy, c, s1, s2, iv) for fr, s1, s2 in comps)
        return 0.5 * dy * acc

    def transmission(self, p, eoff, k, lam, dy, width=None, aperture=REF_APERTURE, centre=None):
        """Cross-slit transmission (pre-shutter clipping) through an aperture of
        shutters, for the fitted source (width=None, both galaxy components when it
        has two) or a point source (width=0), at the fitted centre or at a given
        slit position."""
        c = self.centre(p, eoff, k, lam) if centre is None else centre
        w = self.width(p, lam) if width is None else width
        a, _, f = self._split(k, lam, dy)
        iv = open_intervals(aperture)
        gal = galaxy_components(p) if width is None else None
        if gal is None:
            return ((1 - f) * _transmitted(c, np.sqrt(a ** 2 + w ** 2), iv)
                    + f * _transmitted(c, np.sqrt((PSF_K * a) ** 2 + w ** 2), iv))
        out = 0.0
        for ag, sg in gal:
            ws = sg * w
            out = out + ag * ((1 - f) * _transmitted(c, np.sqrt(a ** 2 + ws ** 2), iv)
                              + f * _transmitted(c, np.sqrt((PSF_K * a) ** 2 + ws ** 2), iv))
        return out

    def throughput(self, p, eoff, k, lam, dy):
        """Along-dispersion throughput tau_k: jwst point-source path loss divided by
        the model's point-source cross-slit transmission through the MOS1X3 aperture,
        both at the position jwst used (the exposure's planned source position)."""
        L, P = self.pl_curve[k]
        pl = np.interp(lam, L, P)
        t = self.transmission(p, eoff, k, lam, dy, width=0.0, centre=self.exps[k].src_y)
        return pl / np.maximum(t, 1e-6)

    def design(self, p, eoff):
        """Sparse (npix x ngrid) matrix A with model = A @ f."""
        rows, cols, vals = [], [], []
        G = {j: self.throughput(p, eoff, j, q['lam'], q['dy']) * self.image(p, eoff, j, q['y'], q['lam'], q['dy'])
             for j, q in self.image_pix.items()}
        for T in self.terms:
            g = T['w'] * G[T['j']][T['upos']]
            g = np.where(T['inside'], g, 0.0)
            rows += [T['rows'], T['rows']]
            cols += [T['idx'], T['idx'] + 1]
            vals += [g * (1 - T['t']), g * T['t']]
        if not vals:
            return sp.csr_matrix((self.npix, self.lam_grid.size))
        return sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                             shape=(self.npix, self.lam_grid.size))

    def solve(self, p, eoff, covariance=False):
        """Weighted least squares for the spectrum at fixed shape. The normal matrix is
        banded (each pixel touches a few neighbouring grid points), so it is solved
        sparse; the dense covariance is formed only on request."""
        A = self.design(p, eoff)
        wgt = np.where(self.use, 1.0 / self.pix['v'], 0.0)
        AtW = A.T.multiply(wgt).tocsr()
        N = (AtW @ A).tocsc()
        b = AtW @ self.pix['d']
        diag = N.diagonal()
        good = diag > 1e-3 * np.median(diag[diag > 0])
        gi = np.flatnonzero(good)
        Ng = N[gi][:, gi] + sp.identity(gi.size, format='csc') * 1e-9 * np.median(diag[good])
        f = np.zeros(diag.size)
        f[good] = splu(Ng).solve(b[good])
        cov = None
        if covariance:
            cov = np.full((diag.size, diag.size), np.nan)
            cov[np.ix_(good, good)] = np.linalg.inv(Ng.toarray())
        return f, cov, good, A

    def residuals(self, q):
        p, eoff = self.unpack(q)
        f, _, _, A = self.solve(p, eoff)
        r = (self.pix['d'] - A @ f) / np.sqrt(self.pix['v'])
        r = np.where(self.use, r, 0.0)
        return np.concatenate([r, self.prior_terms(p, eoff)])

    def prior_terms(self, p, eoff):
        """Normalised prior residuals of the shape (centre, ln width; drift and width
        change when chromatic) and of the pointing offsets. The second galaxy
        component (``fit_two``) has none."""
        pr = self.priors
        pri = [(p[0] - self.c_prior) / pr.centre_sigma,
               (p[2] - np.log(pr.width_median)) / pr.width_log_sigma]
        if self.chromatic:
            pri += [p[1] / pr.drift_sigma, p[3] / pr.width_change_sigma]
        pri += list(eoff[1:] / pr.offset_sigma)
        return pri

    def bounds(self):
        c, B = self.c_prior, self.bounds_cfg
        if self.chromatic:
            lo = [c - B.centre, -B.drift, np.log(W_FLOOR), -B.width_change]
            hi = [c + B.centre, B.drift, np.log(B.width_max), B.width_change]
        else:
            lo, hi = [c - B.centre, np.log(W_FLOOR)], [c + B.centre, np.log(B.width_max)]
        return np.array(lo + [-B.offset] * self.n_eoff), np.array(hi + [B.offset] * self.n_eoff)

    def param_names(self):
        base = ['centre', 'drift', 'width', 'width_change'] if self.chromatic else ['centre', 'width']
        return base + ['offset'] * self.n_eoff

    def bound_hits(self, q, rtol=1e-3):
        """Bitmask (``BOUND_BITS``) of the parameters that ended on a bound. The lower
        width limit is the point-source floor, a legitimate answer, not a hit."""
        lo, hi = self.bounds()
        tol = rtol * (hi - lo)
        bits = 0
        for name, x, a, b, t in zip(self.param_names(), q, lo, hi, tol):
            at_lo, at_hi = x <= a + t, x >= b - t
            if name == 'width':
                at_lo = False
            if at_lo or at_hi:
                bits |= BOUND_BITS[name]
        return bits

    def start_grid(self):
        """Starting shapes: centres around the planned position, three widths."""
        B = self.bounds_cfg
        span = min(0.3, B.centre)
        for c0 in self.c_prior + np.arange(-span, span + 0.01, 0.1):
            for w in (0.03, 0.15, 0.4):
                if w <= B.width_max:
                    yield np.array([c0, 0.0, np.log(w), 0.0])

    def start(self):
        best = None
        for p in self.start_grid():
            q = (list(p) if self.chromatic else [p[0], p[2]]) + [0.0] * self.n_eoff
            s = float(np.sum(self.residuals(np.array(q)) ** 2))
            if best is None or s < best[0]:
                best = (s, np.array(q))
        return best[1]

    def chi2_at(self, p, eoff, use=None):
        """Data chi2 (no prior terms) at a fixed shape, spectrum re-solved, on the
        pixels ``use`` (default: the current outlier mask)."""
        saved = self.use
        if use is not None:
            self.use = use
        try:
            f, _, _, A = self.solve(p, eoff)
            r = (self.pix['d'] - A @ f) / np.sqrt(self.pix['v'])
            return float(np.sum(r[self.use] ** 2))
        finally:
            self.use = saved


def fit(exps, chromatic=True, priors=None, clip=5.0, n_clip=2, grid=None, bounds=None, model=None):
    """Fit the shape (with priors) and the shared spectrum of one source.

    Pixels deviating by more than ``clip`` sigma from the model are masked and the
    fit repeated, ``n_clip`` times. ``model``: reuse a ``ProfileModel`` of the same
    exposures (its outlier mask is reset). The result's ``bound_bits`` records any
    parameter that ended on a bound.
    """
    if model is not None:
        M = model
        M.use = np.ones(M.npix, bool)
    else:
        M = ProfileModel(exps, chromatic=chromatic, priors=priors, grid=grid, bounds=bounds)
    lo, hi = M.bounds()
    q = np.clip(M.start(), lo + 1e-6, hi - 1e-6)
    for it in range(n_clip + 1):
        # 1e-5: shape changes far below its uncertainty, at a fifth of the evaluations of the default 1e-8
        res = least_squares(M.residuals, q, bounds=(lo, hi), x_scale='jac', ftol=1e-5, xtol=1e-5, gtol=1e-5)
        q = res.x
        if it == n_clip:
            break
        p, eoff = M.unpack(q)
        f, _, _, A = M.solve(p, eoff)
        r = (M.pix['d'] - A @ f) / np.sqrt(M.pix['v'])
        new = np.abs(r) < clip
        if np.array_equal(new, M.use):
            break
        M.use = new
    p, eoff = M.unpack(q)
    f, cov, good, A = M.solve(p, eoff, covariance=True)
    r = (M.pix['d'] - A @ f) / np.sqrt(M.pix['v'])
    chi2 = float(np.sum(r[M.use] ** 2))
    n_eff = float(np.sum(M.dof_weight()[M.use]))
    flux = np.where(good, f, np.nan)
    err = np.where(good, np.sqrt(np.abs(np.diag(cov))), np.nan)
    params = dict(centre=float(p[0]), drift=float(p[1]), width=float(np.exp(p[2])),
                  width_change=float(p[3]), pivot=M.pivot, centre_prior=M.c_prior)
    R = FitResult(params=params, eoff=eoff, lam=M.lam_grid, flux=flux, flux_err=err, chi2=chi2,
                  npix=int(round(n_eff)), nfree=int(good.sum()) + len(q), chromatic=M.chromatic, model=M)
    R.bound_bits = M.bound_hits(q)
    R.use = M.use.copy()
    return R


PARTIAL_FREE = {'width': ('width',), 'centre': ('centre',)}


def fit_partial(exps=None, free=('width',), base=None, clip=5.0, n_clip=2, model=None, chromatic=True,
                priors=None, grid=None, bounds=None):
    """Own fit of some of the shape parameters, the others held at a fixed shape.

    free : names among 'centre', 'drift', 'width', 'width_change' that are fitted (with
        their priors); the per-exposure pointing offsets are always fitted.
    base : dict(centre_offset, drift, width, width_change) of the held parameters
        (``shape_vector``); default the default shape.
    For an intermediate detection S/N, where a full own fit follows the noise: the
    width alone (centre at the planned position) or the centre alone.
    """
    names = ['centre', 'drift', 'width', 'width_change']
    if model is not None:
        M = model
        M.use = np.ones(M.npix, bool)
    else:
        M = ProfileModel(exps, chromatic=chromatic, priors=priors, grid=grid, bounds=bounds)
    if not M.chromatic:
        raise ValueError('fit_partial needs the chromatic model (four shape parameters)')
    q0 = np.concatenate([shape_vector(base, M.c_prior, M.priors.width_median), np.zeros(M.n_eoff)])
    sel = [names.index(n) for n in free] + list(range(4, 4 + M.n_eoff))
    lo, hi = M.bounds()

    def full(x):
        q = q0.copy()
        q[sel] = x
        return q

    def res(x):
        return M.residuals(full(x))
    starts = [{}]
    if 'centre' in free:
        starts = [dict(centre=M.c_prior + d) for d in (-0.2, -0.1, 0.0, 0.1, 0.2)]
    if 'width' in free:
        starts = [dict(s, width=np.log(w)) for s in starts for w in (0.03, 0.15, 0.4)]
    best = None
    for s in starts:
        x = q0[sel].copy()
        for nm, v in s.items():
            x[sel.index(names.index(nm))] = v
        c = float(np.sum(res(x) ** 2))
        if best is None or c < best[0]:
            best = (c, x)
    x = np.clip(best[1], lo[sel] + 1e-6, hi[sel] - 1e-6)
    for it in range(n_clip + 1):
        # optimise x + 1: least_squares' xtol test is relative to |x|, which is ~0 for a centre
        # at a planned position of 0 with zero offsets (it would stop at the start)
        r_ = least_squares(lambda y: res(y - 1.0), x + 1.0, bounds=(lo[sel] + 1.0, hi[sel] + 1.0),
                           x_scale='jac', ftol=1e-5, xtol=1e-5, gtol=1e-5)
        x = r_.x - 1.0
        if it == n_clip:
            break
        p, eoff = M.unpack(full(x))
        f, _, _, A = M.solve(p, eoff)
        r = (M.pix['d'] - A @ f) / np.sqrt(M.pix['v'])
        new = np.abs(r) < clip
        if np.array_equal(new, M.use):
            break
        M.use = new
    q = full(x)
    p, eoff = M.unpack(q)
    f, cov, good, A = M.solve(p, eoff, covariance=True)
    r = (M.pix['d'] - A @ f) / np.sqrt(M.pix['v'])
    params = dict(centre=float(p[0]), drift=float(p[1]), width=float(np.exp(p[2])), width_change=float(p[3]),
                  pivot=M.pivot, centre_prior=M.c_prior)
    R = FitResult(params=params, eoff=eoff, lam=M.lam_grid, flux=np.where(good, f, np.nan),
                  flux_err=np.where(good, np.sqrt(np.abs(np.diag(cov))), np.nan), chi2=float(np.sum(r[M.use] ** 2)),
                  npix=int(round(float(np.sum(M.dof_weight()[M.use])))), nfree=int(good.sum()) + len(sel),
                  chromatic=True, model=M)
    R.bound_bits = M.bound_hits(q)
    R.use = M.use.copy()
    return R


def shape_vector(shape, c_prior, width_default):
    """Shape vector (centre, drift, ln width, width change) from a dict with
    ``centre_offset`` (from the planned position), ``drift``, ``width``, ``width_change``;
    with ``halo`` > 0 (and ``halo_ratio``) also h and r of the two-Gaussian galaxy."""
    q = dict(centre_offset=0.0, drift=0.0, width=width_default, width_change=0.0)
    q.update(shape or {})
    v = [c_prior + q['centre_offset'], q['drift'], np.log(max(q['width'], W_FLOOR)), q['width_change']]
    if q.get('halo', 0.0) > 0:
        v += [float(q['halo']), float(q['halo_ratio'])]
    return np.array(v)


def shape_params(p, M):
    """``FitResult.params`` of shape vector p on model M (with ``halo`` / ``halo_ratio``
    only for a two-Gaussian galaxy)."""
    d = dict(centre=float(p[0]), drift=float(p[1]), width=float(np.exp(p[2])), width_change=float(p[3]),
             pivot=M.pivot, centre_prior=M.c_prior)
    if galaxy_components(p) is not None:
        d.update(halo=float(p[4]), halo_ratio=float(p[5]))
    return d


def shape_dict(p, c_prior):
    """The ``shape_vector`` dict of shape vector p (centre as an offset from c_prior)."""
    d = dict(centre_offset=float(p[0] - c_prior), drift=float(p[1]), width=float(np.exp(p[2])),
             width_change=float(p[3]))
    if galaxy_components(p) is not None:
        d.update(halo=float(p[4]), halo_ratio=float(p[5]))
    return d


TWO_GAUSS_STARTS = ((0.3, 3.0), (0.15, 1.8))     # (h, second width / core width) to start from


def fit_two(R0, h_max=0.95, sigma_max=0.6, sigma_min=0.03, starts=TWO_GAUSS_STARTS, tol=1e-6):
    """The two-Gaussian galaxy fitted on top of a finished single-Gaussian fit R0.

    The galaxy becomes (1 - h) G(sigma) + h G(r sigma) with one centre: h is constant
    with wavelength, in [0, ``h_max``]; the second width is a fixed multiple r of the
    core width, so both follow the same width law with wavelength, and it is at most
    ``sigma_max`` slit units at the pivot (a broader component is nearly flat across
    the shutters and cancels in the nod differences like sky: uncapped, it took
    95-138 % more flux on two sources). Every other parameter (centre, drift, core
    width, width change, pointing offsets) is refitted with its prior, from R0's
    solution, on R0's pixels and outlier mask (no new clipping), so its chi2 compares
    with R0's directly. Starts: h and r from ``starts`` (second width clipped to
    [0.04, 0.9 ``sigma_max``]); the lower chi2 is kept.

    Returns a ``FitResult`` whose params carry ``halo`` (h) and ``halo_ratio`` (r) when
    h > 0. ``R0.model``'s outlier mask is left as it was."""
    M = R0.model
    if not M.chromatic:
        raise ValueError('fit_two needs the chromatic model (four shape parameters)')
    use = np.asarray(R0.use, bool).copy()
    saved = M.use
    p0, e0 = _qvec(R0)
    p0 = p0[:4]
    lo, hi = M.bounds()
    lo = np.concatenate([lo[:4], [0.0, np.log(sigma_min)], lo[4:]])
    hi = np.concatenate([hi[:4], [h_max, np.log(sigma_max)], hi[4:]])
    d, sd = M.pix['d'], np.sqrt(M.pix['v'])

    def vec(q):
        return np.concatenate([q[:4], [q[4], np.exp(q[5]) / np.exp(q[2])]]), np.concatenate([[0.0], q[6:]])

    def res(q):
        p, eoff = vec(q)
        f, _, _, A = M.solve(p, eoff)
        r = np.where(M.use, (d - A @ f) / sd, 0.0)
        return np.concatenate([r, M.prior_terms(p, eoff)])

    def chi2(q):
        p, eoff = vec(q)
        f, _, _, A = M.solve(p, eoff)
        return float(np.sum(((d - A @ f) / sd)[M.use] ** 2))
    best = None
    try:
        M.use = use
        for h0, k in starts:
            s0 = float(np.clip(k * np.exp(p0[2]), 0.04, 0.9 * sigma_max))
            q0 = np.clip(np.concatenate([p0, [h0, np.log(s0)], e0[1:]]), lo + 1e-6, hi - 1e-6)
            # optimise q + 1 (least_squares' xtol is relative to |q|; centres and offsets sit near 0)
            sol = least_squares(lambda x: res(x - 1.0), q0 + 1.0, bounds=(lo + 1.0, hi + 1.0), x_scale='jac',
                                ftol=tol, xtol=tol, gtol=tol)
            q = sol.x - 1.0
            c = chi2(q)
            if best is None or c < best[0]:
                best = (c, q)
        q = best[1]
        p, eoff = vec(q)
        f, cov, good, A = M.solve(p, eoff, covariance=True)
        r = (d - A @ f) / sd
        n_eff = float(np.sum(M.dof_weight()[M.use]))
        R = FitResult(params=shape_params(p, M), eoff=eoff, lam=M.lam_grid, flux=np.where(good, f, np.nan),
                      flux_err=np.where(good, np.sqrt(np.abs(np.diag(cov))), np.nan),
                      chi2=float(np.sum(r[M.use] ** 2)), npix=int(round(n_eff)), nfree=int(good.sum()) + q.size,
                      chromatic=True, model=M)
        R.bound_bits = M.bound_hits(np.concatenate([q[:4], q[6:]]))
        R.use = M.use.copy()
        R.halo_fit = dict(h=float(q[4]), sigma2=float(np.exp(q[5])), ratio=float(p[5]),
                          at_cap=bool(q[5] >= np.log(sigma_max) - 1e-3), at_hmax=bool(q[4] >= h_max - 1e-3))
        return R
    finally:
        M.use = saved


def two_gauss_test(R0, R2, dchi2_min=25.0, k=2.0):
    """Adopt the two-Gaussian fit R2 over the single Gaussian R0 (same pixels)?

    Yes when dchi2 / c > ``dchi2_min`` + k ln N_eff, with dchi2 = chi2(R0) - chi2(R2),
    c = max(chi2(R0) / N_eff, 1) (the data's excess scatter, so a source whose noise
    is underestimated needs a larger improvement) and N_eff the independent data
    values (``ProfileModel.dof_weight``). k = 2: the fit adds two parameters.
    Returns (adopt, dict(dchi2 = dchi2 / c, thr, c, nu))."""
    M = R0.model
    nu = float(np.sum(M.dof_weight()[np.asarray(R0.use, bool)]))
    c = max(R0.chi2 / max(nu, 1.0), 1.0)
    ds = (R0.chi2 - R2.chi2) / c
    thr = float(dchi2_min + k * np.log(max(nu, 2.0)))
    return bool(ds > thr), dict(dchi2=float(ds), thr=thr, c=float(c), nu=nu)


def reported_flux_change(R0, R2):
    """Integrated reported flux (true spectrum x ``main_shutter_factor``, summed over the
    spectrum points both fits constrain) of R2 over R0's, minus 1."""
    a = R0.flux * main_shutter_factor(R0, R0.lam)
    b = R2.flux * main_shutter_factor(R2, R2.lam)
    ok = np.isfinite(a) & np.isfinite(b)
    s = float(np.sum(a[ok]))
    return float(np.sum(b[ok]) / s - 1.0) if ok.any() and s != 0 else np.nan


def fit_fixed(exps=None, shape=None, eoff=None, fit_eoff=False, clip=5.0, n_clip=2, model=None,
              use=None, chromatic=True, priors=None, grid=None, bounds=None):
    """Spectrum at a fixed shape: a linear solve.

    shape : dict(centre_offset, drift, width, width_change) at this model's pivot; the
        default is the default shape (star PSF x the prior median width at the planned
        position, no chromatic terms).
    fit_eoff : also fit the per-exposure pointing offsets (with their prior), so a
        transferred shape is compared with the free fit on equal terms.
    use : a fixed pixel mask (no clipping); otherwise ``n_clip`` rounds of clipping at
        ``clip`` sigma.
    The returned chi2 is the data chi2 only (no prior terms)."""
    M = model if model is not None else ProfileModel(exps, chromatic=chromatic, priors=priors, grid=grid,
                                                     bounds=bounds)
    M.use = np.ones(M.npix, bool) if use is None else use.copy()
    p = shape_vector(shape, M.c_prior, M.priors.width_median)
    e = np.zeros(len(M.exps)) if eoff is None else np.asarray(eoff, float).copy()
    nit = 0 if use is not None else n_clip
    for it in range(nit + 1):
        if fit_eoff and M.n_eoff > 0:
            sig = M.priors.offset_sigma
            B = M.bounds_cfg.offset

            def res(x):
                ee = np.concatenate([[0.0], x])
                f, _, _, A = M.solve(p, ee)
                r = np.where(M.use, (M.pix['d'] - A @ f) / np.sqrt(M.pix['v']), 0.0)
                return np.concatenate([r, x / sig])
            sol = least_squares(res, np.clip(e[1:], -B + 1e-6, B - 1e-6), bounds=(-B, B), x_scale='jac',
                                ftol=1e-5, xtol=1e-5, gtol=1e-5)
            e = np.concatenate([[0.0], sol.x])
        if it == nit:
            break
        f, _, _, A = M.solve(p, e)
        r = (M.pix['d'] - A @ f) / np.sqrt(M.pix['v'])
        new = np.abs(r) < clip
        if np.array_equal(new, M.use):
            break
        M.use = new
    f, cov, good, A = M.solve(p, e, covariance=True)
    r = (M.pix['d'] - A @ f) / np.sqrt(M.pix['v'])
    chi2 = float(np.sum(r[M.use] ** 2))
    n_eff = float(np.sum(M.dof_weight()[M.use]))
    params = shape_params(p, M)
    R = FitResult(params=params, eoff=e, lam=M.lam_grid, flux=np.where(good, f, np.nan),
                  flux_err=np.where(good, np.sqrt(np.abs(np.diag(cov))), np.nan), chi2=chi2,
                  npix=int(round(n_eff)), nfree=int(good.sum()) + (M.n_eoff if fit_eoff else 0),
                  chromatic=True, model=M)
    R.bound_bits = 0
    R.use = M.use.copy()
    return R


def noise_scale(M, r):
    """Calibration of the stated errors from the normalised residuals ``r`` of a fit:
    the robust (MAD) variance over all fitted pixels, divided by the variance the
    nod bookkeeping expects ((N-1)/N, ``ProfileModel.dof_weight``). The source
    covers a minority of the fitted pixels, so a shape mismatch moves the median
    absolute residual little, unlike the reduced chi2."""
    rr = r[M.use]
    if rr.size < 10:
        return 1.0
    mad = 1.4826 * np.median(np.abs(rr - np.median(rr)))
    c = mad ** 2 / float(np.median(M.dof_weight()[M.use]))
    return float(c) if np.isfinite(c) and c > 0 else 1.0


def mean_square_scale(M, r, clip=5.0):
    """Calibration of the stated errors from the normalised residuals ``r`` of a fit:
    the mean square of the residuals within ``clip`` sigma, per independent value
    (``ProfileModel.dof_weight``). Unlike the MAD (``noise_scale``) it includes the
    noise's own tails: on blank sources the MAD reads 10-20 % below the mean square,
    and dividing a chi2 by it inflates the detection S/N of nothing."""
    u = np.isfinite(r) & (np.abs(r) < clip)
    if u.sum() < 10:
        return 1.0
    c = float(np.sum(r[u] ** 2) / np.sum(M.dof_weight()[u]))
    return c if np.isfinite(c) and c > 0 else 1.0


def detection_snr(M, clip=5.0, n_clip=2, scale='mean_square'):
    """Integrated detection S/N of a source, the gate of the shape chain.

    For each starting shape of the fit (``ProfileModel.start_grid``) the spectrum is
    solved and its chi2 improvement over no source, dchi2 = f . (A^T W d), is
    computed; the best is used, so compact or offset sources are not penalised by
    the default template. dchi2 is divided by a noise scale c measured from the
    residuals at that best shape, and the number of spectrum points is subtracted
    (its expectation for no source):  snr_det = sqrt(max(dchi2 / c - n, 0)).

    scale : 'mean_square' (default, ``mean_square_scale``: residuals within
        ``clip`` sigma) or 'mad' (``noise_scale``, the statistic before
        2026-10-23, which read 10-20 % low on blanks and put the null at the
        thresholds).

    Outlier pixels are clipped first at the default shape. Returns
    (snr_det, dict with dchi2, n, c, c_mad, c_ms, best shape vector)."""
    M.use = np.ones(M.npix, bool)
    p0 = shape_vector(None, M.c_prior, M.priors.width_median)
    e0 = np.zeros(len(M.exps))
    for _ in range(n_clip):
        f, _, _, A = M.solve(p0, e0)
        r = (M.pix['d'] - A @ f) / np.sqrt(M.pix['v'])
        new = np.abs(r) < clip
        if np.array_equal(new, M.use):
            break
        M.use = new
    best = None
    wgt = None
    for p in M.start_grid():
        f, _, good, A = M.solve(p, e0)
        wgt = np.where(M.use, 1.0 / M.pix['v'], 0.0)
        b = A.T @ (wgt * M.pix['d'])
        d = float(f[good] @ b[good])
        if best is None or d > best[0]:
            best = (d, p, int(good.sum()), A, f)
    d, p, n, A, f = best
    r = (M.pix['d'] - A @ f) / np.sqrt(M.pix['v'])
    c_mad = noise_scale(M, r)
    c_ms = mean_square_scale(M, r, clip=clip)
    if scale not in ('mean_square', 'mad'):
        raise ValueError(f"gate_noise must be 'mean_square' or 'mad', got {scale!r}")
    c = c_ms if scale == 'mean_square' else c_mad
    s2 = d / c - n
    return float(np.sqrt(max(s2, 0.0))), dict(dchi2=d, n=n, c=c, c_mad=c_mad, c_ms=c_ms, shape=p)


def planned_position(exps, tolerance=0.05):
    """Planned slit position of the source (prior centre) and a flag.

    The median SRCYPOS of the science exposures that contain the source. A value of
    exactly 0.000 next to nonzero values is a metafile placeholder and is ignored
    (flag 1); science values that still differ by more than ``tolerance`` give
    flag 2 (genuine disagreement)."""
    vals = [e.src_y for e in exps if e.use_data and e.has_source] or [e.src_y for e in exps if e.use_data] \
        or [e.src_y for e in exps]
    flag = 0
    nonzero = [v for v in vals if v != 0.0]
    if nonzero and len(nonzero) < len(vals):
        vals, flag = nonzero, 1
    if len(vals) > 1 and (max(vals) - min(vals)) > tolerance:
        flag = 2
    return float(np.median(vals)), flag


def map_shape(src_params, pivot_target):
    """A fitted shape (``FitResult.params``: absolute centre at its pivot) re-expressed
    at another pivot, the centre as an offset from that fit's planned position (the
    shape transferred from PRISM to a grating keeps its offset from plan)."""
    s = src_params
    dx = np.log2(pivot_target / s['pivot'])
    out = dict(centre_offset=s['centre'] - s['centre_prior'] + s['drift'] * dx, drift=s['drift'],
               width=max(s['width'] + s['width_change'] * dx, W_FLOOR * 2), width_change=s['width_change'])
    if s.get('halo', 0.0) > 0:          # two-Gaussian galaxy: h and the width ratio carry over unchanged
        out.update(halo=float(s['halo']), halo_ratio=float(s['halo_ratio']))
    return out


def frame_image(result, j, spectrum=None, common=True):
    """Exposure j's own frame: the source light alone (no partners subtracted), with
    the path loss removed (the units of ``Exposure.diff``), on the source's common
    frame (or exposure j's cutout). Zero where the frame does not contain the source,
    NaN off the slitlet. ``spectrum`` replaces the fitted true spectrum (on
    ``result.lam``), e.g. its errors."""
    M = result.model
    e = M.exps[j]
    p, eoff = _qvec(result)
    out = np.full(e.y.shape, np.nan)
    m = np.isfinite(e.y) & np.isfinite(e.wav)
    out[m] = 0.0
    if e.has_source and m.any():
        spec = np.nan_to_num(result.flux if spectrum is None else spectrum)
        lam = e.wav[m]
        s = np.interp(lam, result.lam, spec, left=0.0, right=0.0)
        out[m] = s * M.throughput(p, eoff, j, lam, e.dy[m]) * M.image(p, eoff, j, e.y[m], lam, e.dy[m])
    if common:
        return out
    r0, c0, ny, nx = e.cutout
    return out[r0:r0 + ny, c0:c0 + nx]


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
def _qvec(result):
    P = result.params
    p = [P['centre'], P['drift'], np.log(P['width']), P['width_change']]
    if P.get('halo', 0.0) > 0:
        p += [P['halo'], P['halo_ratio']]
    return np.array(p), result.eoff


def model_frame(result, k, unit=False, applied_pathloss=True, common=False):
    """Model of exposure k's nod difference on its canonical cutout (on the source's
    common frame with ``common``).

    unit : the profile per unit reported flux instead of the fitted model: the true
        spectrum is set to 1 / main_shutter_factor, so a profile-weighted fit of
        the data with this frame returns the reported flux directly.
    applied_pathloss : divide by the path loss stage 2b re-applied, so the frame is
        in the units of the live canonical SCI.
    """
    M = result.model
    e = M.exps[k]
    p, eoff = _qvec(result)
    r0, c0, ny, nx = e.cutout
    sl = (slice(r0, r0 + ny), slice(c0, c0 + nx))
    m = np.isfinite(e.y) & np.isfinite(e.wav)
    for j in e.partners or []:
        m &= np.isfinite(M.exps[j].y) & np.isfinite(M.exps[j].wav)
    out = np.full(e.y.shape, np.nan)
    terms = term_weights(M.exps, k)
    if unit:
        f = main_shutter_factor(result, result.lam)
        true_spec = np.where(f > 0, 1.0 / np.maximum(f, 1e-6), 0.0)
    else:
        true_spec = np.nan_to_num(result.flux)
    val = np.zeros(m.sum())
    for j, w in terms:
        x = M.exps[j]
        lam = x.wav[m]
        spec = np.interp(lam, result.lam, true_spec, left=0.0, right=0.0)
        val += w * spec * M.throughput(p, eoff, j, lam, x.dy[m]) * M.image(p, eoff, j, x.y[m], lam, x.dy[m])
    out[m] = val
    if applied_pathloss:
        out = out / e.pl_applied
    return out if common else out[sl]


def main_shutter_factor(result, lam):
    """Reported-flux factor: the fitted source's transmission through the main
    shutter, divided by a point source's at the planned position (SRCYPOS).

    The true spectrum times this factor is the light in the main shutter on jwst's
    point-source path-loss calibration, which is what cfpipe's optimal extraction
    measures. For a point source at its planned position it equals the true spectrum.

    An exposure without usable pixels (``ok`` empty, e.g. everything masked) has no
    row scale of its own; it is left out of the average (the others carry the
    factor), and only if every exposure is empty the row scale comes from all its
    pixels. Before, its NaN row scale made the whole factor NaN."""
    M = result.model
    p, eoff = _qvec(result)
    lam = np.asarray(lam, float)
    sci = ([i for i, e in enumerate(M.exps) if e.use_data and e.has_source]
           or [i for i, e in enumerate(M.exps) if e.use_data])

    def _row_scale(e, usable):
        dy = np.asarray(e.dy, float)
        sel = np.isfinite(dy) & (np.asarray(e.ok, bool) if usable else np.isfinite(dy))
        return float(np.median(dy[sel])) if sel.any() else np.nan
    dys = {k: _row_scale(M.exps[k], True) for k in sci}
    keep = [k for k in sci if np.isfinite(dys[k])]
    if not keep:
        dys = {k: _row_scale(M.exps[k], False) for k in sci}
        keep = [k for k in sci if np.isfinite(dys[k])]
    if not keep:
        return np.full(lam.shape, np.nan)
    sci = keep
    out = 0.0
    for k in sci:
        dy = np.full_like(lam, dys[k])
        tg = M.transmission(p, eoff, k, lam, dy, aperture=(0,))
        tp = M.transmission(p, eoff, k, lam, dy, width=0.0, aperture=(0,), centre=M.exps[k].src_y)
        out = out + tg / np.maximum(tp, 1e-6)
    return out / len(sci)


def drizzle_frames(result, crf_path, unit=False):
    """Drizzle model frames exactly as stage 3 drizzled the data.

    Spec3's outlier-flagged per-source file (``*_crf.fits``, a MultiExposureModel)
    carries each exposure's DQ and variances; replacing its data with the model and
    re-running jwst's ResampleSpecStep gives the model the data's weights and masks
    (re-drizzling the unmodified file reproduces the data s2d exactly).
    """
    by_name = {e.name: k for k, e in enumerate(result.model.exps)}
    return drizzle_arrays(lambda name: model_frame(result, by_name[name], unit=unit), crf_path)


def drizzle_arrays(frame_of, crf_path):
    """Drizzle arbitrary per-exposure cutout arrays (``frame_of(file name)``, in the
    live canonical units) through Spec3's crf, as the data were drizzled. A name
    for which ``frame_of`` returns None gets zeros."""
    from jwst import datamodels
    from jwst.datamodels import SourceModelContainer
    from jwst.resample import ResampleSpecStep

    with datamodels.open(crf_path) as m:
        for x in m.exposures:
            frame = frame_of(x.meta.filename)
            if frame is None:
                frame = np.zeros(x.data.shape)
            if frame.shape != x.data.shape:
                raise ValueError(f'profile fit: model frame {frame.shape} vs crf {x.data.shape} for {x.meta.filename}')
            x.data = np.nan_to_num(frame).astype(x.data.dtype)
        out = ResampleSpecStep.call(SourceModelContainer(m))
    return out


FIT_VERSION = '1'


def run_single_source(product_name, workspace_dir, groups, bkg_overrides, science,
                      crf_path, chromatic=True, priors=None):
    """Fit one source and write ``{product_name}_pf.fits``: the fitted shape (header)
    and the drizzled 2D profile per unit reported flux (PROF2D) on the stage-3 s2d
    grid. The profile is not normalised: weighting the s2d with it returns the
    reported flux (main-shutter light on the point-source path-loss calibration)."""
    import os
    from astropy.io import fits
    from campfire_pipeline.common.io import log

    exps = load_source(groups, bkg_overrides=bkg_overrides, science=science)
    R = fit(exps, chromatic=chromatic, priors=priors)
    p = R.params
    log(f"{product_name}: profile fit centre {p['centre']:+.3f} (prior {p['centre_prior']:+.3f}), "
        f"width {p['width']:.3f}, drift {p['drift']:+.3f}/oct, width change {p['width_change']:+.3f}/oct, "
        f"chi2/dof {R.chi2_red:.3f}")
    prof = drizzle_frames(R, crf_path, unit=True)

    hdr = fits.Header()
    hdr['PFVER'] = (FIT_VERSION, 'Profile-fit model version')
    hdr['PFCHROM'] = (bool(chromatic), 'Centre and width vary with wavelength')
    hdr['PFCEN'] = (p['centre'], 'Source centre at the pivot [slit units]')
    hdr['PFCENPR'] = (p['centre_prior'], 'Planned source position (prior centre)')
    hdr['PFDRIFT'] = (p['drift'], 'Centre change per octave [slit units]')
    hdr['PFWIDTH'] = (p['width'], 'Galaxy Gaussian sigma at the pivot [slit units]')
    hdr['PFDWIDTH'] = (p['width_change'], 'Width change per octave [slit units]')
    hdr['PFPIVOT'] = (p['pivot'], 'Pivot wavelength [um]')
    hdr['PFCHI2'] = (R.chi2_red, 'Reduced chi2 of the fit')
    hdr['PFNPIX'] = (R.npix, 'Pixels in the fit')
    hdr['PFNEXP'] = (len(exps), 'Exposures in the model (incl. subtracted partners)')
    for k, (e, o) in enumerate(zip(exps, R.eoff)):
        hdr[f'PFOFF{k}'] = (float(o), f'Pointing offset {e.name[:31]}')
    out = fits.HDUList([
        fits.PrimaryHDU(header=hdr),
        fits.ImageHDU(np.asarray(prof.data, float) / 1e12, name='PROF2D'),
    ])
    path = os.path.join(workspace_dir, f'{product_name}_pf.fits')
    out.writeto(path, overwrite=True)
    prof.close()
    return path
