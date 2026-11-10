"""The Poisson variance from each pixel's row neighbors (``nirspec/variance.py``): the neighbor
statistics, the rule (spike, line, level, the bias it removes), the rewrite of a rate file and
its staleness stamps."""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nirspec import variance as V


# ---------------------------------------------------------------------------
# neighbor statistics
# ---------------------------------------------------------------------------
def test_the_neighbor_mean_leaves_the_pixel_out_and_handles_edges():
    a = np.arange(20, dtype=float)[None, :] * np.ones((3, 1))
    a[1, 7] = 1000.0                                       # the pixel itself: never in its own value
    valid = np.ones(a.shape, bool)
    m = V.neighbour_mean(a, valid, half=2)
    assert m[0, 7] == pytest.approx(np.mean([5, 6, 8, 9]))
    assert m[1, 7] == pytest.approx(np.mean([5, 6, 8, 9]))
    assert m[1, 6] == pytest.approx(np.mean([4, 5, 1000, 8]))   # a neighbor's value counts
    assert m[0, 0] == pytest.approx(np.mean([1, 2]))            # row start: only the side there is
    assert m[0, 19] == pytest.approx(np.mean([17, 18]))         # row end
    med = V.neighbour_median(a, valid, half=2)
    assert med[1, 7] == pytest.approx(7.0)


def test_invalid_and_unlit_neighbors_are_skipped_and_none_gives_nan():
    a = np.arange(10, dtype=float)[None, :]
    valid = np.ones(a.shape, bool)
    valid[0, 4] = False                                    # DO_NOT_USE or unlit
    m = V.neighbour_mean(a, valid, half=1)
    assert m[0, 3] == pytest.approx(2.0) and m[0, 5] == pytest.approx(6.0)
    lone = np.zeros(a.shape, bool)
    lone[0, 5] = True
    assert np.isnan(V.neighbour_mean(a, lone, half=2)[0, 5])   # no neighbor at all


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------
def _frame(ny=40, nx=200, bkg=37.0, vr=74.0, seed=0):
    """A background frame in electrons: Poisson counts plus read noise, with jwst's habit of
    taking each pixel's Poisson variance from its own counts (0 where they came out negative)."""
    rng = np.random.default_rng(seed)
    counts = rng.poisson(bkg, size=(ny, nx)).astype(float)
    sci = counts + rng.normal(0.0, np.sqrt(vr), size=(ny, nx))
    vp = np.clip(counts + rng.normal(0.0, np.sqrt(vr) / 3, size=(ny, nx)), 0.0, None)   # own counts, noisy
    return sci, vp, np.full((ny, nx), vr), np.zeros((ny, nx), np.uint32), np.ones((ny, nx), bool)


def test_a_single_pixel_spike_gets_its_neighbors_variance():
    sci, vp, vr, dq, lit = _frame()
    sci[20, 100] += 400.0                                  # a cosmic-ray residue: alone, no light across the slit
    vp[20, 100] += 400.0
    out, guarded = V.poisson_variance(vp, sci, vr, dq, lit)
    assert not guarded[20, 100]
    nb = [c for c in range(95, 106) if c != 100]
    assert out[20, 100] == pytest.approx(np.mean(vp[20, nb]), rel=1e-9)
    assert out[20, 100] < 100                              # no longer hides behind its own variance
    # and it is left out of its neighbors' means
    assert out[20, 101] == pytest.approx(np.mean(vp[20, [c for c in range(96, 107) if c not in (100, 101)]]))


def test_a_line_two_rows_tall_keeps_its_own_variance():
    sci, vp, vr, dq, lit = _frame()
    for r in (20, 21):
        sci[r, 100] += 400.0
        vp[r, 100] += 400.0
    out, guarded = V.poisson_variance(vp, sci, vr, dq, lit)
    assert guarded[20, 100] and guarded[21, 100]
    assert out[20, 100] == vp[20, 100] and out[21, 100] == vp[21, 100]
    # a 1 sigma bump is not guarded
    sci[30, 50] += 1.0 * np.sqrt(37 + 74)
    sci[31, 50] += 1.0 * np.sqrt(37 + 74)
    _, g2 = V.poisson_variance(vp, sci, vr, dq, lit)
    assert not g2[30, 50] and not g2[31, 50]


