"""Tests for the diffraction model of the MSA (``nirspec.optics``, FIT_VERSION 6) and its use in the
profile fit. The physics was validated on calibration stars (design doc); these lock the engine's
arithmetic, the table cache, the model every exposure takes, and the model's wiring into
``ProfileModel`` (through the closed-form stand-in, ``optics_standin``, so no table is built)."""

import numpy as np
import pytest

from campfire_pipeline.nirspec import crds_refs as CR
from campfire_pipeline.nirspec import optics as O
from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH

from optics_standin import Analytic
from test_nirspec_profile_fit import DY, NX, NY, TRUTH, _exposures, _spectrum


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
def _weights_on(values, centre, width):
    """sum_j weight_j * values[j] for one source (``Optics._compact_weights``)."""
    out = 0.0
    for j, wt in O.Optics._compact_weights(np.array([centre]), np.array([width])):
        jj = int(j[0])
        if 0 <= jj < values.size:
            out += wt[0] * values[jj]
    return out


def test_compact_weights_are_linear_interpolation_at_zero_width():
    vals = np.sin(3.0 * O.C) + 0.1 * O.C ** 2
    for c in (-0.4012, 0.0, 0.0063, 1.234):
        assert _weights_on(vals, c, 1e-9) == pytest.approx(np.interp(c, O.C, vals), abs=1e-9)


@pytest.mark.parametrize('width', [0.004, 0.015, 0.03, 0.06])
def test_compact_weights_match_a_gaussian_over_the_interpolated_table(width):
    vals = np.exp(-0.5 * (O.C / 0.08) ** 2) + 0.3 * (np.abs(O.C) < 0.5)      # a core and a step (a shutter edge)
    xs = np.linspace(-7, 7, 20001)
    g = np.exp(-0.5 * xs ** 2)
    g /= g.sum()
    for c in (0.0, 0.4937, -0.21):
        brute = float(np.sum(g * np.interp(c + width * xs, O.C, vals)))
        assert _weights_on(vals, c, width) == pytest.approx(brute, abs=1e-5)    # the weights stop at +-4 widths


def test_point_image_symmetric_and_loses_light_at_the_shutter():
    y, f = O._grid()
    I1, t1 = O.point_image(0.0, 2.0, (0,), 0.996, 0.03, 0.25, y, f)
    I3, t3 = O.point_image(0.0, 2.0, (-1, 0, 1), 0.996, 0.03, 0.25, y, f)
    assert np.allclose(I1[1:], I1[1:][::-1], atol=1e-6 * I1.max())     # symmetric about y = 0
    assert 0.6 < t1 < t3 < 1.0                       # the shutter edges cut light; more open shutters pass more
    I_far, t_far = O.point_image(0.2, 4.5, (0,), 0.996, 0.03, 0.25, y, f)
    assert t_far < t1                                # off centre and at a longer wavelength: more lost


def test_an_upper_edge_moved_down_cuts_light_near_the_upper_bar_only():
    y, f = O._grid()
    up = -0.0137 * O.SLIT                            # the shipped model's upper edge [arcsec]
    def t(c, edges):
        return O.point_image(c * O.SLIT, 4.5, (-1, 0, 1), 0.996, 0.03, 0.25, y, f, edges=edges)[1]
    for c in (0.0, -0.35):                           # the centre and near the lower bar: unchanged to 0.5 %
        assert t(c, (0.0, up)) == pytest.approx(t(c, (0.0, 0.0)), rel=5e-3)
    assert t(0.35, (0.0, up)) < 0.98 * t(0.35, (0.0, 0.0))     # near the upper bar: less light
    assert t(0.35, (0.0, 0.0)) == pytest.approx(t(-0.35, (0.0, 0.0)), rel=1e-6)   # symmetric without it


def test_every_opening_is_exactly_its_height_wherever_it_falls_on_the_grid():
    # the light of a flat field through each shutter is the same for every shutter, and moves by exactly the edge
    # move, so no shutter gains a grid step by rounding (engine 1 opened shutters +-1 to +-3 one 0.0025 arcsec step
    # too far at edges that fall on grid points)
    y, _ = O._grid()
    dy = y[1] - y[0]
    open0 = O.opening_mask(y, (0,)).sum() * dy
    for k in range(-3, 4):
        for lo, hi in ((0.0, 0.0), (0.0, -0.3 * dy), (0.2 * dy, -1.7 * dy)):
            area = O.opening_mask(y, (k,), (lo, hi)).sum() * dy
            assert area == pytest.approx(open0 - lo + hi, abs=1e-9)


