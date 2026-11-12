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
through the diffraction model of the MSA (``optics``: the telescope's pupil and an
empirical kernel before the shutter, the slitlet's open shutters, the spectrograph's
pupil stop and the detector blur), integrated over each pixel. One model serves every
disperser.

tau_k is the along-dispersion throughput: the point-source path loss jwst computed
for exposure k (``pathloss_point``) moved to the shutter centre in the slit direction,
divided by the model's transmission of a point source there through the MOS1X3
aperture. Slit-direction losses are the model's, for the actual source width and
position; the along-dispersion loss and the level are jwst's.

Shape parameters have Gaussian priors (width: log-normal on the bright-galaxy
population), so at low S/N the fit settles on a compact source at the planned
position and the extraction becomes a fixed-template optimal extraction.
"""

import warnings
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import least_squares

from campfire_pipeline.nirspec import crds_refs as CR
from campfire_pipeline.nirspec.constants import MSA_SLIT_MARGIN, MSA_SLIT_PITCH
# the forward model's modules read this one's helpers only when called, and this one reads the
# components only when called, so the import cycle is safe from either end
from campfire_pipeline.nirspec.forward import components as FC
from campfire_pipeline.nirspec.forward.model import ForwardModel

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


# Trace-centre law from calibration-field stars: centre(lambda) = c + D(lambda) + alpha(lambda) * c,
# relative to the trace at 1.0-2.6 um; D per MSA quadrant and detector (falling back to the same
# quadrant on the other detector, then to the detector), alpha per detector; values at the nodes
# PSF_LAM [um] (the centres of the calibration's wavelength ranges). Measured through the
# diffraction model (``optics_calibration.step_laws``; 497 stars of 12497 obs 1-5, 6642 obs 2 and
# NGC 2506, 2026-11-04): with the model the slope with position (alpha) is near zero (the old PSF
# needed -0.11 at 4.7 um). Q1_NRS1 at 4.7 um (too few stars) is Q1_NRS2's: the two agree within
# 0.001 at every other node.
PSF_LAM = np.array([0.85, 1.25, 1.75, 2.30, 2.95, 3.70, 4.70])
CENTRE_LAW = {
    'NRS1': dict(D=np.array([+0.0103, +0.0048, +0.0004, -0.0052, -0.0133, -0.0239, -0.0400]),
                 alpha=np.array([+0.0032, +0.0026, +0.0007, -0.0022, -0.0014, +0.0008, -0.0011])),
    'NRS2': dict(D=np.array([+0.0114, +0.0047, +0.0006, -0.0055, -0.0141, -0.0252, -0.0443]),
                 alpha=np.array([+0.0015, +0.0024, +0.0013, -0.0039, -0.0054, -0.0017, -0.0128])),
    'Q1_NRS1': dict(D=np.array([+0.0065, +0.0021, +0.0001, -0.0033, -0.0085, -0.0151, -0.0273])),
    'Q1_NRS2': dict(D=np.array([+0.0060, +0.0028, +0.0000, -0.0030, -0.0083, -0.0152, -0.0273])),
    'Q2_NRS2': dict(D=np.array([+0.0149, +0.0060, +0.0010, -0.0072, -0.0184, -0.0328, -0.0589])),
    'Q3_NRS1': dict(D=np.array([+0.0068, +0.0037, +0.0005, -0.0035, -0.0090, -0.0162, -0.0269])),
    'Q4_NRS1': dict(D=np.array([+0.0144, +0.0062, +0.0005, -0.0073, -0.0191, -0.0335, -0.0616])),
}

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
    """The open intervals (slit-frame units) of shutters ``open_k``: each +-OPEN_HALF about its
    centre."""
    return [(k * MSA_SLIT_PITCH - OPEN_HALF, k * MSA_SLIT_PITCH + OPEN_HALF) for k in open_k]


def centre_law(lam, det, quad=None, law=None):
    """(D, alpha) per wavelength for a detector and MSA quadrant.

    law : replaces ``CENTRE_LAW`` (same layout; an entry may carry its own wavelength
        nodes as ``lam``, default ``PSF_LAM``), e.g. ``NO_CENTRE_LAW`` to measure the trace
        centre itself."""
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
# calibration-field stars, 5091 exposure offsets fitted freely (2026-11-02; programs 12497 obs 1-5 and 6642 obs 2, PRISM and six gratings): the planned
# shifts take the rms of the offsets from 0.0100 to 0.0043 slit units, as well as a fitted
# row or MSA-row law in sample and better for a held-out observation; nothing is left against
# detector column, row or wavelength. The scale measured / planned is 1.03 overall but varies
# per visit (PRISM 0.85-1.21; the gratings of 6642 obs 2, 0.5-0.88, with that visit's PRISM), so
# the planned shifts are used as they stand (scale 1). Through the diffraction model (2026-11-04,
# ``optics_calibration.step_laws``, PRISM, NGC 2506 added): rms 0.0137 -> 0.0060 with the planned
# shifts, scale 0.95-1.10 per visit (one visit of 26 star-exposures 0.68): unchanged.
NOD_OFFSET_LAW = dict(scale=1.0)


def _written_srcy(e):
    """SRCYPOS as jwst wrote it, where a metafile placeholder reads exactly 0.0 (``src_y``
    carries the bar offset); ``src_y`` for an exposure built without it."""
    return e.src_y_jwst if np.isfinite(e.src_y_jwst) else e.src_y


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
    placeholder = any(_written_srcy(e) != 0.0 for e in exps if e.has_source)
    out = np.zeros(len(exps))
    for k, e in enumerate(exps):
        if not e.has_source or not np.isfinite(e.src_y) or (placeholder and _written_srcy(e) == 0.0):
            continue
        out[k] = float(law['scale']) * (e.src_y - c)
    return out


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
    src_y_jwst: float = np.nan     # SRCYPOS as jwst wrote it (shutter pitches; ``source_position``)
    pl_ref: str = ''               # jwst's path-loss reference file, as the cal file names it
    src_x_jwst: float = np.nan     # SRCXPOS as jwst wrote it (shutter pitches along the dispersion)
    bar_offset: float = 0.0        # added to ``y`` and ``src_y`` (slit units): where the observation's bars sit
                                   # against jwst's slit frame (``bar_offset``)
    slit_loss: CR.SlitLoss = CR.SlitLoss()   # whose path loss the model takes, per direction (``throughput``)
    mbkg: np.ndarray = None        # a single frame's master background, in the units of ``diff`` on the same
                                   # frame (``mbkg_extraction.single_frame_exposures``): the column of the
                                   # master-background scale term (``ProfileModel``, ``mbkg_sigma``)
    pa_slit_x: float = np.nan      # position angle on the sky [deg, east of north] of the slit frame's +x
    pa_slit_y: float = np.nan      # (along the dispersion) and +y (along the slit), from the slit's WCS
                                   # (``slit_axes``): how a source model on the sky lies in this exposure

    @property
    def dy(self):
        """Pixel height in slit_frame units (``pixel_height``)."""
        return pixel_height(self.y)


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


def _float_or_nan(v):
    return float(v) if v is not None else np.nan


def _row_curve(e, a):
    """(wavelength, value) of the per-pixel array ``a`` along the row nearest the
    source's planned position, sorted by wavelength, finite and positive only."""
    yy = np.where(np.isfinite(e.y), e.y, np.inf)
    r = np.argmin(np.abs(yy - e.src_y), axis=0)
    cols = np.arange(e.wav.shape[1])
    lam, v = e.wav[r, cols], np.asarray(a, float)[r, cols]
    ok = np.isfinite(lam) & np.isfinite(v) & (v > 0)
    o = np.argsort(lam[ok])
    return lam[ok][o], v[ok][o]