def test_zero_jwst_variance_is_replaced_and_unlit_pixels_keep_theirs():
    sci, vp, vr, dq, lit = _frame()
    vp[10, 10] = 0.0
    lit[:, 150:] = False
    out, _ = V.poisson_variance(vp, sci, vr, dq, lit)
    assert out[10, 10] > 10
    np.testing.assert_array_equal(out[:, 150:], vp[:, 150:])
    dq[5, 5] = V.DO_NOT_USE
    out2, _ = V.poisson_variance(vp, sci, vr, dq, lit)
    assert out2[5, 6] == pytest.approx(np.mean(vp[5, [c for c in range(1, 12) if c not in (5, 6)]]))


def test_the_bias_of_a_weighted_fit_is_gone_and_the_level_is_right():
    """Seeded Monte Carlo (200 x 2048 pixels, background 37 e, read noise 74 e^2): the background's
    scale fitted with 1 / (VP + VR) weights. With each pixel's own variance (here following its
    Poisson counts only, so about -1 e x VP / (VP + VR) per pixel: -1 %) it reads low; with the
    neighbors' it is within 0.3 % of the truth, and the mean variance is the true Poisson
    variance within 1 %."""
    b, vr0 = 37.0, 74.0
    sci, vp, vr, dq, lit = _frame(ny=200, nx=2048, bkg=b, vr=vr0, seed=3)

    def scale(var):
        w = 1.0 / var
        return float(np.sum(w * sci) / np.sum(w * b))
    own = scale(vp + vr) - 1
    out, guarded = V.poisson_variance(vp, sci, vr, dq, lit)
    new = scale(out + vr) - 1
    assert own < -0.007
    assert abs(new) < 0.003 and abs(new) < abs(own) / 3
    assert float(np.mean(out)) == pytest.approx(b, rel=0.01)
    assert guarded.mean() < 1e-4


# ---------------------------------------------------------------------------
# the rate file
# ---------------------------------------------------------------------------
def _rate(path, sci, vp, vr, lit, err=None):
    pytest.importorskip("jwst")
    from jwst.datamodels import ImageModel
    m = ImageModel(sci.shape)
    m.data = sci.astype(np.float32)
    m.err = (np.ones_like(sci) if err is None else err).astype(np.float32)
    m.var_rnoise = vr.astype(np.float32)
    m.var_poisson = vp.astype(np.float32)
    m.dq = np.zeros(sci.shape, np.uint32)
    m.meta.instrument.grating = "PRISM"
    m.save(path)
    m.close()
    with fits.open(path, mode="update") as h:
        h[0].header["CFBKGSUB"] = True
        h[0].header["CFBKGRMS"] = 2.0
        h[0].header["CFBKGDT"] = "2026-11-10T00:00:00"
        h.append(fits.ImageHDU(np.zeros(sci.shape, np.float32), name="CFBKG"))
        h.append(fits.ImageHDU((~lit).astype(np.uint8), name="CFBKGMASK"))


def test_the_rate_rewrite_keeps_jwst_variance_and_repeats_identically(tmp_path):
    from jwst.datamodels import ImageModel
    sci, vp, vr, dq, lit = _frame(ny=30, nx=120)
    lit[:, :10] = False
    path = str(tmp_path / "jw00001001001_03101_00001_nrs1_rate.fits")
    _rate(path, sci, vp, vr, lit)
    frac = V.apply_to_rate(path)
    with fits.open(path) as h:
        vp1 = np.array(h["VAR_POISSON"].data)
        np.testing.assert_array_equal(h["VAR_POISSON_JWST"].data, vp.astype(np.float32))
        np.testing.assert_allclose(h["ERR"].data, np.sqrt(vp1.astype(float) + vr), rtol=1e-6)
        hdr = h[0].header
        assert (hdr["CFVARPS"], hdr["CFVARHW"], hdr["CFVARXS"], hdr["CFVARSS"]) == (V.VERSION, 5, 5.0, 2.0)
        assert hdr["CFVARNG"] == pytest.approx(frac, abs=1e-6)
    assert not np.array_equal(vp1[:, 20:], vp[:, 20:].astype(np.float32))
    np.testing.assert_array_equal(vp1[:, :10], vp[:, :10].astype(np.float32))     # unlit: jwst's
    V.apply_to_rate(path)                                  # again: from VAR_POISSON_JWST, the same arrays
    with fits.open(path) as h:
        np.testing.assert_array_equal(h["VAR_POISSON"].data, vp1)
        assert sum(x.name == "VAR_POISSON_JWST" for x in h) == 1
    with ImageModel(path) as m:                            # still a valid datamodel
        np.testing.assert_array_equal(m.var_poisson, vp1)


