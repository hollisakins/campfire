"""The master-background spectrum (``mbkg_extraction``): the frames in memory, their
outlier flags and 2-D through the nodded pixel maps, the single-frame exposures and fit
at a fixed shape, the self-subtraction share, the ``MBKGPREF`` / ``MBKGWHY`` rule and the
``_spec.fits`` extensions (``stage3.opt_ext_single_source``)."""

import os

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.metadata.science_hash import science_hash
from campfire_pipeline.nirspec import mbkg_extraction as MX
from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import stage3 as S3

from test_nirspec_profile_fit import NX, NY, TRUTH, _exposures, _spectrum, _truth_result


# ---------------------------------------------------------------------------
# the fit on single frames
# ---------------------------------------------------------------------------
def _master_background(e, level=50.0):
    """A master background on exposure ``e``'s pixels (units of ``diff``): rising with
    wavelength, darker towards the shutter edges (the bars' shadow)."""
    from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH
    edge = (1.0 - np.cos(2 * np.pi * e.y / MSA_SLIT_PITCH)) / 2.0         # 0 at shutter centres, 1 at the bars
    return level * (1.0 + 0.1 * e.wav) * (1.0 - 0.5 * edge ** 4)


def _single_frames(params, flux_scale, noise, seed=2, src_y=0.1, mbkg_error=0.0):
    """The three exposures of ``_exposures`` as single frames (nothing subtracted), each
    the source's own light plus noise, and the truth. Each carries its master background
    (``Exposure.mbkg``, ``_master_background``); ``mbkg_error`` of it is left in the frame,
    as when the master background subtracted was that much too faint in the slitlet."""
    exps = _exposures(src_y=src_y)
    for e in exps:
        e.partners = []
    R = _truth_result(exps, params, [0.0, 0.0, 0.0], flux_scale)
    rng = np.random.default_rng(seed)
    for k, e in enumerate(exps):
        e.mbkg = _master_background(e)
        e.diff = (PF.model_frame(R, k, applied_pathloss=False) + mbkg_error * e.mbkg
                  + noise * rng.normal(size=(NY, NX)))
        e.var = np.full((NY, NX), noise ** 2)
    return exps, R


def _reported_ratio(R, truth, lo=1.0, hi=4.8):
    """Summed reported flux of ``R`` over the truth's (same grid)."""
    g = np.isfinite(R.flux) & (R.lam > lo) & (R.lam < hi)
    rep = lambda X: float(np.sum((X.flux * PF.reported_factor(X, X.lam))[g]))     # noqa: E731
    return rep(R) / rep(truth)


def test_single_frames_at_a_fixed_shape_recover_the_spectrum():
    exps, truth = _single_frames(TRUTH, flux_scale=200.0, noise=1.0)
    M = PF.ProfileModel(exps, chromatic=True)
    assert np.all(M.dof_weight() == 1.0)                      # one independent value per pixel
    np.testing.assert_array_equal(M.diff_variance(0), exps[0].var)
    shape = dict(centre_offset=TRUTH['centre'] - M.c_prior, drift=TRUTH['drift'], width=TRUTH['width'],
                 width_change=TRUTH['width_change'])
    R = PF.fit_fixed(model=M, shape=shape)
    good = np.isfinite(R.flux) & (R.lam > 1.0) & (R.lam < 4.8)
    assert abs(np.median(R.flux[good] / (200.0 * _spectrum(R.lam[good]))) - 1) < 0.01
    pull = (R.flux[good] - 200.0 * _spectrum(R.lam[good])) / R.flux_err[good]
    assert 0.7 < np.std(pull) < 1.3
    assert R.params['centre'] == pytest.approx(TRUTH['centre'])


def test_self_subtraction_is_small_for_a_compact_source_and_large_for_a_wide_one():
    exps = _exposures(src_y=0.0)
    compact = _truth_result(exps, dict(centre=0.0, drift=0.0, width=PF.W_FLOOR, width_change=0.0), [0, 0, 0], 1.0)
    wide = _truth_result(exps, dict(centre=0.0, drift=0.0, width=1.2, width_change=0.0), [0, 0, 0], 1.0)
    assert MX.self_subtraction(compact, exps) < 0.02
    assert MX.self_subtraction(wide, exps) > 0.10


def _nodded_choice(exps, width, mbkg_sigma=0.01):
    """What ``fit_product`` hands ``own_shape``: the nodded chain's shape (here a given width at
    the truth's centre) refitted on the single frames with the master-background scale term,
    and its ``ChainResult``."""
    from campfire_pipeline.nirspec import profile_chain as PC
    M = PF.ProfileModel(exps, chromatic=True, mbkg_sigma=mbkg_sigma)
    shape = dict(centre_offset=TRUTH['centre'] - M.c_prior, drift=0.0, width=width, width_change=0.0)
    R0 = PF.fit_fixed(model=M, shape=shape)
    return R0, shape, PC.ChainResult(result=R0, shape='own', chain='gated', reason='own_fit')


def test_the_master_background_frames_fit_a_wide_source_wider_than_the_nodded_shape():
    wide = dict(TRUTH, width=0.9, width_change=0.0)
    exps, _ = _single_frames(wide, flux_scale=200.0, noise=1.0)
    R0, shape0, chain = _nodded_choice(exps, 0.4)
    R, shape, C = MX.own_shape(R0, shape0, chain, {})
    assert (C.shape, C.reason) == ('own', 'mbkg_own')
    assert R.params['width'] == pytest.approx(0.9, rel=0.1)
    assert shape['width'] == R.params['width']
    cards = C.header_cards()
    assert cards['SRCWIDTH'] == R.params['width']
    assert C.snr_det > 0
    # the scale term ran, found nothing to take up, and is in the cards
    assert abs(R.mbkg_scale) < 3 * R.mbkg_scale_err and 0 < R.mbkg_scale_err < 0.01
    assert (cards['MBKGSCL'], cards['MBKGSCLE']) == (R.mbkg_scale, R.mbkg_scale_err)


