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


# ---------------------------------------------------------------------------
# Named conditions under which a product keeps the 3-pixel boxcar
# ---------------------------------------------------------------------------
class ProfileFitFallback(ValueError):
    """A known condition under which the profile fit cannot run for a product. The
    product's best spectrum is then the 3-pixel boxcar, with ``reason`` in the header
    (``EXTRWHY``). Every other exception is a failure of the stage."""
    reason = 'fallback'


class NotBackgroundSubtracted(ProfileFitFallback):
    """Old-layout observation: the canonical files hold the unsubtracted frames."""
    reason = 'not_bkg_subtracted'


class NoUsablePixels(ProfileFitFallback):
    """No usable pixel in the target's open shutters."""
    reason = 'no_usable_pixels'


class TooFewWavelengths(ProfileFitFallback):
    """Too few wavelength samples on the source row for a spectrum grid."""
    reason = 'too_few_wavelengths'

# Cross-dispersion PSF (PRISM): core sigma in native rows and wing fraction per
# detector, wing sigma = PSF_K * core. Lower quartile of per-star core widths from
# 135 calibration-field stars (programs 12497, 6642), joint wing fraction.
# NOTE: derived from nod differences built with the path-loss correction applied;
# to be re-derived through this module (bright stars, small effect expected).
# Layout: ``psf_components`` reads this legacy two-component form and a general list of
# components (share, width, offset, before/after the shutter) per wavelength node; refits in
# both layouts come from ``psf_calibration``.
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


def psf_params(lam, det, tables=None):
    """Core sigma (native rows) and wing fraction at each wavelength.

    tables : replaces ``PSF_TABLES`` (same layout; an entry may carry its own
        wavelength nodes as ``lam``, default ``PSF_LAM``), e.g. a PSF being
        calibrated (``psf_calibration``)."""
    tab = (PSF_TABLES if tables is None else tables)[det]
    nodes = tab.get('lam', PSF_LAM)
    return np.interp(lam, nodes, tab['S_rows']), np.interp(lam, nodes, tab['f'])


def psf_components(lam, det, tables=None):
    """The PSF at each wavelength as a list of Gaussian components, each a dict of arrays
    (one value per wavelength):

    frac      share of the light;
    sigma     width [native rows];
    offset    displacement from the trace centre [native rows, towards slit +y];
    q         share of sigma^2 that acts before the shutter (clipped by it); the rest blurs
              the clipped light after it (0.5: the even split of the original model);
    pre       True: the offset acts before the shutter (the light enters it displaced and
              is clipped there); False: after it (displaced on the detector).

    A detector's table (``PSF_TABLES`` layout, or ``tables``) is either

    legacy  ``{'S_rows': [...], 'f': [...]}`` (optional ``'lam'``, default ``PSF_LAM``): a
            core of sigma S_rows and share 1 - f, a wing of sigma ``PSF_K`` x S_rows and
            share f, no offsets, q = 0.5 (the model as it was built); or
    general ``{'lam': [...], 'components': [{'sigma': [...], 'frac': [...], 'offset': [...],
            'q': 0.5, 'pre': True}, ...]}``: values per wavelength node; exactly one component
            omits 'frac' and takes the remainder; 'offset' defaults to 0, 'q' to 0.5, 'pre'
            to True; a scalar instead of a list holds at every wavelength.

    Values are linear in wavelength between the nodes and held at the end nodes beyond them
    (a general table should have nodes covering the whole range it is used over)."""
    tab = (PSF_TABLES if tables is None else tables)[det]
    lam = np.asarray(lam, float)
    if 'components' not in tab:
        S, f = psf_params(lam, det, tables)
        zero = np.zeros_like(lam)
        return [dict(frac=1 - f, sigma=S, offset=zero, q=0.5, pre=True),
                dict(frac=f, sigma=PSF_K * S, offset=zero, q=0.5, pre=True)]
    nodes = np.asarray(tab['lam'], float)

    def val(v, default):
        if v is None:
            return np.full(lam.shape, default, float)
        v = np.asarray(v, float)
        return np.full(lam.shape, float(v)) if v.ndim == 0 else np.interp(lam, nodes, v)
    out, rest = [], None
    for i, c in enumerate(tab['components']):
        d = dict(sigma=val(c['sigma'], 0.0), offset=val(c.get('offset'), 0.0), q=float(c.get('q', 0.5)),
                 pre=bool(c.get('pre', True)))
        if 'frac' in c:
            d['frac'] = val(c['frac'], 0.0)
        else:
            rest = i
        out.append(d)
    if rest is None:
        raise ValueError('a general PSF table needs one component without "frac" (the remainder)')
    out[rest]['frac'] = 1.0 - sum(c['frac'] for i, c in enumerate(out) if i != rest)
    return out


