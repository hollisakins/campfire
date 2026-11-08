"""Tests for the forward-model ("profile fit") NIRSpec extraction.

Synthetic three-nod slitlets on a common detector frame: the slitlet is fixed on
the detector and the source moves one shutter between nods, as in a real
3-shutter nod pattern. Data are generated with the module's own image model (the
diffraction model's closed-form stand-in, ``optics_standin``) from a known shape and
spectrum, so these tests lock the solver, the priors and the nod bookkeeping; the
physics is validated on real data separately.
"""

import numpy as np
import pytest

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec.constants import MSA_SLIT_MARGIN, MSA_SLIT_PITCH
from campfire_pipeline.nirspec.stage2 import nod_background_partners

DY = 0.227            # slit units per detector row
NY, NX = 30, 160


def _exposures(src_y=0.1, pl=0.8, det='NRS1'):
    """Three nods of one 3-shutter slitlet: source in the middle, bottom and top shutter."""
    rows = np.arange(NY)[:, None] * np.ones((1, NX))
    lam = np.linspace(0.8, 5.0, NX)[None, :] * np.ones((NY, 1)) + 0.002 * (rows - NY / 2)
    r_mid = NY / 2
    shift = MSA_SLIT_PITCH / DY
    setups = [((-1, 0, 1), r_mid), ((0, 1, 2), r_mid - shift), ((-2, -1, 0), r_mid + shift)]
    exps = []
    for i, (open_k, r_src) in enumerate(setups):
        y = (rows - r_src) * DY
        exps.append(PF.Exposure(
            name=f'jw00000001001_03101_0000{i + 1}_nrs1_1.fits', group=0, detector=det, grating='PRISM/CLEAR',
            quadrant=1, diff=np.zeros((NY, NX)), var=np.ones((NY, NX)), ok=np.ones((NY, NX), bool),
            wav=lam.copy(), y=y, pl_applied=np.full((NY, NX), pl), pl_point=np.full((NY, NX), pl),
            src_y=src_y, open_k=open_k, partners=[j for j in range(3) if j != i], use_data=True,
            cutout=(0, 0, NY, NX)))
    return exps


def _spectrum(lam):
    return 1.0 + 0.5 * np.sin(lam * 2.0) + 0.3 * lam


def _inject(exps, params, eoff, flux_scale, noise, seed=1):
    """Fill the nod differences from a known shape and spectrum. Noise is drawn per
    frame and differenced like the data, so the differences share their partners'
    noise; ``var`` is the frame variance, as stage 2b leaves it."""
    R = _truth_result(exps, params, eoff, flux_scale)
    rng = np.random.default_rng(seed)
    frame_noise = [noise * rng.normal(size=(NY, NX)) for _ in exps]
    for k, e in enumerate(exps):
        m = PF.model_frame(R, k, applied_pathloss=False)
        e.diff = m + frame_noise[k] - np.mean([frame_noise[j] for j in e.partners], axis=0)
        e.var = np.full(m.shape, noise ** 2)
    return R


def _truth_result(exps, params, eoff, flux_scale):
    M = PF.ProfileModel(exps, chromatic=True)
    lam = M.lam_grid
    return PF.FitResult(params=dict(params, pivot=M.pivot, centre_prior=M.c_prior), eoff=np.asarray(eoff, float),
                        lam=lam, flux=flux_scale * _spectrum(lam), flux_err=np.zeros_like(lam),
                        chi2=0.0, npix=0, nfree=0, chromatic=True, model=M)


TRUTH = dict(centre=0.15, drift=0.02, width=0.2, width_change=-0.04)


