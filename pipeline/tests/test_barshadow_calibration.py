"""Unit tests for the empirical bar-shadow derivation (synthetic inputs, no data, no CRDS)."""
import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nirspec import barshadow_calibration as B


def test_segment_map_three_shutter_slitlet():
    # state '1x1': shutters 0 (open), 1 (source), 2 (open); margins at j = -1 and j = 3
    j = np.array([0, 0, 1, 1, 2, 2, -1, 3, 3])
    f = np.array([-0.3, 0.2, -0.1, 0.1, -0.2, 0.4, -0.1, -0.4, 0.3])
    seg, y = B.segment_map(j, f, '1x1')
    S = B.SEGMENTS
    assert seg[0] == S.index('edge_lower') and y[0] == pytest.approx(-0.3)      # closed below the first shutter
    assert seg[1] == S.index('bar_lower') and y[1] == pytest.approx(0.2 - 1.0)  # bar between shutter 0 and the source
    assert seg[2] == S.index('bar_upper') and y[2] == pytest.approx(-0.1)       # the source shutter still maps (caller excludes it)
    assert seg[3] == S.index('bar_lower')
    assert seg[4] == S.index('bar_upper') and y[4] == pytest.approx(-0.2)
    assert seg[5] == S.index('edge_upper') and y[5] == pytest.approx(0.4)       # closed above the last shutter
    assert seg[6] == S.index('edge_lower') and y[6] == pytest.approx(-1.1)      # margin below
    assert seg[7] == S.index('edge_upper') and y[7] == pytest.approx(0.6)       # margin above
    assert seg[8] == S.index('edge_upper') and y[8] == pytest.approx(1.3)


def test_segment_map_closed_shutter_inside():
    # '1x01': shutter 2 is closed; shutter 3's lower half faces a closed shutter -> outer edge
    seg, y = B.segment_map(np.array([2, 3, 1]), np.array([-0.2, -0.2, 0.3]), '1x01')
    S = B.SEGMENTS
    assert seg[0] == -1                                   # closed shutter measures nothing
    assert seg[1] == S.index('edge_lower')
    assert seg[2] == S.index('edge_upper')                # the source's upper half faces the closed shutter


def _trapezoid(y, half_open=0.3, floor=0.45):
    """A shutter profile: 1 inside |y| < half_open, linear to the floor at the bar."""
    a = np.abs(y - np.rint(y))
    return np.clip(1 - (a - half_open) / (0.5 - half_open) * (1 - floor), floor, 1.0)


def _synthetic_cells(grid, profile_fn, n=200):
    """Accumulated (seg, ybin, col, 3) cells sampled from a known profile."""
    total = np.zeros((len(B.SEGMENTS), len(B.Y_CENTRES), grid.n_w, 3))
    rng = np.random.default_rng(1)
    for si, name in enumerate(B.SEGMENTS):
        lo, hi = {'edge_lower': (-1, 0), 'edge_upper': (0, 1), 'bar_lower': (-1, -0.5), 'bar_upper': (-0.5, 0)}[name]
        for bi, yc in enumerate(B.Y_CENTRES):
            if not (lo <= yc <= hi):
                continue
            val = profile_fn(yc)
            samples = val + rng.normal(0, 0.02, size=(n, grid.n_w))
            total[si, bi, :, 0] = samples.sum(axis=0)
            total[si, bi, :, 1] = (samples ** 2).sum(axis=0)
            total[si, bi, :, 2] = n
    return total


def test_build_tables_reproduces_a_known_profile():
    grid = B.Grid(w0=1e-6, dw=0.05e-6, n_w=21, y0=-1.0, dy=0.002, n_y=1001)
    total = _synthetic_cells(grid, _trapezoid)
    t = B.build_tables(total, grid, smooth_cols=0, n_min=20)
    rows = grid.rows
    truth = _trapezoid(rows)
    for key in ('data1x1', 'data1x3'):
        arr = t[key]
        inside = np.abs(rows - np.rint(rows)) < 0.48
        assert np.nanmax(np.abs(arr[inside, 10] - truth[inside])) < 0.02, key
        assert np.nanmean(arr[np.abs(rows) < 0.1, :]) == pytest.approx(1.0, abs=0.01)   # normalised at the centre
    # the upper half of DATA1X3 mirrors the lower half
    d3 = t['data1x3']
    assert np.allclose(d3[rows > 0.01][:, 5], d3[rows < -0.01][::-1][: (rows > 0.01).sum()][:, 5], atol=1e-6)


