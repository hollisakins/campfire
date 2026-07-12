"""Mosaic thumbnail pair: size-capped PNG renditions of a mosaic SCI.

A fixed /4 downsample scaled with the mosaic (a wide 30 mas strip produced
~10k-px, tens-of-MB "thumbnails"), so the render is capped by target long
side instead — written twice per mosaic: a small ``_thumb.png`` for the web
table and a large ``_quicklook.png`` for the click-to-enlarge popup.
"""

import matplotlib

matplotlib.use('Agg')

import matplotlib.pyplot as plt
import numpy as np

from campfire_pipeline.nircam.steps._plots import plot_mosaic_thumbnail


def _dims(path):
    img = plt.imread(str(path))
    return img.shape[1], img.shape[0]  # (w, h)


def test_table_thumbnail_cap(tmp_path):
    sci = np.random.default_rng(0).normal(size=(2200, 5000)).astype(np.float32)
    out = tmp_path / 'thumb.png'
    plot_mosaic_thumbnail(sci, str(out), max_dim=500)
    w, h = _dims(out)
    assert max(w, h) <= 500
    assert max(w, h) == 500  # ceil(5000/500)=10 -> exactly at the cap
    assert out.stat().st_size < 1024 * 1024


def test_quicklook_cap(tmp_path):
    sci = np.random.default_rng(1).normal(size=(2200, 5000)).astype(np.float32)
    out = tmp_path / 'quicklook.png'
    plot_mosaic_thumbnail(sci, str(out), max_dim=4096)
    w, h = _dims(out)
    assert max(w, h) <= 4096
    assert max(w, h) == 2500  # ceil(5000/4096)=2 -> half size


def test_small_mosaic_saved_at_native_size(tmp_path):
    sci = np.random.default_rng(2).normal(size=(200, 300)).astype(np.float32)
    out = tmp_path / 'thumb.png'
    plot_mosaic_thumbnail(sci, str(out), max_dim=500)
    assert _dims(out) == (300, 200)  # under the cap: factor 1, no upsampling