def test_an_edge_moves_the_light_continuously_below_one_grid_step():
    y, f = O._grid()
    dy = y[1] - y[0]
    ts = [O.point_image(0.15, 4.5, (-1, 0, 1), 0.996, 0.03, 0.25, y, f, edges=(0.0, -e))[1]
          for e in np.linspace(0.0, 2 * dy, 9)]
    steps = np.diff(ts)
    assert np.all(steps < 0)                         # every quarter step cuts light, none is lost to the grid
    assert steps.max() / steps.min() < 1.5           # and about equally


def test_edges_enter_the_cache_key_only_when_set(small_grids, tmp_path):
    a = O.Optics(0.996, 0.0344, -0.0026, 0.25, _kernel(), cache_dir=str(tmp_path))
    b = O.Optics(0.996, 0.0344, -0.0026, 0.25, _kernel(), cache_dir=str(tmp_path), edges=(0.0, 0.0))
    c = O.Optics(0.996, 0.0344, -0.0026, 0.25, _kernel(), cache_dir=str(tmp_path), edges=(0.0, -0.0137))
    assert a._path('img', (0,), True) == b._path('img', (0,), True)      # the symmetric tables keep their files
    assert a._path('img', (0,), True) != c._path('img', (0,), True)
    assert c.edges_as == (0.0, pytest.approx(-0.0137 * O.SLIT))


def test_a_lock_left_by_a_dead_process_is_taken_over_at_once(tmp_path):
    import os
    import time
    path = str(tmp_path / 'tab.npy')
    lock = path + '.lock'
    open(lock, 'w').close()
    old = time.time() - O.LOCK_WAIT - 10
    os.utime(lock, (old, old))
    t0 = time.time()
    tab = O._cached(path, lambda: np.arange(3.0))
    assert time.time() - t0 < 5 and np.array_equal(tab, np.arange(3.0)) and not os.path.exists(lock)


def test_kernel_at_normalised_and_linear_in_log_wavelength():
    delta = np.linspace(-2, 2, 31)
    narrow = np.exp(-0.5 * (delta / 0.05) ** 2)
    broad = np.exp(-0.5 * (delta / 0.2) ** 2)
    opt = O.Optics(0.996, 0.0344, -0.0026, 0.25, [dict(lam=1.0, delta=delta, k=narrow),
                                                   dict(lam=4.0, delta=delta, k=broad)])
    x = np.linspace(-2, 2, 321)
    k1, k4, k2 = opt.kernel_at(0.5, x), opt.kernel_at(5.0, x), opt.kernel_at(2.0, x)
    for k in (k1, k4, k2):
        assert k.sum() == pytest.approx(1.0)
    p1 = np.interp(x, delta, narrow)
    assert np.allclose(k1, p1 / p1.sum())            # held at the end node
    p4 = np.interp(x, delta, broad)
    assert np.allclose(k2, 0.5 * p1 / p1.sum() + 0.5 * p4 / p4.sum())   # half way in ln(lambda)


@pytest.fixture
def small_grids(monkeypatch):
    """Few wavelengths and source positions, so a table builds in a second."""
    monkeypatch.setattr(O, 'LAM', np.array([1.0, 3.0]))
    monkeypatch.setattr(O, 'C', np.round(np.arange(-0.25, 0.2501, 0.0125), 5))
    monkeypatch.setattr(O, 'Y', np.round(np.arange(-1.0, 1.0001, 0.025), 5))


def _kernel(width=0.03):
    delta = np.linspace(-0.2, 0.2, 9)
    return [dict(lam=1.0, delta=delta, k=np.exp(-0.5 * (delta / width) ** 2))]


