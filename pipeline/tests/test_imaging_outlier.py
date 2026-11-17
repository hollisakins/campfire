"""Field-level outlier detection on synthetic chips (imaging/outlier.py).

Every chip looks at the same analytic scene (Gaussian stars on a flat, already
subtracted background) through its own tangent-plane WCS, so dithers and
rotations are exact and the truth about every pixel is known.
"""

import numpy as np
import pytest
from astropy.wcs import WCS

from campfire_pipeline.imaging.exposure import ChipArrays
from campfire_pipeline.imaging.outlier import (_world_to_pixel,
                                               detect_outliers, stack_grid,
                                               star_protection)

SCALE = 0.1                    # arcsec per pixel
CENTER = (150.0, 2.0)          # deg
SIGMA_BG = 1.0                 # background noise per pixel
POISSON = 0.02                 # source variance per unit flux


def _wcs(shape, dx=0.0, dy=0.0, rot=0.0):
    """TAN WCS whose pixel (nx/2 + dx, ny/2 + dy) looks at CENTER."""
    ny, nx = shape
    th = np.radians(rot)
    s = SCALE / 3600.0
    w = WCS(naxis=2)
    w.wcs.crpix = [nx / 2 + dx + 1.0, ny / 2 + dy + 1.0]
    w.wcs.crval = list(CENTER)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.cd = np.array([[-s * np.cos(th), s * np.sin(th)],
                         [s * np.sin(th), s * np.cos(th)]])
    return w


def _scene(ra, dec, stars, fwhm):
    """Surface brightness of the stars at sky positions (arcsec offsets)."""
    x = (np.asarray(ra) - CENTER[0]) * np.cos(np.radians(CENTER[1])) * 3600.0
    y = (np.asarray(dec) - CENTER[1]) * 3600.0
    sig = fwhm / 2.3548
    out = np.zeros(np.shape(x))
    for sx, sy, amp in stars:
        out += amp * np.exp(-0.5 * ((x - sx) ** 2 + (y - sy) ** 2) / sig ** 2)
    return out


class FakeChip:
    instrument = 'fake'
    filter = 'f000w'
    exptime = xposure = 1000.0
    pixel_area_arcsec2 = SCALE ** 2

    def __init__(self, key, shape, wcs, stars, fwhm, rng, noise=True):
        self.key, self.path, self.shape, self._w = key, key, shape, wcs
        yy, xx = np.indices(shape, dtype=float)
        ra, dec = wcs.all_pix2world(xx, yy, 0)
        self.truth = _scene(ra, dec, stars, fwhm)
        var = SIGMA_BG ** 2 + POISSON * self.truth
        self.sci = self.truth + (rng.normal(0, 1, shape) * np.sqrt(var) if noise else 0)
        self.good = np.ones(shape, bool)

    def load(self):
        return ChipArrays(
            sci=self.sci.astype(np.float32),
            var_rnoise=np.full(self.shape, SIGMA_BG ** 2, np.float32),
            var_poisson=(POISSON * np.clip(self.truth, 0, None)).astype(np.float32),
            var_flat=np.zeros(self.shape, np.float32), good=self.good.copy())

    def pixel_to_world(self, x, y):
        return self._w.all_pix2world(x, y, 0)

    def provenance(self):
        return {'key': self.key}


def _stars(rng, n=25, half=8.0, amp=(30, 400)):
    return [(rng.uniform(-half, half), rng.uniform(-half, half),
             rng.uniform(*amp)) for _ in range(n)]


def _dithered(rng, n, stars, fwhm, shape=(200, 200), noise=True, rot=None):
    offs = [(rng.uniform(-4, 4), rng.uniform(-4, 4)) for _ in range(n)]
    rot = rot or [0.0] * n
    return [FakeChip(f'c{k}', shape, _wcs(shape, dx, dy, r), stars, fwhm, rng,
                     noise=noise)
            for k, ((dx, dy), r) in enumerate(zip(offs, rot))]


def _hit(chip, rng, n, amp):
    """Add ``n`` single-pixel hits of ``amp`` away from the frame edge."""
    ny, nx = chip.shape
    y = rng.integers(10, ny - 10, n)
    x = rng.integers(10, nx - 10, n)
    chip.sci[y, x] += amp
    hit = np.zeros(chip.shape, bool)
    hit[y, x] = True
    return hit