def _pa_v3(path):
    from astropy.io import fits
    try:
        v = fits.getheader(path, 1).get('PA_V3')
        return float(v) if v is not None else np.nan
    except (OSError, IndexError, ValueError, TypeError):
        return np.nan


def slit_axes(slit):
    """(position angle of the slit frame's +x, of its +y) on the sky [deg, east of north] for a
    jwst slit with a WCS: the two axes traced from the slit's centre through ``slit_frame`` to
    ``world`` at 2 um (the directions do not change with wavelength). NaN without a WCS."""
    lam = 2.0e-6
    try:
        t = slit.meta.wcs.get_transform('slit_frame', 'world')
        ra0, dec0, _ = t(0.0, 0.0, lam)

        def pa(dx, dy):
            ra, dec, _ = t(dx, dy, lam)
            e = (ra - ra0) * np.cos(np.radians(dec0))
            return float(np.degrees(np.arctan2(e, dec - dec0)) % 360.0)
        return pa(0.25, 0.0), pa(0.0, 0.25)
    except Exception:                               # noqa: BLE001 - no usable WCS: no orientation, not an error here
        return np.nan, np.nan


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


# what jwst wrote into a cal file, read through the boundary module (``crds_refs``)
_applied_pathloss = CR.applied_pathloss
_pathloss_ref = CR.pathloss_ref
source_position = CR.slit_position          # the planned position in slit-frame units from SRCYPOS


def load_options(stage_config):
    """The loader's keyword arguments from ``[nirspec.stage3]`` as stage 3 resolved it: the measured
    bar offsets (``bar_offset``) and whose path loss the model takes (``crds_refs.SlitLoss``)."""
    sc = stage_config or {}
    return dict(bar_offsets=sc.get('bar_offsets'), slit_loss=CR.SlitLoss.from_config(sc))


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