def test_a_faint_source_takes_its_own_fit_too():
    """No detection gate (user, 2026-11-10): a faint source takes its own fit on these
    frames, the prior deciding its shape."""
    exps, _ = _single_frames(TRUTH, flux_scale=0.3, noise=1.0)
    R0, shape0, chain = _nodded_choice(exps, 0.2)
    R, shape, C = MX.own_shape(R0, shape0, chain, {})
    assert R is not R0 and C.shape == 'own' and C.reason.split(';')[0] == 'mbkg_own'
    assert np.isfinite(C.snr_det) and 0.05 < R.params['width'] < 0.45


def test_the_fixed_chain_keeps_the_nodded_shape():
    exps, _ = _single_frames(TRUTH, flux_scale=200.0, noise=1.0)
    R0, shape0, chain = _nodded_choice(exps, 0.2)
    R, _, C = MX.own_shape(R0, shape0, chain, {'profile_fit_shape': 'fixed'})
    assert R is R0 and C.reason == 'nodded;own_fit'


def test_a_width_on_its_bound_is_kept_and_flagged():
    """A source wider than the width bound: the fit on its bound is kept, flagged (until
    2026-11-10 it fell back to the nodded shape)."""
    wide = dict(TRUTH, width=0.9, width_change=0.0)
    exps, _ = _single_frames(wide, flux_scale=200.0, noise=1.0)
    R0, shape0, chain = _nodded_choice(exps, 0.4)
    R, _, C = MX.own_shape(R0, shape0, chain, {'bound_width_max': 0.5})
    assert R is not R0 and C.shape == 'own'
    assert C.reason.startswith('mbkg_own;bound:') and 'width' in C.reason
    assert C.bound_bits & PF.BOUND_BITS['width'] and R.params['width'] == pytest.approx(0.5, rel=1e-2)


# ---------------------------------------------------------------------------
# the master-background scale term
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('error', [0.02, -0.02])
def test_the_scale_term_takes_up_a_master_background_level_error(error):
    """A compact source on frames whose master background was 2 % off in the slitlet: a fit
    without the term reports a biased flux; with it, the scale comes out at the error and
    the flux at the truth, at the true shape and in the free fit (``own_shape``)."""
    exps, truth = _single_frames(TRUTH, flux_scale=30.0, noise=1.0, mbkg_error=error)
    M = PF.ProfileModel(exps, chromatic=True)
    shape = dict(centre_offset=TRUTH['centre'] - M.c_prior, drift=TRUTH['drift'], width=TRUTH['width'],
                 width_change=TRUTH['width_change'])
    plain = PF.fit_fixed(model=M, shape=shape)
    term = PF.fit_fixed(model=PF.ProfileModel(exps, chromatic=True, mbkg_sigma=0.01), shape=shape)
    assert abs(_reported_ratio(plain, truth) - 1) > 0.05
    assert term.mbkg_scale == pytest.approx(error, abs=4 * term.mbkg_scale_err)
    assert abs(_reported_ratio(term, truth) - 1) < 0.03
    R0, shape0, chain = _nodded_choice(exps, 0.2)
    R, _, C = MX.own_shape(R0, shape0, chain, {})
    assert C.reason.split(';')[0] == 'mbkg_own' and R.mbkg_scale == pytest.approx(error, abs=0.002)
    assert R.params['width'] == pytest.approx(TRUTH['width'], abs=0.05)
    assert abs(_reported_ratio(R, truth) - 1) < 0.05


def test_a_two_percent_level_is_fitted_and_kept_below_the_cap():
    exps, _ = _single_frames(TRUTH, flux_scale=30.0, noise=1.0, mbkg_error=0.02)
    R0, shape0, chain = _nodded_choice(exps, 0.2)
    R, _, C = MX.own_shape(R0, shape0, chain, {})
    assert R.mbkg_scale == pytest.approx(0.02, abs=0.002) and np.isnan(R.mbkg_scale_capped)
    cards = C.header_cards()
    assert cards['MBKGSCL'] == R.mbkg_scale and not np.isfinite(cards['MBKGSCLC'])


@pytest.mark.parametrize('error', [0.10, -0.10])
def test_a_scale_at_the_cap_is_set_to_zero_and_the_fit_is_the_one_without_the_term(error):
    """A 10 % level (or any light the term would take for background): the fitted scale
    reaches the cap (0.03), so the result is exactly the fit without the term, with
    MBKGSCL = 0 and the fitted value in MBKGSCLC."""
    exps, _ = _single_frames(TRUTH, flux_scale=30.0, noise=1.0, mbkg_error=error)
    R0, shape0, chain = _nodded_choice(exps, 0.2)
    R, shape, C = MX.own_shape(R0, shape0, chain, {})
    R0n, shape0n, chain_n = _nodded_choice(exps, 0.2, mbkg_sigma=None)
    Rn, shapen, Cn = MX.own_shape(R0n, shape0n, chain_n, {'mbkg_scale_allowance': 0.0})
    assert R.mbkg_scale == 0.0 and np.sign(R.mbkg_scale_capped) == np.sign(error)
    assert abs(R.mbkg_scale_capped) >= 0.03
    assert R.model.mbkg_sigma is None                       # refitted without the term
    assert shape == shapen and C.reason == Cn.reason and C.snr_det == Cn.snr_det
    np.testing.assert_array_equal(R.flux, Rn.flux)
    cards = C.header_cards()
    assert cards['MBKGSCL'] == 0.0 and cards['MBKGSCLC'] == R.mbkg_scale_capped
    from campfire_pipeline.nirspec import extraction_cards as XC
    h = fits.Header()
    for k, v in cards.items():
        XC.put(h, k, v)
    assert h['MBKGSCL'] == 0.0 and 'MBKGSCLE' not in h and h['MBKGSCLC'] == R.mbkg_scale_capped


def test_the_cap_applies_to_a_nodded_shape_kept_too():
    """The fixed chain keeps the nodded shape; its refit's scale is capped the same way."""
    exps, _ = _single_frames(TRUTH, flux_scale=30.0, noise=1.0, mbkg_error=0.10)
    R0, shape0, chain = _nodded_choice(exps, 0.2)
    R, _, C = MX.own_shape(R0, shape0, chain, {'profile_fit_shape': 'fixed'})
    assert C.reason == 'nodded;own_fit' and R.mbkg_scale == 0.0 and abs(R.mbkg_scale_capped) >= 0.03
    assert R.model.mbkg_sigma is None


