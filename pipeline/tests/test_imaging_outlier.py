"""Field-level outlier detection on synthetic chips (imaging/outlier.py).

Every chip looks at the same analytic scene (Gaussian stars on a flat, already
subtracted background) through its own tangent-plane WCS, so dithers and
rotations are exact and the truth about every pixel is known.
"""

import numpy as np
import pytest
from astropy.wcs import WCS

from campfire_pipeline.imaging.exposure import ChipArrays
from campfire_pipeline.imaging.outlier import (abs_gradient, detect_outliers,
                                               stack_grid)

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


def test_abs_gradient_is_the_largest_step_to_a_neighbor():
    img = np.zeros((5, 5), np.float32)
    img[2, 2] = 3.0
    img[2, 3] = 1.0
    g = abs_gradient(img)
    assert g[2, 2] == 3.0 and g[2, 3] == 2.0 and g[1, 2] == 3.0 and g[0, 0] == 0.0


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
