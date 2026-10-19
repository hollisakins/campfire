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

_GLX, _GLW = np.polynomial.legendre.leggauss(8)
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


def load_group(cal_files, bkg_overrides=None, science=None, group=0):
    """Exposures of one stage-2b background group, from the canonical files.

    The live SCI is stage 2b's nod difference with the path loss re-applied; this
    removes the applied correction again (the inverse of jwst's ``do_correction_mos``),
    which recovers the frame jwst's background step produced.

    science : set of file names that stage 3 combines (default: all with a partner list).
    """
    from jwst.datamodels import MultiSlitModel
    from campfire_pipeline.nirspec.stage2 import nod_background_partners

    models = [MultiSlitModel(f) for f in cal_files]
    slits = [m.slits[0] for m in models]
    y0, x0, shape = _common_frame(slits)
    partners = nod_background_partners(cal_files, bkg_overrides)
    exps = []
    for f, m, s, part in zip(cal_files, models, slits, partners):
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
            cutout=(s.ystart - y0, s.xstart - x0, ny, nx), path=str(f)))
    for m in models:
        m.close()
    return exps


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
    centre_sigma: float = 0.15        # centre vs the planned position (SRCYPOS), slit units
    drift_sigma: float = 0.05         # centre change per octave of wavelength
    width_median: float = 0.15        # galaxy Gaussian sigma, slit units (bright-galaxy population)
    width_log_sigma: float = 0.7      # natural-log scatter of the width prior
    width_change_sigma: float = 0.05  # width change per octave, slit units
    offset_sigma: float = 0.015       # per-exposure pointing offset (stars: median 0.01, 95 % < 0.02)


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


class ProfileModel:
    """Pixel table, spectrum grid and the linear model for a set of exposures."""

    def __init__(self, exps, chromatic=True, priors=None, grid=None):
        self.exps = exps
        self.chromatic = chromatic
        self.priors = priors or Priors()
        self.c_prior = float(np.mean([e.src_y for e in exps if e.use_data]))
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
        of exposure k's pixels selected by mask."""
        e = self.exps[k]
        out = [(k, e.y[mask], e.wav[mask], e.dy[mask], 1.0)]
        for j in e.partners:
            p = self.exps[j]
            out.append((j, p.y[mask], p.wav[mask], p.dy[mask], -1.0 / len(e.partners)))
        return out

    def _region(self, e):
        m = np.zeros(e.y.shape, bool)
        for lo, hi in open_intervals(e.open_k):
            m |= (e.y > lo - FIT_MARGIN) & (e.y < hi + FIT_MARGIN)
        return m

    def _build_pixels(self):
        rows = []
        for k, e in enumerate(self.exps):
            if not e.use_data:
                continue
            m = e.ok & self._region(e)
            for j in e.partners:      # every subtracted frame must cover the pixel
                p = self.exps[j]
                m &= np.isfinite(p.y) & np.isfinite(p.wav) & np.isfinite(p.pl_point) & np.isfinite(p.var) & (p.var > 0)
            rows.append((k, m))
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
            e = next(x for x in self.exps if x.use_data and x.detector == det)
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
        for k, m in self.pix_masks:
            for (j, y, lam, dy, w) in self._terms(k, m):
                idx = np.clip(np.searchsorted(g, lam) - 1, 0, g.size - 2)
                t = np.clip((lam - g[idx]) / (g[idx + 1] - g[idx]), 0.0, 1.0)
                inside = (lam >= g[0]) & (lam <= g[-1])
                self.terms.append(dict(k=k, j=j, y=y, lam=lam, dy=dy, w=w, idx=idx, t=t, inside=inside))
        # term rows map onto the pixel table in order
        offs, n = [], 0
        for k, m in self.pix_masks:
            offs.append((n, n + int(m.sum())))
            n += int(m.sum())
        it = iter(self.terms)
        for (a, b), (k, m) in zip(offs, self.pix_masks):
            for _ in range(1 + len(self.exps[k].partners)):
                next(it)['rows'] = np.arange(a, b)
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
        comps = [(1 - f, np.sqrt(a ** 2 + w ** 2), b), (f, np.sqrt((PSF_K * a) ** 2 + w ** 2), PSF_K * b)]
        acc = 0.0
        for xi, wi in zip(_GLX, _GLW):
            yy = y + 0.5 * dy * xi
            acc = acc + wi * sum(fr * _clipblur(yy, c, s1, s2, iv) for fr, s1, s2 in comps)
        return 0.5 * dy * acc

    def transmission(self, p, eoff, k, lam, dy, width=None, aperture=REF_APERTURE, centre=None):
        """Cross-slit transmission (pre-shutter clipping) through an aperture of
        shutters, for the fitted source (width=None) or a point source (width=0),
        at the fitted centre or at a given slit position."""
        c = self.centre(p, eoff, k, lam) if centre is None else centre
        w = self.width(p, lam) if width is None else width
        a, _, f = self._split(k, lam, dy)
        iv = open_intervals(aperture)
        return ((1 - f) * _transmitted(c, np.sqrt(a ** 2 + w ** 2), iv)
                + f * _transmitted(c, np.sqrt((PSF_K * a) ** 2 + w ** 2), iv))

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
        for T in self.terms:
            g = T['w'] * self.throughput(p, eoff, T['j'], T['lam'], T['dy']) * self.image(p, eoff, T['j'], T['y'], T['lam'], T['dy'])
            g = np.where(T['inside'], g, 0.0)
            rows += [T['rows'], T['rows']]
            cols += [T['idx'], T['idx'] + 1]
            vals += [g * (1 - T['t']), g * T['t']]
        return sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                             shape=(self.npix, self.lam_grid.size))

    def solve(self, p, eoff):
        """Weighted least squares for the spectrum at fixed shape."""
        A = self.design(p, eoff)
        wgt = np.where(self.use, 1.0 / self.pix['v'], 0.0)
        AtW = A.T.multiply(wgt)
        N = (AtW @ A).toarray()
        b = AtW @ self.pix['d']
        diag = np.diag(N).copy()
        good = diag > 1e-3 * np.median(diag[diag > 0])
        f = np.zeros(diag.size)
        cov = np.full((diag.size, diag.size), np.nan)
        Ng = N[np.ix_(good, good)] + np.eye(good.sum()) * 1e-9 * np.median(diag[good])
        try:
            L = np.linalg.cholesky(Ng)
            f[good] = np.linalg.solve(L.T, np.linalg.solve(L, b[good]))
            Li = np.linalg.solve(L, np.eye(good.sum()))
            cov[np.ix_(good, good)] = Li.T @ Li
        except np.linalg.LinAlgError:
            f[good] = np.linalg.lstsq(Ng, b[good], rcond=None)[0]
        return f, cov, good, A

    def residuals(self, q):
        p, eoff = self.unpack(q)
        f, _, _, A = self.solve(p, eoff)
        r = (self.pix['d'] - A @ f) / np.sqrt(self.pix['v'])
        r = np.where(self.use, r, 0.0)
        pr = self.priors
        pri = [(p[0] - self.c_prior) / pr.centre_sigma,
               (p[2] - np.log(pr.width_median)) / pr.width_log_sigma]
        if self.chromatic:
            pri += [p[1] / pr.drift_sigma, p[3] / pr.width_change_sigma]
        pri += list(eoff[1:] / pr.offset_sigma)
        return np.concatenate([r, pri])

    def bounds(self):
        c = self.c_prior
        lo = [c - 0.6, -0.5, np.log(W_FLOOR), -0.4] if self.chromatic else [c - 0.6, np.log(W_FLOOR)]
        hi = [c + 0.6, 0.5, np.log(1.5), 0.4] if self.chromatic else [c + 0.6, np.log(1.5)]
        return np.array(lo + [-0.3] * self.n_eoff), np.array(hi + [0.3] * self.n_eoff)

    def start(self):
        best = None
        for c0 in self.c_prior + np.arange(-0.3, 0.31, 0.1):
            for w in (0.03, 0.15, 0.4):
                q = ([c0, 0.0, np.log(w), 0.0] if self.chromatic else [c0, np.log(w)]) + [0.0] * self.n_eoff
                s = float(np.sum(self.residuals(np.array(q)) ** 2))
                if best is None or s < best[0]:
                    best = (s, np.array(q))
        return best[1]