def test_the_solve_with_the_term_is_the_joint_least_squares():
    """The scale eliminated through the spectrum's factorisation equals the dense solve of
    [A | b] with the allowance on the scale's diagonal: spectrum, scale, covariance, and the
    chi2 improvement over the no-source fit (the scale solved alone)."""
    exps, _ = _single_frames(TRUTH, flux_scale=30.0, noise=1.0, mbkg_error=0.01)
    sigma = 0.01
    M = PF.ProfileModel(exps, chromatic=True, mbkg_sigma=sigma)
    p = PF.shape_vector(dict(width=0.2), M.c_prior, 0.15)
    s = M.linear_solve(p, np.zeros(3), covariance=True)
    g = s.good
    X = np.column_stack([s.A.toarray()[:, g], M.pix['b']])
    w = 1.0 / M.pix['v']
    N = X.T @ (w[:, None] * X)
    N[np.arange(g.sum()), np.arange(g.sum())] += 1e-9 * np.median(np.diag(N)[:g.sum()])
    N[-1, -1] += 1.0 / sigma ** 2
    rhs = X.T @ (w * M.pix['d'])
    x = np.linalg.solve(N, rhs)
    np.testing.assert_allclose(s.f[g], x[:-1], rtol=1e-6, atol=1e-8 * np.abs(x[:-1]).max())
    assert s.scale == pytest.approx(x[-1], rel=1e-6)
    cov = np.linalg.inv(N)
    assert s.scale_err == pytest.approx(np.sqrt(cov[-1, -1]), rel=1e-6)
    np.testing.assert_allclose(s.cov[np.ix_(g, g)], cov[:-1, :-1], rtol=1e-5, atol=1e-10 * np.abs(cov).max())
    np.testing.assert_allclose(s.model, X @ x, rtol=1e-6, atol=1e-8)
    d, B = M.pix['d'], M.pix['b']
    chi2_src = np.sum(w * (d - X @ x) ** 2) + (x[-1] / sigma) ** 2
    s0 = np.sum(w * B * d) / (np.sum(w * B * B) + 1 / sigma ** 2)
    chi2_none = np.sum(w * (d - s0 * B) ** 2) + (s0 / sigma) ** 2
    assert s.dchi2 == pytest.approx(chi2_none - chi2_src, rel=1e-5)


def test_the_allowance_holds_a_scale_the_data_do_not_measure_at_zero():
    exps, _ = _single_frames(TRUTH, flux_scale=30.0, noise=1.0)
    for e in exps:
        e.mbkg = np.zeros_like(e.mbkg)                     # nothing to scale
    M = PF.ProfileModel(exps, chromatic=True, mbkg_sigma=0.01)
    s = M.linear_solve(PF.shape_vector(None, M.c_prior, 0.15), np.zeros(3))
    assert s.scale == 0.0 and s.scale_err == pytest.approx(0.01)


def test_the_detection_snr_does_not_count_a_level_error_as_a_source():
    """Empty frames whose master background was 2 % too faint: without the term the
    leftover background reads as a strong detection; with it in both the fit and the
    no-source comparison, the detection S/N is that of noise."""
    exps, _ = _single_frames(TRUTH, flux_scale=0.0, noise=1.0, mbkg_error=0.02)
    plain, _ = PF.detection_snr(PF.ProfileModel(exps, chromatic=True))
    term, _ = PF.detection_snr(PF.ProfileModel(exps, chromatic=True, mbkg_sigma=0.01))
    assert plain > 20 and term < 8


def test_the_term_is_refused_where_it_does_not_belong():
    """Not on nod differences (the master background cancels there), not without each
    exposure's master background, never a silent model whose prediction is not A @ f."""
    with pytest.raises(ValueError):
        PF.ProfileModel(_exposures(), chromatic=True, mbkg_sigma=0.01)       # nod differences
    exps, _ = _single_frames(TRUTH, flux_scale=30.0, noise=1.0)
    exps[1].mbkg = None
    with pytest.raises(ValueError):
        PF.ProfileModel(exps, chromatic=True, mbkg_sigma=0.01)
    with pytest.raises(ValueError):
        PF.ProfileModel(_single_frames(TRUTH, 30.0, 1.0)[0], chromatic=True, mbkg_sigma=-0.01)
    M = PF.ProfileModel(_single_frames(TRUTH, 30.0, 1.0)[0], chromatic=True, mbkg_sigma=0.01)
    with pytest.raises(ValueError):
        M.solve(PF.shape_vector(None, M.c_prior, 0.15), np.zeros(3))
    # a model without the term: solve and linear_solve agree, the scale is zero
    M0 = PF.ProfileModel(_single_frames(TRUTH, 30.0, 1.0)[0], chromatic=True, mbkg_sigma=0)
    p = PF.shape_vector(None, M0.c_prior, 0.15)
    f, _, _, A = M0.solve(p, np.zeros(3))
    s = M0.linear_solve(p, np.zeros(3))
    np.testing.assert_array_equal(f, s.f)
    np.testing.assert_array_equal(A @ f, s.model)
    assert s.scale == 0.0 and np.isnan(PF.fit_fixed(model=M0).mbkg_scale)


def test_own_shape_needs_the_master_background_unless_the_term_is_off():
    exps, _ = _single_frames(TRUTH, flux_scale=200.0, noise=1.0)
    R0, shape0, chain = _nodded_choice(exps, 0.2, mbkg_sigma=None)
    for e in R0.model.exps:
        e.mbkg = None
    with pytest.raises(ValueError):
        MX.own_shape(R0, shape0, chain, {})
    R, _, C = MX.own_shape(R0, shape0, chain, {'mbkg_scale_allowance': 0.0})
    assert C.reason == 'mbkg_own' and np.isnan(R.mbkg_scale)
    assert not np.isfinite(C.header_cards()['MBKGSCL'])       # left out of the header (extraction_cards.put)