def test_evaluate_table_reads_the_right_rows():
    grid = B.Grid(w0=1e-6, dw=0.05e-6, n_w=21, y0=-1.0, dy=0.002, n_y=1001)
    d1 = np.tile(np.linspace(0, 1, grid.n_y)[:, None], (1, grid.n_w))     # value == (row+1)/2
    d3 = np.tile(np.linspace(2, 3, grid.n_y)[:, None], (1, grid.n_w))
    table = dict(data1x1=d1, data1x3=d3, grid=grid)
    S = B.SEGMENTS
    v = B.evaluate_table(table, np.array([S.index('edge_lower'), S.index('edge_upper'), S.index('bar_lower')]),
                         np.array([-0.5, 0.5, -0.75]), np.array([1.5, 1.5, 1.5]))
    assert v[0] == pytest.approx(0.25, abs=1e-3)
    assert v[1] == pytest.approx(0.75, abs=1e-3)
    assert v[2] == pytest.approx(2.125, abs=1e-3)


def test_pool_sky_rejects_a_scaled_shutter():
    grid = B.Grid(w0=1e-6, dw=0.05e-6, n_w=21, y0=-1.0, dy=0.002, n_y=1001)
    base = np.linspace(0.2, 1.0, grid.n_w)
    shutters = {('a', i): (base * (1 + 0.01 * (i % 3)), np.full(grid.n_w, 30)) for i in range(12)}
    shutters[('bad', 0)] = (base * 1.5, np.full(grid.n_w, 30))
    sky, n, scales = B.pool_sky(shutters, grid)
    assert not scales[('bad', 0)][1]
    assert sum(cl for _, cl in scales.values()) == 12
    assert np.allclose(sky, base, rtol=0.02)


def test_write_reference_roundtrip(tmp_path):
    grid = B.Grid(w0=1e-6, dw=0.05e-6, n_w=21, y0=-1.0, dy=0.002, n_y=1001)
    # a synthetic template in the CRDS layout
    hdus = [fits.PrimaryHDU()]
    hdus[0].header['REFTYPE'] = 'BARSHADOW'; hdus[0].header['PEDIGREE'] = 'INFLIGHT 2022-01-01 2023-01-01'
    for name in ('DATA1X1', 'VAR1X1', 'DATA1X3', 'VAR1X3'):
        h = fits.ImageHDU(data=np.ones((grid.n_y, grid.n_w)), name=name)
        h.header['CRVAL1'] = grid.w0; h.header['CDELT1'] = grid.dw; h.header['CRPIX1'] = 1.0
        h.header['CRVAL2'] = grid.y0; h.header['CDELT2'] = grid.dy; h.header['CRPIX2'] = 1.0
        hdus.append(h)
    template = tmp_path / 'template.fits'; fits.HDUList(hdus).writeto(template)
    total = _synthetic_cells(grid, _trapezoid)
    t = B.build_tables(total, grid, smooth_cols=0)
    out = tmp_path / 'empirical.fits'
    B.write_reference(t, str(template), str(out), dict(observations=['obs_a', 'obs_b'], n_pixels=123, n_shutters=4,
                                                       version='0.0.test', options=dict(grating='PRISM')))
    with fits.open(out) as h:
        assert [x.name for x in h] == ['PRIMARY', 'DATA1X1', 'VAR1X1', 'DATA1X3', 'VAR1X3']
        assert h[0].header['CFBSNOBS'] == 2 and h[0].header['CFBSNPIX'] == 123
        assert any('observation: obs_a' in str(c) for c in h[0].header['HISTORY'])
        assert h['DATA1X1'].header['CRVAL2'] == -1.0 and h['DATA1X1'].data.dtype.itemsize == 4
    back = B.load_table(str(out))
    assert back['grid'].n_w == grid.n_w
    assert np.allclose(back['data1x1'], t['data1x1'], atol=1e-6)


def test_source_column_when_out():
    # a three-point nod stepping one column down: the source leaves a two-shutter slitlet at dither 3
    assert B.source_column_when_out({1: 28, 2: 27}, 3) == 26
    # out at dither 1 instead, stepping up
    assert B.source_column_when_out({2: 27, 3: 28}, 1) == 26
    # a single known point gives no direction; an irregular pattern no step
    assert B.source_column_when_out({1: 28}, 3) is None
    assert B.source_column_when_out({1: 28, 2: 27, 3: 27}, 4) is None
    assert B.source_column_when_out({}, 2) is None