def centre_law(lam, det, quad=None, law=None):
    """(D, alpha) per wavelength for a detector and MSA quadrant.

    law : replaces ``CENTRE_LAW`` (same layout; an entry may carry its own wavelength
        nodes as ``lam``, default ``PSF_LAM``), e.g. ``NO_CENTRE_LAW`` to measure the trace
        centre itself (``psf_calibration``)."""
    law = CENTRE_LAW if law is None else law
    other = 'NRS2' if det == 'NRS1' else 'NRS1'
    entry = law[det]
    for key in (f'Q{quad}_{det}', f'Q{quad}_{other}'):
        if key in law:
            entry = law[key]
            break
    D = np.interp(lam, entry.get('lam', PSF_LAM), entry['D'])
    return D, np.interp(lam, law[det].get('lam', PSF_LAM), law[det]['alpha'])


# The centre law switched off: the fitted centre is where the light is.
NO_CENTRE_LAW = {det: dict(D=np.zeros(PSF_LAM.size), alpha=np.zeros(PSF_LAM.size)) for det in ('NRS1', 'NRS2')}

# Nod geometry: each exposure's image of the source sits at the source's planned position in
# that exposure (its own SRCYPOS, from the MSA planning metadata), not at one position shared by
# all nods. The planned positions of a source differ between nods by up to ~0.02 slit units
# (~0.08 detector rows), in proportion to the nod step and linearly with the slitlet's detector
# row, because the nod step projected on the MSA varies across the field. Measured on 465
# calibration-field stars, 5091 exposure offsets fitted freely (``psf_calibration``,
# 2026-11-02; programs 12497 obs 1-5 and 6642 obs 2, PRISM and six gratings): the planned
# shifts take the rms of the offsets from 0.0100 to 0.0043 slit units, as well as a fitted
# row or MSA-row law in sample and better for a held-out observation; nothing is left against
# detector column, row or wavelength. The scale measured / planned is 1.03 overall but varies
# per visit (PRISM 0.85-1.21; the gratings of 6642 obs 2, 0.5-0.88, with that visit's PRISM), so
# the planned shifts are used as they stand (scale 1).
NOD_OFFSET_LAW = dict(scale=1.0)


