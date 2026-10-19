"""Tests for the 1D extraction primitives in nirspec/extraction.py."""

import numpy as np

from campfire_pipeline.nirspec.extraction import boxcar_profile, extract_with_profile


def _source(profile, flux=15.0, n_cols=2000, sigma=1.0, seed=0):
    """A source whose light follows ``profile`` (any normalisation), total ``flux`` per column."""
    rng = np.random.default_rng(seed)
    p = np.asarray(profile, float)[:, None] / np.sum(profile)
    data = flux * p + sigma * rng.normal(size=(p.shape[0], n_cols))
    return data, np.full(data.shape, sigma)


def test_boxcar_fallback_profile_returns_total_flux():
    """The 3-px boxcar fallback sums to 3; used as the optimal profile it must still
    return the source flux (it returned a third of it), with errors that match."""
    prof = boxcar_profile(8.5, 11.5, 21)
    assert np.isclose(prof.sum(), 3.0)                    # unnormalised, as the fallback builds it
    data, err = _source(prof)
    fnu, fnu_err = extract_with_profile(prof, data.copy(), err.copy(), ivw=True)
    assert abs(np.mean(fnu) / 15.0 - 1) < 0.01
    assert 0.95 < np.std(fnu) / np.median(fnu_err) < 1.05  # errors describe the scatter


def test_normalised_profile_unchanged():
    """Profiles that already sum to 1 (optext_profile) extract exactly as before."""
    prof = np.zeros(21)
    prof[[9, 10, 11]] = [0.25, 0.5, 0.25]
    data, err = _source(prof)
    fnu, fnu_err = extract_with_profile(prof, data.copy(), err.copy(), ivw=True)
    p = prof[:, None]
    assert np.allclose(fnu, np.sum(p * data, 0) / np.sum(p ** 2, 0))
    assert np.allclose(fnu_err, np.sqrt(1 / np.sum(p ** 2 / err ** 2, 0)))
