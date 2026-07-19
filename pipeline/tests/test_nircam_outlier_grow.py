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

    n_large, n_added, added = grow_outlier_regions(sci, dq, min_area=100,
                                                   expand_nsigma=1.5)
    assert n_large == 1
    assert n_added > 0
    assert added is not None and int(added.sum()) == n_added
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
    n_large, n_added, added = grow_outlier_regions(sci, dq, min_area=100)
    assert n_large == 0 and n_added == 0 and added is None
    assert ((dq & pf['DO_NOT_USE']) != 0).sum() == 0


def test_deblend_releases_touching_galaxy():
    """A galaxy whose isophotes touch the artifact's above-threshold wings
    must be released by deblending, not swallowed by the flood."""
    sci, dq, _ = _scene(3)
    yy, xx = np.mgrid[0:512, 0:512]
    # artifact: horizontal ridge; its above-threshold corridor overlaps the
    # galaxy's outer isophotes (connected flood path through faint wings,
    # with a genuine saddle between ridge and galaxy peaks)
    ridge = 4 * SIG * np.exp(-0.5 * ((yy - 256.0) / 6.0) ** 2) \
        * ((xx > 100) & (xx < 235))
    # galaxy: compact source offset from the ridge axis, clearly brighter
    # than the artifact (peak-ratio release criterion)
    rr2_gal = (yy - 277.0) ** 2 + (xx - 200.0) ** 2
    gal = 15 * SIG * np.exp(-0.5 * rr2_gal / 6.0 ** 2)
    sci += ridge + gal

    seed = (xx > 110) & (xx < 150) & (np.abs(yy - 256) < 4)
    assert seed.sum() >= 100

    gal_core = rr2_gal <= 4 ** 2
    ridge_bright = ridge > 2 * SIG

    # without the deblend guard the flood swallows the galaxy
    dq_off = dq.copy()
    dq_off[seed] |= pf['OUTLIER']
    grow_outlier_regions(sci, dq_off, min_area=100, expand_nsigma=1.5,
                         deblend=False)
    masked_off = (dq_off & pf['DO_NOT_USE']) != 0
    assert masked_off[gal_core].mean() > 0.9

    # with it, the galaxy's deblended child is released; ridge still masked
    dq[seed] |= pf['OUTLIER']
    grow_outlier_regions(sci, dq, min_area=100, expand_nsigma=1.5,
                         deblend=True)
    masked = (dq & pf['DO_NOT_USE']) != 0
    assert masked[gal_core].mean() < 0.1
    assert masked[ridge_bright].mean() > 0.6


def test_negative_seed_expands_through_negative_wings():
    """A large seed on an oversubtracted (negative) region expands through
    the negative wings; with negative=False it stays confined to the seed."""
    sci, dq, _ = _scene(4)
    yy, xx = np.mgrid[0:512, 0:512]
    hole_flux = -4 * SIG * np.exp(
        -0.5 * (((yy - 256.0) / 30.0) ** 2 + ((xx - 256.0) / 12.0) ** 2))
    sci += hole_flux
    deep = hole_flux < -2 * SIG

    seed = (np.abs(yy - 256) < 20) & (np.abs(xx - 256) < 8)
    dq[seed] |= pf['OUTLIER']
    assert seed.sum() >= 100

    dq_off = dq.copy()
    n_large, n_added, added = grow_outlier_regions(sci, dq, min_area=100,
                                                   expand_nsigma=1.5)
    assert n_large == 1 and n_added > 0
    masked = (dq & pf['DO_NOT_USE']) != 0
    assert masked[deep].mean() > 0.6

    _, n_added_off, _ = grow_outlier_regions(sci, dq_off, min_area=100,
                                             expand_nsigma=1.5,
                                             negative=False)
    assert n_added_off < n_added / 10


def test_edge_strip_does_not_seed():
    """OUTLIER strips on already-DO_NOT_USE exposure edges (blot/median
    mismatch at the frame boundary) must not seed expansion into real
    galaxies touching the edge — rj0911 f200w regression."""
    sci, dq, _ = _scene(5)
    yy, xx = np.mgrid[0:512, 0:512]
    # bright galaxy touching the left edge (isophotes cross the trim zone)
    rr2_gal = (yy - 256.0) ** 2 + (xx - 12.0) ** 2
    sci += 20 * SIG * np.exp(-0.5 * rr2_gal / 8.0 ** 2)
    gal_core = rr2_gal <= 5 ** 2

    # edge trim: outer 4 px are DO_NOT_USE before outlier detection
    trim = np.zeros((512, 512), bool)
    trim[:4, :] = trim[-4:, :] = trim[:, :4] = trim[:, -4:] = True
    # detection flags a contiguous strip along the trimmed left edge
    strip = (xx < 2)
    dq[strip] |= pf['OUTLIER']
    assert strip.sum() >= 100

    # without either guard the strip seeds and floods the galaxy
    # (edge_buffer=0 isolates the DNU exclusion from the geometric buffer)
    dq_bug = dq.copy()
    n_bug, _, _ = grow_outlier_regions(sci, dq_bug, min_area=100,
                                       expand_nsigma=1.5, edge_buffer=0)
    assert n_bug >= 1
    assert (((dq_bug & pf['DO_NOT_USE']) != 0)[gal_core]).mean() > 0.9

    # with it, the weightless strip cannot seed: nothing grows
    n_large, n_added, added = grow_outlier_regions(
        sci, dq, min_area=100, expand_nsigma=1.5, preexisting_dnu=trim,
        edge_buffer=0)
    assert n_large == 0 and n_added == 0 and added is None
    assert (((dq & pf['DO_NOT_USE']) != 0)[gal_core]).sum() == 0


