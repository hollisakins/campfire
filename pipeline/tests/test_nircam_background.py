"""Tests for the per-exposure 2D background step.

The scientific content lives in nircamx ``SubtractBackground`` (reused); these
tests pin the behaviour the ``background`` step relies on — a smooth ramp is
removed while a compact source's peak is conserved — plus the orchestrator /
provenance wiring (ordering, CFP chain, reset guard).
"""

import numpy as np
import pytest
from astropy.io import fits


def _synthetic_exposure(tmp_path, ramp_amp=0.1, src_amp=5.0):
    """Write a small SCI/ERR/DQ FITS: linear ramp + Gaussian source + noise."""
    rng = np.random.default_rng(0)
    n = 256
    yy, xx = np.mgrid[0:n, 0:n]
    ramp = ramp_amp * (xx / n) + 0.5 * ramp_amp * (yy / n)
    src = src_amp * np.exp(-((xx - 128) ** 2 + (yy - 128) ** 2) / (2 * 3.0 ** 2))
    sci = (ramp + src + rng.normal(0, 0.01, (n, n))).astype(np.float32)
    err = np.full((n, n), 0.01, np.float32)
    err[:4, :] = np.nan          # off-detector border
    dq = np.zeros((n, n), np.int32)
    path = tmp_path / 'exp.fits'
    fits.HDUList([
        fits.PrimaryHDU(),
        fits.ImageHDU(sci, name='SCI'),
        fits.ImageHDU(err, name='ERR'),
        fits.ImageHDU(dq, name='DQ'),
    ]).writeto(path, overwrite=True)
    return str(path), ramp, src


# Small-image-safe config (Background2D grids must accommodate filter_size).
_CFG = dict(bg_box_size=32, bg_filter_size=3, ring_radius_in=20, ring_width=3,
            ring_clip_box_size=64, ring_clip_filter_size=3, ring_downsample=1,
            tier_dilate_size=[10, 8, 6, 4])


def test_background_removes_ramp_conserves_source(tmp_path):
    from campfire_pipeline.nircam.bkgsub import SubtractBackground
    path, ramp, src = _synthetic_exposure(tmp_path)
    bg = SubtractBackground.from_config(_CFG)
    sub, mask, bitmask = bg.compute(path)

    # Ramp removed: in a source-free, on-detector region the residual is ~flat
    # about zero (the input there was the ramp, ~0.05-0.1).
    blank = np.zeros((256, 256), bool)
    blank[200:250, 20:230] = True
    blank &= ~mask & np.isfinite(sub)
    assert abs(np.median(sub[blank])) < 0.01           # ramp gone (~0)
    assert np.std(sub[blank]) < 0.02                   # flat

    # Source peak conserved (only the local background, ~0.075, is removed).
    peak_in = float(src[128, 128])                     # ~5.0
    peak_out = float(sub[126:131, 126:131].max())
    assert peak_out > peak_in - 0.2


def test_from_config_forwards_known_keys_only():
    from campfire_pipeline.nircam.bkgsub import SubtractBackground
    bg = SubtractBackground.from_config(
        dict(enabled=True, plot=False, bg_box_size=77, bogus='x'))
    assert bg.bg_box_size == 77          # forwarded
    assert not hasattr(bg, 'enabled')    # non-field keys ignored, no crash


def test_orchestrator_and_cfp_wiring():
    from campfire_pipeline.nircam import orchestrate as o
    from campfire_pipeline.common import cfp
    from campfire_pipeline.nircam.cli import _SCI_MUTATING_STEPS

    names = [s for s, _ in o.PROCESS_STEPS]
    assert ('background', 'CFP_BKG') in o.PROCESS_STEPS
    # Order: image2 -> background -> artifact -> striping.
    assert (names.index('image2') < names.index('background')
            < names.index('artifact') < names.index('striping'))
    assert o._RUNNERS['background'] is o._run_background
    assert 'background' in _SCI_MUTATING_STEPS

    # clear_from(CFP_BKG) must cascade through artifact + striping + image2.
    chain = cfp.CFP_KEYS[cfp.CFP_KEYS.index('CFP_BKG'):]
    assert {'CFP_ART', 'CFP_1F', 'CFP_IMG2', 'CFP_OUT'} <= set(chain)
    assert cfp.format(CFP_BKG='box=128')['CFP_BKG'][0] == 'box=128'


def test_background_disabled_by_default():
    from campfire_pipeline.config import load_config
    cfg = load_config()['nircam']['background']
    assert cfg['enabled'] is False       # opt-in