def load_group(cal_files, bkg_overrides=None, science=None, group=0, single_frames=False, pre_bkgsub=False,
               bar_offsets=None, slit_loss=None):
    """Exposures of one stage-2b background group, from the canonical files.

    The live SCI is stage 2b's nod difference with the path loss re-applied; this
    removes the applied correction again (the inverse of jwst's ``do_correction_mos``),
    which recovers the frame jwst's background step produced.

    bar_offsets : {disperser ('PRISM/CLEAR'): {quadrant: offset}} (``bar_offset.table``), or
        None for none: the offset is added to every pixel's slit y and to the source's planned
        position, so the model's shutters sit where the observation's background puts its bars.
        jwst's own path-loss position (``src_y_jwst``) is not moved.
    slit_loss : whose path loss the model takes (``crds_refs.SlitLoss``); None for the default.

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
        grating = f'{m.meta.instrument.grating}/{m.meta.instrument.filter}'.upper()
        detector = str(m.meta.instrument.detector).upper()
        quadrant = int(s.quadrant) if s.quadrant is not None else None
        bar = float((bar_offsets or {}).get(grating, {}).get(quadrant, 0.0))
        pa_x, pa_y = slit_axes(s)
        exps.append(Exposure(
            name=name, group=group, detector=detector, grating=grating, quadrant=quadrant,
            diff=_place(diff, s, y0, x0, shape), var=_place(var * pl ** 2, s, y0, x0, shape),
            ok=_place(ok.astype(float), s, y0, x0, shape, fill=0.0) > 0,
            wav=_place(np.asarray(s.wavelength, float), s, y0, x0, shape), y=_place(ys + bar, s, y0, x0, shape),
            pl_applied=_place(pl, s, y0, x0, shape), pl_point=_place(np.asarray(s.pathloss_point, float), s, y0, x0, shape),
            src_y=source_position(float(s.source_ypos)) + bar, src_y_jwst=float(s.source_ypos), bar_offset=bar,
            open_k=open_shutters(s.shutter_state, s.slit_ymin, s.slit_ymax),
            partners=part, use_data=(part is not None) and (science is None or name in science),
            cutout=(s.ystart - y0, s.xstart - x0, ny, nx), path=str(f), origin=(y0, x0),
            has_source=bool(has_src), pa_v3=_pa_v3(f), pl_ref=_pathloss_ref(m), slit_loss=slit_loss or CR.SlitLoss(),
            src_x_jwst=_float_or_nan(getattr(s, 'source_xpos', None)), pa_slit_x=pa_x, pa_slit_y=pa_y))
    for m in models:
        m.close()
    for e in exps:
        if not e.pl_ref:
            warnings.warn(f'profile fit: {e.name} names no path-loss reference; jwst\'s loss along the slit '
                          'stays in its data and the model applies its own as well')
    for ref in sorted({e.pl_ref for e in exps if e.pl_ref}):
        CR.check_references(pathloss=ref)          # warns once when a calibration was derived against another file
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


OUTLIER = 16              # jwst DQ OUTLIER: set by Spec3's outlier detection in the crf


def crf_outliers(crf_dq, live_dq=None):
    """The pixels Spec3's outlier detection flagged in one exposure: ``OUTLIER`` in the crf's DQ
    (on the canonical's cutout), less any the canonical's live DQ ``live_dq`` already had. The one
    place this choice is made, for the nodded fit (``mask_crf_outliers``) and the master-background
    frames (``mbkg_extraction.outlier_flags``)."""
    flagged = (np.asarray(crf_dq) & OUTLIER) > 0
    if live_dq is not None:
        flagged &= (np.asarray(live_dq) & OUTLIER) == 0
    return flagged


def mask_crf_outliers(exps, crf_dq):
    """Leave Spec3's outliers out of the nod differences (in place, ``Exposure.ok``; user,
    2026-11-10: the fits no longer clip against their own model). ``crf_dq``: {canonical file
    name: the crf's DQ of that exposure, on its cutout} (``drizzle_map.crf_dq``). A nod difference's
    pixel is left out when the same detector pixel is flagged in that exposure or in any exposure
    it was differenced with (a cosmic ray in a partner's frame is in this difference too, with the
    opposite sign). Exposures share their group's common frame, so a detector pixel is the same
    array position in all of them. Returns (pixels left out, names of science exposures with no
    entry in ``crf_dq``)."""
    flags = {}
    for k, e in enumerate(exps):
        dq = crf_dq.get(e.name)
        if dq is None or e.cutout is None:
            continue
        r0, c0, ny, nx = e.cutout
        if np.shape(dq) != (ny, nx):
            raise ValueError(f'{e.name}: crf DQ {np.shape(dq)} is not on the canonical cutout {(ny, nx)}')
        m = np.zeros(e.y.shape, bool)
        m[r0:r0 + ny, c0:c0 + nx] = crf_outliers(dq)
        flags[k] = m
    missing = [e.name for e in exps if e.use_data and e.name not in crf_dq]
    n = 0
    for k, e in enumerate(exps):
        if not e.use_data:
            continue
        bad = np.zeros(e.y.shape, bool)
        for j in [k] + list(e.partners or []):
            if j in flags:
                bad |= flags[j]
        n += int(np.sum(bad & e.ok))
        e.ok = e.ok & ~bad
    return n, missing


def load_source(groups, bkg_overrides=None, science=None, single_frames=False, pre_bkgsub=False, bar_offsets=None,
                slit_loss=None):
    """All exposures of a source: ``groups`` is a list of canonical-file lists, one per
    stage-2b background group, and ``bkg_overrides`` the matching list of that group's
    nod overrides (or None). Partner indices are made global. ``single_frames``,
    ``pre_bkgsub``, ``bar_offsets`` and ``slit_loss``: see ``load_group`` (``load_options``
    gives the last two from the stage config)."""
    out = []
    overrides = bkg_overrides if isinstance(bkg_overrides, (list, tuple)) else [bkg_overrides] * len(groups)
    for g, files in enumerate(groups):
        exps = load_group(files, bkg_overrides=overrides[g], science=science, group=g, single_frames=single_frames,
                          pre_bkgsub=pre_bkgsub, bar_offsets=bar_offsets, slit_loss=slit_loss)
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
    shape chain (``profile_chain``) passes the recalibrated ones from the config."""
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


# bit of each shape parameter in a bound-hit mask (named in SHAPEWHY 'bound:...'; 16 was the per-exposure
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
    mbkg_scale: float = np.nan        # fitted master-background scale (``ProfileModel.mbkg_sigma``); NaN without the term
    mbkg_scale_err: float = np.nan
    mbkg_scale_capped: float = np.nan  # the scale fitted before the cap set it to zero (mbkg_extraction.own_shape)

    @property
    def chi2_red(self):
        return self.chi2 / max(self.npix - self.nfree, 1)


@dataclass
class Solution:
    """The linear solve at one shape (``ProfileModel.linear_solve``)."""
    f: np.ndarray                     # spectrum on the grid (0 where unconstrained)
    cov: np.ndarray                   # its covariance (on request; NaN where unconstrained), else None
    good: np.ndarray                  # grid points constrained by the data
    A: object                         # design matrix (sparse): the source's part of the model is A @ f
    model: np.ndarray                 # the whole model on the fitted pixels (source + scale x master background)
    dchi2: float                      # chi2 improvement over no source (the scale term in both)
    scale: float = 0.0                # master-background scale; 0 without the term
    scale_err: float = np.nan


def fit_pixel_masks(exps):
    """(exposure index, pixel mask) of the pixels a fit uses: usable data inside an open
    shutter (+-OPEN_HALF, plus ``FIT_MARGIN``), covered by every subtracted frame."""
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


def pixel_height(y):
    """Pixel height in slit_frame units of a slit-position image ``y`` (NaN outside the
    slit): the central difference of y down each column, one-sided at a slit's first and
    last row (the row beyond is outside the slit; slitlets longer than three shutters put
    partner terms on those rows)."""
    y = np.asarray(y, float)
    d = np.abs(np.gradient(y, axis=0))
    edge = np.isnan(d) & np.isfinite(y)
    if edge.any():
        up = np.abs(np.diff(y, axis=0, append=np.nan))
        down = np.abs(np.diff(y, axis=0, prepend=np.nan))
        d = np.where(edge, np.where(np.isfinite(up), up, down), d)
    return d


def cover(grid, lam_lo, lam_hi):
    """``grid`` (sorted) extended at each end, by the step of its end, until it spans
    ``lam_lo``-``lam_hi``: every pixel a spectrum on it models then falls between two
    points. Unchanged when it already does or has fewer than two points."""
    g = np.asarray(grid, float)
    if g.size < 2 or not (np.isfinite(lam_lo) and np.isfinite(lam_hi)):
        return g
    lo, hi = g[1] - g[0], g[-1] - g[-2]
    n_lo = int(np.ceil((g[0] - lam_lo) / lo)) if lam_lo < g[0] and lo > 0 else 0
    n_hi = int(np.ceil((lam_hi - g[-1]) / hi)) if lam_hi > g[-1] and hi > 0 else 0
    return np.concatenate([g[0] - lo * np.arange(n_lo, 0, -1), g, g[-1] + hi * np.arange(1, n_hi + 1)])


def nod_terms(exps, k):
    """(exposure, weight) of every frame of exposure k's nod difference: +1 for k's own
    frame and -1/n for each of its n subtracted partners."""
    parts = list(exps[k].partners or [])
    return [(k, 1.0)] + [(j, -1.0 / len(parts)) for j in parts]


def term_weights(exps, k):
    """``nod_terms`` without the frames that do not contain the source
    (``Exposure.has_source``). Those were subtracted, so the others' weights are
    unchanged, but they put no source light on the difference."""
    return [(j, w) for j, w in nod_terms(exps, k) if exps[j].has_source]


def nod_variance(var, partner_vars):
    """Variance to weight a nod difference with, from its own frame's variance and its
    partners' (``ProfileModel.diff_variance``): ((n+1)/n)**2 times the mean frame
    variance of the set, or ``var`` as given without partners."""
    n = len(partner_vars)
    if n == 0:
        return var
    s2 = (var + sum(partner_vars)) / (n + 1)
    return ((n + 1) / n) ** 2 * s2


def model_optics(exps, optics=None):
    """The diffraction model of each exposure of a ``ProfileModel`` (``optics.optics_for`` its
    detector), or ``optics`` as given (a list, one per exposure)."""
    if optics is not None:
        return list(optics)
    from campfire_pipeline.nirspec import optics as O
    return [O.optics_for(e.detector) for e in exps]


def optics_version():
    """The diffraction model's version, for headers."""
    from campfire_pipeline.nirspec import optics as O
    return O.version()


class ProfileModel(ForwardModel):
    """Stage 3's model: one source, a Gaussian along the slit (``forward.SlitGaussian``), on the
    forward model's pixels, spectrum grid and placement (``forward.ForwardModel``), with its shape
    as a parameter vector p = (centre, drift, ln width, width change), Gaussian priors and bounds
    for the non-linear fit (``fit``).

    centre_law_tables : replaces ``CENTRE_LAW`` for this model (``centre_law``), e.g.
        ``NO_CENTRE_LAW`` to measure the trace centre on stars; None uses the module's.
    nod_law : the nod geometry (``nod_shifts``): None for ``NOD_OFFSET_LAW``, False for one
        position shared by every exposure (to measure the offsets themselves).
    optics : the diffraction model, a list with one ``optics.Optics`` (or an object with its
        ``image`` and ``transmission``) per exposure, e.g. one being calibrated; None takes
        the shipped model of each exposure's detector (``model_optics``).
    mbkg_sigma : the master-background scale term, for single frames with their master
        background subtracted (``mbkg_extraction``): the model adds one constant times each
        exposure's master background (``Exposure.mbkg``), the same constant for every
        exposure, solved with the spectrum at every shape and held near zero by a Gaussian
        of this width (the penalty (scale / mbkg_sigma)^2 joins the chi2). It takes up an
        error in the master background's level in the slitlet (about 1 % either way), which
        a source model would otherwise fit as a wide source. None (or 0): no term. Not for
        nod differences, where the master background cancels."""

    centre_law_tables = None
    nod_law = None
    nod_shift = None
    optics = None
    mbkg_sigma = None

    def __init__(self, exps, chromatic=True, priors=None, grid=None, bounds=None, c_prior=None,
                 centre_law_tables=None, nod_law=None, optics=None, mbkg_sigma=None):
        self.chromatic = chromatic
        self.priors = priors or Priors()
        self.bounds_cfg = bounds or Bounds()
        super().__init__(exps, grid=grid, optics=optics, centre_law_tables=centre_law_tables, nod_law=nod_law,
                         c_prior=c_prior, mbkg_sigma=mbkg_sigma)

    @classmethod
    def for_placement(cls, exps, grid, priors=None, bounds=None, optics=None):
        """A model that only places a given spectrum on the exposures (``frame_image``,
        ``throughput``, ``transmission``): no pixel table and no data, so it exists for a
        source without a usable pixel of its own (a contaminant predicted from its
        3-pixel boxcar, ``profile_contam.boxcar_pass1``). ``grid`` is the spectrum grid."""
        M = cls.__new__(cls)
        M.exps, M.chromatic = exps, True
        M.optics = model_optics(exps, optics)
        M.nod_shift = nod_shifts(exps)
        M.priors, M.bounds_cfg = priors or Priors(), bounds or Bounds()
        M.c_prior, M.srcy_flag = planned_position(exps)
        M.lam_grid = np.asarray(grid, float)
        M.pivot = float(np.exp(np.median(np.log(M.lam_grid))))
        M.lam_rng = (float(np.percentile(M.lam_grid, 2)), float(np.percentile(M.lam_grid, 98)))
        M.pix_masks, M.terms, M.image_pix, M.npix = [], [], {}, 0
        M._pl_curves()
        return M

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

    def component(self, p, eoff=None):
        """The source of shape vector ``p`` as a forward-model component."""
        return FC.SlitGaussian(p[0], p[1], np.exp(p[2]), p[3], eoff=eoff)

    def _x(self, lam):
        return self.log2_lam(lam)

    def centre(self, p, eoff, k, lam):
        """Exposure k's trace centre: the shared source centre moved to the source's planned
        position in exposure k (``nod_shift``), plus k's offset, then the centre law."""
        return self.component(p, eoff).centre_at(self, k, lam)

    def width(self, p, lam):
        """The source's width (slit units) on the sky, before the trace-centre law's stretch."""
        return self.component(p).width_at(self, lam)

    def image_width(self, p, k, lam):
        """The source's width (slit units) where it lands in exposure k: ``width`` times the
        trace-centre law's stretch (``ForwardModel.trace_stretch``)."""
        return self.component(p).image_width(self, k, lam)

    def image(self, p, eoff, k, y, lam, dy):
        """Exposure k's source image at slit positions y (pixel-integrated, per unit
        total flux, before the along-dispersion throughput): the galaxy through the
        diffraction model (``optics``) and the open shutters, from its tables, scaled from
        their row height to this pixel's."""
        return self.component(p, eoff).image(self, k, y, lam, dy)

    def transmission(self, p, eoff, k, lam, dy, width=None, aperture=REF_APERTURE, centre=None):
        """Cross-slit transmission (the light that passes the shutters and the spectrograph's
        stop) through an aperture of shutters, for the fitted source (width=None: its width
        where it lands, ``image_width``) or a point source (width=0), at the fitted centre or
        at a given slit position."""
        c = self.centre(p, eoff, k, lam) if centre is None else centre
        w = self.image_width(p, k, lam) if width is None else width
        return self.optics[k].transmission(aperture, lam, c, np.maximum(np.asarray(w, float), W_FLOOR))

    def throughput(self, p, eoff, k, lam, dy, fixed=None):
        """Along-dispersion throughput tau_k: jwst's level and its along-dispersion
        dependence, with its slit-direction dependence replaced by the model's. jwst's
        point-source path loss at the planned position is moved to the shutter centre
        (``crds_refs.point_y_ratio``), times the model's correction to its
        along-dispersion dependence (``x_correction``, at SRCXPOS) and the model's ratio for
        the fitted source over a point there (``x_extended_ratio``: a round source), and
        divided by the model's transmission of a point source at the centre through the
        MOS1X3 aperture. The fitted spectrum is then the source's total light.

        The exposure's ``slit_loss`` (``crds_refs.SlitLoss``) can hand either direction back to
        jwst: y = "jwst" keeps jwst's point path loss at the planned position and divides by the
        model's point transmission there instead (the model then shapes the image and its change
        with the fitted position and width; a point source at the planned position gets jwst's
        loss exactly); x = "jwst" leaves out ``x_correction``. ``fixed``: ``throughput_fixed``
        at these wavelengths, when already at hand."""
        if fixed is None:
            fixed = self.throughput_fixed(k, lam, dy)
        return fixed * self.component(p).x_ratio(self, k, lam)

    def design(self, p, eoff):
        """Sparse (npix x ngrid) matrix A with model = A @ f."""
        return self.design_matrix([self.component(p, eoff)])

    def linear_solve(self, p, eoff, covariance=False):
        """Weighted least squares at fixed shape (``ForwardModel.solve`` with the one
        component): the spectrum, and with the master-background term (``mbkg_sigma``) its
        scale, solved together. Returns a ``Solution``."""
        s = self.solve_components([self.component(p, eoff)], covariance=covariance)
        return Solution(f=s.spectra[0], cov=s.cov, good=s.good[0], A=s.A, model=s.model, dchi2=s.dchi2,
                        scale=s.scale, scale_err=s.scale_err)

    def solve(self, p, eoff, covariance=False):
        """(spectrum, covariance, good, A) of ``linear_solve``, for a model whose prediction
        is A @ f: without the master-background term (refused with it; use
        ``linear_solve`` and its ``model``)."""
        if self.mbkg_sigma is not None:
            raise ValueError('profile fit: a model with the master-background term is not A @ f; use linear_solve')
        s = self.linear_solve(p, eoff, covariance=covariance)
        return s.f, s.cov, s.good, s.A

    def residuals(self, q):
        """Normalised data residuals and prior terms at shape ``q``, the spectrum (and the
        master-background scale, with its allowance as one more term) solved there."""
        p, eoff = self.unpack(q)
        s = self.linear_solve(p, eoff)
        r = (self.pix['d'] - s.model) / np.sqrt(self.pix['v'])
        r = np.where(self.use, r, 0.0)
        pr = self.priors
        pri = [(p[0] - self.c_prior) / pr.centre_sigma,
               (p[2] - np.log(pr.width_median)) / pr.width_log_sigma]
        if self.chromatic:
            pri += [p[1] / pr.drift_sigma, p[3] / pr.width_change_sigma]
        if self.mbkg_sigma is not None:
            pri.append(s.scale / self.mbkg_sigma)
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
            r = (self.pix['d'] - self.linear_solve(p, eoff).model) / np.sqrt(self.pix['v'])
            return float(np.sum(r[self.use] ** 2))
        finally:
            self.use = saved


# The shape fit's numerics. The optics tables are float32, so one model evaluation is good to about
# 1e-7 and least_squares' default finite-difference step (1.5e-8) returns noise for the derivatives:
# the fit then stopped wherever no step it tried lowered the chi2, short of the minimum and at a
# point set by rounding (zenith_p15 549120: the centre at 0.205 or 0.247 slit units, the minimum
# at 0.177). A step of 1e-4 in each parameter (slit units, ln width) is far above that noise and
# far below any shape's uncertainty. The fit stops when a step lowers the chi2 by less than
# FIT_DCHI2 (a shape 0.1 sigma from the minimum is 0.01 above it); a relative ftol of 1e-5 stopped
# at steps of 0.2 on a chi2 of 18000.
FIT_DIFF_STEP = 1e-4
FIT_DCHI2 = 0.01


def fit(exps, chromatic=True, priors=None, grid=None, bounds=None, model=None):
    """Fit the shape (with priors) and the shared spectrum of one source.

    Every usable pixel is fitted: no pixel is clipped against the model (user,
    2026-11-10). The outliers are Spec3's (``mask_crf_outliers``, already out of the
    exposures' ``ok``); a clip against the model removed real light where the model is
    worst (an extended source's wings). ``model``: reuse a ``ProfileModel`` of the same
    exposures (its pixel mask ``use`` is reset to all). The result's ``bound_bits``
    records any parameter that ended on a bound.
    """
    if model is not None:
        M = model
        M.use = np.ones(M.npix, bool)
    else:
        M = ProfileModel(exps, chromatic=chromatic, priors=priors, grid=grid, bounds=bounds)
    lo, hi = M.bounds()
    q = np.clip(M.start(), lo + 1e-6, hi - 1e-6)
    chi2_start = float(np.sum(M.residuals(q) ** 2))
    q = least_squares(M.residuals, q, bounds=(lo, hi), x_scale='jac', diff_step=FIT_DIFF_STEP,
                      ftol=FIT_DCHI2 / max(chi2_start, 1.0), xtol=1e-10, gtol=1e-10).x
    p, eoff = M.unpack(q)
    s = M.linear_solve(p, eoff, covariance=True)
    r = (M.pix['d'] - s.model) / np.sqrt(M.pix['v'])
    chi2 = float(np.sum(r[M.use] ** 2))
    n_eff = float(np.sum(M.dof_weight()[M.use]))
    flux = np.where(s.good, s.f, np.nan)
    err = np.where(s.good, np.sqrt(np.abs(np.diag(s.cov))), np.nan)
    params = dict(centre=float(p[0]), drift=float(p[1]), width=float(np.exp(p[2])),
                  width_change=float(p[3]), pivot=M.pivot, centre_prior=M.c_prior)
    term = M.mbkg_sigma is not None
    R = FitResult(params=params, eoff=eoff, lam=M.lam_grid, flux=flux, flux_err=err, chi2=chi2,
                  npix=int(round(n_eff)), nfree=int(s.good.sum()) + len(q) + int(term), chromatic=M.chromatic,
                  model=M, mbkg_scale=s.scale if term else np.nan, mbkg_scale_err=s.scale_err)
    R.bound_bits = M.bound_hits(q)
    R.use = M.use.copy()
    return R


def shape_vector(shape, c_prior, width_default):
    """Shape vector (centre, drift, ln width, width change) from a dict with
    ``centre_offset`` (from the planned position), ``drift``, ``width``, ``width_change``."""
    q = dict(centre_offset=0.0, drift=0.0, width=width_default, width_change=0.0)
    q.update(shape or {})
    return np.array([c_prior + q['centre_offset'], q['drift'], np.log(max(q['width'], W_FLOOR)), q['width_change']])


def fit_fixed(exps=None, shape=None, eoff=None, model=None, use=None, chromatic=True, priors=None, grid=None,
              bounds=None):
    """Spectrum at a fixed shape: a linear solve on every usable pixel (no clip against the
    model, as ``fit``).

    shape : dict(centre_offset, drift, width, width_change) at this model's pivot; the
        default is the default shape (star PSF x the prior median width at the planned
        position, no chromatic terms).
    eoff : per-exposure pointing offsets, held (default zero, as every fit now takes them).
    use : a pixel mask to fit on (default: every pixel of the model).
    The returned chi2 is the data chi2 only (no prior terms)."""
    M = model if model is not None else ProfileModel(exps, chromatic=chromatic, priors=priors, grid=grid,
                                                     bounds=bounds)
    M.use = np.ones(M.npix, bool) if use is None else use.copy()
    p = shape_vector(shape, M.c_prior, M.priors.width_median)
    e = np.zeros(len(M.exps)) if eoff is None else np.asarray(eoff, float).copy()
    s = M.linear_solve(p, e, covariance=True)
    r = (M.pix['d'] - s.model) / np.sqrt(M.pix['v'])
    chi2 = float(np.sum(r[M.use] ** 2))
    n_eff = float(np.sum(M.dof_weight()[M.use]))
    params = dict(centre=float(p[0]), drift=float(p[1]), width=float(np.exp(p[2])), width_change=float(p[3]),
                  pivot=M.pivot, centre_prior=M.c_prior)
    term = M.mbkg_sigma is not None
    R = FitResult(params=params, eoff=e, lam=M.lam_grid, flux=np.where(s.good, s.f, np.nan),
                  flux_err=np.where(s.good, np.sqrt(np.abs(np.diag(s.cov))), np.nan), chi2=chi2,
                  npix=int(round(n_eff)), nfree=int(s.good.sum()) + int(term),
                  chromatic=bool(p[1] != 0 or p[3] != 0), model=M,     # the held shape varies with wavelength
                  mbkg_scale=s.scale if term else np.nan, mbkg_scale_err=s.scale_err)
    R.bound_bits = 0
    R.use = M.use.copy()
    return R


NOISE_CLIP = 5.0        # mean_square_scale: residuals beyond this do not count in the noise level (nothing is masked)


def mean_square_scale(M, r, clip=NOISE_CLIP):
    """Calibration of the stated errors from the normalised residuals ``r`` of a fit:
    the mean square of the residuals within ``clip`` sigma, per independent value
    (``ProfileModel.dof_weight``). It includes the noise's own tails: on blank
    sources a MAD reads 10-20 % below the mean square, and dividing a chi2 by it
    inflates the detection S/N of nothing. The clip only keeps a few strong residuals
    out of the noise estimate; no pixel is left out of any fit."""
    u = np.isfinite(r) & (np.abs(r) < clip)
    if u.sum() < 10:
        return 1.0
    c = float(np.sum(r[u] ** 2) / np.sum(M.dof_weight()[u]))
    return c if np.isfinite(c) and c > 0 else 1.0


def detection_snr(M):
    """Integrated detection S/N of a source (``DETSNR``). Reported; the shape chains take
    no decision on it.

    For each starting shape of the fit (``ProfileModel.start_grid``) the spectrum is
    solved and its chi2 improvement over no source, dchi2 = f . (A^T W d), is
    computed; the best is used, so compact or offset sources are not penalised by
    the default template. With the master-background term (``ProfileModel.mbkg_sigma``)
    the scale is solved in both, the source's fit and the no-source comparison, each
    with its allowance, so a master-background level error is not counted as a source.
    dchi2 is divided by a noise scale c measured from the residuals at that best shape
    (``mean_square_scale``), and the number of spectrum points is subtracted (its
    expectation for no source): snr_det = sqrt(max(dchi2 / c - n, 0)).

    Every usable pixel counts (no clip against the model; the outliers are Spec3's,
    ``mask_crf_outliers``). Returns (snr_det, dict with dchi2, n, c, best shape vector)."""
    M.use = np.ones(M.npix, bool)
    e0 = np.zeros(len(M.exps))
    best = None
    for p in M.start_grid():
        s = M.linear_solve(p, e0)
        if best is None or s.dchi2 > best[0].dchi2:
            best = (s, p)
    s, p = best
    d, n = s.dchi2, int(s.good.sum())
    r = (M.pix['d'] - s.model) / np.sqrt(M.pix['v'])
    c = mean_square_scale(M, r)
    s2 = d / c - n
    return float(np.sqrt(max(s2, 0.0))), dict(dchi2=d, n=n, c=c, shape=p)


def planned_position(exps, tolerance=0.05):
    """Planned slit position of the source (prior centre) and a flag.

    The median SRCYPOS of the science exposures that contain the source. A value of
    exactly 0.000 next to nonzero values is a metafile placeholder and is ignored
    (flag 1); science values that still differ by more than ``tolerance`` give
    flag 2 (genuine disagreement)."""
    sel = [e for e in exps if e.use_data and e.has_source] or [e for e in exps if e.use_data] or list(exps)
    vals = [e.src_y for e in sel]
    flag = 0
    nonzero = [e.src_y for e in sel if _written_srcy(e) != 0.0]
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
        spectrum is set to 1 / reported_factor, so a profile-weighted fit of the data
        with this frame returns the reported flux directly.
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
        f = reported_factor(result, result.lam)
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


def reported_factor(result, lam):
    """Reported-flux factor: the share of the fitted source's light that falls within the
    shutter's open width across the dispersion (the shutter's edges, +-optics.X_HALF about
    its centre), and anywhere along the slit. For the round Gaussian of the fitted width
    sigma (arcsec, at each wavelength) at the planned position x = SRCXPOS * X_PITCH of each
    exposure: Phi((X_HALF - x) / s) - Phi((-X_HALF - x) / s), s = sqrt(sigma^2 + X_BLUR^2)
    with the scatter of the true position about the planned one that the optics model's
    along-dispersion tables carry, averaged over the exposures holding the source.

    The true spectrum (the source's total light) times this factor is the reported flux:
    the light that entered the shutter, so nothing is extrapolated across the shutter, where
    the data do not constrain the shape (user, 2026-11-10; replaced the 0.8" circle). Along
    the slit the fitted profile is measured. A point source at the centre gives one; an
    off-centre source gives less, as its light in the shutter is less."""
    from scipy.special import erf
    from campfire_pipeline.nirspec.optics import SLIT, X_BLUR, X_HALF, X_PITCH
    M = result.model
    p, _ = _qvec(result)
    lam = np.asarray(lam, float)
    s = np.sqrt((M.width(p, lam) * SLIT) ** 2 + X_BLUR ** 2) * np.sqrt(2.0)
    xs = [e.src_x_jwst * X_PITCH if np.isfinite(e.src_x_jwst) else 0.0
          for e in M.exps if e.has_source] or [0.0]
    return np.mean([0.5 * (erf((X_HALF - x) / s) - erf((-X_HALF - x) / s)) for x in xs], axis=0)


def boxcar_factor(result, lam):
    """What stage 3's 3-pixel boxcar (``fnu_3px``) measures of the source, per unit true
    spectrum: the source's light within +- 1.5 detector rows of its planned
    position (SRCYPOS, where Spec3 centres the extraction aperture), in the s2d's units:
    the forward model (``throughput`` times the image) divided by the path loss stage 2b
    applied, as ``model_frame`` writes it. Averaged over the science exposures that
    contain the source; a row is the exposure's median pixel height."""
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
        L, P = _row_curve(e, e.pl_applied)
        if L.size <= 3:
            continue
        box = sum(M.image(p, eoff, k, np.full_like(lam, e.src_y + r * dy), lam, d) for r in (-1, 0, 1))
        tau = M.throughput(p, eoff, k, lam, d)
        out, n = out + box * tau / np.interp(lam, L, P), n + 1
    return out / n if n else np.full(lam.shape, np.nan)


def refit_at_shape(result, exps, mbkg_sigma=None):
    """The spectrum of ``exps`` at the shape a fit chose (``result``, e.g. a shape chain's
    result: its model's priors, bounds, grid, prior centre and pivot, the shape defined at
    that pivot): the same source on other data (the scan's pixels, the frames minus the
    master background, there with the master-background scale term ``mbkg_sigma``).
    Returns (FitResult, shape dict as ``fit_fixed`` takes it)."""
    M0 = result.model
    by_detector = {e.detector: o for e, o in zip(M0.exps, M0.optics)}
    optics = [by_detector.get(e.detector) for e in exps]
    M = ProfileModel(exps, chromatic=M0.chromatic, priors=M0.priors, bounds=M0.bounds_cfg, c_prior=M0.c_prior,
                     grid=M0.lam_grid, centre_law_tables=M0.centre_law_tables, nod_law=M0.nod_law,
                     optics=optics if all(o is not None for o in optics) else None, mbkg_sigma=mbkg_sigma)
    # the shape is defined at the fit's pivot and clamped at the fit's wavelength range (``_x``)
    M.pivot, M.lam_rng = M0.pivot, M0.lam_rng
    p = result.params
    shape = dict(centre_offset=p['centre'] - M0.c_prior, drift=p['drift'], width=p['width'],
                 width_change=p['width_change'])
    return fit_fixed(model=M, shape=shape), shape


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


T_MIN_LIGHT = 1e-3      # transmission_frame: pixels where the source's MSA-free light is below this share of its peak are NaN


def transmission_frame(result, k):
    """Transmission of the fitted source in exposure k on its canonical cutout: per pixel,
    the source's light there through the exposure's open shutters (the model image, at its
    fitted centre and width) over its light with no MSA (``Optics.without_msa``, every
    shutter of the range open). The slit-direction part only: the along-dispersion losses
    and the throughput are not in it, and the source's own spectrum cancels. One where
    the shutters take nothing, below one at the bars and the slitlet's edges and beyond its
    ends. It can exceed one in a source's faint far wing (up to ~1.4 for a compact source
    one shutter away), where the shutters' diffraction spreads light from its bright core
    outward. NaN off the frame and where the source puts almost no light
    (``T_MIN_LIGHT`` of its peak in the frame). ``data / T`` is
    then the 2-D as the source would look with no MSA."""
    from campfire_pipeline.nirspec import optics as O
    M = result.model
    e = M.exps[k]
    p, eoff = _qvec(result)
    r0, c0, ny, nx = e.cutout
    sl = (slice(r0, r0 + ny), slice(c0, c0 + nx))
    m = np.isfinite(e.y) & np.isfinite(e.wav)
    out = np.full(e.y.shape, np.nan)
    y, lam = e.y[m], e.wav[m]
    c, w = M.centre(p, eoff, k, lam), M.image_width(p, k, lam)
    num = M.optics[k].image(e.open_k, y, lam, c, w)
    den = M.optics[k].without_msa().image(O.ALL_OPEN, y, lam, c, w)
    ok = den > T_MIN_LIGHT * np.max(den, initial=0.0)
    out[m] = np.where(ok, num / np.where(ok, den, 1.0), np.nan)
    return out[sl]


def drizzle_transmission(result, dmap):
    """``transmission_frame`` of every exposure drizzled through the product's pixel maps
    (``drizzle_arrays``): the exposures' transmissions averaged with the data's weights. The
    drizzle takes a NaN as zero, so pixels where the source puts almost no light come out
    near zero rather than NaN."""
    by_name = {e.name: k for k, e in enumerate(result.model.exps)}
    return drizzle_arrays(lambda name: transmission_frame(result, by_name[name]) if name in by_name else None,
                          dmap)


FIT_VERSION = '12'                  # 2: no per-exposure pointing offsets fitted; 3: each nod at its planned position; 4: three-component smooth PSF; 5: geometric shutter with smooth transmission; 6: every disperser through the diffraction model (optics); 7: the observation's bar offset from its background (bar_offset); 8: exact shutter edges in the optics tables (optics engine 2); 9: a SRCYPOS placeholder found with the bar offset applied; 10: no detection gate and fits on a bound kept (profile_chain), the master-background scale term (mbkg_extraction); 11: the reported flux is the light that entered the shutter across the dispersion (no 0.8" circle); 12: the source's width stretched along the slit by the trace-centre law as its position is, the shape fit's derivatives at steps of 1e-4 and stopped at a chi2 change of 0.01 (FIT_DIFF_STEP, FIT_DCHI2)

