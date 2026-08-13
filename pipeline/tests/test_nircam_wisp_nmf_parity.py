"""campfire's NMF amplitude solve must reproduce nmfwisp at its legacy defaults.

``_nmf_amplitudes`` replaced the ``nmfwisp.fit_wisp`` call so the fit region and
the pixel weighting become reachable, but its defaults (``hsnr``/``ivar``) are
supposed to be *bit-for-bit* what ``estimate_wisp_standard`` did. Two things can
break that silently: an edit to our solve, or an nmfwisp release that changes
theirs. Either would move deployed pixel values with no config change and no
version signal, so it is worth a guard.

Synthetic data on purpose -- no staged frames needed, so this runs anywhere
nmfwisp is importable.
"""
import numpy as np
import pytest

from campfire_pipeline.nircam.steps.wisp import _nmf_amplitudes

nmfwisp = pytest.importorskip('nmfwisp.nmfwisp')


def _fake(ncomp=3, n=192, seed=11):
    """A wisp-like scene: smooth positive components + sources + noise."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:n, 0:n].astype(float)
    tmpl = np.empty((ncomp, n, n))
    for c in range(ncomp):
        cy, cx = 60 + 25 * c, 70 - 15 * c
        tmpl[c] = np.exp(-(((yy - cy) / (34.0 + 9 * c)) ** 2
                           + ((xx - cx) / (19.0 + 6 * c)) ** 2))
    truth = np.array([0.9, 0.35, 0.6])[:ncomp]
    sci = np.einsum('i,imn->mn', truth, tmpl) + 0.05
    sci += rng.normal(0, 0.01, sci.shape)
    for _ in range(12):                       # a few sources to be masked
        sy, sx = rng.integers(10, n - 10, 2)
        sci[sy - 3:sy + 3, sx - 3:sx + 3] += 3.0
    err = np.full((n, n), 0.01) + 0.004 * np.sqrt(np.maximum(sci, 0))
    src = sci > 0.6
    wmask = tmpl.sum(0) > 0.05 * tmpl.sum(0).max()
    hsnr = tmpl.sum(0) > 0.01 * tmpl.sum(0).max()
    return sci, err, src, tmpl, wmask, hsnr


@pytest.mark.parametrize('ncomp', [1, 2, 3])
def test_legacy_defaults_match_nmfwisp(ncomp):
    sci, err, src, tmpl, wmask, hsnr = _fake(ncomp=ncomp)

    W_cf, model_cf, _sky, used = _nmf_amplitudes(
        sci, err, src, tmpl, wmask, hsnr, region='hsnr', sigma='ivar')
    assert used == 'hsnr'

    data2, err2, mask2 = nmfwisp.process_data(
        sci.copy(), err.copy(), src.copy(), wmask)
    _wsub, model_nw, _we, W_nw, _We = nmfwisp._subtract_wisp(
        data2, tmpl, err2, mask2, None, hsnr, bool_weighted=False)

    W_nw = np.asarray(W_nw).ravel()
    scale = max(float(np.max(np.abs(W_nw))), 1e-12)
    assert np.max(np.abs(W_cf - W_nw)) / scale < 1e-9, (
        f'amplitudes diverged from nmfwisp: {W_cf} vs {W_nw}')

    fin = np.isfinite(model_cf) & np.isfinite(model_nw)
    assert np.max(np.abs(model_cf[fin] - model_nw[fin])) < 1e-9


def test_region_and_sigma_knobs_change_the_fit():
    """Guard against the knobs silently becoming no-ops."""
    sci, err, src, tmpl, wmask, hsnr = _fake()
    W_legacy, _m, _s, _u = _nmf_amplitudes(sci, err, src, tmpl, wmask, hsnr,
                                           region='hsnr', sigma='ivar')
    W_new, _m2, _s2, _u2 = _nmf_amplitudes(sci, err, src, tmpl, wmask, hsnr,
                                           region='t30', sigma='flat')
    assert not np.allclose(W_legacy, W_new)
    assert np.all(W_new >= 0), 'NNLS must stay non-negative'


def test_degenerate_region_falls_back_rather_than_crashing():
    """A threshold that starves the fit must fall back, not raise or return junk."""
    sci, err, src, tmpl, wmask, hsnr = _fake()
    W, model, _sky, used = _nmf_amplitudes(sci, err, src, tmpl, wmask, hsnr,
                                           region='t99', sigma='flat')
    assert W.shape == (tmpl.shape[0],)
    assert np.all(np.isfinite(model))
    # the fallback must be REPORTED, not silent -- CFP_WISP is stamped from
    # this value, so a silent swap would claim t99 on an hSNR fit
    assert used == 'hsnr(fallback)', (
        f'starved region fell back but reported {used!r}')


def test_unknown_options_are_rejected():
    sci, err, src, tmpl, wmask, hsnr = _fake()
    with pytest.raises(ValueError):
        _nmf_amplitudes(sci, err, src, tmpl, wmask, hsnr, region='nope')
    with pytest.raises(ValueError):
        _nmf_amplitudes(sci, err, src, tmpl, wmask, hsnr, sigma='nope')


def test_exhausted_region_reports_none_rather_than_a_silent_no_op():
    """Both regions starved must be distinguishable from a successful fit.

    Returning zeros with no signal let ``_fit_nmf`` subtract nothing, stamp
    ``CFP_WISP``, and log success — after which ``should_skip`` treats an
    unsubtracted exposure as processed forever. The caller can only stamp the
    ``skipped`` sentinel if the solve says it failed, so assert the contract:
    ``region_used is None`` and the model is exactly zero.
    """
    sci, err, _src, tmpl, wmask, hsnr = _fake()
    everything = np.ones(sci.shape, bool)          # nothing survives masking
    W, model, _sky, used = _nmf_amplitudes(
        sci, err, everything, tmpl, wmask, hsnr, region='t50', sigma='ivar')
    assert used is None, f'exhausted fit reported {used!r} instead of None'
    assert not np.any(model), 'exhausted fit must return an all-zero model'
    assert W.shape == (tmpl.shape[0],)
    assert not np.any(W)


def test_successful_fit_reports_the_region_it_used():
    """The happy path must echo the requested region, not a fallback."""
    sci, err, src, tmpl, wmask, hsnr = _fake()
    for region in ('hsnr', 't30'):
        _W, _m, _s, used = _nmf_amplitudes(
            sci, err, src, tmpl, wmask, hsnr, region=region, sigma='ivar')
        assert used == region, f'{region} reported as {used!r}'