def test_corrected_inputs_take_the_fitted_level_out_of_the_data():
    data = np.full((3, 4), 10.0, np.float32)
    dq = np.zeros((3, 4), np.uint32)
    dq[0, 0] = PF.DO_NOT_USE
    bg = np.full((3, 4), 100.0)
    bg[0, 0] = np.nan                                      # no master background there (DO_NOT_USE)
    inputs = {'a': dict(data=data, dq=dq, err=np.ones((3, 4), np.float32))}
    out = MX.corrected_inputs(inputs, {'a': MX.Frame(inputs['a'], dq, background=bg)}, 0.02)
    assert out['a']['data'].dtype == np.float32
    np.testing.assert_allclose(out['a']['data'][1:], 8.0)
    assert out['a']['data'][0, 0] == 10.0
    assert out['a']['dq'] is dq and out['a']['err'] is inputs['a']['err']
    np.testing.assert_array_equal(inputs['a']['data'], 10.0)            # the inputs are untouched


# ---------------------------------------------------------------------------
# MBKGPREF / MBKGWHY, NEXP / NEXPBKG
# ---------------------------------------------------------------------------
def test_exposure_counts():
    assert MX.exposure_counts(dict(filename=['a', 'b', 'c'], bkg_nods=['2,3', '3', '1,2'])) == (3, 1)
    assert MX.exposure_counts(dict(filename=['a'], bkg_nods=['excluded'])) == (1, 0)
    assert MX.exposure_counts(dict(filename=['a', 'b'], bkg_nods=['N/A', 'N/A'])) == (2, None)


def test_preferred_takes_the_first_reason_that_applies():
    clear = dict(filename=['a', 'b'], bkg_nods=['2', '1'], bkg_why=['', 'neighbor 9 at 0.030 of background'])
    flagged = dict(clear, bkg_why=['every background exposure: neighbor 9 at 0.050 of background', ''])
    assert MX.preferred(clear, 0.02, 0.10) == (False, '')
    assert MX.preferred(clear, 0.15, 0.10) == (True, 'extended')
    assert MX.preferred(flagged, 0.15, 0.10) == (True, 'no_clear_background')
    assert MX.preferred(dict(filename=['a'], bkg_nods=['excluded'], bkg_why=['']), 0.0, 0.1) == (True, 'single_exposure')
    assert MX.preferred(clear, None, 0.10) == (False, '')


# ---------------------------------------------------------------------------
# the frames, in memory
# ---------------------------------------------------------------------------
def _canonical(tmp_path, ny=6, nx=8):
    """A canonical whose live slit is a nod difference (99) and whose PRE_BKGSUB arrays
    hold the calibrated frame (5)."""
    from jwst.datamodels import MultiSlitModel, SlitModel
    from campfire_pipeline.nirspec import canonical as C

    live_dq = np.zeros((ny, nx), np.uint32)
    live_dq[1, 1] = MX.OUTLIER | PF.DO_NOT_USE              # a partner's flag stage 2b carried over
    m = MultiSlitModel()
    m.slits.append(SlitModel(data=np.full((ny, nx), 99.0, np.float32), err=np.ones((ny, nx), np.float32),
                             dq=live_dq, var_poisson=np.full((ny, nx), 9.0, np.float32),
                             var_rnoise=np.full((ny, nx), 1.0, np.float32),
                             var_flat=np.full((ny, nx), 0.25, np.float32)))
    canonical = str(tmp_path / 'jw07076001001_03101_00001_nrs1_7.fits')
    m.save(canonical)
    pre = {'SCI': np.full((ny, nx), 5.0, np.float32), 'VAR_POISSON': np.full((ny, nx), 2.0, np.float32),
           'VAR_RNOISE': np.full((ny, nx), 1.0, np.float32), 'DQ': np.zeros((ny, nx), np.uint32)}
    C.append_extras(canonical, extra_hdus=C.make_prefixed_hdus(pre, C.PRE_BKGSUB_PREFIX))
    return canonical, live_dq


def test_frame_arrays_subtract_the_master_background(tmp_path, monkeypatch):
    ny, nx = 6, 8
    canonical, live_dq = _canonical(tmp_path, ny, nx)
    sky = np.full((ny, nx), 3.0)
    sky[0, 0] = np.nan                                     # a pixel the master background does not cover
    monkeypatch.setattr('campfire_pipeline.nirspec.stage2.master_background_on',
                        lambda path: (sky, np.full((ny, nx), 0.5)))
    f = MX.frame_arrays(canonical)
    frame, live = f.arrays, f.live_dq
    assert not os.path.exists(MX.subdir(str(tmp_path)))  # nothing written
    assert len(f.origin) == 2
    np.testing.assert_allclose(frame['data'][1:, :], 2.0)  # the calibrated frame, not the nod difference
    assert np.isnan(frame['data'][0, 0]) and (frame['dq'][0, 0] & PF.DO_NOT_USE)
    assert frame['dq'][1, 1] == 0                          # the frame's own DQ, not the nod difference's
    np.testing.assert_allclose(frame['var_poisson'][1:, :], 2.5)
    np.testing.assert_allclose(frame['var_rnoise'], 1.0)
    np.testing.assert_allclose(frame['var_flat'], 0.25)
    np.testing.assert_allclose(frame['err'][1:, :], np.sqrt(3.75))
    assert {frame[k].dtype for k in ('data', 'err', 'var_poisson')} == {np.dtype(np.float32)}
    np.testing.assert_array_equal(live, live_dq)
    np.testing.assert_array_equal(f.background, sky)         # what was subtracted: the scale term's column


def test_frame_arrays_without_a_master_background(tmp_path, monkeypatch):
    monkeypatch.setattr('campfire_pipeline.nirspec.stage2.master_background_on', lambda path: None)
    with pytest.raises(MX.NoMasterBackground):
        MX.frame_arrays(str(tmp_path / 'jw07076001001_03101_00001_nrs1_7.fits'))