def test_the_flag_test_is_the_library_routine_nircam_runs(monkeypatch):
    """HST and NIRCam run one flag test, stcal's flag_resampled_crs: the HST
    pass calls it once per chip to be flagged, with the thresholds given."""
    import stcal.outlier_detection.utils as su

    calls = []
    real = su.flag_resampled_crs

    def spy(sci, err, blot, snr1, snr2, scale1, scale2, backg):
        calls.append((snr1, snr2, scale1, scale2, backg, sci.shape))
        return real(sci, err, blot, snr1, snr2, scale1, scale2, backg)

    monkeypatch.setattr(su, 'flag_resampled_crs', spy)
    rng = np.random.default_rng(0)
    chips = _dithered(rng, 3, [], 0.25)
    detect_outliers(chips, snr=(4.0, 3.0), scale=(1.5, 0.7),
                    flag_keys=[chips[0].key])
    assert calls == [(4.0, 3.0, 1.5, 0.7, 0.0, chips[0].shape)]


def test_stack_grid_covers_every_chip():
    rng = np.random.default_rng(0)
    chips = _dithered(rng, 3, [], 0.25, rot=[0.0, 20.0, -35.0])
    grid = stack_grid(chips)
    assert grid.pixel_scale == pytest.approx(SCALE)
    nx, ny = grid.shape
    for c in chips:
        h, w = c.shape
        ra, dec = c.pixel_to_world(np.array([0, w - 1, w - 1, 0.0]),
                                   np.array([0, 0, h - 1, h - 1.0]))
        x, y = grid.world_to_pixel(ra, dec)
        assert x.min() >= 0 and y.min() >= 0 and x.max() <= nx - 1 and y.max() <= ny - 1


def test_noiseless_dithered_scene_is_reproduced_and_nothing_is_flagged():
    # The blot of the median has to land on each chip's own pixels: with no
    # noise and no hits the model equals the data to within what resampling a
    # well-sampled star costs, at any dither and rotation.
    rng = np.random.default_rng(1)
    stars = _stars(rng)
    chips = _dithered(rng, 4, stars, 0.30, noise=False, rot=[0.0, 0.0, 15.0, -40.0])
    res = detect_outliers(chips)
    for c in chips:
        assert res['flags'][c.key].sum() == 0
        assert res['stats'][c.key]['n_testable'] > 0.5 * c.sci.size
    assert np.nanmax(res['median']) > 100          # the stars are in the median


def test_hits_are_found_and_clean_pixels_left_alone():
    rng = np.random.default_rng(2)
    stars = _stars(rng)
    chips = _dithered(rng, 4, stars, 0.30)
    hits = {c.key: _hit(c, rng, 150, 15.0) for c in chips}
    res = detect_outliers(chips)
    from scipy.ndimage import binary_dilation
    for c in chips:
        mask, hit = res['flags'][c.key], hits[c.key]
        testable = res['stats'][c.key]['n_testable']
        found = (mask & hit).sum() / hit.sum()
        # a 15 sigma hit on blank background is far above the 3.5 sigma bar; the
        # few misses sit on stars, where the gradient term raises the bar
        assert found > 0.9
        stray = mask & ~binary_dilation(hit, structure=np.ones((3, 3), bool))
        assert stray.sum() < 2e-3 * testable


def test_two_exposures_need_the_minimum():
    # With two chips the median is the mean, so a hit in one drags the model up
    # by half its height and both pixels would differ from it by the same
    # amount. Taking the minimum where the two disagree puts the model on the
    # clean value, and only the hit pixel is flagged.
    #
    # The hits are 3x3 pixels wide: a single-pixel hit is spread so thin by the
    # single drizzle that the mean of two barely moves, and the plain median
    # copes; a hit a few pixels across is the case that needs the minimum.
    rng = np.random.default_rng(3)
    stars = _stars(rng, n=10)
    chips = _dithered(rng, 2, stars, 0.30)
    ys, xs = rng.integers(10, 190, 60), rng.integers(10, 190, 60)
    hit = np.zeros(chips[0].shape, bool)
    for y, x in zip(ys, xs):
        chips[0].sci[y - 1:y + 2, x - 1:x + 2] += 40.0
        hit[y - 1:y + 2, x - 1:x + 2] = True
    res = detect_outliers(chips)
    assert (res['flags']['c0'] & hit).sum() > 0.9 * hit.sum()
    # the other chip, clean at those places, is not flagged for the first's hits
    assert res['flags']['c1'].sum() < 2e-3 * chips[1].sci.size

    # PAIRED: without the minimum the model sits half way up every hit, and the
    # clean chip is flagged there as well.
    off = detect_outliers(chips, minmed_max_n=0)
    assert off['flags']['c1'].sum() > 10 * max(res['flags']['c1'].sum(), 1)


