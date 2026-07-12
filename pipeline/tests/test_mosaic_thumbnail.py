"""Mosaic thumbnail size cap: a fixed /4 downsample scaled with the mosaic
(a wide 30 mas strip produced ~10k-px, tens-of-MB "thumbnails"); the long
side is now capped at ``max_dim`` while small mosaics keep the old behavior.
"""

import matplotlib

matplotlib.use('Agg')

import matplotlib.pyplot as plt
import numpy as np

from campfire_pipeline.nircam.steps._plots import plot_mosaic_thumbnail


def _dims(path):
    img = plt.imread(str(path))
    return img.shape[1], img.shape[0]  # (w, h)


def test_large_mosaic_is_capped(tmp_path):
    sci = np.random.default_rng(0).normal(size=(2200, 5000)).astype(np.float32)
    out = tmp_path / 'thumb.png'
    plot_mosaic_thumbnail(sci, str(out), downsample=4, max_dim=1024)
    w, h = _dims(out)
    assert max(w, h) <= 1024
    # near the cap, not over-shrunk (ceil(5000/1024)=5 -> 1000 px)
    assert max(w, h) == 1000
    assert out.stat().st_size < 5 * 1024 * 1024


def test_small_mosaic_keeps_fixed_downsample(tmp_path):
    sci = np.random.default_rng(1).normal(size=(400, 800)).astype(np.float32)
    out = tmp_path / 'thumb.png'
    plot_mosaic_thumbnail(sci, str(out), downsample=4, max_dim=1024)
    assert _dims(out) == (200, 100)  # exactly /4, cap not engaged


def test_max_dim_none_restores_old_behavior(tmp_path):
    sci = np.random.default_rng(2).normal(size=(512, 8192)).astype(np.float32)
    out = tmp_path / 'thumb.png'
    plot_mosaic_thumbnail(sci, str(out), downsample=4, max_dim=None)
    assert _dims(out) == (2048, 128)  # fixed /4, uncapped