# ---------------------------------------------------------------------------
# geometry and bookkeeping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('state,n_below,n_above,expect', [
    ('1x1', 1, 1, (-1, 0, 1)),
    ('x11', 0, 2, (0, 1, 2)),
    ('11x', 2, 0, (-2, -1, 0)),
    ('x1', 0, 1, (0, 1)),                      # two-shutter slitlet
    ('11x11', 2, 2, (-2, -1, 0, 1, 2)),        # five-shutter slitlet
    ('10x11', 2, 2, (-2, 0, 1, 2)),            # closed shutter inside the slitlet
    ('x', 0, 0, (0,)),
])
def test_open_shutters_from_jwst_bounds(state, n_below, n_above, expect):
    ymin = -(n_below * MSA_SLIT_PITCH + MSA_SLIT_MARGIN)
    ymax = n_above * MSA_SLIT_PITCH + MSA_SLIT_MARGIN
    assert PF.open_shutters(state, ymin, ymax) == expect


def test_nod_partners_default_and_overrides():
    files = [f'jw01_03101_0000{i}_nrs1_7.fits' for i in (1, 2, 3)]
    assert nod_background_partners(files) == [[1, 2], [0, 2], [0, 1]]
    assert nod_background_partners(files, {'2': [3]}) == [[1, 2], [2], [0, 1]]
    assert nod_background_partners(files, {'1': []}) == [None, [0, 2], [0, 1]]


def test_model_nod_differences_sum_to_zero():
    """One sky seen at three positions: the three model differences cancel at every
    pixel, including with per-exposure pointing offsets."""
    exps = _exposures()
    R = _truth_result(exps, TRUTH, [0.0, 0.01, -0.02], 1.0)
    frames = [PF.model_frame(R, k, applied_pathloss=False) for k in range(3)]
    total = np.nansum(frames, axis=0)
    scale = np.nanmax(np.abs(frames[0]))
    assert np.nanmax(np.abs(total)) < 1e-3 * scale


def test_transmission_point_source_centred():
    """A centred point source through the 3-shutter aperture loses only its far wings."""
    M = PF.ProfileModel(_exposures(src_y=0.0), chromatic=False)
    lam = np.array([1.0, 3.0])
    t = M.transmission(np.array([0.0, 0.0, np.log(0.15), 0.0]), np.zeros(3), 0, lam, np.full(2, DY),
                       width=0.0, centre=0.0, aperture=(0,))
    assert np.all((t > 0.85) & (t < 1.0))


def test_one_disperser_per_fit(monkeypatch):
    a, b = _exposures(), _exposures()
    for e in b:
        e.grating = 'G395M/F290LP'
    monkeypatch.setattr(PF, 'load_group', lambda files, **kw: a if files == 'a' else b)
    with pytest.raises(ValueError, match='one disperser'):
        PF.load_source(['a', 'b'])


# ---------------------------------------------------------------------------
# fitting
# ---------------------------------------------------------------------------
def test_recovers_shape_and_spectrum_at_high_snr():
    exps = _exposures()
    _inject(exps, TRUTH, [0.0, 0.0, 0.0], flux_scale=200.0, noise=1.0)   # the nods land where commanded
    R = PF.fit(exps, chromatic=True)
    assert abs(R.params['centre'] - TRUTH['centre']) < 0.02
    assert abs(R.params['width'] - TRUTH['width']) < 0.02
    assert abs(R.params['width_change'] - TRUTH['width_change']) < 0.02
    good = np.isfinite(R.flux) & (R.lam > 1.0) & (R.lam < 4.8)
    ratio = R.flux[good] / (200.0 * _spectrum(R.lam[good]))
    assert abs(np.median(ratio) - 1) < 0.01
    pull = (R.flux[good] - 200.0 * _spectrum(R.lam[good])) / R.flux_err[good]
    assert 0.7 < np.std(pull) < 1.3                 # errors describe the scatter
    assert 0.9 < R.chi2_red < 1.1


