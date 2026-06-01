"""Tests for the campfire-native NIRCam drizzle (`campfire_pipeline.nircam.drizzle`).

Regression coverage for the variance-pass sanitization. Before the fix, the
variance components were summed before drizzling, so one ``inf``/``nan``/
negative component propagated into ``var_total * weight``; cdriz spread it
across every output pixel the kernel touched, and ``inf`` is sticky in the
running weighted mean — so one bad input pixel blew up the ERR for all
co-located inputs (inf-dominated ERR maps).

``_sanitize_variance`` masks each component independently with
``(var >= 0) & isfinite(var)`` (matching stcal's per-component masking), so a
bad component drops only its own term. A pixel is discarded entirely only when
*no* component is valid. This degrades gracefully when a component is bad across
many inputs (a flat-field column, a reference region), where masking the summed
variance would instead punch a NaN hole in the ERR map.

The integration tests drive the **real** ``drizzle.resample.Drizzle`` (the same
primitive ``drizzle_tile`` uses) through an identity pixmap, so they reproduce
the bug and validate the fix deterministically without depending on WCS
construction.
"""

import numpy as np
import pytest

from campfire_pipeline.nircam.drizzle import _sanitize_variance

Drizzle = pytest.importorskip("drizzle.resample").Drizzle


# ---------------------------------------------------------------------------
# unit tests: the per-component masking helper
# ---------------------------------------------------------------------------

def test_sanitize_variance_sums_clean_components():
    vr = np.full((1, 3), 1e-3, np.float32)
    vp = np.full((1, 3), 2e-3, np.float32)
    vf = np.full((1, 3), 1e-4, np.float32)
    weight = np.full((1, 3), 7.0, np.float32)
    var_total, var_weight = _sanitize_variance(vr, vp, vf, weight)
    np.testing.assert_allclose(var_total, 3.1e-3, rtol=1e-6)
    np.testing.assert_array_equal(var_weight, weight)


def test_sanitize_variance_drops_only_the_bad_component():
    # col 0 clean; col 1 bad poisson; col 2 negative flat
    vr = np.array([[1e-3, 1e-3, 1e-3]], np.float32)
    vp = np.array([[2e-3, np.inf, 2e-3]], np.float32)
    vf = np.array([[1e-4, 1e-4, -5.0]], np.float32)
    weight = np.full((1, 3), 10.0, np.float32)
    var_total, var_weight = _sanitize_variance(vr, vp, vf, weight)

    np.testing.assert_allclose(var_total[0, 0], 3.1e-3, rtol=1e-6)   # all three
    np.testing.assert_allclose(var_total[0, 1], 1.1e-3, rtol=1e-6)   # poisson dropped
    np.testing.assert_allclose(var_total[0, 2], 3.0e-3, rtol=1e-6)   # flat dropped
    assert np.isfinite(var_total).all()
    # every pixel keeps >= 1 valid component, so the SCI weight is untouched
    np.testing.assert_array_equal(var_weight, weight)


def test_sanitize_variance_drops_pixel_with_no_valid_component():
    vr = np.array([[1e-3, np.inf]], np.float32)
    vp = np.array([[2e-3, np.nan]], np.float32)
    vf = np.array([[1e-4, -1.0]], np.float32)
    weight = np.full((1, 2), 9.0, np.float32)
    var_total, var_weight = _sanitize_variance(vr, vp, vf, weight)
    assert var_weight[0, 0] == 9.0      # has valid components
    assert var_weight[0, 1] == 0.0      # fully dead -> dropped
    assert np.isfinite(var_total).all()
    assert weight[0, 1] == 9.0          # input weight not mutated


# ---------------------------------------------------------------------------
# integration tests: real Drizzle, identity pixmap, the variance trick
# ---------------------------------------------------------------------------

def _identity_pixmap(ny, nx):
    iy, ix = np.indices((ny, nx), dtype=np.float64)
    pm = np.empty((ny, nx, 2), np.float64)
    pm[..., 0] = ix
    pm[..., 1] = iy
    return pm


