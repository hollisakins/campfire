"""Tests for the first pass on single (master-background-subtracted) frames.

The synthetic slitlet of ``test_nirspec_profile_fit``: three nods, the source one
shutter apart in each. Here each exposure is a single frame (no partners), as
``first_pass.load_group_sky`` builds them, so the data are the source's own light
plus noise.
"""

import numpy as np

from campfire_pipeline.nirspec import first_pass as FP
from campfire_pipeline.nirspec import profile_fit as PF

from test_nirspec_profile_fit import NX, NY, TRUTH, _exposures, _spectrum, _truth_result


def _single_frames(flux_scale, noise, seed=1):
    exps = _exposures()
    for k, e in enumerate(exps):
        e.nod_partners, e.partners = e.partners, []
    R = _truth_result(exps, TRUTH, [0.0, 0.0, 0.0], flux_scale)
    rng = np.random.default_rng(seed)
    for k, e in enumerate(exps):
        e.diff = PF.model_frame(R, k, applied_pathloss=False) + noise * rng.normal(size=(NY, NX))
        e.var = np.full((NY, NX), noise ** 2)
    return exps


def test_single_frame_model_is_the_source_alone():
    """With no partners an exposure's model is its own frame of the source: no
    negative copies, and the weights are its frame variance."""
    exps = _single_frames(100.0, 1.0)
    R = _truth_result(exps, TRUTH, [0.0, 0.0, 0.0], 100.0)
    for k in range(3):
        m = PF.model_frame(R, k, applied_pathloss=False)
        assert np.nanmin(m) >= -1e-9 and np.nanmax(m) > 0
    M = R.model
    assert np.allclose(M.diff_variance(0), exps[0].var)
    assert np.all(M.dof_weight() == 1.0)


def test_first_pass_recovers_the_spectrum_on_single_frames(tmp_path):
    exps = _single_frames(200.0, 1.0)
    P1 = FP.compute_first_pass(exps, sid=1, maps={}, workspace=str(tmp_path), is_prism=True,
                               stage_config={'contam_pass1_smooth_prism': 0})
    assert P1.reliable and P1.spec_src == 'master' and P1.shape_src == 'own'
    good = np.isfinite(P1.flux_raw) & (P1.lam > 1.0) & (P1.lam < 4.8)
    ratio = P1.flux_raw[good] / (200.0 * _spectrum(P1.lam[good]))
    assert abs(np.median(ratio) - 1) < 0.02
    assert all(e.partners == [] for e in exps)                    # restored after the frames
    assert all(e.nod_partners is not None for e in exps)


def test_first_pass_refuses_nod_differences(tmp_path):
    exps = _exposures()
    try:
        FP.compute_first_pass(exps, sid=1, maps={}, workspace=str(tmp_path), is_prism=True, stage_config={})
    except ValueError as e:
        assert 'single frames' in str(e)
    else:
        raise AssertionError('nod differences accepted')