def _long_slitlet_exposures(half=3, ny=56):
    """Three nods of a (2 half + 1)-shutter slitlet on a common frame, y NaN outside each
    exposure's slit (+-half pitches + margin), as jwst leaves the rows beyond a slit's ends."""
    rows = np.arange(ny)[:, None] * np.ones((1, NX))
    lam = np.linspace(0.8, 5.0, NX)[None, :] * np.ones((ny, 1)) + 0.002 * (rows - ny / 2)
    shift = MSA_SLIT_PITCH / DY
    open_k = tuple(range(-half, half + 1))
    edge = half * MSA_SLIT_PITCH + MSA_SLIT_MARGIN
    exps = []
    for i, r_src in enumerate((ny / 2, ny / 2 - shift, ny / 2 + shift)):
        y = (rows - r_src) * DY
        y = np.where(np.abs(y + (r_src - ny / 2) * DY) <= edge, y, np.nan)
        exps.append(PF.Exposure(
            name=f'jw00000001001_03101_0000{i + 1}_nrs1_1.fits', group=0, detector='NRS1', grating='PRISM/CLEAR',
            quadrant=1, diff=np.zeros((ny, NX)), var=np.ones((ny, NX)), ok=np.isfinite(y), wav=lam.copy(), y=y,
            pl_applied=np.full((ny, NX), 0.8), pl_point=np.full((ny, NX), 0.8), src_y=0.1, open_k=open_k,
            partners=[j for j in range(3) if j != i], use_data=True, cutout=(0, 0, ny, NX)))
    return exps


def test_long_slitlet_row_height_at_slit_ends():
    """The row height is defined on every row of the slit, its first and last included (a
    central difference there reaches outside the slit), and equals the row pitch."""
    for e in _long_slitlet_exposures():
        inside = np.isfinite(e.y)
        assert np.all(np.isfinite(e.dy[inside]))
        assert np.allclose(e.dy[inside], DY)


def test_long_slitlet_fit():
    """A seven-shutter slitlet (one long open column, as in program 4465): the partner terms
    reach the slit's end rows and the fit still recovers the shape and the spectrum."""
    exps = _long_slitlet_exposures()
    R0 = _truth_result(exps, TRUTH, [0.0, 0.0, 0.0], 200.0)
    rng = np.random.default_rng(5)
    noise = [rng.normal(size=exps[0].y.shape) for _ in exps]
    for k, e in enumerate(exps):
        e.diff = PF.model_frame(R0, k, applied_pathloss=False) + noise[k] - np.mean([noise[j] for j in e.partners], axis=0)
    R = PF.fit(exps, chromatic=True)
    assert abs(R.params['centre'] - TRUTH['centre']) < 0.02
    assert abs(R.params['width'] - TRUTH['width']) < 0.02
    good = np.isfinite(R.flux) & (R.lam > 1.0) & (R.lam < 4.8)
    assert abs(np.median(R.flux[good] / (200.0 * _spectrum(R.lam[good]))) - 1) < 0.01


def test_faint_source_falls_back_to_prior_shape():
    """With little signal the shape settles on the prior: a compact source at the
    planned position, not a wide or displaced one."""
    exps = _exposures(src_y=0.1)
    _inject(exps, dict(centre=0.1, drift=0.0, width=0.15, width_change=0.0), [0, 0, 0], flux_scale=0.3, noise=1.0, seed=3)
    R = PF.fit(exps, chromatic=True)
    pr = PF.Priors()
    assert abs(np.log(R.params['width'] / pr.width_median)) < 1.0 * pr.width_log_sigma
    assert abs(R.params['centre'] - 0.1) < 2 * pr.centre_sigma


def test_no_signal_gives_zero_flux_within_errors():
    exps = _exposures()
    _inject(exps, TRUTH, [0, 0, 0], flux_scale=0.0, noise=1.0, seed=5)
    R = PF.fit(exps, chromatic=False)
    good = np.isfinite(R.flux)
    chi = R.flux[good] / R.flux_err[good]
    assert abs(np.mean(chi)) < 0.2 and 0.8 < np.std(chi) < 1.2