def test_tables_cached_by_model_and_kernel(small_grids, tmp_path, monkeypatch):
    a = O.Optics(0.996, 0.0344, -0.0026, 0.25, _kernel(), cache_dir=str(tmp_path))
    tab = np.asarray(a.image_table((-1, 0, 1)))
    assert tab.shape == (2, O.C.size, O.Y.size)
    assert len(list(tmp_path.glob('img_*.npy'))) == 2            # raw and with the kernel
    monkeypatch.setattr(O.Optics, '_raw_image', lambda self, mask: pytest.fail('rebuilt a cached table'))
    b = O.Optics(0.996, 0.0344, -0.0026, 0.25, _kernel(), cache_dir=str(tmp_path))
    assert np.array_equal(np.asarray(b.image_table((-1, 0, 1))), tab)
    c = O.Optics(0.996, 0.0344, -0.0026, 0.25, _kernel(0.05), cache_dir=str(tmp_path))
    assert not np.array_equal(np.asarray(c.image_table((-1, 0, 1))), tab)   # new kernel, same raw table
    assert len(list(tmp_path.glob('img_*.npy'))) == 3
    assert O.Optics._mask((-7, -1, 0, 1, 6)) == (-1, 0, 1)        # shutters beyond the tables' reach dropped


def test_x_extended_ratio(small_grids, tmp_path, monkeypatch):
    """Along the dispersion: one for a point, falling as the source widens, and a wide source
    (flat over the shutter) passes the integral of a point's transmission over its Gaussian's
    peak, 1 / sqrt(2 pi) sigma: less than the shutter's geometric width 2 X_HALF, the more so
    in the red, where light diffracted at the narrow shutter misses the stop."""
    monkeypatch.setattr(O, 'XC', np.round(np.arange(-1.5, 1.5001, 0.005), 4))
    opt = O.Optics(0.996, 0.0344, -0.0026, 0.25, _kernel(), cache_dir=str(tmp_path))
    lam = np.array([1.0, 3.0])
    assert np.allclose(opt.x_extended_ratio(lam, 0.1, 0.003), 1.0)
    r = [opt.x_extended_ratio(lam, 0.0, w) for w in (0.1, 0.3, 0.9)]
    assert np.all(r[0] < 1.0) and np.all(r[1] < r[0]) and np.all(r[2] < r[1])
    tab = np.asarray(opt.x_transmission_table())
    point = np.array([np.interp(0.0, O.XC, t) for t in tab])
    s = 0.9 * O.SLIT
    area = tab.sum(axis=1) * (O.XC[1] - O.XC[0])
    assert np.allclose(r[2] * point, area / (np.sqrt(2 * np.pi) * s), rtol=0.03)
    assert area[1] < area[0] < 2 * O.X_HALF
    assert np.isfinite(opt.x_extended_ratio(lam, np.nan, 0.3)).all()       # no SRCXPOS: the centre


def test_kernel_spreads_the_image(small_grids, tmp_path):
    opt = O.Optics(0.996, 0.0344, -0.0026, 0.25, _kernel(), cache_dir=str(tmp_path))
    y = np.linspace(-0.6, 0.6, 241)
    a = opt.image((0,), y, np.full(y.size, 1.0), 0.0, 0.003)
    b = opt._raw_image((0,))[0, np.argmin(np.abs(O.C)), :]
    sel = np.abs(O.Y) < 0.6
    rms = lambda yy, I: np.sqrt(np.sum(I * yy ** 2) / np.sum(I))      # noqa: E731
    assert rms(y, a) > rms(O.Y[sel], b[sel])


# ---------------------------------------------------------------------------
# which exposures take the model
# ---------------------------------------------------------------------------
def test_every_disperser_takes_the_shipped_model(optics_model):
    shipped = O.load_optics()
    assert set(shipped) == {'version', 'NRS1', 'NRS2'}
    assert O.optics_for('nrs2').params == shipped['NRS2'].params
    exps = _exposures()
    for grating in ('PRISM/CLEAR', 'G395M/F290LP', 'G140H/F100LP'):
        for e in exps:
            e.grating = grating
        assert PF.model_optics(exps) == [O.optics_for('NRS1')] * 3
    mine = [Analytic()] * 3
    assert PF.model_optics(exps, optics=mine) == mine               # a model being calibrated


def test_positions_in_slit_units():
    assert PF.source_position(0.2) == pytest.approx(0.2 * MSA_SLIT_PITCH)


def test_tests_run_the_standin_by_default():
    assert isinstance(O.optics_for('NRS1'), Analytic)
    assert all(isinstance(o, Analytic) for o in PF.ProfileModel(_exposures()).optics)


