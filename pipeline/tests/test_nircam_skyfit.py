"""Regression tests for skyfit helpers.

Guards the byte-order bug in ``fit_sky``: the old ``byteswap(inplace=True)``
+ ``view`` idiom (used to give bottleneck a native-order array) only produced
correct values from *non-native* input and silently corrupted an
already-native array into garbage. The cal-frame striping step feeds it a
native ``float64`` working copy, which turned the background into ~1e-315
denormals (or huge values), wrecking the 1/f correction.
"""

import numpy as np

from campfire_pipeline.nircam.skyfit import fit_sky


def _flat_sky(seed=1, shape=(1024, 1024), noise=0.02):
    rng = np.random.default_rng(seed)
    # ~zero-mean (pedestal-subtracted) background, native float64
    return (noise * rng.standard_normal(shape)).astype(np.float64)


def test_fit_sky_native_byteorder_not_corrupted():
    data = _flat_sky()
    big = data.astype(data.dtype.newbyteorder('>'))  # big-endian copy

    b_native = fit_sky(data.copy(), use_bottleneck=True)
    b_big = fit_sky(big.copy(), use_bottleneck=True)
    b_nobottle = fit_sky(data.copy(), use_bottleneck=False)

    # None should be garbage (the bug produced ~1e-315 denormals or huge values).
    for b in (b_native, b_big, b_nobottle):
        assert np.isfinite(b).all()
        assert np.nanmax(np.abs(b)) < 1.0

    # The bottleneck path (any byte order) must match the plain path exactly —
    # it only reorders bytes, it must not change values.
    assert np.allclose(b_native, b_nobottle, atol=1e-6)
    assert np.allclose(b_big, b_nobottle, atol=1e-6)