def test_edge_buffer_excludes_near_edge_seeds():
    """A saturated star core flagged near the frame edge (weight-carrying,
    so the DNU exclusion can't catch it) must not seed a flood into the
    star's own PSF wings; interior artifacts in the same frame still grow."""
    sci, dq, _ = _scene(7)
    yy, xx = np.mgrid[0:512, 0:512]
    # bright star whose core sits 30 px from the top edge
    rr2_star = (yy - 482.0) ** 2 + (xx - 256.0) ** 2
    sci += 100 * SIG * np.exp(-0.5 * rr2_star / 25.0 ** 2)
    star_seed = rr2_star <= 12 ** 2
    dq[star_seed] |= pf['OUTLIER']
    assert star_seed.sum() >= 100

    # interior artifact ridge, far from the star
    ridge_flux = 4 * SIG * np.exp(-0.5 * ((yy - 150.0) / 6.0) ** 2) \
        * ((xx > 100) & (xx < 400))
    sci += ridge_flux
    ridge_seed = (xx > 150) & (xx < 190) & (np.abs(yy - 150) < 4)
    dq[ridge_seed] |= pf['OUTLIER']
    assert ridge_seed.sum() >= 100

    n_large, n_added, _ = grow_outlier_regions(sci, dq, min_area=100,
                                               expand_nsigma=1.5)
    masked = (dq & pf['DO_NOT_USE']) != 0
    # star wings untouched (only the pre-flagged core stays OUTLIER)
    star_wings = (rr2_star <= 60 ** 2) & ~star_seed
    assert n_large == 1
    assert masked[star_wings].mean() < 0.05
    assert masked[ridge_flux > 2 * SIG].mean() > 0.6

    # with the buffer off, the star seed floods its own PSF wings
    dq2 = np.zeros_like(dq)
    dq2[star_seed] |= pf['OUTLIER']
    grow_outlier_regions(sci, dq2, min_area=100, expand_nsigma=1.5,
                         edge_buffer=0)
    assert (((dq2 & pf['DO_NOT_USE']) != 0)[star_wings]).mean() > 0.3


def test_interior_artifact_still_seeds_with_dnu_trim():
    """The DNU-seed exclusion must not disable growth for genuine interior
    artifacts (with an edge trim present, as in production)."""
    sci, dq, _ = _scene(6)
    yy, xx = np.mgrid[0:512, 0:512]
    ridge_flux = 4 * SIG * np.exp(-0.5 * ((yy - 256.0) / 6.0) ** 2) \
        * ((xx > 100) & (xx < 400))
    sci += ridge_flux

    trim = np.zeros((512, 512), bool)
    trim[:4, :] = trim[-4:, :] = trim[:, :4] = trim[:, -4:] = True

    seed = (xx > 150) & (xx < 190) & (np.abs(yy - 256) < 4)
    dq[seed] |= pf['OUTLIER']
    assert seed.sum() >= 100

    n_large, n_added, _ = grow_outlier_regions(
        sci, dq, min_area=100, expand_nsigma=1.5, preexisting_dnu=trim)
    assert n_large == 1 and n_added > 0
    masked = (dq & pf['DO_NOT_USE']) != 0
    assert masked[ridge_flux > 2 * SIG].mean() > 0.6


def test_plot_outlier_grown_overlay(tmp_path):
    """plot_outlier renders detected + grown overlays without error."""
    import matplotlib
    matplotlib.use('Agg')
    from campfire_pipeline.nircam.steps._plots import plot_outlier

    rng = np.random.default_rng(5)
    sci = rng.normal(0, SIG, (64, 64))
    detected = np.zeros((64, 64), bool)
    detected[10:14, 10:14] = True
    grown = np.zeros((64, 64), bool)
    grown[14:20, 10:14] = True

    out = tmp_path / 'outlier.pdf'
    plot_outlier(sci, detected, grown=grown, save_file=str(out), title='t')
    assert out.exists() and out.stat().st_size > 0
    # grown=None keeps the pre-grow single-overlay behavior
    out2 = tmp_path / 'outlier2.pdf'
    plot_outlier(sci, detected, save_file=str(out2))
    assert out2.exists() and out2.stat().st_size > 0
