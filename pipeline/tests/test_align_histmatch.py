"""Tests for the JHAT-ported offset-histogram matcher (nircam/align/histmatch).

Catalogs are synthetic tangent-plane tables (TPx/TPy, arcsec) — the frame the
matcher actually sees after ``tweakwcs`` pools a group's detectors. The key
regression here is the *overflow regime*: spatially-correlated detection/refcat
catalogs at the real COSMOS production counts, where ``tweakwcs.XYXYMatch``
dies with ``MatchSourceConfusionError`` (39% of COSMOS LW exposures) and the
histogram-consensus matcher must instead recover the offset.
"""

import numpy as np
import pytest
from astropy.table import Table

from campfire_pipeline.nircam.align.histmatch import (
    OffsetHistogramMatch,
    _sigma_clip_median,
    _smoothed_hist_peak,
)

LW_PSCALE = 0.063   # arcsec / px
SW_PSCALE = 0.031


def _tab(xy):
    return Table({'TPx': xy[:, 0], 'TPy': xy[:, 1]})


def _rot(theta_deg, about):
    th = np.radians(theta_deg)
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    return lambda xy: (xy - about) @ R.T + about


# --- helpers ----------------------------------------------------------------

def test_smoothed_hist_peak_finds_pileup():
    rng = np.random.default_rng(0)
    d = np.concatenate([rng.uniform(-3, 3, 500),        # false-pair floor
                        rng.normal(1.25, 0.02, 80)])    # consensus pile-up
    center, height, fwhm = _smoothed_hist_peak(d, 0.00126, 0.0126)
    assert abs(center - 1.25) < 0.05
    assert fwhm < 0.5


def test_smoothed_hist_peak_short_histogram():
    # fewer bins than the smoothing kernel: the peak index must still map onto
    # the bin edges (regression: mode='same' returned the KERNEL's length)
    d = np.array([1.0, 1.001, 1.002, 1.5])
    center, _, _ = _smoothed_hist_peak(d, 0.02, 0.2)
    assert 0.9 < center < 1.6


def test_sigma_clip_median_keeps_core():
    rng = np.random.default_rng(1)
    vals = np.concatenate([rng.normal(0, 0.02, 90), rng.uniform(1, 3, 10)])
    mask = np.ones(vals.size, dtype=bool)
    out = _sigma_clip_median(vals, mask, 3.0)
    assert np.all(np.abs(vals[out]) < 0.5)
    assert np.count_nonzero(out) >= 80


# --- matcher: recovery ------------------------------------------------------

def test_recovers_translation_under_contamination():
    # 120 true sources on a ~130" pool, 10x refcat junk; offset (3.2, -1.7)"
    rng = np.random.default_rng(42)
    true = rng.uniform(0, 130, (120, 2))
    offset = np.array([3.2, -1.7])
    im = true - offset + rng.normal(0, 0.01, true.shape)
    ref = np.vstack([true, rng.uniform(-20, 150, (1200, 2))])

    m = OffsetHistogramMatch(searchrad=70.0)
    ri, ii = m(_tab(ref), _tab(im), tp_pscale=LW_PSCALE)
    assert len(ri) >= 100
    rec = ref[ri] - im[ii]
    assert abs(np.median(rec[:, 0]) - offset[0]) < 0.05
    assert abs(np.median(rec[:, 1]) - offset[1]) < 0.05
    # true rows come first in ref, aligned with im indices
    assert np.mean(ri == ii) > 0.9


def test_recovers_rotation_via_slope_scan():
    # 0.15 deg roll about the pool centre + sub-arcsec shift: displacement
    # varies +-0.17" across the pool, well beyond the true-pair scatter, so
    # only the rotation-slope scan can stack the peak; pure-histogram matching
    # with no de-rotation would smear it.
    rng = np.random.default_rng(7)
    true = rng.uniform(0, 130, (120, 2))
    im = _rot(0.15, np.array([65.0, 65.0]))(true) - [0.8, 0.5]
    im += rng.normal(0, 0.01, im.shape)
    ref = np.vstack([true, rng.uniform(-20, 150, (1200, 2))])

    m = OffsetHistogramMatch(searchrad=70.0)
    ri, ii = m(_tab(ref), _tab(im), tp_pscale=LW_PSCALE)
    assert len(ri) >= 100
    assert np.mean(ri == ii) > 0.9