# ---------------------------------------------------------------------------
# wiring into the profile fit (an analytic stand-in for the tables)
# ---------------------------------------------------------------------------
def test_model_image_and_throughput_from_the_optics(monkeypatch):
    exps = _exposures(src_y=0.1)
    for e in exps:
        e.src_y_jwst, e.pl_ref = 0.1 / MSA_SLIT_PITCH, 'jwst_nirspec_pathloss_0010.fits'
    monkeypatch.setattr(CR, 'point_y_ratio', lambda ref, lam, srcy: np.full(np.shape(lam), 1.25))
    M = PF.ProfileModel(exps, optics=[Analytic()] * 3)
    p = np.array([0.1, 0.0, np.log(0.05), 0.0])
    e = exps[0]
    y, lam, dy = e.y[:, 40], e.wav[:, 40], e.dy[:, 40]
    c = M.centre(p, np.zeros(3), 0, lam)
    # the width where the source lands: 0.05 on the sky, stretched along the slit by the
    # trace-centre law as its position is
    w = 0.05 * M.trace_stretch(0, lam)
    assert np.allclose(M.image_width(p, 0, lam), w) and np.ptp(w) > 0
    want = Analytic().image(e.open_k, y, lam, c, w) * dy / O.DY_ROW
    assert np.allclose(M.image(p, np.zeros(3), 0, y, lam, dy), want)
    tau = M.throughput(p, np.zeros(3), 0, lam, dy)
    t0 = Analytic().transmission(PF.REF_APERTURE, lam, 0.0, PF.W_FLOOR)
    assert np.allclose(tau, 0.8 * 1.25 / t0)          # jwst's level moved to the centre, over the model's there


def test_throughput_takes_the_x_correction_at_srcxpos():
    class Corrected(Analytic):
        def x_correction(self, lam, srcx):           # one without SRCXPOS, as Optics.x_correction
            return np.full(np.shape(lam), 1.0 - srcx if np.isfinite(srcx) else 1.0)
    exps = _exposures()
    for e, x in zip(exps, (0.0, 0.1, np.nan)):
        e.src_x_jwst = x
    p = np.array([0.0, 0.0, np.log(0.05), 0.0])
    lam, dy = exps[0].wav[:, 40], exps[0].dy[:, 40]
    plain = PF.ProfileModel(exps, optics=[Analytic()] * 3)
    corr = PF.ProfileModel(exps, optics=[Corrected()] * 3)
    for k, want in enumerate((1.0, 0.9, 1.0)):       # no SRCXPOS: no correction
        a, b = plain.throughput(p, np.zeros(3), k, lam, dy), corr.throughput(p, np.zeros(3), k, lam, dy)
        assert np.allclose(b, want * a)


def test_fit_through_the_optics_recovers_shape_and_flux(monkeypatch):
    monkeypatch.setattr(CR, 'point_y_ratio', lambda ref, lam, srcy: np.ones(np.shape(lam)))
    exps = _exposures()
    M = PF.ProfileModel(exps, chromatic=True, optics=[Analytic()] * 3)
    lam = M.lam_grid
    truth = PF.FitResult(params=dict(TRUTH, pivot=M.pivot, centre_prior=M.c_prior), eoff=np.zeros(3), lam=lam,
                         flux=200 * _spectrum(lam), flux_err=np.zeros_like(lam), chi2=0.0, npix=0, nfree=0,
                         chromatic=True, model=M)
    rng = np.random.default_rng(3)
    noise = [rng.normal(size=(NY, NX)) for _ in exps]
    for k, e in enumerate(exps):
        e.diff = PF.model_frame(truth, k, applied_pathloss=False) + noise[k] - np.mean([noise[j] for j in e.partners], axis=0)
        e.var = np.ones((NY, NX))
    R = PF.fit(exps, model=PF.ProfileModel(exps, chromatic=True, optics=[Analytic()] * 3))
    assert R.params['centre'] == pytest.approx(TRUTH['centre'], abs=0.02)
    assert R.params['width'] == pytest.approx(TRUTH['width'], abs=0.02)
    good = np.isfinite(R.flux) & (R.lam > 1.0) & (R.lam < 4.8)
    assert np.median(R.flux[good] / (200 * _spectrum(R.lam[good]))) == pytest.approx(1.0, abs=0.01)
    assert PF.optics_version() == O.version()