def nod_shifts(exps, law=None, c_ref=None):
    """Per exposure: the shift of its source image from the shared centre, slit units:
    ``law['scale']`` x (its SRCYPOS - the reference position ``c_ref``, default the planned
    position of ``planned_position``). Zero for an exposure whose SRCYPOS is a placeholder
    (exactly 0.000 next to nonzero values) or that does not contain the source.
    law : None for ``NOD_OFFSET_LAW``, False for no shifts."""
    exps = list(exps)
    if law is False or not exps:
        return np.zeros(len(exps))
    law = NOD_OFFSET_LAW if law is None else law
    c = planned_position(exps)[0] if c_ref is None else float(c_ref)
    vals = [e.src_y for e in exps if e.has_source]
    placeholder = any(v != 0.0 for v in vals)
    out = np.zeros(len(exps))
    for k, e in enumerate(exps):
        if not e.has_source or not np.isfinite(e.src_y) or (placeholder and e.src_y == 0.0):
            continue
        out[k] = float(law['scale']) * (e.src_y - c)
    return out


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
    this exposure's own frame variance (``own_variance``, from before stage 2b, which
    adds the subtracted frames' noise to the live arrays; the model adds the partners'
    share itself, ``ProfileModel.diff_variance``). ``pl_applied`` is the correction stage 2b re-applied (point or
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


def own_variance(path, slit):
    """The frame's own Poisson + read-noise variance, before stage 2b: the canonical's
    ``PRE_BKGSUB_VAR_POISSON`` / ``_VAR_RNOISE`` when present (stage 2b adds the
    subtracted frames' noise to the live arrays, ``stage2.background_variance``; the
    model adds them itself, ``ProfileModel.diff_variance``), else the live arrays."""
    from campfire_pipeline.nirspec import canonical as C
    pre = C.read_slit_arrays(path, exts=('VAR_POISSON', 'VAR_RNOISE'), prefix=C.PRE_BKGSUB_PREFIX)
    vp = pre.get('VAR_POISSON', np.asarray(slit.var_poisson, float))
    vr = pre.get('VAR_RNOISE', np.asarray(slit.var_rnoise, float))
    if np.shape(vp) != np.shape(slit.data) or np.shape(vr) != np.shape(slit.data):
        vp, vr = np.asarray(slit.var_poisson, float), np.asarray(slit.var_rnoise, float)
    return np.asarray(vp, float) + np.asarray(vr, float)


def _applied_pathloss(m, s):
    """The path-loss correction applied to slit ``s`` of model ``m`` (point or uniform, as
    jwst's ``do_correction_mos`` chose it), or ones when none was applied."""
    pl_type = str(getattr(s, 'pathloss_correction_type', '') or '').upper()
    applied = 'COMPLETE' in str(m.meta.cal_step.pathloss).upper()
    if applied and pl_type == 'UNIFORM':
        return np.asarray(s.pathloss_uniform, float)
    if applied:
        return np.asarray(s.pathloss_point, float)
    return np.ones(s.data.shape)


def rebuild_nod_differences(frames, dqs, partners):
    """Stage 2b's nod differences, rebuilt in memory from the frames before subtraction.

    ``frames`` / ``dqs``: per exposure its frame with the path loss removed and its DQ, on
    the group's common frame, 0 outside its cutout (stage 2b pads to the common region with
    zeros, ``stage2.pad_to_common_detector_region``); ``partners`` as
    ``stage2.nod_background_partners`` gives them. Per exposure (diff, dq): the frame minus
    the mean of its partners' values, NaN left out and 0 where no partner has one (jwst's
    ``average_background``: its 3-sigma clip removes nothing from at most four values),
    DQ the OR of all; an excluded exposure (None) keeps its own frame and DQ."""
    out = []
    for i, part in enumerate(partners):
        if part is None:
            out.append((frames[i], dqs[i]))
            continue
        stack = np.array([frames[j] for j in part], float)
        n = np.sum(np.isfinite(stack), axis=0)
        with np.errstate(invalid='ignore', divide='ignore'):
            mean = np.where(n > 0, np.nansum(stack, axis=0) / np.maximum(n, 1), 0.0)
        dq = np.asarray(dqs[i]).copy()
        for j in part:
            dq = dq | np.asarray(dqs[j])
        out.append((frames[i] - mean, dq))
    return out


def _pre_bkgsub_differences(cal_files, heads, models, slits, partners, y0, x0, shape):
    """Each exposure's nod difference (path loss removed) and DQ on its own cutout, rebuilt
    from the canonicals' frames before subtraction with ``partners``
    (``rebuild_nod_differences``): ``PRE_BKGSUB_SCI`` / ``_DQ``, or the live arrays of a
    canonical stage 2b left calibrated (``CFP_BKG`` excluded or skipped). None when a file
    of the group has neither."""
    from campfire_pipeline.nirspec import canonical as C
    pre = []
    for f, h, s in zip(cal_files, heads, slits):
        p = C.read_slit_arrays(f, exts=('SCI', 'DQ'), prefix=C.PRE_BKGSUB_PREFIX)
        if 'SCI' not in p and str(h.get('CFP_BKG', '')).startswith(('excluded', 'skipped')):
            p = {'SCI': np.asarray(s.data), 'DQ': np.asarray(s.dq)}
        pre.append(p)
    if any('SCI' not in p or np.shape(p['SCI']) != s.data.shape for p, s in zip(pre, slits)):
        return None
    frames = [_place(np.asarray(p['SCI'], float) * _applied_pathloss(m, s), s, y0, x0, shape, fill=0.0)
              for p, m, s in zip(pre, models, slits)]
    dqs = [_place(np.asarray(p.get('DQ', np.zeros(s.data.shape)), float), s, y0, x0, shape, fill=0.0)
           .astype(np.uint32) for p, s in zip(pre, slits)]
    out = []
    for (d, q), s in zip(rebuild_nod_differences(frames, dqs, partners), slits):
        r, c = s.ystart - y0, s.xstart - x0
        ny, nx = s.data.shape
        out.append((d[r:r + ny, c:c + nx], q[r:r + ny, c:c + nx]))
    return out


def load_group(cal_files, bkg_overrides=None, science=None, group=0, single_frames=False, pre_bkgsub=False):
    """Exposures of one stage-2b background group, from the canonical files.

    The live SCI is stage 2b's nod difference with the path loss re-applied; this
    removes the applied correction again (the inverse of jwst's ``do_correction_mos``),
    which recovers the frame jwst's background step produced.

    science : set of file names that stage 3 combines (default: all with a partner list).
    single_frames : the files are single frames with their background removed some other
        way (e.g. master-background-subtracted copies; ``mbkg_extraction`` builds its
        frames in memory instead, ``single_frame_exposures``): no exposure is
        subtracted from another (``partners == []``).
    pre_bkgsub : the nod differences of ``bkg_overrides`` (the override files' partners,
        never the canonicals' ``BKGNODS``), rebuilt from the frames before subtraction
        (``_pre_bkgsub_differences``), so they do not depend on which partners stage 2b
        last subtracted (the joint first pass whose brightness decides the same-slitlet
        overrides, ``stage3.run_profile_fit_phase``). A group with a canonical that holds
        no frame before subtraction is read as without it.
    """
    from astropy.io import fits
    from jwst.datamodels import MultiSlitModel
    from campfire_pipeline.nirspec.stage2 import nod_background_partners

    models = [MultiSlitModel(f) for f in cal_files]
    heads = [fits.getheader(f) for f in cal_files]
    srcflux = [str(h.get('SRCFLUX', 'T')).strip().upper() != 'F' for h in heads]
    slits = [m.slits[0] for m in models]
    y0, x0, shape = _common_frame(slits)
    rebuilt = None
    if single_frames:
        partners = [[] for _ in cal_files]
    else:
        partners = None
        if pre_bkgsub:
            partners = nod_background_partners(cal_files, bkg_overrides)
            rebuilt = _pre_bkgsub_differences(cal_files, heads, models, slits, partners, y0, x0, shape)
            if rebuilt is None:
                partners = None
        if partners is None:
            partners = partners_from_headers(cal_files, heads)
        if partners is None:
            partners = nod_background_partners(cal_files, bkg_overrides)
    exps = []
    for k, (f, m, s, part, has_src) in enumerate(zip(cal_files, models, slits, partners, srcflux)):
        if part is not None and 'COMPLETE' not in str(m.meta.cal_step.bkg_subtract).upper():
            raise NotBackgroundSubtracted(f'profile fit: {f} is not background-subtracted (stage 2b has not run on it)')
        ny, nx = s.data.shape
        yy, xx = np.mgrid[:ny, :nx]
        _, ys, _ = s.meta.wcs.get_transform('detector', 'slit_frame')(xx, yy)
        pl = _applied_pathloss(m, s)
        var = (own_variance(f, s)
               + (np.asarray(s.var_flat, float) if s.var_flat is not None and np.size(s.var_flat) else 0.0))
        if rebuilt is not None:
            diff, dq = rebuilt[k]
        else:
            diff, dq = np.asarray(s.data, float) * pl, s.dq
        dq_bad = (np.asarray(dq) & DO_NOT_USE) > 0
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


def load_source(groups, bkg_overrides=None, science=None, single_frames=False, pre_bkgsub=False):
    """All exposures of a source: ``groups`` is a list of canonical-file lists, one per
    stage-2b background group, and ``bkg_overrides`` the matching list of that group's
    nod overrides (or None). Partner indices are made global. ``single_frames`` and
    ``pre_bkgsub``: see ``load_group``."""
    out = []
    overrides = bkg_overrides if isinstance(bkg_overrides, (list, tuple)) else [bkg_overrides] * len(groups)
    for g, files in enumerate(groups):
        exps = load_group(files, bkg_overrides=overrides[g], science=science, group=g, single_frames=single_frames,
                          pre_bkgsub=pre_bkgsub)
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


@dataclass
class Bounds:
    """Hard limits of the shape fit (half-ranges; width: upper limit). Defaults are
    the original (V0) values."""
    centre: float = 0.6               # centre within +- this of the planned position
    drift: float = 0.5                # per octave
    width_max: float = 1.5
    width_change: float = 0.4         # per octave


# bit of each shape parameter in a bound-hit mask (header PFBOUND; 16 was the per-exposure
# pointing offsets, no longer fitted)
BOUND_BITS = dict(centre=1, width=2, drift=4, width_change=8)


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
    """Pixel table, spectrum grid and the linear model for a set of exposures.

    psf_tables / centre_law_tables : replace ``PSF_TABLES`` / ``CENTRE_LAW`` for this
        model (``psf_params`` / ``centre_law``); None uses the module's tables. For
        calibrating them on stars (``psf_calibration``), not for extraction.
    nod_law : the nod geometry (``nod_shifts``): None for ``NOD_OFFSET_LAW``, False for one
        position shared by every exposure (to measure the offsets themselves)."""

    psf_tables = None
    centre_law_tables = None
    nod_shift = None

    def __init__(self, exps, chromatic=True, priors=None, grid=None, bounds=None, c_prior=None,
                 psf_tables=None, centre_law_tables=None, nod_law=None):
        self.exps = exps
        self.chromatic = chromatic
        self.psf_tables = psf_tables
        self.centre_law_tables = centre_law_tables
        self.nod_shift = nod_shifts(exps, nod_law)
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
            raise NoUsablePixels('profile fit: no usable pixels')
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
        (``dof_weight``).

        An exposure with no partners (an empty list: a single frame with the background
        subtracted some other way, e.g. the master background) is its own frame: its
        variance as given, one independent value per pixel."""
        e = self.exps[k]
        n = len(e.partners)
        if n == 0:
            return e.var
        s2 = (e.var + sum(self.exps[j].var for j in e.partners)) / (n + 1)
        return ((n + 1) / n) ** 2 * s2

    def dof_weight(self):
        """Independent values per fitted pixel, (N-1)/N for a set of N nods, 1 for a
        frame with no partners."""
        n = np.array([len(self.exps[k].partners) for k in self.pix['e']])
        return np.where(n == 0, 1.0, n / (n + 1.0))

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
            raise TooFewWavelengths('profile fit: too few wavelength samples')
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
        self._pl_curves()

    @classmethod
    def for_placement(cls, exps, grid, priors=None, bounds=None):
        """A model that only places a given spectrum on the exposures (``frame_image``,
        ``throughput``, ``transmission``): no pixel table and no data, so it exists for a
        source without a usable pixel of its own (a contaminant predicted from its
        3-pixel boxcar, ``profile_contam.boxcar_pass1``). ``grid`` is the spectrum grid."""
        M = cls.__new__(cls)
        M.exps, M.chromatic = exps, True
        M.nod_shift = nod_shifts(exps)
        M.priors, M.bounds_cfg = priors or Priors(), bounds or Bounds()
        M.c_prior, M.srcy_flag = planned_position(exps)
        M.lam_grid = np.asarray(grid, float)
        M.pivot = float(np.exp(np.median(np.log(M.lam_grid))))
        M.lam_rng = (float(np.percentile(M.lam_grid, 2)), float(np.percentile(M.lam_grid, 98)))
        M.pix_masks, M.terms, M.image_pix, M.npix = [], [], {}, 0
        M._pl_curves()
        return M

    def _pl_curves(self):
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
        """(shape vector, per-exposure pointing offsets) of fit parameters ``q``. The
        offsets are zero: the nods land where commanded (headers: within ~0.001 slit
        units), and a fitted offset absorbed overlapping neighbours' light instead (G395M,
        2026-11-01). They stay in the interfaces as an all-zero vector."""
        if self.chromatic:
            c0, dc, lnw, dw = q[:4]
        else:
            c0, lnw = q[:2]; dc = dw = 0.0
        return np.array([c0, dc, lnw, dw]), np.zeros(len(self.exps))

    def _x(self, lam):
        lo, hi = np.log2(self.lam_rng[0] / self.pivot), np.log2(self.lam_rng[1] / self.pivot)
        return np.clip(np.log2(lam / self.pivot), lo, hi)

    def centre(self, p, eoff, k, lam):
        """Exposure k's trace centre: the shared source centre moved to the source's planned
        position in exposure k (``nod_shift``), plus k's offset, then the centre law."""
        e = self.exps[k]
        cg = p[0] + p[1] * self._x(lam)
        if self.nod_shift is not None:
            cg = cg + self.nod_shift[k]
        D, a = centre_law(lam, e.detector, e.quadrant, law=self.centre_law_tables)
        return cg + eoff[k] + D + a * cg

    def width(self, p, lam):
        return np.maximum(np.exp(p[2]) + p[3] * self._x(lam), W_FLOOR)

    def _components(self, k, lam, dy):
        """Per PSF component (``psf_components``) in slit units: (share, sigma before the
        shutter, sigma after it, offset before it, offset after it)."""
        out = []
        for c in psf_components(lam, self.exps[k].detector, tables=self.psf_tables):
            s = c['sigma'] * dy
            a = s / np.sqrt(2.0) if c['q'] == 0.5 else s * np.sqrt(c['q'])
            b = s / np.sqrt(2.0) if c['q'] == 0.5 else s * np.sqrt(1.0 - c['q'])
            off = c['offset'] * dy
            out.append((c['frac'], a, b, off if c['pre'] else 0.0, 0.0 if c['pre'] else off))
        return out

    def image(self, p, eoff, k, y, lam, dy):
        """Exposure k's source image at slit positions y (pixel-integrated, per unit
        total flux, before the along-dispersion throughput): per PSF component, the galaxy
        convolved with the component's part before the shutter, clipped by the open
        shutters, blurred by its part after the shutter, displaced before or after."""
        c = self.centre(p, eoff, k, lam)
        w = self.width(p, lam)
        iv = open_intervals(self.exps[k].open_k)
        comps = [(fr, np.sqrt(a ** 2 + w ** 2), b, o1, o2) for fr, a, b, o1, o2 in self._components(k, lam, dy)]
        acc = 0.0
        for xi, wi in zip(_GLX, _GLW):
            yy = y + 0.5 * dy * xi
            acc = acc + wi * sum(fr * _clipblur(yy - o2, c + o1, s1, s2, iv) for fr, s1, s2, o1, o2 in comps)
        return 0.5 * dy * acc

    def transmission(self, p, eoff, k, lam, dy, width=None, aperture=REF_APERTURE, centre=None):
        """Cross-slit transmission (pre-shutter clipping) through an aperture of
        shutters, for the fitted source (width=None) or a point source (width=0),
        at the fitted centre or at a given slit position."""
        c = self.centre(p, eoff, k, lam) if centre is None else centre
        w = self.width(p, lam) if width is None else width
        iv = open_intervals(aperture)
        return sum(fr * _transmitted(c + o1, np.sqrt(a ** 2 + w ** 2), iv)
                   for fr, a, _, o1, _ in self._components(k, lam, dy))

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
        pr = self.priors
        pri = [(p[0] - self.c_prior) / pr.centre_sigma,
               (p[2] - np.log(pr.width_median)) / pr.width_log_sigma]
        if self.chromatic:
            pri += [p[1] / pr.drift_sigma, p[3] / pr.width_change_sigma]
        return np.concatenate([r, pri])

    def bounds(self):
        c, B = self.c_prior, self.bounds_cfg
        if self.chromatic:
            lo = [c - B.centre, -B.drift, np.log(W_FLOOR), -B.width_change]
            hi = [c + B.centre, B.drift, np.log(B.width_max), B.width_change]
        else:
            lo, hi = [c - B.centre, np.log(W_FLOOR)], [c + B.centre, np.log(B.width_max)]
        return np.array(lo), np.array(hi)

    def param_names(self):
        return ['centre', 'drift', 'width', 'width_change'] if self.chromatic else ['centre', 'width']

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
            q = list(p) if self.chromatic else [p[0], p[2]]
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
        their priors).
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
    q0 = shape_vector(base, M.c_prior, M.priors.width_median)
    sel = [names.index(n) for n in free]
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
    ``centre_offset`` (from the planned position), ``drift``, ``width``, ``width_change``."""
    q = dict(centre_offset=0.0, drift=0.0, width=width_default, width_change=0.0)
    q.update(shape or {})
    return np.array([c_prior + q['centre_offset'], q['drift'], np.log(max(q['width'], W_FLOOR)), q['width_change']])


def fit_fixed(exps=None, shape=None, eoff=None, clip=5.0, n_clip=2, model=None,
              use=None, chromatic=True, priors=None, grid=None, bounds=None):
    """Spectrum at a fixed shape: a linear solve.

    shape : dict(centre_offset, drift, width, width_change) at this model's pivot; the
        default is the default shape (star PSF x the prior median width at the planned
        position, no chromatic terms).
    eoff : per-exposure pointing offsets, held (default zero, as every fit now takes them).
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
    params = dict(centre=float(p[0]), drift=float(p[1]), width=float(np.exp(p[2])), width_change=float(p[3]),
                  pivot=M.pivot, centre_prior=M.c_prior)
    R = FitResult(params=params, eoff=e, lam=M.lam_grid, flux=np.where(good, f, np.nan),
                  flux_err=np.where(good, np.sqrt(np.abs(np.diag(cov))), np.nan), chi2=chi2,
                  npix=int(round(n_eff)), nfree=int(good.sum()),
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
    return dict(centre_offset=s['centre'] - s['centre_prior'] + s['drift'] * dx, drift=s['drift'],
                width=max(s['width'] + s['width_change'] * dx, W_FLOOR * 2), width_change=s['width_change'])


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
    M = result.model
    p = [result.params['centre'], result.params['drift'], np.log(result.params['width']), result.params['width_change']]
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


def boxcar_factor(result, lam):
    """What stage 3's 3-pixel boxcar (``fnu_3px``) measures of the source, per unit true
    spectrum: the source's light within +- 1.5 detector rows of its planned
    position (SRCYPOS, where Spec3 centres the extraction aperture), on jwst's
    point-source calibration (the s2d is the frame divided by the point-source path
    loss, so a pixel holds f S / T_point, ``throughput``). Averaged over the science
    exposures that contain the source; a row is the exposure's median pixel height."""
    M = result.model
    p, eoff = _qvec(result)
    lam = np.asarray(lam, float)
    sci = ([i for i, e in enumerate(M.exps) if e.use_data and e.has_source]
           or [i for i, e in enumerate(M.exps) if e.use_data] or list(range(len(M.exps))))
    out, n = 0.0, 0
    for k in sci:
        e = M.exps[k]
        dy = np.asarray(e.dy, float)
        dy = float(np.median(dy[np.isfinite(dy)])) if np.isfinite(dy).any() else np.nan
        if not np.isfinite(dy):
            continue
        d = np.full_like(lam, dy)
        # the window as three pixel-integrated rows of height dy
        box = sum(M.image(p, eoff, k, np.full_like(lam, e.src_y + r * dy), lam, d) for r in (-1, 0, 1))
        tp = M.transmission(p, eoff, k, lam, d, width=0.0, centre=e.src_y)
        out, n = out + box / np.maximum(tp, 1e-6), n + 1
    return out / n if n else np.full(lam.shape, np.nan)


def drizzle_frames(result, dmap, unit=False):
    """Drizzle model frames exactly as stage 3 drizzled the data: through the product's
    pixel maps (``drizzle_map.DrizzleMap``, from Spec3's outlier-flagged crf), so the
    model takes the data's weights and masks on the data's grid. Returns the 2-D array
    (float32, the s2d's grid)."""
    by_name = {e.name: k for k, e in enumerate(result.model.exps)}
    return drizzle_arrays(lambda name: model_frame(result, by_name[name], unit=unit), dmap)


def drizzle_arrays(frame_of, dmap):
    """Drizzle arbitrary per-exposure cutout arrays (``frame_of(file name)``, in the
    live canonical units) through the product's pixel maps, as the data were drizzled.
    A name for which ``frame_of`` returns None gets zeros."""
    return dmap.drizzle({name: frame_of(name) for name in dmap.names})['data']


FIT_VERSION = '3'                     # 2: no per-exposure pointing offsets fitted; 3: each nod at its planned position


def run_single_source(product_name, workspace_dir, groups, bkg_overrides, science,
                      crf_path, chromatic=True, priors=None):
    """Fit one source and write ``{product_name}_pf.fits``: the fitted shape (header)
    and the drizzled 2D profile per unit reported flux (PROF2D) on the stage-3 s2d
    grid. The profile is not normalised: weighting the s2d with it returns the
    reported flux (main-shutter light on the point-source path-loss calibration)."""
    import os
    from astropy.io import fits
    from campfire_pipeline.common.io import log
    from campfire_pipeline.nirspec import drizzle_map as DM

    exps = load_source(groups, bkg_overrides=bkg_overrides, science=science)
    R = fit(exps, chromatic=chromatic, priors=priors)
    p = R.params
    log(f"{product_name}: profile fit centre {p['centre']:+.3f} (prior {p['centre_prior']:+.3f}), "
        f"width {p['width']:.3f}, drift {p['drift']:+.3f}/oct, width change {p['width_change']:+.3f}/oct, "
        f"chi2/dof {R.chi2_red:.3f}")
    s2d = os.path.join(workspace_dir, f'{product_name}_s2d.fits')
    prof = drizzle_frames(R, DM.DrizzleMap.from_crf(crf_path, DM.step_settings(s2d)), unit=True)

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
        fits.ImageHDU(np.asarray(prof, float) / 1e12, name='PROF2D'),
    ])
    path = os.path.join(workspace_dir, f'{product_name}_pf.fits')
    out.writeto(path, overwrite=True)
    return path
