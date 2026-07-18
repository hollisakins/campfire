"""Tests for waterfall growth of large OUTLIER DQ regions
(steps.outlier.grow_outlier_regions)."""
import numpy as np
import pytest

jwst = pytest.importorskip('jwst')
from jwst.datamodels.dqflags import pixel as pf  # noqa: E402

from campfire_pipeline.nircam.steps.outlier import grow_outlier_regions  # noqa: E402

SIG = 0.05


def _scene(seed=0):
    rng = np.random.default_rng(seed)
    sci = rng.normal(0, SIG, (512, 512))
    dq = np.zeros((512, 512), np.uint32)
    return sci, dq, rng


def test_waterfall_follows_ridge_and_ignores_crs():
    """A short flagged segment of an elongated 4-sigma ridge expands along
    the ridge's own morphology; cosmic-ray speckles are untouched."""
    sci, dq, rng = _scene()
    yy, xx = np.mgrid[0:512, 0:512]
    t = (xx - 100.0) - (yy - 100.0)              # across-ridge coordinate
    along = ((xx - 100.0) + (yy - 100.0)) / 2.0  # along-ridge coordinate
    on = (along > 0) & (along < 300)
    ridge_flux = np.where(on, 4 * SIG * np.exp(-0.5 * (t / 4.0) ** 2), 0.0)
    sci += ridge_flux
    ridge_bright = ridge_flux > 2 * SIG          # the visually obvious part

    seed = on & (along > 100) & (along < 140) & (np.abs(t) < 4)
    dq[seed] |= pf['OUTLIER']
    assert seed.sum() >= 100
    for _ in range(200):                         # CR speckles
        y, x = rng.integers(0, 512, 2)
        dq[y, x] |= pf['OUTLIER']

    n_large, n_added = grow_outlier_regions(sci, dq, min_area=100,
                                            expand_nsigma=1.5)
    assert n_large == 1
    assert n_added > 0
    masked = (dq & pf['DO_NOT_USE']) != 0
    # the expansion escapes the seed and covers most of the bright ridge...
    assert masked[ridge_bright].mean() > 0.6
    assert masked.sum() > 4 * seed.sum()
    # ...but stays confined to the ridge corridor (CRs not grown, no flood
    # into blank sky): almost nothing masked > 25 px from the ridge axis
    corridor = on & (np.abs(t) < 25)
    assert masked[~corridor].sum() < 800


def test_growth_cap_falls_back_to_dilation_on_star_halo():
    """A seed on a bright star's core must NOT flood the whole PSF halo:
    the max_factor cap replaces the expansion with a seed dilation."""
    sci, dq, _ = _scene(1)
    yy, xx = np.mgrid[0:512, 0:512]
    rr2 = (yy - 256.0) ** 2 + (xx - 256.0) ** 2
    sci += 100 * SIG * np.exp(-0.5 * rr2 / 40.0 ** 2)   # huge smooth halo

    seed = rr2 <= 10 ** 2
    dq[seed] |= pf['OUTLIER']

    halo_above = (100 * SIG * np.exp(-0.5 * rr2 / 40.0 ** 2)) > 1.5 * SIG
    assert halo_above.sum() > 40 * seed.sum()   # uncapped flood would blow up

    grow_outlier_regions(sci, dq, min_area=100, expand_nsigma=1.5,
                         max_factor=40, fallback_radius=12, skirt=2)
    masked = (dq & pf['DO_NOT_USE']) != 0
    # fallback: seed dilated by ~fallback_radius+skirt, NOT the whole halo
    assert masked.sum() < np.pi * (10 + 12 + 2 + 2) ** 2
    assert masked.sum() < 0.15 * halo_above.sum()


def test_noop_without_large_components():
    sci, dq, rng = _scene(2)
    for _ in range(300):
        y, x = rng.integers(0, 512, 2)
        dq[y, x] |= pf['OUTLIER']
    n_large, n_added = grow_outlier_regions(sci, dq, min_area=100)
    assert n_large == 0 and n_added == 0
    assert ((dq & pf['DO_NOT_USE']) != 0).sum() == 0
