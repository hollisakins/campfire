"""Unlisted open shutters (nirspec/unlisted_open.py): the fixed-light map, the traces
found in it, the background-shape verdict, and master_bkg's search per configuration."""
import numpy as np

from campfire_pipeline.nirspec import master_bkg as MB
from campfire_pipeline.nirspec import unlisted_open as U


def _fit(ratios, chi2n=1.2, n=1000):
    return dict(bands=[(i, i + 1, r, 0.01) for i, r in enumerate(ratios)], chi2=chi2n * n, n=n)


def test_verdict_background_shaped():
    # 549120's shutter in zenith_p15: 0.92-0.98 over six bands, chi2/n 1.13
    assert U.verdict(_fit([0.92, 0.95, 0.98, 0.96, 0.93, 0.94], chi2n=1.13))


def test_verdict_rejects_other_spectra():
    assert not U.verdict(_fit([0.35, 0.45, 0.55, 0.62, 0.68, 0.70]))        # rising: G395M-like chromatic
    assert not U.verdict(_fit([6.2, 6.5, 6.9, 6.4, 6.3, 6.6], chi2n=8.2))    # the NRS1 instrumental patch
    assert not U.verdict(_fit([0.02, 0.04, 0.06, 0.03, 0.05, 0.04]))        # too faint to be a shutter
    assert not U.verdict(_fit([0.9, 0.95]))                                   # too few bands
    assert not U.verdict(_fit([0.92, 0.95, 0.98, 0.96], chi2n=2.5))          # the shape is right, the fit is not


def test_shutter_name_never_an_msaoper_index():
    # the painter skips msaoper failed-open shutters by their numeric names
    name = U.shutter_name(4, 86, 43)
    assert name == 'U4_86_43' and not name.isdigit()


def _maps(fixed_val, source_rows, shape=(40, 200)):
    """Three nod positions: fixed light on rows 10-13 everywhere, a source on
    ``source_rows[n]`` in nod position n."""
    per_nod = {}
    for n, rows in enumerate(source_rows, 1):
        m = np.zeros(shape)
        m[10:14] += fixed_val
        m[rows] += 5.0
        per_nod[n] = (m, np.full(shape, 0.05))
    return per_nod


def test_minimum_over_nods_keeps_fixed_light_drops_a_source():
    per_nod = _maps(1.0, [slice(20, 24), slice(24, 28), slice(28, 32)])
    means = np.array([m for m, e in per_nod.values()])
    excess = means.min(0)
    fixed = dict(excess=excess, err=np.full(excess.shape, 0.05), sky=np.full(excess.shape, 1.0))
    traces = U.find_traces(fixed, width=8, snr=5, min_frac=0.2, min_cols=48, min_rows=2)
    assert len(traces) == 1
    t = traces[0]
    assert t['rows'] == (10, 13)
    nc = U.nod_consistency(t, per_nod)
    assert nc['spread'] < 0.01                     # the same in every nod position: fixed


def test_nod_consistency_flags_moving_light():
    shape = (40, 200)
    per_nod = {}
    for n, amp in enumerate((1.0, 3.0, 6.0), 1):  # a large source whose light on the trace changes with the nod
        m = np.zeros(shape)
        m[10:14] = amp
        per_nod[n] = (m, np.full(shape, 0.05))
    mask = np.zeros(shape, bool)
    mask[10:14] = True
    nc = U.nod_consistency(dict(mask=mask), per_nod)
    assert nc['spread'] > 0.2 and nc['chi2'] / nc['dof'] > 9


def test_search_per_configuration_prism_only(monkeypatch):
    keys = {'a_00001_nrs1_rate.fits': ('m_msa.fits', 1, 'PRISM', 'CLEAR', 'NRS1'),
            'a_00002_nrs1_rate.fits': ('m_msa.fits', 1, 'PRISM', 'CLEAR', 'NRS1'),
            'b_00001_nrs1_rate.fits': ('m_msa.fits', 1, 'G395M', 'F290LP', 'NRS1')}
    monkeypatch.setattr(U, 'group_key', lambda p: keys[p])
    calls = []

    def fake_detect(ps, logger=None):
        calls.append(sorted(ps))
        return [dict(q=4, x=86, y=43, t=0.9, chi2n=1.1, ratio_lo=0.92, ratio_hi=0.98)]

    monkeypatch.setattr(U, 'detect', fake_detect)
    found, status = MB._unlisted_open(list(keys))
    assert calls == [['a_00001_nrs1_rate.fits', 'a_00002_nrs1_rate.fits']]     # one search per configuration
    assert set(found) == {'a_00001_nrs1', 'a_00002_nrs1'}
    assert status['b_00001_nrs1'] == 'not searched (G395M)'
    assert status['a_00001_nrs1'] == 'searched, 1 found'