def _drizzle_variance_trick(inputs, shape, *, sanitize):
    """Mirror drizzle_tile's SCI + variance passes on an identity pixmap.

    ``inputs`` is a list of ``(var_rnoise, var_poisson, var_flat, weight)``.
    With ``sanitize=False`` this is the pre-fix behaviour (raw sum, shared SCI
    weight, normalize by the SCI weight); with ``sanitize=True`` it uses the
    production ``_sanitize_variance`` and normalizes by ``outvarw``.
    """
    ny, nx = shape
    pm = _identity_pixmap(ny, nx)
    outsci = np.zeros(shape, np.float32); outwht = np.zeros(shape, np.float32)
    outvar = np.zeros(shape, np.float32); outvarw = np.zeros(shape, np.float32)
    sci = Drizzle(out_img=outsci, out_wht=outwht, kernel="square",
                  fillval="INDEF", disable_ctx=True)
    var = Drizzle(out_img=outvar, out_wht=outvarw, kernel="square",
                  fillval="INDEF", disable_ctx=True)
    addkw = dict(exptime=1000.0, pixmap=pm, pixfrac=1.0, in_units="cps",
                 xmin=0, xmax=nx - 1, ymin=0, ymax=ny - 1)
    with np.errstate(all="ignore"):
        for vr, vp, vf, weight in inputs:
            sci.add_image(data=np.zeros(shape, np.float32), weight_map=weight,
                          **addkw)
            if sanitize:
                var_total, var_weight = _sanitize_variance(vr, vp, vf, weight)
            else:
                var_total = (vr + vp + vf).astype(np.float32)   # raw sum
                var_weight = weight
            var.add_image(data=(var_total * weight).astype(np.float32),
                          weight_map=var_weight, **addkw)
        denom = outvarw if sanitize else outwht
        err = np.sqrt(np.where(denom > 0, outvar / denom, np.nan)).astype(np.float32)
    return err, outwht


def _const(shape, value):
    return np.full(shape, value, np.float32)


@pytest.mark.parametrize("bad_value", [np.inf, np.nan, -1.0])
def test_variance_trick_sanitized_vs_raw(bad_value):
    """One bad variance component at a GOOD pixel in one input: the raw trick
    produces a non-finite ERR there; the sanitized trick stays finite."""
    shape = (5, 5)
    weight = _const(shape, 1000.0)
    clean = (_const(shape, 1e-3), _const(shape, 2e-3), _const(shape, 1e-4),
             weight)
    vp_bad = _const(shape, 2e-3); vp_bad[2, 2] = bad_value
    bad = (_const(shape, 1e-3), vp_bad, _const(shape, 1e-4), weight)
    inputs = [clean, bad, clean]

    raw_err, _ = _drizzle_variance_trick(inputs, shape, sanitize=False)
    san_err, san_wht = _drizzle_variance_trick(inputs, shape, sanitize=True)

    covered = san_wht > 0
    assert covered[2, 2]
    assert not np.isfinite(raw_err[2, 2])         # pre-fix: poisoned
    assert np.isfinite(san_err[covered]).all()    # fixed: all covered finite
    assert san_err[2, 2] > 0
    # away from the bad pixel the two agree (masking is local)
    off = covered.copy(); off[2, 2] = False
    np.testing.assert_allclose(san_err[off], raw_err[off], rtol=1e-5)


def test_per_component_masking_survives_component_bad_across_all_inputs():
    """A component that is bad in EVERY input at a pixel (e.g. a flat-field
    column) must still yield a finite ERR from the surviving components — this
    is the case where masking the summed variance would punch a NaN hole."""
    shape = (4, 4)
    weight = _const(shape, 500.0)
    inputs = []
    for _ in range(3):
        vf = _const(shape, 1e-4); vf[1, 1] = np.inf      # flat bad in all inputs
        inputs.append((_const(shape, 1e-3), _const(shape, 2e-3), vf, weight))

    err, wht = _drizzle_variance_trick(inputs, shape, sanitize=True)
    assert wht[1, 1] > 0
    assert np.isfinite(err[1, 1])                         # NOT a NaN hole
    # ERR reflects rnoise+poisson only (flat dropped), averaged over 3 equal-
    # weight inputs: V = var_per_input / N for N equal measurements.
    np.testing.assert_allclose(err[1, 1], np.sqrt((1e-3 + 2e-3) / 3), rtol=1e-3)
    assert not np.isinf(err).any()


def test_variance_trick_all_components_bad_gives_nan_not_inf():
    """A pixel where no input has any valid variance component has no noise
    estimate -> ERR is NaN (missing-data convention), never inf or 0."""
    shape = (4, 4)
    weight = _const(shape, 500.0)
    inputs = []
    for _ in range(2):
        vr = _const(shape, 1e-3); vr[1, 1] = np.inf
        vp = _const(shape, 2e-3); vp[1, 1] = np.inf
        vf = _const(shape, 1e-4); vf[1, 1] = np.inf
        inputs.append((vr, vp, vf, weight))

    err, wht = _drizzle_variance_trick(inputs, shape, sanitize=True)
    assert not np.isinf(err).any()
    assert np.isnan(err[1, 1])
    other = (wht > 0).copy(); other[1, 1] = False
    assert np.isfinite(err[other]).all()