def test_recovers_large_acquisition_offset():
    # 60" offset — far beyond 1-NN capture; the gross 2-D-histogram stage must
    # pre-shift before the consensus stage can work.
    rng = np.random.default_rng(11)
    true = rng.uniform(0, 130, (150, 2))
    offset = np.array([-42.0, 41.0])
    im = true - offset + rng.normal(0, 0.02, true.shape)
    ref = np.vstack([true, rng.uniform(-80, 210, (1500, 2))])

    m = OffsetHistogramMatch(searchrad=70.0)
    ri, ii = m(_tab(ref), _tab(im), tp_pscale=LW_PSCALE)
    assert len(ri) >= 100
    rec = ref[ri] - im[ii]
    assert abs(np.median(rec[:, 0]) - offset[0]) < 0.1
    assert abs(np.median(rec[:, 1]) - offset[1]) < 0.1


def test_iterate_mode_needs_no_gross_stage():
    # searchrad=None (the iterate passes): a small residual offset is matched
    # by pure 1-NN + consensus.
    rng = np.random.default_rng(13)
    true = rng.uniform(0, 130, (100, 2))
    im = true - [0.3, 0.2] + rng.normal(0, 0.01, true.shape)
    ref = np.vstack([true, rng.uniform(-10, 140, (800, 2))])

    m = OffsetHistogramMatch(searchrad=None)
    ri, ii = m(_tab(ref), _tab(im), tp_pscale=LW_PSCALE)
    assert len(ri) >= 85
    assert np.mean(ri == ii) > 0.9


def test_pooled_sparse_detectors_share_one_histogram():
    # Four sparse SW "detectors" (8 sources each — hopeless individually) in
    # one pooled frame: the shared histogram must still lock onto the offset.
    # This is the pooling advantage the port must preserve.
    rng = np.random.default_rng(17)
    corners = [(0, 0), (68, 0), (0, 68), (68, 68)]     # 2x2 SW module layout
    true = np.vstack([rng.uniform(0, 64, (8, 2)) + c for c in corners])
    offset = np.array([1.1, -0.9])
    im = true - offset + rng.normal(0, 0.01, true.shape)
    ref = np.vstack([true, rng.uniform(-10, 142, (600, 2))])

    m = OffsetHistogramMatch(searchrad=70.0)
    ri, ii = m(_tab(ref), _tab(im), tp_pscale=SW_PSCALE)
    assert len(ri) >= 24
    rec = ref[ri] - im[ii]
    assert abs(np.median(rec[:, 0]) - offset[0]) < 0.05
    assert abs(np.median(rec[:, 1]) - offset[1]) < 0.05
    # every quadrant (detector) contributes matched sources
    quadrant = (im[ii][:, 0] > 66).astype(int) * 2 + (im[ii][:, 1] > 66)
    assert len(np.unique(quadrant)) == 4


# --- the production overflow regime -----------------------------------------

def _overflow_regime(rng):
    """COSMOS-like catalogs at the real failing counts: 427 detections /
    4128 refs, spatially correlated — every detection IS a refcat galaxy and
    the refcat resolves substructure (several refs within ~2" of a detection).
    """
    n_det = 427
    centers = rng.uniform(0, 129, (n_det, 2))
    offset = np.array([0.4, -0.3])
    im = centers - offset + rng.normal(0, 0.02, centers.shape)
    parts = [centers]
    for _ in range(8):
        sel = rng.random(n_det) < 0.55
        parts.append(centers[sel] + rng.normal(0, 1.0, (int(sel.sum()), 2)))
    ref = np.vstack(parts)
    extra = rng.uniform(-15, 145, (max(0, 4128 - len(ref)), 2))
    return np.vstack([ref, extra])[:4128], im, offset


def test_xyxymatch_overflows_where_histmatch_solves():
    from tweakwcs.matchutils import MatchSourceConfusionError, XYXYMatch

    rng = np.random.default_rng(101)
    ref, im, offset = _overflow_regime(rng)

    xyxy = XYXYMatch(use2dhist=True, searchrad=70.0, tolerance=2.0,
                     separation=1.0)
    with pytest.raises(MatchSourceConfusionError):
        xyxy(_tab(ref), _tab(im), tp_pscale=LW_PSCALE)

    m = OffsetHistogramMatch(searchrad=70.0)
    ri, ii = m(_tab(ref), _tab(im), tp_pscale=LW_PSCALE)
    assert len(ri) >= 300
    rec = ref[ri] - im[ii]
    assert abs(np.median(rec[:, 0]) - offset[0]) < 0.05
    assert abs(np.median(rec[:, 1]) - offset[1]) < 0.05
    # first 427 ref rows are the true counterparts, aligned with im indices
    assert np.mean(ri == ii) > 0.9