def test_outlier_flags_are_the_nodded_crf_and_the_hot_pixels():
    """The one place of the choice: what the nodded Spec3 flagged in this exposure and the
    hot pixels are flagged in its master-background frame, on top of its own mask; flags
    the canonical already had are not taken from the crf (the frame keeps its own DQ)."""
    a = dict(data=np.ones((4, 5), np.float32), dq=np.zeros((4, 5), np.uint32))
    a['dq'][3, 4] = PF.DO_NOT_USE                          # no master background there
    live = np.zeros((4, 5), np.uint32)
    live[0, 0] = MX.OUTLIER | PF.DO_NOT_USE                # in the nod difference before Spec3
    crf = live.copy()
    crf[2, 1] = MX.OUTLIER | PF.DO_NOT_USE                 # flagged by the nodded Spec3
    crf[1, 3] = 4                                          # another flag: not an outlier
    hot = np.zeros((4, 5), bool)
    hot[0, 4] = True
    out = MX.outlier_flags(MX.Frame(a, live), crf, hot)
    assert out['dq'][2, 1] == MX.OUTLIER | PF.DO_NOT_USE and out['dq'][0, 4] == MX.OUTLIER | PF.DO_NOT_USE
    assert out['dq'][0, 0] == 0 and out['dq'][1, 3] == 0 and out['dq'][3, 4] == PF.DO_NOT_USE
    assert int(np.sum(out['dq'] > 0)) == 3
    assert a['dq'][2, 1] == 0                              # the frame itself is untouched
    assert out['dq'].dtype == np.uint32


# ---------------------------------------------------------------------------
# the hot-pixel mask
# ---------------------------------------------------------------------------
def _scene(n_exp=3, hot=((10, 30),), cosmic=None, seed=4, origins=None):
    """Frames of one detector region (err 1): noise, a smooth band fixed on the detector
    (rows 3-6, 40 sigma, a slow ramp along the dispersion), a source trace that moves by 5
    rows between exposures with a one-pixel emission line (60 sigma), hot pixels fixed on
    the detector (30 sigma) and optionally a cosmic ray in one exposure."""
    ny, nx = 24, 60
    rng = np.random.default_rng(seed)
    origins = origins or [(100, 200)] * n_exp
    frames, truth = {}, {}
    for k in range(n_exp):
        oy, ox = origins[k]
        yy, xx = np.mgrid[:ny, :nx]
        dy, dx = yy + oy, xx + ox                          # detector pixel
        d = rng.normal(0.0, 1.0, (ny, nx))
        d += np.where((dy >= 103) & (dy <= 106), 40.0 * (1 + 0.01 * dx - 2.0), 0.0)   # the band
        src = 110 + 5 * k
        d += 20.0 * np.exp(-0.5 * ((dy - src) / 0.8) ** 2)
        d[(dy == src) & (dx == 225)] += 60.0               # its emission line
        for y, x in hot:
            d[(dy == y + 100) & (dx == x + 200)] += 30.0
        if cosmic is not None and k == cosmic[0]:
            d[(dy == cosmic[1] + 100) & (dx == cosmic[2] + 200)] += 100.0
        arrays = dict(data=d.astype(np.float32), dq=np.zeros((ny, nx), np.uint32), err=np.ones((ny, nx), np.float32))
        frames[f'e{k}'] = MX.Frame(arrays, np.zeros((ny, nx), np.uint32), 'NRS1', (oy, ox))
    return frames


def test_a_hot_pixel_fixed_in_every_exposure_is_flagged():
    frames = _scene()
    hot = MX.hot_pixels(frames)
    for n, m in hot.items():
        assert m[10, 30] and int(m.sum()) == 1, n


def test_a_fixed_smooth_band_and_a_moving_source_are_not_flagged():
    """The band is fixed on the detector in every exposure but smooth along its rows; the
    source (and its one-pixel line, 60 sigma above its row) moves between exposures."""
    frames = _scene(hot=())
    for n, f in frames.items():
        s = MX.local_contrast(f)
        assert np.nanmax(s) > MX.HOT_SIGMA                 # the line stands out in its own exposure
    assert not any(m.any() for m in MX.hot_pixels(frames).values())


def test_a_cosmic_ray_in_one_exposure_is_not_flagged():
    frames = _scene(hot=(), cosmic=(1, 18, 45))
    assert MX.local_contrast(frames['e1'])[18, 45] > 50
    assert not any(m.any() for m in MX.hot_pixels(frames).values())


def test_hot_pixels_are_matched_on_the_detector():
    """Cutouts at different detector positions: the same detector pixel, at different
    cutout pixels, is flagged in each; other detectors are separate."""
    frames = _scene(origins=[(100, 200), (102, 199), (100, 200)])
    hot = MX.hot_pixels(frames)
    assert hot['e0'][10, 30] and hot['e2'][10, 30] and hot['e1'][8, 31]
    assert sum(int(m.sum()) for m in hot.values()) == 3
    frames['e2'].detector = 'NRS2'                         # two exposures left on NRS1: still every one
    hot = MX.hot_pixels(frames)
    assert hot['e0'][10, 30] and hot['e1'][8, 31] and not hot['e2'].any()


def test_one_exposure_flags_nothing_two_must_agree():
    one = _scene(n_exp=1)
    assert not MX.hot_pixels(one)['e0'].any()               # nothing tells a hot pixel from the source
    two = _scene(n_exp=2)
    assert all(m[10, 30] for m in MX.hot_pixels(two).values())
    two['e1'].arrays['data'][10, 30] = 0.0                 # high in one of two: not flagged
    assert not any(m.any() for m in MX.hot_pixels(two).values())
    three = _scene()
    three['e2'].arrays['dq'][10, 30] = PF.DO_NOT_USE       # unusable in one: the other two decide
    hot = MX.hot_pixels(three)
    assert hot['e0'][10, 30] and hot['e1'][10, 30] and not hot['e2'][10, 30]


