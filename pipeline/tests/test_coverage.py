"""The extended slitlet geometry and the counter's footprint rule, on a fake slit transform."""
import numpy as np

from campfire_pipeline.nirspec import coverage as C


def _transform(x, y):
    """A slit whose trace runs along x: wavelength 1 + 0.001 x um (in metres, as jwst's
    slit frame gives it), slit y = (y - 10) / 1.15 heights, defined for |slit y| within
    the slitlet's extent [-1.05, 3.35] like jwst's pads, NaN elsewhere."""
    x = np.asarray(x, float); y = np.asarray(y, float)
    ys = (y - 10.0)                     # slit-frame y in open-shutter heights
    lam = (1.0 + 0.001 * x) * 1e-6
    inside = (ys >= -1.05) & (ys <= 3.35)
    return np.where(inside, x, np.nan), np.where(inside, ys, np.nan), np.where(inside, lam, np.nan)


def test_geometry_on_block_covers_the_slit_extent_and_converts_units():
    g = C.geometry_on_block(_transform, 0, 0, 0, 20, 'x11', 1.15, cutoffs=(None, None), ncols=50, nrows=20)
    assert g['lam'].shape == (20, 50) and np.nanmax(g['lam']) < 2       # um, not metres
    assert np.isclose(np.nanmin(g['lam']), 1.0) and np.isclose(np.nanmax(g['lam']), 1.049)
    # rows 9-13 lie within the slitlet's pads: finite there, NaN beyond
    assert g['finite'][9:14].all() and not g['finite'][:8].any() and not g['finite'][15:].any()
    # pitches: slit y 1.15 heights is one pitch -> shutter 1 (open), j = 1
    assert np.isclose(g['ys'][11, 0], 1.0 / 1.15) and g['j'][10, 0] == 0 and g['is_open'][10, 0]
    assert g['seg'].shape == g['lam'].shape and g['footprint'].sum() == g['finite'].sum()


def test_cutoffs_bound_the_footprint_not_the_transform():
    g = C.geometry_on_block(_transform, 0, 0, 0, 20, 'x11', 1.15, cutoffs=(1.0095, 1.0305), ncols=50, nrows=20)
    cols = np.flatnonzero(g['footprint'].any(axis=0))
    assert cols[0] == 10 and cols[-1] == 30                                 # 1.0095 <= lam <= 1.0305
    assert g['finite'].any(axis=0).sum() == 50                              # the transform is finite everywhere
    assert C.cutoffs_for('g395m', 'f290lp') == (2.60, 5.7) and C.cutoffs_for('G140M', 'F100LP') == (0.96, None)
    assert C.cutoffs_for('G140H', 'F100LP') == (None, None)


def test_cutout_origin_shifts_the_transform_and_add_block_places_the_rows():
    # a cutout-relative transform (jwst's extract_2d slit): the same slit seen from a cutout at (x0, y0) = (5, 4)
    g = C.geometry_on_block(_transform, 5, 4, 0, 24, 'x11', 1.15, ncols=60, nrows=24)
    assert g['finite'][13:18].all() and not g['finite'][:12].any()         # rows 9-13 moved to 13-17
    assert np.isclose(np.nanmin(g['lam']), 1.0 - 0.005)                     # column 0 is x = -5 in the slit's frame
    frame = np.zeros((40, 60), np.int16)
    C.add_block(frame, g)
    assert frame[13:18].all() and not frame[18:].any() and frame.sum() == g['footprint'].sum()


def test_cache_path_changes_with_the_cutoffs_and_the_version():
    a = C.cache_path('/x/jw_rate.fits', '/c', (None, None))
    b = C.cache_path('/x/jw_rate.fits', '/c', (0.96, None))
    assert a != b and f'_v{C.COVERAGE_VERSION}' in a and a.endswith('.npy')
    assert C.cache_path('/x/jw_rate.fits', '/c', (0.96, None)) == b


def test_failed_open_shutters_carry_a_commanded_slitlets_margin(monkeypatch):
    from jwst.msaflagopen import msaflag_open
    from jwst.msaflagopen.msaflag_open import Slit
    jwst_rec = Slit(1, 7, 0, 7, 1, -0.5, 0.5, 3, 0, 'x', slit_id=1)        # as create_slitlets makes them
    monkeypatch.setattr(msaflag_open, 'create_slitlets', lambda ref: [jwst_rec])
    rec, = C.failed_open_slitlets('msaoper.json')
    assert (rec.ymin, rec.ymax) == (-1.0, 1.0) and rec.shutter_state == 'x' and rec.quadrant == 3
    assert jwst_rec.slit_yscale == 1.0 and np.isclose(C._pitch(rec.slit_yscale), 1.15)    # a real shutter's pitch

    # on a one-shutter slit defined over that extent, the rows past the open area are its
    # margin (j = -1 / 1) and read the closed-neighbor rows of the 1x1 profile
    def tr(x, y):
        x = np.asarray(x, float); ys = np.asarray(y, float) - 10.0
        inside = np.abs(ys) <= C.FAILED_OPEN_Y
        lam = (1.0 + 0.001 * x) * 1e-6
        return np.where(inside, x, np.nan), np.where(inside, ys, np.nan), np.where(inside, lam, np.nan)
    g = C.geometry_on_block(tr, 0, 0, 0, 20, 'x', 1.15, ncols=10, nrows=20)
    col = g['finite'][:, 0]
    assert np.flatnonzero(col).tolist() == [9, 10, 11]
    assert g['is_open'][10, 0] and not g['is_open'][9, 0] and not g['is_open'][11, 0]
    assert g['j'][9, 0] == -1 and g['j'][11, 0] == 1
    from campfire_pipeline.nirspec.barshadow_calibration import SEGMENTS
    assert g['seg'][9, 0] == SEGMENTS.index('edge_lower') and g['seg'][11, 0] == SEGMENTS.index('edge_upper')
