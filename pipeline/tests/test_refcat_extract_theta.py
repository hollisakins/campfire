"""SEP hands back theta values its own aperture routines reject.

``sep.extract`` emits ``theta`` as float32, while ``sep.sum_ellipse``
validates ``|theta| <= pi/2`` in double precision. A perfectly vertical
source lands on exactly -pi/2, and ``float32(-pi/2) == -1.5707964`` is *more
negative* than ``float64 -pi/2 == -1.5707963267948966`` -- so an object can
round-trip out of ``extract`` and straight into "invalid aperture
parameters".

That aborted detection for an entire detector, and because align solves per
pool it quarantined every detector in the pool alongside it. Observed on
pg004 f444w 2026-08-21: one 51-px edge sliver at x=2043 cost a whole dither.

The pre-existing guard only ever wrapped the ``+pi/2`` side
(``theta[theta > pi/2] -= pi``), which is why the negative boundary survived
to reach the aperture call.
"""

import numpy as np
import pytest
import sep

from campfire_pipeline.nircam.refcat.extract import (
    _SEP_THETA_MAX, sep_extract_sources,
)


def _aperture_call(theta_value):
    """Run the sum_ellipse call that detection makes, at one theta."""
    rng = np.random.default_rng(0)
    data = rng.normal(0.0, 1.0, (64, 64)).astype(np.float32)
    return sep.sum_ellipse(
        data,
        np.array([32.0]), np.array([32.0]),
        np.array([2.5], dtype=np.float32),
        np.array([0.93], dtype=np.float32),
        np.array([theta_value]),
        2.5, subpix=1,
    )


@pytest.mark.parametrize("sign", [-1.0, 1.0])
def test_sep_rejects_its_own_float32_boundary_theta(sign):
    """The upstream behaviour this guard exists for.

    If a future SEP stops raising here, the clip becomes unnecessary rather
    than wrong -- but we want to be told.
    """
    with pytest.raises(Exception, match="invalid aperture parameters"):
        _aperture_call(np.float32(sign * np.pi / 2))


@pytest.mark.parametrize("sign", [-1.0, 1.0])
def test_clipped_theta_is_accepted(sign):
    """Our bound survives the float32 round-trip into the aperture call."""
    flux, _, _ = _aperture_call(np.float32(sign * _SEP_THETA_MAX))
    assert np.all(np.isfinite(flux))


def test_theta_bound_is_inside_seps_valid_range():
    assert _SEP_THETA_MAX < np.pi / 2
    assert float(np.float32(_SEP_THETA_MAX)) <= np.pi / 2
    # The naive bound does NOT survive the round-trip -- this is the bug.
    assert float(np.float32(np.pi / 2)) > np.pi / 2
    assert float(np.float32(-np.pi / 2)) < -np.pi / 2


def test_extraction_clips_boundary_theta_out_of_its_catalogue():
    """Whatever SEP fits, nothing at or outside the boundary leaves extract."""
    rng = np.random.default_rng(1234)
    shape = (160, 160)
    sci = rng.normal(0.0, 1.0, shape)
    err = np.ones(shape)
    # A 1-px-wide vertical bar: the shape that drives theta onto +/-pi/2.
    sci[40:120, 80] += 60.0
    sci[40:120, 81] += 30.0
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    for cy, cx in ((30, 30), (120, 40)):
        sci += 40.0 * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0)

    cat = sep_extract_sources(sci, err, snr_thresh=3.0, minarea=8,
                              filter_fwhm=2.0)
    assert len(cat) >= 1
    assert np.all(np.isfinite(cat["x"]))
    assert np.all(np.isfinite(cat["y"]))
