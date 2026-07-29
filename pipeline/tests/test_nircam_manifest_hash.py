"""Tests for the resample config hash (tile staleness detection).

The resample step skips background subtraction entirely for a tile whose
manifest and ``_i2d_before_bkgsub.fits`` both exist, so the config hash is
the only thing that reruns bkgsub on existing tiles. It must therefore
cover every bkgsub setting that changes mosaic pixels, plus an algorithm
version for behavior changes at identical settings (the guard rollout).
"""

import dataclasses

from campfire_pipeline.nircam.bkgsub import SubtractBackground
from campfire_pipeline.nircam.manifest import (
    BKGSUB_PIXEL_DEFAULTS, _resample_config_hash,
)


def _h(cfg):
    return _resample_config_hash(cfg, '60mas')


def test_guard_and_tier_settings_change_hash():
    base = _h({})
    # the settings this rollout changes must all hash distinctly
    assert _h({'bg_guard': False}) != base
    assert _h({'guard_trough_t': 3.0}) != base
    assert _h({'guard_ceiling_boxes': [32, 64]}) != base
    assert _h({'tier_kernel_size': [25, 15, 5]}) != base
    assert _h({'bg_interpolator': 'IDW'}) != base
    assert _h({'bg_reject': True}) != base
    # ... and settings that don't affect pixels must not
    assert _h({'plot': False, 'split_extensions': False}) == base


def test_algorithm_version_marks_pre_guard_manifests_stale():
    """A pre-guard (v1) manifest hash can never equal a current hash.

    The v1 hash was built from only pixfrac/kernel/pixel_scale/
    background_subtract (+ conditional wht_aware/bg_reject keys); the
    current hash always adds ``bkgsub_algorithm`` and the bkgsub settings
    when background subtraction is on, so every historical manifest
    rehashes stale and its tile reruns bkgsub — the rollout the changelog
    promises.
    """
    import hashlib
    import json

    v1 = json.dumps({
        'pixfrac': 1, 'kernel': 'square', 'pixel_scale': '60mas',
        'background_subtract': True,
    }, sort_keys=True)
    v1_hash = f'sha256:{hashlib.sha256(v1.encode()).hexdigest()}'
    assert _h({}) != v1_hash


def test_no_bkgsub_keys_hashed_when_subtraction_disabled():
    """background_subtract=False tiles never touch bkgsub, so guard/tier
    tuning must not mark them stale."""
    off = {'background_subtract': False}
    assert _h(off) == _h({**off, 'bg_guard': False, 'guard_trough_t': 3.0})


def test_defaults_dict_matches_subtract_background_fields():
    """Every hashed key must be a real SubtractBackground field with the
    same default, so the hash can't drift from what resample_step applies
    (resample_step constructs SubtractBackground from this same dict)."""
    fields = {f.name: f for f in dataclasses.fields(SubtractBackground)}
    for key, default in BKGSUB_PIXEL_DEFAULTS.items():
        assert key in fields, key
        f = fields[key]
        dc_default = (f.default_factory()
                      if f.default_factory is not dataclasses.MISSING
                      else f.default)
        # resample_step's mosaic-stage defaults deliberately differ from
        # the dataclass (per-exposure) defaults for these two:
        if key in ('ring_downsample', 'bg_guard'):
            continue
        assert dc_default == default, key