def test_same_place_hit_in_two_of_three_is_found_in_both():
    # No dither, so pixel (y, x) is the same sky in all three chips. Two of
    # them are hit there: the median of three is a hit value, the minimum is
    # the clean one.
    rng = np.random.default_rng(4)
    shape = (120, 120)
    chips = [FakeChip(f'c{k}', shape, _wcs(shape), [], 0.3, rng) for k in range(3)]
    ys, xs = rng.integers(10, 110, 40), rng.integers(10, 110, 40)
    for c in chips[:2]:
        c.sci[ys, xs] += 30.0
    res = detect_outliers(chips)
    for key in ('c0', 'c1'):
        assert res['flags'][key][ys, xs].mean() > 0.9
    assert res['flags']['c2'][ys, xs].mean() < 0.1


def test_a_chip_that_stands_alone_is_not_judged():
    rng = np.random.default_rng(5)
    shape = (120, 120)
    a = FakeChip('a', shape, _wcs(shape), [], 0.3, rng)
    b = FakeChip('b', shape, _wcs(shape, dx=60.0), [], 0.3, rng)   # half overlap
    hit = _hit(a, rng, 400, 30.0)
    res = detect_outliers([a, b])
    yy, xx = np.indices(shape)
    ra, dec = a.pixel_to_world(xx, yy)
    bx, by = b._w.all_world2pix(ra, dec, 0)
    shared = (bx > 4) & (bx < shape[1] - 5) & (by > 4) & (by < shape[0] - 5)
    alone = (bx < -4) | (bx > shape[1] + 3)
    assert (res['flags']['a'] & hit & shared).sum() > 0.9 * (hit & shared).sum()
    assert (res['flags']['a'] & alone).sum() == 0
    assert res['stats']['a']['n_testable'] < 0.6 * a.sci.size


def test_undersampled_star_cores_survive_sub_pixel_dithers():
    # Stars 1.3 pixels wide (WFC3/IR-like) change their peak pixel by tens of
    # percent with the sub-pixel phase. With the thresholds used for that
    # detector the gradient term has to absorb it.
    rng = np.random.default_rng(6)
    stars = _stars(rng, n=30, amp=(100, 2000))
    chips = _dithered(rng, 4, stars, 0.13)
    res = detect_outliers(chips, snr=(8.0, 5.0), scale=(2.5, 0.7))
    for c in chips:
        core = c.truth > 50.0
        assert core.sum() > 30
        assert (res['flags'][c.key] & core).sum() <= 0.02 * core.sum()


def test_bad_pixels_are_neither_used_nor_reported():
    rng = np.random.default_rng(7)
    chips = _dithered(rng, 3, [], 0.3)
    chips[0].sci[50:60, 50:60] = 500.0          # a defect the adapter knows about
    chips[0].good[50:60, 50:60] = False
    res = detect_outliers(chips)
    assert res['flags']['c0'][50:60, 50:60].sum() == 0
    for c in chips[1:]:                          # and it did not reach the median
        assert res['flags'][c.key].sum() < 2e-3 * c.sci.size


# -- stars that moved between the exposures ---------------------------------

_MJD_A, _MJD_B = 57600.0, 59700.0        # 2016.6 and 2022.3: 5.75 years apart
_PM_FAST, _PM_SLOW = 26.0, 1.0           # mas/yr: 0.15 and 0.006 arcsec over that


def _year(mjd):
    from astropy.time import Time
    return float(Time(mjd, format='mjd').jyear)


def _moving_scene(rng):
    """Five chips at one date and two 5.75 years later, of one fast star, one
    slow star and one that does not move; plus the catalog of the two with a
    proper motion (positions at 2016.0)."""
    from astropy.table import Table

    start = {'fast': (-3.0, -2.0), 'slow': (4.0, 3.0), 'fixed': (0.0, 5.0)}
    rate = {'fast': _PM_FAST / 1000.0, 'slow': _PM_SLOW / 1000.0, 'fixed': 0.0}

    def stars_at(mjd):
        dt = _year(mjd) - 2016.0
        return [(x, y + rate[name] * dt, 2000.0) for name, (x, y) in start.items()]

    shape = (160, 160)
    chips, epochs = [], {}
    for k in range(7):
        mjd = _MJD_A if k < 5 else _MJD_B
        dx, dy = rng.uniform(-4, 4, 2)
        chips.append(FakeChip(f'c{k}', shape, _wcs(shape, dx, dy),
                              stars_at(mjd), 0.2, rng))
        epochs[f'c{k}'] = mjd
    cosd = np.cos(np.radians(CENTER[1]))
    names = ('fast', 'slow')
    catalog = Table({
        'RA': [CENTER[0] + start[n][0] / 3600.0 / cosd for n in names],
        'DEC': [CENTER[1] + start[n][1] / 3600.0 for n in names],
        'pmra': [0.0, 0.0], 'pmdec': [_PM_FAST, _PM_SLOW],
        'ref_epoch': [2016.0, 2016.0]})
    return chips, epochs, catalog, stars_at