def test_outlier_pixels_are_masked():
    exps = _exposures()
    _inject(exps, TRUTH, [0, 0, 0], flux_scale=50.0, noise=1.0, seed=7)
    exps[1].diff[NY // 2, 40:43] += 500.0            # cosmic-ray-like hits
    R = PF.fit(exps, chromatic=False)
    assert (~R.model.use).sum() >= 3
    good = np.isfinite(R.flux) & (R.lam > 1.0) & (R.lam < 4.8)
    assert abs(np.median(R.flux[good] / (50.0 * _spectrum(R.lam[good]))) - 1) < 0.03


def test_reported_factor_is_one_for_a_point_source_anywhere():
    """The reported flux is the source's light inside REPORT_APERTURE: all of a point
    source's, wherever it sits in the shutter."""
    for y in (0.0, 0.3):
        exps = _exposures(src_y=y)
        R = _truth_result(exps, dict(centre=y, drift=0.0, width=PF.W_FLOOR, width_change=0.0), [0, 0, 0], 1.0)
        assert np.allclose(PF.reported_factor(R, np.array([1.0, 2.0, 4.0])), 1.0)


def test_reported_factor_bounds_a_wide_source():
    """A round Gaussian of sigma s arcsec keeps 1 - exp(-R^2 / 2 s^2) inside the circle of
    radius R = REPORT_APERTURE / 2, and the share falls as the source widens."""
    from campfire_pipeline.nirspec.optics import SLIT
    exps = _exposures()
    lam = np.array([1.0, 2.0, 4.0])
    shares = []
    for w in (0.1, 0.4, 1.5):
        R = _truth_result(exps, dict(centre=0.0, drift=0.0, width=w, width_change=0.0), [0, 0, 0], 1.0)
        f = PF.reported_factor(R, lam)
        s = w * SLIT
        assert np.allclose(f, 1.0 - np.exp(-(0.5 * PF.REPORT_APERTURE) ** 2 / (2 * s ** 2)))
        shares.append(f[0])
    assert shares[0] > 0.99 > shares[1] > shares[2] > 0.1


def test_boxcar_factor_is_what_the_model_frames_hold():
    """``boxcar_factor`` per unit true spectrum is the three rows around the planned
    position of the model frames in the s2d's units (divided by the path loss stage 2b
    applied), so a boxcar turned into a true spectrum is placed back at the same level.
    The applied path loss differs from jwst's point loss here, and the source sits off
    centre, so a factor on any other calibration would not match."""
    rows = np.arange(NY)[:, None] * np.ones((1, NX))
    exps = _exposures(src_y=DY, pl=0.8)
    for e in exps:
        r_src = np.round(-e.y[0, 0] / DY)
        e.y = (rows - r_src) * DY                # rows land on the planned position
        e.wav = np.repeat(e.wav[NY // 2][None, :], NY, axis=0)
        e.pl_applied = np.full((NY, NX), 0.6)
        e.partners = []
    R = _truth_result(exps, dict(centre=DY, drift=0.0, width=0.2, width_change=0.0), [0, 0, 0], 1.0)
    lam = exps[0].wav[0, 20:-20]
    spec = np.interp(lam, R.lam, R.flux)
    want = []
    for k, e in enumerate(exps):
        frame = PF.model_frame(R, k, applied_pathloss=True)
        r0 = int(np.argmin(np.abs(e.y[:, 0] - e.src_y)))
        want.append(frame[r0 - 1:r0 + 2, 20:-20].sum(axis=0) / spec)
    assert np.allclose(PF.boxcar_factor(R, lam), np.mean(want, axis=0), rtol=1e-6)


# ---------------------------------------------------------------------------
# frames without the source (SRCFLUX = F)
# ---------------------------------------------------------------------------
def _frames_from_truth(exps, R):
    """Each exposure's positive frame (source light only), evaluated directly with the
    image model and the throughput, independently of the nod bookkeeping: zero for a
    frame that does not contain the source."""
    M = R.model
    p = np.array([R.params['centre'], R.params['drift'], np.log(R.params['width']), R.params['width_change']])
    out = []
    for j, e in enumerate(exps):
        F = np.zeros(e.y.shape)
        if e.has_source:
            spec = np.interp(e.wav, R.lam, R.flux)
            F = spec * M.throughput(p, R.eoff, j, e.wav, e.dy) * M.image(p, R.eoff, j, e.y, e.wav, e.dy)
        out.append(F)
    return out


def _placeholder_set():
    """Three nods of a 2-shutter-like set where nod 3 does not contain the source: its
    slit frame is jwst's placeholder, centred on a shutter shared with nod 2."""
    exps = _exposures(src_y=0.05)
    ph = exps[2]
    ph.y = exps[1].y.copy()
    ph.open_k = exps[1].open_k
    ph.src_y = 0.0
    ph.has_source = False
    ph.use_data = False          # stage 3 drops SRCFLUX = F nods from the science set
    return exps


def test_partner_without_source_contributes_nothing():
    """Regression (review blocker 1): a partner frame that does not contain the source
    must not put a negative copy of the source on the other nods' differences."""
    exps = _placeholder_set()
    truth = dict(centre=0.05, drift=0.0, width=0.15, width_change=0.0)
    R = _truth_result(exps, truth, [0, 0, 0], 100.0)
    frames = _frames_from_truth(exps, R)
    assert np.all(frames[2] == 0)
    for k in (0, 1):
        expect = frames[k] - np.mean([frames[j] for j in exps[k].partners], axis=0)
        got = PF.model_frame(R, k, applied_pathloss=False)
        inside = np.ones(got.shape, bool)
        for j, _ in PF.term_weights(exps, k):          # the model spectrum is zero off its grid
            inside &= (exps[j].wav > R.lam[0]) & (exps[j].wav < R.lam[-1])
        scale = np.nanmax(np.abs(expect))
        assert np.nanmax(np.abs(got - expect)[inside]) < 1e-6 * scale
    assert PF.term_weights(exps, 0) == [(0, 1.0), (1, -0.5)]
    assert PF.term_weights(exps, 2) == [(0, -0.5), (1, -0.5)]


def test_fit_recovers_flux_when_a_partner_lacks_the_source():
    exps = _placeholder_set()
    truth = dict(centre=0.05, drift=0.0, width=0.15, width_change=0.0)
    R = _truth_result(exps, truth, [0, 0, 0], 100.0)
    frames = _frames_from_truth(exps, R)
    rng = np.random.default_rng(11)
    noise = [rng.normal(size=(NY, NX)) for _ in exps]
    for k, e in enumerate(exps):
        e.diff = frames[k] + noise[k] - np.mean([frames[j] + noise[j] for j in e.partners], axis=0)
        e.var = np.ones((NY, NX))
    F = PF.fit(exps, chromatic=False)
    good = np.isfinite(F.flux) & (F.lam > 1.0) & (F.lam < 4.8)
    assert abs(np.median(F.flux[good] / (100.0 * _spectrum(F.lam[good]))) - 1) < 0.02
    assert F.chi2_red < 1.2
    # the old behaviour (the placeholder frame treated as containing the source) cannot fit these
    # data: chi2/dof ~1.7 over ~3000 pixels
    exps[2].has_source = True
    B = PF.fit(exps, chromatic=False)
    assert B.chi2_red > 1.5 * F.chi2_red


# ---------------------------------------------------------------------------
# nod geometry (NOD_OFFSET_LAW): each exposure at its own planned position
# ---------------------------------------------------------------------------
def _nodded(src=(0.10, 0.115, 0.085)):
    """The three nods with planned positions that differ between nods, as the MSA planning
    metadata gives them (the nod step projected on the MSA varies across the field)."""
    exps = _exposures(src_y=src[0])
    for e, s in zip(exps, src):
        e.src_y = s
    return exps


def test_nod_shifts_follow_each_exposures_planned_position():
    exps = _nodded()
    assert np.allclose(PF.nod_shifts(exps), [0.0, 0.015, -0.015])
    assert np.allclose(PF.nod_shifts(exps, law=False), 0.0)
    assert np.allclose(PF.nod_shifts(exps, law=dict(scale=2.0)), [0.0, 0.03, -0.03])
    exps[2].src_y = 0.0                      # a placeholder next to real values: no shift
    assert PF.nod_shifts(exps)[2] == 0.0
    exps[1].has_source = False               # a frame without the source: no shift
    assert PF.nod_shifts(exps)[1] == 0.0


def test_a_placeholder_is_found_with_a_bar_offset_applied():
    """``src_y`` carries the bar offset, so a placeholder reads the offset there; it is found
    on the SRCYPOS jwst wrote (``src_y_jwst``)."""
    from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH
    bar = 0.012
    exps = _nodded()
    for e in exps:
        e.src_y_jwst = e.src_y / MSA_SLIT_PITCH
        e.src_y = e.src_y + bar
    exps[2].src_y_jwst, exps[2].src_y = 0.0, bar           # the placeholder, moved by the offset
    c, flag = PF.planned_position(exps)
    assert flag == 1 and np.isclose(c, np.median([0.10, 0.115]) + bar)
    assert PF.nod_shifts(exps)[2] == 0.0


def test_a_single_exposure_is_not_shifted():
    """With one exposure (no nod partners) the reference is its own planned position: the
    law changes nothing."""
    e = _nodded()[1]
    e.partners = []
    assert PF.nod_shifts([e])[0] == 0.0
    M_on, M_off = PF.ProfileModel([e]), PF.ProfileModel([e], nod_law=False)
    p = np.array([0.115, 0.0, np.log(0.1), 0.0])
    assert np.allclose(M_on.design(p, np.zeros(1)).toarray(), M_off.design(p, np.zeros(1)).toarray())


def test_partner_copies_move_with_their_own_exposure():
    """Exposure k's model difference is its own frame minus its partners' frames, each at its
    own planned position, and the law moves an exposure's image by its shift."""
    exps = _nodded()
    R = _truth_result(exps, TRUTH, [0, 0, 0], 100.0)
    own = [PF.frame_image(R, j) for j in range(3)]
    for k in range(3):
        expect = own[k] - np.mean([own[j] for j in exps[k].partners], axis=0)
        got = PF.model_frame(R, k, applied_pathloss=False, common=True)
        inside = np.isfinite(got) & np.isfinite(expect)
        for j, _ in PF.term_weights(exps, k):
            inside &= (exps[j].wav > R.lam[0]) & (exps[j].wav < R.lam[-1])
        assert np.nanmax(np.abs(got - expect)[inside]) < 1e-6 * np.nanmax(np.abs(expect))
    p = np.array([TRUTH['centre'], 0.0, np.log(TRUTH['width']), 0.0])
    lam = np.array([1.5, 3.0])
    M_off = PF.ProfileModel(exps, nod_law=False)
    for k, s in enumerate([0.0, 0.015, -0.015]):
        dc = R.model.centre(p, np.zeros(3), k, lam) - M_off.centre(p, np.zeros(3), k, lam)
        assert np.allclose(dc, s * (1 + PF.centre_law(lam, 'NRS1', 1)[1]))


def test_known_nod_law_is_fitted_and_needed():
    """Data made with the nods at their planned positions: the model with the law fits them
    (chi2 ~ 1, centre recovered); one shared position cannot."""
    exps = _nodded()
    truth = dict(centre=0.10, drift=0.0, width=0.05, width_change=0.0)
    _inject(exps, truth, [0, 0, 0], flux_scale=300.0, noise=1.0, seed=21)
    R = PF.fit(exps, chromatic=False)
    assert 0.9 < R.chi2_red < 1.1
    assert abs(R.params['centre'] - 0.10) < 0.005
    B = PF.fit(exps, chromatic=False, model=PF.ProfileModel(exps, chromatic=False, nod_law=False))
    assert B.chi2_red > 1.2 * R.chi2_red
