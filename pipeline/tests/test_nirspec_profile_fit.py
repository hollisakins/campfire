"""Tests for the forward-model ("profile fit") NIRSpec extraction.

Synthetic three-nod slitlets on a common detector frame: the slitlet is fixed on
the detector and the source moves one shutter between nods, as in a real
3-shutter nod pattern. Data are generated with the module's own image model from a
known shape and spectrum, so these tests lock the solver, the priors and the
nod bookkeeping; the physics is validated on real data separately.
"""

import json
from pathlib import Path

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


def test_main_shutter_factor_is_one_for_centred_point_like_source():
    exps = _exposures(src_y=0.0)
    R = _truth_result(exps, dict(centre=0.0, drift=0.0, width=PF.W_FLOOR, width_change=0.0), [0, 0, 0], 1.0)
    f = PF.main_shutter_factor(R, np.array([1.0, 2.0, 4.0]))
    assert np.allclose(f, 1.0, atol=0.03)


def test_main_shutter_factor_ignores_an_exposure_without_usable_pixels():
    """An exposure with every pixel masked used to make the factor NaN (its row scale
    was the median of nothing), which switched off the pass-1 guard for that source."""
    exps = _exposures(src_y=0.1)
    R = _truth_result(exps, TRUTH, [0, 0, 0], 1.0)
    lam = np.array([1.0, 2.0, 4.0])
    f_all = PF.main_shutter_factor(R, lam)
    exps[1].ok[:] = False
    f = PF.main_shutter_factor(R, lam)
    assert np.all(np.isfinite(f))
    assert np.allclose(f, f_all, rtol=0.05)
    for e in exps:                                   # nothing usable anywhere: still finite
        e.ok[:] = False
    assert np.all(np.isfinite(PF.main_shutter_factor(R, lam)))


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
    # the old behaviour (the placeholder frame treated as containing the source) cannot fit these data
    exps[2].has_source = True
    B = PF.fit(exps, chromatic=False)
    assert B.chi2_red > 2 * F.chi2_red


# ---------------------------------------------------------------------------
# general PSF (psf_components): the shipped table, read and written out independently
# ---------------------------------------------------------------------------
PSF_FILE = Path(PF.__file__).parent.parent / 'data' / 'nirspec_profile_psf.json'


def _image_written_out(M, p, k, y, lam, dy):
    """The image formula for the shipped PSF file, written out: per component, the
    galaxy convolved with half its variance before the shutter, clipped by the open
    shutters, blurred by the other half after it; offsets before or after the shutter;
    the component without a share takes the remainder."""
    raw = json.loads(PSF_FILE.read_text())[M.exps[k].detector]
    comps = []
    for c in raw['components']:
        comps.append(dict(sigma=np.interp(lam, raw['lam'], c['sigma']),
                          frac=np.interp(lam, raw['lam'], c['frac']) if 'frac' in c else None,
                          offset=np.interp(lam, raw['lam'], c['offset']) if 'offset' in c else 0.0,
                          pre=c.get('pre', True)))
    rest = 1.0 - sum(c['frac'] for c in comps if c['frac'] is not None)
    c0 = M.centre(p, np.zeros(len(M.exps)), k, lam)
    w = M.width(p, lam)
    iv = PF.open_intervals(M.exps[k].open_k)
    acc = 0.0
    for xi, wi in zip(PF._GLX, PF._GLW):
        yy = y + 0.5 * dy * xi
        for c in comps:
            half = c['sigma'] * dy / np.sqrt(2.0)
            off = c['offset'] * dy
            fr = rest if c['frac'] is None else c['frac']
            if c['pre']:
                acc = acc + wi * fr * PF._clipblur(yy, c0 + off, np.sqrt(half ** 2 + w ** 2), half, iv)
            else:
                acc = acc + wi * fr * PF._clipblur(yy - off, c0, np.sqrt(half ** 2 + w ** 2), half, iv)
    return 0.5 * dy * acc


def test_default_psf_is_the_shipped_table():
    exps = _exposures(src_y=0.12)
    M = PF.ProfileModel(exps)
    p = np.array([0.12, 0.01, np.log(0.08), 0.0])
    for k in range(3):
        e = exps[k]
        y, lam, dy = e.y.ravel(), e.wav.ravel(), e.dy.ravel()
        assert np.allclose(M.image(p, np.zeros(3), k, y, lam, dy), _image_written_out(M, p, k, y, lam, dy),
                           rtol=1e-12, atol=1e-15)
    # three components, the wing and the shoulder displaced after the shutter, over 0.6-5.3 um
    comps = PF.psf_components(np.array([0.6, 2.0, 5.3]), 'NRS2')
    assert len(comps) == 3 and [c['pre'] for c in comps[1:]] == [False, False]
    assert np.allclose(sum(c['frac'] for c in comps), 1.0)
    for det in ('NRS1', 'NRS2'):
        assert PF.PSF_TABLES[det]['lam'][0] <= 0.6 and PF.PSF_TABLES[det]['lam'][-1] >= 5.3


def test_general_psf_components_offsets_and_split():
    """A component's offset moves its light; before the shutter it is clipped there, after
    it the light lands beyond the shutter edge."""
    exps = _exposures(src_y=0.0)
    p = np.array([0.0, 0.0, np.log(PF.W_FLOOR), 0.0])
    one = lambda off, pre: {'NRS1': dict(lam=[0.5, 6.0], components=[dict(sigma=0.4, offset=off, pre=pre)])}
    lam = np.full(400, 2.0)
    dy = np.full(400, DY)
    y = np.linspace(-2.0, 2.0, 400)
    for pre in (True, False):
        M = PF.ProfileModel(exps, psf_tables=one(1.0, pre))
        im = M.image(p, np.zeros(3), 0, y, lam, dy)
        assert abs(np.sum(im * y) / np.sum(im) - 1.0 * DY) < 0.01      # light centred one row towards +y
    # a source at the top edge of its shutter: an offset before the shutter is clipped, after it is not
    t_pre = PF.ProfileModel(exps, psf_tables=one(1.5, True)).transmission(
        np.array([0.4, 0.0, np.log(PF.W_FLOOR), 0.0]), np.zeros(3), 0, lam[:1], dy[:1], aperture=(0,), centre=0.4)
    t_post = PF.ProfileModel(exps, psf_tables=one(1.5, False)).transmission(
        np.array([0.4, 0.0, np.log(PF.W_FLOOR), 0.0]), np.zeros(3), 0, lam[:1], dy[:1], aperture=(0,), centre=0.4)
    assert t_pre[0] < t_post[0]
    comps = PF.psf_components(np.array([1.0, 3.0]), 'NRS1', {'NRS1': dict(lam=[0.6, 5.3], components=[
        dict(sigma=[0.3, 0.6]), dict(sigma=1.0, frac=[0.2, 0.4], offset=0.5, pre=False)])})
    assert np.allclose(comps[0]['frac'] + comps[1]['frac'], 1.0)
    assert comps[1]['pre'] is False and np.allclose(comps[1]['offset'], 0.5)


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