def _near(chip, star, radius_px):
    """Pixels of ``chip`` within ``radius_px`` of a star (arcsec offsets)."""
    cosd = np.cos(np.radians(CENTER[1]))
    x, y = chip._w.all_world2pix(CENTER[0] + star[0] / 3600.0 / cosd,
                                 CENTER[1] + star[1] / 3600.0, 0)
    yy, xx = np.indices(chip.shape)
    return np.hypot(xx - x, yy - y) <= radius_px


def test_world_to_pixel_inverts_the_chip_mapping():
    shape = (200, 300)
    rng = np.random.default_rng(8)
    chip = FakeChip('r', shape, _wcs(shape, dx=3.3, dy=-2.1, rot=27.0), [], 0.2,
                    rng, noise=False)
    x = rng.uniform(-20, shape[1] + 20, 50)
    y = rng.uniform(-20, shape[0] + 20, 50)
    ra, dec = chip.pixel_to_world(x, y)
    bx, by = _world_to_pixel(chip, ra, dec)
    assert np.max(np.hypot(bx - x, by - y)) < 1e-3


def test_a_star_that_moved_is_clipped_without_protection():
    # the case the protection exists for: the two later chips lose the core
    rng = np.random.default_rng(9)
    chips, epochs, catalog, stars_at = _moving_scene(rng)
    res = detect_outliers(chips)
    fast_late = stars_at(_MJD_B)[0]
    for c in chips[5:]:
        assert (res['flags'][c.key] & _near(c, fast_late, 3.0)).sum() >= 3


def test_moved_stars_are_protected_and_nothing_else_is():
    rng = np.random.default_rng(9)
    chips, epochs, catalog, stars_at = _moving_scene(rng)
    hits = {c.key: _hit(c, rng, 40, 40.0) for c in chips}
    masks, info = star_protection(chips, catalog, epochs, radius=0.4,
                                  min_motion=0.15)
    # both catalog stars are on the chips; only the fast one moved 0.15 pixel
    assert info['n_stars'] == 2 and info['n_protected'] == 1
    for c in chips:
        fast, slow, fixed = stars_at(epochs[c.key])
        assert masks[c.key][_near(c, fast, 3.5)].all()       # 0.4 arcsec = 4 px
        assert not masks[c.key][_near(c, slow, 6.0)].any()
        assert not masks[c.key][~_near(c, fast, 5.0)].any()

    res = detect_outliers(chips, protect=masks)
    for c in chips:
        flags = res['flags'][c.key]
        assert (flags & masks[c.key]).sum() == 0             # the star is left alone
        assert res['stats'][c.key]['n_protected'] > 0
        away = hits[c.key] & ~masks[c.key]
        assert (flags & away).sum() > 0.9 * away.sum()       # hits are still found
        for other in stars_at(epochs[c.key])[1:]:            # still stars are clean
            assert (flags & _near(c, other, 3.0) & ~hits[c.key]).sum() <= 1


def test_no_protection_when_the_exposures_are_close_in_time():
    rng = np.random.default_rng(10)
    chips, epochs, catalog, _ = _moving_scene(rng)
    same = {key: _MJD_A + 0.01 * k for k, key in enumerate(epochs)}
    masks, info = star_protection(chips, catalog, same)
    assert info['n_stars'] == 2 and info['n_protected'] == 0
    assert not any(m.any() for m in masks.values())


def test_a_catalog_without_proper_motions_protects_nothing():
    rng = np.random.default_rng(11)
    chips, epochs, catalog, _ = _moving_scene(rng)
    masks, info = star_protection(chips, catalog['RA', 'DEC'], epochs)
    assert info['n_protected'] == 0
    assert not any(m.any() for m in masks.values())