class _FakeMap:
    """MsaMap stand-in: shutter y of a run fills detector rows 5 (y - 1) .. 5 y - 1 over
    every column, wavelength rising along the columns; x does not matter."""
    shape = (60, 200)

    def exact(self, candidates):
        out = []
        for c in candidates:
            q, x, y, ny = (tuple(c) + (1,))[:4]
            r0, h = 5 * (y - 1), 5 * ny
            rows = np.arange(h)
            j = np.repeat((rows // 5)[:, None], self.shape[1], 1)
            lam = np.broadcast_to(1.0 + np.arange(self.shape[1]) / 50.0, (h, self.shape[1])).copy()
            out.append(dict(state='x' + '1' * (ny - 1), row_lo=r0, lam=lam, j=j, footprint=np.ones((h, self.shape[1]), bool)))
        return out


def _background(g, rate_sky, rate_q, table):
    return np.where(g['footprint'], 2.0 / g['lam'], np.nan)       # a smooth, falling background


def _leak(ys, t=0.9, seed=1, sigma=0.25):
    rng = np.random.default_rng(seed)
    ex = np.zeros(_FakeMap.shape)
    for y in ys:
        lam = 1.0 + np.arange(_FakeMap.shape[1]) / 50.0
        ex[5 * (y - 1):5 * y] += t * 2.0 / lam
    mask = ex > 0
    ex += rng.normal(0, sigma, ex.shape)
    return dict(excess=ex, err=np.full(ex.shape, sigma)), dict(mask=mask)   # a trace a few sigma per pixel, as obs 24's


def test_a_leak_through_three_shutters_grows_to_the_run(monkeypatch):
    monkeypatch.setattr(U, 'background_through', _background)
    fixed, trace = _leak([4, 5, 6])
    best = U.refine(trace, fixed, _FakeMap(), [dict(q=4, x=188, y=6)], None, None, None, neighbors=2)
    assert best[0]['ny'] == 1 and not U.verdict(best[0])           # one shutter leaves two thirds of the trace
    run = U.grow(trace, fixed, _FakeMap(), best[0], None, None, None)
    assert (run['y'], run['ny']) == (4, 3)
    assert np.allclose(run['ts'], 0.9, atol=0.01) and abs(run['t'] - 0.9) < 0.01
    assert U.verdict(run)


def test_a_one_shutter_leak_does_not_grow(monkeypatch):
    monkeypatch.setattr(U, 'background_through', _background)
    fixed, trace = _leak([5])
    best = U.refine(trace, fixed, _FakeMap(), [dict(q=4, x=188, y=5)], None, None, None, neighbors=2)
    run = U.grow(trace, fixed, _FakeMap(), best[0], None, None, None)
    assert (run['y'], run['ny']) == (5, 1) and U.verdict(run)


def test_a_run_is_one_slit_like_a_commanded_slitlet():
    from jwst.assign_wcs.nirspec import MSA_SLIT_SCALES
    s = U.one_shutter(7, 4, 188, 24, 3)
    assert s.shutter_state == 'x11' and (s.xcen, s.ycen) == (188, 24)
    pitch = U.CV._pitch(MSA_SLIT_SCALES[1])
    assert s.ymin == -U.CV.FAILED_OPEN_Y and abs(s.ymax - (U.CV.FAILED_OPEN_Y + 2 * pitch)) < 1e-9
    assert U.shutter_name(4, 188, 24, 3) == 'U4_188_24-26' and U.run_label(4, 188, 24, 3) == 'q4 x188 y24-26'