def test_master_background_two_d_through_the_nodded_map():
    """``drizzle_2d``: the frames drizzled as data through the product's map, with the
    nodded outlier flags and the hot pixels masking their pixels."""
    from test_nirspec_drizzle_map import NX as DX, NY as DY, _exposures as _dexps, _map, _wcs
    exps = _dexps()
    out_wcs = _wcs(0.0, 0.0, shape=(DY, DX))
    d = _map(exps, out_wcs)
    d.exposures[0].arrays['dq'][6, 6] |= MX.OUTLIER | PF.DO_NOT_USE      # the nodded Spec3's flag
    frames = {}
    for e in exps:
        frame = {k: np.array(e[k]) for k in ('data', 'err', 'var_poisson', 'var_rnoise', 'var_flat')}
        frame['data'] = np.full((DY, DX), 3.0, np.float32)
        frame['data'][9, 14] = 3.0 + 50 * frame['err'][9, 14]          # a hot pixel, in both
        frame['dq'] = np.zeros((DY, DX), np.uint32)
        frames[e['name']] = MX.Frame(frame, np.zeros((DY, DX), np.uint32), 'NRS1', (1, 1))
    inputs = MX.flagged_inputs(d, frames)
    out = MX.drizzle_2d(d, inputs)
    hot = np.zeros((DY, DX), int)
    hot[9, 14] = MX.OUTLIER | PF.DO_NOT_USE
    outlier = [np.where((x.arrays['dq'] & MX.OUTLIER) > 0, MX.OUTLIER | PF.DO_NOT_USE, 0) | hot for x in d.exposures]
    flagged = d.drizzle({e['name']: {**frames[e['name']].arrays, 'dq': (frames[e['name']].arrays['dq'] | outlier[k])}
                         for k, e in enumerate(exps)}, variances=True)
    clean = d.drizzle({e['name']: frames[e['name']].arrays for e in exps}, variances=True)
    np.testing.assert_array_equal(out['data'], flagged['data'])
    np.testing.assert_array_equal(out['err'], flagged['err'])
    assert not np.array_equal(out['wht'], clean['wht'])   # the flags took the pixels' weight
    np.testing.assert_allclose(out['data'][np.isfinite(out['data'])], 3.0, rtol=1e-6)
    with pytest.raises(ValueError):
        MX.flagged_inputs(d, {exps[0]['name']: frames[exps[0]['name']]})

    # a model image takes the frames' masks and weights, not the nodded crf's: where the
    # hot pixel is masked in the frames only, the two differ
    model = {e['name']: np.full((DY, DX), float(k + 1)) for k, e in enumerate(exps)}
    on_frames = MX.drizzle_on_frames(d, inputs, model)
    want = d.drizzle({n: {**inputs[n], 'data': model[n].astype(np.float32)} for n in model})['data']
    np.testing.assert_array_equal(on_frames, want)
    assert not np.array_equal(on_frames, d.drizzle(model)['data'])


def test_single_frame_exposures_from_the_nodded_load():
    """The fit's master-background exposures, in memory: the science exposures of the
    nodded load with their frame (times the path loss) in place of the nod difference,
    nothing subtracted, usable pixels from the frame, on their own common frame (a
    background-only exposure that widened the nodded frame is left out)."""
    exps = _exposures()
    pad = 2
    for k, e in enumerate(exps):
        for f in ('diff', 'var', 'wav', 'y', 'pl_applied', 'pl_point'):
            a = np.full((NY + 2 * pad, NX), np.nan)
            a[pad:pad + NY] = getattr(e, f)
            setattr(e, f, a)
        e.ok = np.pad(e.ok, ((pad, pad), (0, 0)))
        e.origin = (11, 21)
        e.cutout = (pad, 0, NY, NX)
    exps[0].use_data = False                               # a background exposure only
    exps[0].cutout = (0, 0, NY + 2 * pad, NX)              # with a taller cutout
    exps[1].pl_applied[pad + 3, 5] = 0.0                   # no path loss there
    frames = {}
    for e in exps:
        data = np.full((NY, NX), 4.0, np.float32)
        dq = np.zeros((NY, NX), np.uint32)
        data[0, 0] = np.nan
        dq[1, 1] = PF.DO_NOT_USE
        frames[e.name] = MX.Frame(dict(data=data, dq=dq), dq)
    out = MX.single_frame_exposures(exps, frames)
    assert [x.name for x in out] == [exps[1].name, exps[2].name]
    for x, e in zip(out, exps[1:]):
        assert x.partners == [] and x.use_data and x.group == 0
        assert x.cutout == (0, 0, NY, NX) and x.origin == (11 + pad, 21) and x.diff.shape == (NY, NX)
        np.testing.assert_array_equal(x.var, e.var[pad:pad + NY])
        np.testing.assert_array_equal(x.wav, e.wav[pad:pad + NY])
        np.testing.assert_array_equal(x.diff, np.asarray(frames[e.name].arrays['data'], float) * e.pl_applied[pad:pad + NY])
        assert not x.ok[0, 0] and not x.ok[1, 1] and x.ok[2, 2]
        assert e.partners and e.diff.shape == (NY + 2 * pad, NX)   # the nodded exposures are untouched
    assert not out[0].ok[3, 5] and out[1].ok[3, 5]
    M = PF.ProfileModel(out, chromatic=True)
    assert np.all(M.dof_weight() == 1.0)                   # single frames: one independent value per pixel
    # the frames' arrays with their outlier flags (flagged_inputs) leave those pixels out too
    flagged = {}
    for n, f in frames.items():
        a = dict(f.arrays)
        a['dq'] = a['dq'].copy()
        a['dq'][4, 6] |= MX.OUTLIER | PF.DO_NOT_USE
        flagged[n] = a
    out2 = MX.single_frame_exposures(exps, flagged)
    assert all(not x.ok[4, 6] for x in out2) and all(x.ok[4, 7] for x in out2)
    assert all(x.mbkg is None for x in out2)
    # with the master backgrounds: placed like the frames, times the path loss (units of diff)
    bgs = {e.name: np.full((NY, NX), 7.0) for e in exps}
    out3 = MX.single_frame_exposures(exps, frames, backgrounds=bgs)
    for x, e in zip(out3, exps[1:]):
        np.testing.assert_array_equal(x.mbkg, 7.0 * e.pl_applied[pad:pad + NY])
    M3 = PF.ProfileModel(out3, chromatic=True, mbkg_sigma=0.01)
    assert M3.pix['b'].size == M3.npix and np.all(np.isfinite(M3.pix['b']))