def test_a_rate_without_its_background_mask_is_refused(tmp_path):
    pytest.importorskip("jwst")
    path = str(tmp_path / "r_rate.fits")
    sci, vp, vr, dq, lit = _frame(ny=8, nx=30)
    _rate(path, sci, vp, vr, lit)
    with fits.open(path, mode="update") as h:
        del h["CFBKGMASK"]
    with pytest.raises(ValueError):
        V.apply_to_rate(path)


def test_ensure_variance_rewrites_only_missing_or_other_versions(tmp_path):
    sci, vp, vr, dq, lit = _frame(ny=8, nx=30)
    a, b, c = (str(tmp_path / f"{n}_rate.fits") for n in "abc")
    for p in (a, b, c):
        _rate(p, sci, vp, vr, lit)
    V.apply_to_rate(b)
    with fits.open(c, mode="update") as h:
        h[0].header["CFBKGSUB"] = False                    # no background subtraction: not ours to touch
    assert V.ensure_variance([a, b, c]) == [a]
    with fits.open(b, mode="update") as h:
        h[0].header["CFVARPS"] = "nbr-median-v0"           # another version
    assert V.ensure_variance([a, b, c]) == [b]
    assert V.is_current(a) and V.is_current(b) and not V.is_current(c)
    assert V.stamps_of([a, b, str(tmp_path / "missing.fits")]) == V.VERSION
    assert V.stamps_of([c]) == "none"


def test_restore_puts_back_jwst_variance(tmp_path):
    """``masks.restore_pre_bkgsub`` after the rewrite: VAR_POISSON is jwst's again, ERR is
    sqrt(VP + VR) with the un-rescaled read noise, the extension and the cards are gone."""
    from jwst.datamodels import ImageModel
    from campfire_pipeline.nirspec import masks
    sci, vp, vr, dq, lit = _frame(ny=8, nx=40)
    path = str(tmp_path / "jw00001001001_03101_00001_nrs1_rate.fits")
    _rate(path, sci, vp, vr, lit)
    V.apply_to_rate(path)
    masks.restore_pre_bkgsub(path)
    with ImageModel(path) as m:
        np.testing.assert_array_equal(m.var_poisson, vp.astype(np.float32))
        np.testing.assert_allclose(m.var_rnoise, vr / 2.0, rtol=1e-6)
        np.testing.assert_allclose(m.err, np.sqrt(vp.astype(np.float32) + vr.astype(np.float32) / 2.0), rtol=1e-6)
    with fits.open(path) as h:
        assert "VAR_POISSON_JWST" not in h
        assert not any(k in h[0].header for k in V.CARDS)


def test_canonical_variance_staleness(tmp_path):
    """Stage 2a remakes a canonical (or bkgslits file) whose CFVARPS differs from its rate's."""
    from campfire_pipeline.nirspec.stage2 import _canonical_variance_changed
    rate, can = str(tmp_path / "r_rate.fits"), str(tmp_path / "r_7.fits")
    for p, card in ((rate, V.VERSION), (can, V.VERSION)):
        h = fits.Header()
        h["CFVARPS"] = card
        fits.PrimaryHDU(header=h).writeto(p)
    assert not _canonical_variance_changed(can, rate)
    with fits.open(can, mode="update") as h:
        h[0].header["CFVARPS"] = "N/A"
    assert _canonical_variance_changed(can, rate)
    with fits.open(can, mode="update") as h:
        del h[0].header["CFVARPS"]                          # made before the card existed
    assert _canonical_variance_changed(can, rate)
    assert not _canonical_variance_changed(str(tmp_path / "absent.fits"), rate)
