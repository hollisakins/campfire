"""Wiring of the visit tie into ``align``: which catalog a field aligns to, how
pools are grouped by visit, and the fixed-window centroid option."""

from __future__ import annotations

import os
from types import SimpleNamespace

import numpy as np

from campfire_pipeline.nircam import orchestrate
from campfire_pipeline.nircam.align.detect import detect_sources


def _cfg(tie_enabled, **tie):
    return {'refcat': 'ext.ecsv', 'tie': {'enabled': tie_enabled, **tie}}


def test_align_uses_the_external_catalog_without_the_tie(tmp_path):
    d = str(tmp_path)
    open(os.path.join(d, 'f_tie_consensus.ecsv'), 'w').close()
    assert orchestrate._resolve_align_refcat(
        _cfg(False), d, field_name='f') == os.path.join(d, 'ext.ecsv')
    # no tie section at all
    assert orchestrate._resolve_align_refcat(
        {'refcat': 'ext.ecsv'}, d, field_name='f') == os.path.join(d, 'ext.ecsv')


def test_align_uses_the_consensus_catalog_once_it_exists(tmp_path):
    d = str(tmp_path)
    # enabled, not built yet: the external catalog
    assert orchestrate._resolve_align_refcat(
        _cfg(True), d, field_name='f') == os.path.join(d, 'ext.ecsv')
    open(os.path.join(d, 'f_tie_consensus.ecsv'), 'w').close()
    assert orchestrate._resolve_align_refcat(
        _cfg(True), d, field_name='f') == os.path.join(d, 'f_tie_consensus.ecsv')
    # the tie's own first stage asks for the external one regardless
    assert orchestrate._resolve_align_refcat(
        _cfg(True), d, field_name='f', external=True) == os.path.join(d, 'ext.ecsv')
    # a custom output name
    open(os.path.join(d, 'mine.ecsv'), 'w').close()
    assert orchestrate._resolve_align_refcat(
        _cfg(True, catalog='mine.ecsv'), d, field_name='f') == os.path.join(d, 'mine.ecsv')


def _pool(key, rootname):
    return SimpleNamespace(key=key, members=[SimpleNamespace(rootname=rootname)])


def test_visit_tasks_group_pools_by_visit_and_module():
    pools = [_pool('jw01324001001_02101_00001', 'jw01324001001_02101_00001_nrcalong'),
             _pool('jw01324001001_02101_00002', 'jw01324001001_02101_00002_nrcalong'),
             _pool('jw02561001002_02101_00001', 'jw02561001002_02101_00001_nrcalong'),
             _pool('jw02561001002_02101_00002:b', 'jw02561001002_02101_00002_nrcblong')]
    # one pending pool: its whole visit is solved, the other visit is left alone
    tasks, solved = orchestrate._visit_tasks(pools, [pools[1]])
    assert [t[0] for t in tasks] == ['jw01324001001']
    assert [k for k, _ in tasks[0][1]] == [pools[0].key, pools[1].key]
    assert solved == pools[:2]
    # per-module pools of one visit are separate tasks
    tasks, _ = orchestrate._visit_tasks(pools, pools)
    assert sorted(t[0] for t in tasks) == ['jw01324001001', 'jw02561001002',
                                           'jw02561001002:b']


def _image_with_lopsided_sources(seed=0, n=40, size=400):
    """Compact cores, each with a faint extended wing displaced to +x."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size].astype(float)
    img = np.zeros((size, size))
    pos = []
    grid = np.linspace(40, size - 40, 7)
    for gx in grid:
        for gy in grid:
            if len(pos) >= n:
                break
            x0, y0 = gx + rng.uniform(-0.5, 0.5), gy + rng.uniform(-0.5, 0.5)
            pos.append((x0, y0))
            img += 100.0 * np.exp(-0.5 * ((xx - x0) ** 2 + (yy - y0) ** 2) / 1.2 ** 2)
            img += 6.0 * np.exp(-0.5 * ((xx - x0 - 5.0) ** 2 + (yy - y0) ** 2) / 4.0 ** 2)
    err = np.full(img.shape, 0.05)
    return img + rng.normal(0, 0.05, img.shape), err, np.array(pos)


def _offsets(cat, pos):
    from scipy.spatial import cKDTree
    d, j = cKDTree(pos).query(np.c_[cat['x'], cat['y']])
    k = d < 2.0
    return np.asarray(cat['x'])[k] - pos[j[k], 0], np.asarray(cat['y'])[k] - pos[j[k], 1]


def test_fixed_window_centroid_follows_the_core():
    img, err, pos = _image_with_lopsided_sources()
    wide = detect_sources(img, err, snr_min=10.0)
    narrow = detect_sources(img, err, snr_min=10.0, centroid_sigma=1.5)
    assert len(wide) >= 30 and len(narrow) == len(wide)
    wx, wy = _offsets(wide, pos)
    nx, ny = _offsets(narrow, pos)
    # the adaptive window is pulled toward the wing; the fixed window much less
    assert np.median(wx) > 0.15
    assert abs(np.median(nx)) < 0.5 * np.median(wx)
    assert abs(np.median(ny)) < 0.05 and abs(np.median(wy)) < 0.05


def test_centroid_sigma_zero_is_the_adaptive_window():
    img, err, _ = _image_with_lopsided_sources(seed=1)
    a = detect_sources(img, err, snr_min=10.0)
    b = detect_sources(img, err, snr_min=10.0, centroid_sigma=0.0)
    assert np.array_equal(np.asarray(a['x']), np.asarray(b['x']))
    assert np.array_equal(np.asarray(a['y']), np.asarray(b['y']))