# --- degeneracy / robustness ------------------------------------------------

def test_starved_catalogs_return_empty():
    rng = np.random.default_rng(23)
    im = rng.uniform(0, 100, (50, 2))
    ref2 = rng.uniform(0, 100, (2, 2))
    m = OffsetHistogramMatch(searchrad=70.0)
    for a, b in ((_tab(ref2), _tab(im)), (_tab(im), _tab(ref2[:2]))):
        ri, ii = m(a, b, tp_pscale=LW_PSCALE)
        assert len(ri) == 0 and len(ii) == 0


def test_disjoint_fields_return_empty():
    # no reference source within searchrad of any image source
    rng = np.random.default_rng(29)
    im = rng.uniform(0, 100, (50, 2))
    ref = rng.uniform(500, 600, (50, 2))
    m = OffsetHistogramMatch(searchrad=70.0)
    ri, ii = m(_tab(ref), _tab(im), tp_pscale=LW_PSCALE)
    assert len(ri) == 0


def test_missing_tp_columns_raise():
    m = OffsetHistogramMatch()
    good = _tab(np.zeros((5, 2)))
    bad = Table({'TPx': np.zeros(5)})
    with pytest.raises(KeyError):
        m(bad, good)
    with pytest.raises(KeyError):
        m(good, bad)


def test_bad_histocut_order_raises():
    with pytest.raises(ValueError):
        OffsetHistogramMatch(histocut_order='xy')


# --- delta_mag_lim pair cut --------------------------------------------------

def test_delta_mag_lim_cuts_brightness_disagreement():
    # Two interleaved source populations at the SAME positions offset: the
    # positional consensus alone cannot separate them, but their mags disagree
    # with the refcat by 6 mag, so delta_mag_lim=[-3, 4] must cut them.
    rng = np.random.default_rng(31)
    true = rng.uniform(0, 130, (100, 2))
    im = true - [0.5, 0.2] + rng.normal(0, 0.01, true.shape)
    ref = np.vstack([true, rng.uniform(-10, 140, (400, 2))])

    ref_tab = _tab(ref)
    ref_mag = np.full(len(ref), 22.0)
    ref_tab['mag'] = ref_mag
    im_tab = _tab(im)
    ids = np.arange(len(im))
    im_tab['id'] = ids
    # first 50 image sources agree with the refcat brightness; the rest are
    # 6 mag brighter than their refcat counterparts (dmag = -6 < -3)
    image_mags = {int(i): (22.0 if i < 50 else 16.0) for i in ids}

    base = OffsetHistogramMatch(searchrad=70.0)
    ri0, ii0 = base(ref_tab, im_tab, tp_pscale=LW_PSCALE)
    assert len(ri0) >= 90                       # without the cut: both halves

    m = OffsetHistogramMatch(searchrad=70.0, delta_mag_lim=(-3.0, 4.0),
                             image_mags=image_mags)
    ri, ii = m(ref_tab, im_tab, tp_pscale=LW_PSCALE)
    assert len(ri) >= 40
    assert np.all(ii < 50)                      # disagreeing half is gone


def test_delta_mag_lim_never_punishes_missing_mags():
    # Sources absent from the image_mags lookup (uncalibrated detector) and
    # refcat rows with non-finite mag pass the cut unjudged.
    rng = np.random.default_rng(37)
    true = rng.uniform(0, 130, (80, 2))
    im = true - [0.4, 0.1] + rng.normal(0, 0.01, true.shape)
    ref_tab = _tab(np.vstack([true, rng.uniform(-10, 140, (300, 2))]))
    ref_tab['mag'] = np.nan                     # refcat carries no usable mags
    im_tab = _tab(im)
    im_tab['id'] = np.arange(len(im))

    m = OffsetHistogramMatch(searchrad=70.0, delta_mag_lim=(-3.0, 4.0),
                             image_mags={0: 22.0})
    ri, ii = m(ref_tab, im_tab, tp_pscale=LW_PSCALE)
    assert len(ri) >= 70                        # nothing was cut on mags