# ---------------------------------------------------------------------------
# the _spec.fits
# ---------------------------------------------------------------------------
NAMES = ['jw07076001001_03101_00001_nrs1_7.fits', 'jw07076001001_03101_00002_nrs1_7.fits']


def _inputs(tmp_path, product, amp, where, contam=0.1, wave=None, bkg_why=('', ''), ny=30, x1d=True):
    """A minimal s2d (and x1d) of a source of ``amp`` uJy with a Gaussian profile and its
    _pf.fits under ``where`` (the workspace, or its ``mbkg`` subdirectory: the
    master-background 2-D has no x1d)."""
    nx = 60
    rows = np.arange(ny)[:, None]
    prof = np.exp(-0.5 * ((rows - 15) / 1.2) ** 2) * np.ones((1, nx))
    prof = prof / prof[:, :1].sum()
    wave = np.linspace(1.0, 5.0, nx) if wave is None else wave
    os.makedirs(where, exist_ok=True)
    exp = fits.table_to_hdu(Table(dict(filename=NAMES, bkg_nods=['2', '1'], bkg_why=list(bkg_why))))
    exp.name = 'EXPOSURES'
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(amp * prof / 1e12, name='SCI'),
                  fits.ImageHDU(np.full((ny, nx), 0.01 / 1e12), name='ERR'),
                  fits.ImageHDU(np.tile(wave, (ny, 1)), name='WAVELENGTH'),
                  fits.ImageHDU(np.ones((ny, nx)), name='WHT'), exp]).writeto(os.path.join(where, f'{product}_s2d.fits'))
    if x1d:
        x1 = fits.BinTableHDU.from_columns([fits.Column(name='WAVELENGTH', format='D', array=wave)], name='EXTRACT1D')
        x1.header['EXTRYSTR'] = 13
        x1.header['EXTRYSTP'] = 18
        fits.HDUList([fits.PrimaryHDU(), x1]).writeto(os.path.join(where, f'{product}_x1d.fits'))
    h = fits.Header()
    h['NCONTAM'] = 1
    h['SELFSUB'] = 0.02
    fits.HDUList([fits.PrimaryHDU(header=h), fits.ImageHDU(prof / 1e12, name='PROF2D'),
                  fits.ImageHDU(np.full(prof.shape, 0.8), name='TRANS2D'),
                  fits.ImageHDU(contam * amp * prof / 1e12, name='CONTAM2D'),
                  fits.ImageHDU(0.0 * prof, name='CONTERR2D')]).writeto(os.path.join(where, f'{product}_pf.fits'))


def _master_backgrounds(tmp_path, sources=('pool', 'exposure')):
    from campfire_pipeline.nirspec.profile_contam import exposure_key
    for name, src in zip(NAMES, sources):
        h = fits.Header()
        h['MBKGSRC'] = src
        h['CFMBKGV'] = '1.5'
        fits.PrimaryHDU(header=h).writeto(tmp_path / f'{exposure_key(name)}_mbkg.fits')


def test_spec_fits_carries_the_master_background_spectrum(tmp_path):
    _inputs(tmp_path, 'p', 10.0, str(tmp_path))
    _inputs(tmp_path, 'p_mbkg', 8.0, MX.subdir(str(tmp_path)), x1d=False)
    _master_backgrounds(tmp_path)
    S3.opt_ext_single_source('p', str(tmp_path), profile_fit=True, mbkg_spectrum=True)
    path = tmp_path / 'p_spec.fits'
    with fits.open(path) as h:
        ext = [x.name for x in h[1:]]
        # last, in order, with the master-background fit's model images; the nodded ones carry theirs
        assert ext[-7:] == list(S3.MBKG_EXTENSIONS)
        assert {'PROF2D', 'TRANS2D', 'CONTAM2D'} <= set(ext)
        np.testing.assert_allclose(h['TRANS2D_MBKG'].data, 0.8)
        # the full 2-D model, PROF2D x (fnu_prof - fnu_contam) + CONTAM2D, is the master-background 2-D
        t = h['SPEC1D_MBKG'].data
        model = h['PROF2D_MBKG'].data * (t['fnu_prof'] - np.nan_to_num(t['fnu_contam']))[None, :] \
            + h['CONTAM2D_MBKG'].data
        np.testing.assert_allclose(model[:, 2:-2], h['SCI_MBKG'].data[:, 2:-2], rtol=1e-4, atol=1e-17)
        assert h['SPEC1D_MBKG'].columns.names == h['SPEC1D'].columns.names
        np.testing.assert_array_equal(h['SPEC1D_MBKG'].data['wave'], h['SPEC1D'].data['wave'])
        assert h['SCI_MBKG'].data.shape == h['SCI'].data.shape == h['ERR_MBKG'].data.shape
        assert h['PROF1D_MBKG'].columns.names == h['PROF1D'].columns.names
        np.testing.assert_allclose(h['SPEC1D_MBKG'].data['fnu'][2:-2], 8.0, rtol=1e-5)
        np.testing.assert_allclose(h['SPEC1D'].data['fnu'][2:-2], 10.0, rtol=1e-5)
        hd, hm = h[0].header, h['SPEC1D_MBKG'].header
        assert (hd['NEXP'], hd['NEXPBKG']) == (2, 1)
        assert (hd['MBKGPREF'], hd['MBKGWHY'], hd['MBKGNEXP']) == (False, '', 1)
        assert (hm['EXTRACT'], hm['MBKGVER'], hm['MBKGSRC']) == ('profile', '1.5', 'combined,exposure')
    assert not os.listdir(MX.subdir(str(tmp_path)))           # intermediates removed
    # the master-background spectrum is not part of the science identity (line fits stay fresh)
    with fits.open(path) as h:
        keep = fits.HDUList([x.copy() for x in h if x.name not in S3.MBKG_EXTENSIONS])
        for k in ('MBKGPREF', 'MBKGWHY', 'MBKGNEXP'):
            del keep[0].header[k]
        keep.writeto(tmp_path / 'nodded_only.fits')
    assert science_hash(path) == science_hash(tmp_path / 'nodded_only.fits')


