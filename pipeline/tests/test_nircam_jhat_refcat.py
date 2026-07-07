"""Reference-catalog resolution for the NIRCam ``jhat`` step.

``_resolve_refcat`` accepts either a single ``refcat`` string (one catalog for
every filter) or a ``refcat_dict`` mapping filter -> filename. When both are
given, ``refcat_dict`` entries win per-filter and ``refcat`` is the fallback.
The heavy ``jhat`` import is lazy inside ``jhat_step``, so importing the helper
here is cheap.
"""
import os

import pytest

from campfire_pipeline.nircam.steps.jhat import _resolve_refcat

REFCAT_DIR = '/refcats'


def test_single_refcat_applies_to_every_filter():
    cfg = {'refcat': 'gaia_dr3.cat'}
    for filt in ('f444w', 'f200w', 'f115w'):
        assert _resolve_refcat(cfg, REFCAT_DIR, filt) == os.path.join(
            REFCAT_DIR, 'gaia_dr3.cat')


def test_refcat_dict_maps_per_filter():
    cfg = {'refcat_dict': {'f444w': 'lw.cat', 'f200w': 'sw.cat'}}
    assert _resolve_refcat(cfg, REFCAT_DIR, 'f444w') == os.path.join(
        REFCAT_DIR, 'lw.cat')
    assert _resolve_refcat(cfg, REFCAT_DIR, 'f200w') == os.path.join(
        REFCAT_DIR, 'sw.cat')


def test_dict_wins_per_filter_and_single_is_fallback():
    cfg = {'refcat': 'default.cat', 'refcat_dict': {'f444w': 'lw.cat'}}
    # Listed filter uses the dict entry.
    assert _resolve_refcat(cfg, REFCAT_DIR, 'f444w') == os.path.join(
        REFCAT_DIR, 'lw.cat')
    # Unlisted filter falls back to the single refcat.
    assert _resolve_refcat(cfg, REFCAT_DIR, 'f200w') == os.path.join(
        REFCAT_DIR, 'default.cat')


def test_missing_both_raises():
    with pytest.raises(ValueError, match="missing 'refcat'/'refcat_dict'"):
        _resolve_refcat({}, REFCAT_DIR, 'f444w')


def test_dict_without_entry_and_no_fallback_raises():
    cfg = {'refcat_dict': {'f444w': 'lw.cat'}}
    with pytest.raises(ValueError, match="no entry for filter 'f200w'"):
        _resolve_refcat(cfg, REFCAT_DIR, 'f200w')