def fit(exps, chromatic=True, priors=None, clip=5.0, n_clip=2, grid=None):
    """Fit the shape (with priors) and the shared spectrum of one source.

    Pixels deviating by more than ``clip`` sigma from the model are masked and the
    fit repeated, ``n_clip`` times.
    """
    M = ProfileModel(exps, chromatic=chromatic, priors=priors, grid=grid)
    lo, hi = M.bounds()
    q = np.clip(M.start(), lo + 1e-6, hi - 1e-6)
    for it in range(n_clip + 1):
        res = least_squares(M.residuals, q, bounds=(lo, hi), x_scale='jac')
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
    f, cov, good, A = M.solve(p, eoff)
    r = (M.pix['d'] - A @ f) / np.sqrt(M.pix['v'])
    chi2 = float(np.sum(r[M.use] ** 2))
    n_eff = float(np.sum(M.dof_weight()[M.use]))
    flux = np.where(good, f, np.nan)
    err = np.where(good, np.sqrt(np.abs(np.diag(cov))), np.nan)
    params = dict(centre=float(p[0]), drift=float(p[1]), width=float(np.exp(p[2])),
                  width_change=float(p[3]), pivot=M.pivot, centre_prior=M.c_prior)
    return FitResult(params=params, eoff=eoff, lam=M.lam_grid, flux=flux, flux_err=err, chi2=chi2,
                     npix=int(round(n_eff)), nfree=int(good.sum()) + len(q), chromatic=chromatic, model=M)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
def _qvec(result):
    M = result.model
    p = [result.params['centre'], result.params['drift'], np.log(result.params['width']), result.params['width_change']]
    return np.array(p), result.eoff


def model_frame(result, k, unit=False, applied_pathloss=True):
    """Model of exposure k's nod difference on its canonical cutout.

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
    terms = [(k, 1.0)] + [(j, -1.0 / len(e.partners)) for j in (e.partners or [])]
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
    return out[sl]


def main_shutter_factor(result, lam):
    """Reported-flux factor: the fitted source's transmission through the main
    shutter, divided by a point source's at the planned position (SRCYPOS).

    The true spectrum times this factor is the light in the main shutter on jwst's
    point-source path-loss calibration, which is what cfpipe's optimal extraction
    measures. For a point source at its planned position it equals the true spectrum."""
    M = result.model
    p, eoff = _qvec(result)
    sci = [i for i, e in enumerate(M.exps) if e.use_data]
    out = 0.0
    for k in sci:
        dy = np.full_like(lam, np.nanmedian(M.exps[k].dy[M.exps[k].ok]))
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
    from jwst import datamodels
    from jwst.datamodels import SourceModelContainer
    from jwst.resample import ResampleSpecStep

    by_name = {e.name: k for k, e in enumerate(result.model.exps)}
    with datamodels.open(crf_path) as m:
        for x in m.exposures:
            k = by_name[x.meta.filename]
            frame = model_frame(result, k, unit=unit)
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