def test_the_spectrum_plot_is_drawn_from_the_product_alone(tmp_path):
    """The diagnostic reads only the _spec.fits: with the master-background spectrum, and
    without it or a profile fit (those rows say so)."""
    from campfire_pipeline.nirspec.plots import plot_spectrum_qa
    _inputs(tmp_path, 'p', 10.0, str(tmp_path))
    _inputs(tmp_path, 'p_mbkg', 8.0, MX.subdir(str(tmp_path)), x1d=False)
    _master_backgrounds(tmp_path)
    S3.opt_ext_single_source('p', str(tmp_path), profile_fit=True, mbkg_spectrum=True, plot_optext=True)
    assert (tmp_path / 'p_spec.pdf').stat().st_size > 0
    with fits.open(tmp_path / 'p_spec.fits') as h:
        fits.HDUList([x.copy() for x in h if x.name not in S3.MBKG_EXTENSIONS + ('PROF2D', 'TRANS2D', 'CONTAM2D')]) \
            .writeto(tmp_path / 'bare_spec.fits')
    plot_spectrum_qa(str(tmp_path / 'bare_spec.fits'))
    assert (tmp_path / 'bare_spec.pdf').stat().st_size > 0


def test_flagged_exposure_prefers_the_master_background(tmp_path):
    _inputs(tmp_path, 'p', 10.0, str(tmp_path), bkg_why=('every background exposure: neighbor 9 at 0.050 of '
                                                          'background', ''))
    _inputs(tmp_path, 'p_mbkg', 8.0, MX.subdir(str(tmp_path)), x1d=False)
    S3.opt_ext_single_source('p', str(tmp_path), profile_fit=True, mbkg_spectrum=True)
    hd = fits.getheader(tmp_path / 'p_spec.fits')
    assert (hd['MBKGPREF'], hd['MBKGWHY']) == (True, 'no_clear_background')


def test_a_two_d_of_another_shape_leaves_out_the_master_background_spectrum(tmp_path):
    """The master-background 2-D is on the nodded grid by construction; a file of another
    shape (stale) is not used."""
    _inputs(tmp_path, 'p', 10.0, str(tmp_path))
    _inputs(tmp_path, 'p_mbkg', 8.0, MX.subdir(str(tmp_path)), ny=31, x1d=False)
    S3.opt_ext_single_source('p', str(tmp_path), profile_fit=True, mbkg_spectrum=True)
    with fits.open(tmp_path / 'p_spec.fits') as h:
        assert not any(x.name in S3.MBKG_EXTENSIONS for x in h)
        assert 'MBKGPREF' not in h[0].header and h[0].header['NEXP'] == 2


def test_the_master_background_spectrum_is_written_on_the_nodded_wave(tmp_path):
    """No 1-D of its own (no Spec3, no x1d): the nodded aperture rows and ``wave``."""
    _inputs(tmp_path, 'p', 10.0, str(tmp_path))
    _inputs(tmp_path, 'p_mbkg', 8.0, MX.subdir(str(tmp_path)), x1d=False)
    S3.opt_ext_single_source('p', str(tmp_path), profile_fit=True, mbkg_spectrum=True)
    with fits.open(tmp_path / 'p_spec.fits') as h:
        np.testing.assert_array_equal(h['SPEC1D_MBKG'].data['wave'], h['SPEC1D'].data['wave'])


def test_no_master_background_spectrum_without_its_two_d(tmp_path):
    _inputs(tmp_path, 'p', 10.0, str(tmp_path))
    S3.opt_ext_single_source('p', str(tmp_path), profile_fit=True, mbkg_spectrum=True)
    with fits.open(tmp_path / 'p_spec.fits') as h:
        assert not any(x.name in S3.MBKG_EXTENSIONS for x in h)


def test_combined_product_carries_the_master_background_spectrum(tmp_path):
    """The groups' master-background spectra combined, with the stacked 2-D the stacked
    Spec3 drizzled through its pixel maps (``run_stage3_single_source``; here written
    directly)."""
    n = 60
    wave = np.linspace(3.0, 5.0, n)
    cols = ('opt', '3px', '4px', '5px', 'prof', 'contam')
    files = []
    for i, f in enumerate((2.0, 2.0)):
        t = Table({'wave': wave})
        for c in cols:
            t[f'fnu_{c}'] = np.full(n, 0.0 if c == 'contam' else f)
            t[f'fnu_{c}_err'] = np.full(n, 0.1)
        h = fits.table_to_hdu(t)
        h.name = 'SPEC1D_MBKG'
        h.header['EXTRACT'] = 'profile'
        prim = fits.PrimaryHDU()
        prim.header['SELFSUB'] = 0.01
        name = f'p_g{i}_spec.fits'
        fits.HDUList([prim, h]).writeto(tmp_path / name)
        files.append(name)
    sci_shape = (7, n)
    path = MX.product_paths(str(tmp_path), 'p', 7)['s2d']

    def two_d(shape):
        MX.write_2d_file(path, dict(data=np.ones(shape, np.float32), err=np.ones(shape, np.float32),
                                    wht=np.ones(shape, np.float32)))

    exposures = {'filename': ['a', 'b'], 'bkg_nods': ['2', '1'], 'bkg_why': ['every background exposure: x', '']}
    clip = dict(sigma_clip_enabled=False)
    args = (files, str(tmp_path), 'p', 7, wave, [1.0, 3.0], sci_shape, 2, 5, fits.Header(), exposures, False, clip,
            0.10)
    # no stacked master-background 2-D (an exposure without a master background): none
    assert S3._combined_mbkg(*args) == ([], {})
    two_d(sci_shape)
    hdus, cards = S3._combined_mbkg(*args)
    assert [h.name for h in hdus] == ['SPEC1D_MBKG', 'SCI_MBKG', 'ERR_MBKG', 'PROF1D_MBKG']
    spec = hdus[0].data
    assert np.array_equal(spec['wave'], wave) and np.allclose(spec['fnu_opt'], 2.0)
    assert cards['MBKGPREF'][0] and cards['MBKGWHY'][0] == 'no_clear_background'
    # a stacked 2-D of another shape than the nodded one: no master-background spectrum
    two_d((8, n))
    assert S3._combined_mbkg(*args) == ([], {})
