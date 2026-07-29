"""Regression tests for the mosaic bkgsub-done record (issue #427).

``_i2d_before_bkgsub.fits`` existence used to be the *only* record that
background subtraction had already run on a mosaic tile, so deleting the
(20–130 GB) snapshot to reclaim space made the next up-to-date run silently
subtract the background a second time, in place. The record is now the
``CFP_BKGS`` stamp on the i2d primary header; the snapshot is a deletable
rollback convenience, with its existence kept only as a legacy fallback
(which backfills the stamp).

These tests drive the real ``resample_step`` control flow with the manifest
staleness checks stubbed to "up-to-date" and a fake ``SubtractBackground``
that subtracts a constant — so a double subtraction is directly measurable
in the SCI pixels.
"""

import os
import sys
import types

import numpy as np
from astropy.io import fits

import campfire_pipeline.nircam.manifest as manifest_mod
import campfire_pipeline.nircam.steps.resample as resample_mod
from campfire_pipeline.nircam.manifest import (
    MOSAIC_BKGSUB_KEY, bkgsub_stamp_value, build_mosaic_name,
)

SKY = 10.0  # constant SCI level of the fixture mosaic
BKG = 1.0   # constant the fake SubtractBackground removes


class FakeSubtractBackground:
    """Stand-in for SubtractBackground: subtracts the constant ``BKG``.

    Matches the real interface used by resample_step: constructed with the
    full kwarg soup, ``call(path)`` writes ``<path>_bkgsub.fits`` and sets
    ``self.outfile``. Each application shifts SCI down by ``BKG``, so a
    double subtraction is visible as SKY - 2*BKG.
    """

    def __init__(self, **kwargs):
        self.outfile = None

    def call(self, filepath):
        self.outfile = filepath.replace('.fits', '_bkgsub.fits')
        with fits.open(filepath) as hdul:
            hdul['SCI'].data = hdul['SCI'].data - BKG
            # The real SubtractBackground always appends SRCMASK; the skip
            # logic uses its presence to tell a subtracted i2d from a
            # restored pre-bkgsub snapshot.
            hdul.append(fits.ImageHDU(
                np.zeros((4, 4), dtype=np.uint8), name='SRCMASK'))
            hdul.writeto(self.outfile, overwrite=True)
        return self.outfile


def _mosaic_path(tmp_path):
    name = build_mosaic_name('f444w', 'cosmos', '30mas', 'T1')
    return os.path.join(str(tmp_path), f'{name}_i2d.fits')


def _pre_bkg_path(tmp_path):
    return _mosaic_path(tmp_path).replace('_i2d.fits',
                                          '_i2d_before_bkgsub.fits')


def _write_mosaic(path, sci_level, stamped=False, srcmask=False):
    sci = fits.ImageHDU(np.full((4, 4), sci_level, dtype=np.float32),
                        name='SCI')
    err = fits.ImageHDU(np.ones((4, 4), dtype=np.float32), name='ERR')
    wht = fits.ImageHDU(np.ones((4, 4), dtype=np.float32), name='WHT')
    primary = fits.PrimaryHDU()
    if stamped:
        primary.header[MOSAIC_BKGSUB_KEY] = 'v0 cfg=deadbeef0000'
    hdus = [primary, sci, err, wht]
    if srcmask:
        hdus.append(fits.ImageHDU(np.zeros((4, 4), dtype=np.uint8),
                                  name='SRCMASK'))
    fits.HDUList(hdus).writeto(path, overwrite=True)


def _sci_level(path):
    return float(fits.getdata(path, extname='SCI')[0, 0])


def _run(tmp_path, monkeypatch, step_config=None):
    """Run resample_step on the fixture tile; returns the captured log."""
    captured = []
    monkeypatch.setattr(resample_mod, 'select_overlapping_files',
                        lambda files, poly: ['exp1.fits'])
    monkeypatch.setattr(resample_mod, 'log', captured.append)
    monkeypatch.setattr(manifest_mod, 'check_inputs_changed',
                        lambda *a, **k: (False, []))
    monkeypatch.setattr(manifest_mod, 'check_config_changed',
                        lambda *a, **k: False)
    # resample_step imports SubtractBackground lazily from
    # campfire_pipeline.nircam.bkgsub; pre-seeding sys.modules intercepts
    # that import without loading the real module (and its jwst/photutils
    # stack), which the control flow under test never needs.
    fake_bkgsub_mod = types.ModuleType('campfire_pipeline.nircam.bkgsub')
    fake_bkgsub_mod.SubtractBackground = FakeSubtractBackground
    monkeypatch.setitem(sys.modules, 'campfire_pipeline.nircam.bkgsub',
                        fake_bkgsub_mod)

    field = types.SimpleNamespace(
        name='cosmos',
        tiles={'T1': None},
        filter_dir=lambda f: str(tmp_path),
        get_tile_corners=lambda t: [(0, 0), (0, 1), (1, 1), (1, 0)],
    )
    cfg = {'pixel_scale': '30mas', 'split_extensions': False, 'plot': False}
    cfg.update(step_config or {})
    resample_mod.resample_step(
        'f444w', ['exp1.fits'], field, cfg, 'v0.0.0', overwrite=False,
    )
    return captured


def _skipped(logs):
    return any('skipping background subtraction' in m for m in logs)


# ---------------------------------------------------------------------------
# The issue-#427 failure mode
# ---------------------------------------------------------------------------

def test_deleting_snapshot_does_not_double_subtract(tmp_path, monkeypatch):
    mosaic = _mosaic_path(tmp_path)
    pre_bkg = _pre_bkg_path(tmp_path)
    _write_mosaic(mosaic, SKY)

    # First up-to-date pass: mosaic exists but is unsubtracted (no stamp, no
    # snapshot), so bkgsub runs once.
    logs = _run(tmp_path, monkeypatch)
    assert not _skipped(logs)
    assert _sci_level(mosaic) == SKY - BKG
    assert MOSAIC_BKGSUB_KEY in fits.getheader(mosaic)
    assert _sci_level(pre_bkg) == SKY  # snapshot holds the unsubtracted data

    # The failure mode: delete the snapshot to reclaim space, re-run.
    # Pre-fix, existence of the snapshot was the only bkgsub-done record, so
    # this run subtracted again (SCI -> SKY - 2*BKG). The stamp must prevent
    # that.
    os.remove(pre_bkg)
    logs = _run(tmp_path, monkeypatch)
    assert _skipped(logs)
    assert _sci_level(mosaic) == SKY - BKG
    assert not os.path.exists(pre_bkg)  # skip does not resurrect the snapshot


def test_snapshot_never_carries_the_stamp(tmp_path, monkeypatch):
    # Restoring the snapshot over the i2d is the rollback path; if the
    # snapshot carried the stamp, the rolled-back (unsubtracted) mosaic
    # would wrongly skip bkgsub forever after.
    mosaic = _mosaic_path(tmp_path)
    _write_mosaic(mosaic, SKY)
    _run(tmp_path, monkeypatch)
    assert MOSAIC_BKGSUB_KEY not in fits.getheader(_pre_bkg_path(tmp_path))


# ---------------------------------------------------------------------------
# Legacy fallback + backfill
# ---------------------------------------------------------------------------

def test_legacy_mosaic_skips_via_snapshot_and_backfills(tmp_path, monkeypatch):
    # A mosaic subtracted before the stamp existed: no CFP_BKGS, snapshot on
    # disk, SRCMASK extension present (SubtractBackground always appends it).
    # It must keep skipping, and the skip must backfill the stamp so the
    # snapshot becomes deletable.
    mosaic = _mosaic_path(tmp_path)
    pre_bkg = _pre_bkg_path(tmp_path)
    _write_mosaic(mosaic, SKY - BKG, srcmask=True)
    _write_mosaic(pre_bkg, SKY)

    logs = _run(tmp_path, monkeypatch)
    assert _skipped(logs)
    assert _sci_level(mosaic) == SKY - BKG
    assert MOSAIC_BKGSUB_KEY in fits.getheader(mosaic)

    # Now the snapshot is deletable: the next run skips via the stamp alone.
    os.remove(pre_bkg)
    logs = _run(tmp_path, monkeypatch)
    assert _skipped(logs)
    assert _sci_level(mosaic) == SKY - BKG


def test_restored_snapshot_reruns_instead_of_backfilling(tmp_path, monkeypatch):
    # Rollback by *copying* the snapshot over the i2d, leaving the snapshot
    # in place: the restored i2d has unsubtracted pixels, no stamp, and no
    # SRCMASK extension. The fallback must NOT mistake it for a legacy
    # subtracted mosaic (which would backfill the stamp and permanently skip
    # subtraction on unsubtracted data) — it must re-run bkgsub.
    import shutil

    mosaic = _mosaic_path(tmp_path)
    pre_bkg = _pre_bkg_path(tmp_path)
    _write_mosaic(mosaic, SKY)

    _run(tmp_path, monkeypatch)  # subtract once: SCI -> SKY - BKG, snapshot
    shutil.copy2(pre_bkg, mosaic)  # rollback, snapshot left behind
    assert _sci_level(mosaic) == SKY

    logs = _run(tmp_path, monkeypatch)
    assert not _skipped(logs)
    assert _sci_level(mosaic) == SKY - BKG  # re-subtracted exactly once
    assert MOSAIC_BKGSUB_KEY in fits.getheader(mosaic)
    assert _sci_level(pre_bkg) == SKY  # fresh snapshot of the restored input


def test_stamped_mosaic_skips_without_snapshot(tmp_path, monkeypatch):
    mosaic = _mosaic_path(tmp_path)
    _write_mosaic(mosaic, SKY - BKG, stamped=True)
    logs = _run(tmp_path, monkeypatch)
    assert _skipped(logs)
    assert _sci_level(mosaic) == SKY - BKG


# ---------------------------------------------------------------------------
# keep_pre_bkgsub opt-out
# ---------------------------------------------------------------------------

def test_keep_pre_bkgsub_false_skips_snapshot(tmp_path, monkeypatch):
    mosaic = _mosaic_path(tmp_path)
    _write_mosaic(mosaic, SKY)
    cfg = {'keep_pre_bkgsub': False}

    logs = _run(tmp_path, monkeypatch, step_config=cfg)
    assert not _skipped(logs)
    assert _sci_level(mosaic) == SKY - BKG
    assert MOSAIC_BKGSUB_KEY in fits.getheader(mosaic)
    assert not os.path.exists(_pre_bkg_path(tmp_path))

    # And the stamp still guards the next run.
    logs = _run(tmp_path, monkeypatch, step_config=cfg)
    assert _skipped(logs)
    assert _sci_level(mosaic) == SKY - BKG


# ---------------------------------------------------------------------------
# Stamp value
# ---------------------------------------------------------------------------

def test_stamp_value_tracks_pixel_settings_only():
    base = bkgsub_stamp_value({})
    assert base.startswith(f'v{manifest_mod._BKGSUB_ALGORITHM_VERSION} cfg=')
    # Pixel-affecting setting changes the hash...
    assert bkgsub_stamp_value({'ring_radius_in': 99}) != base
    assert bkgsub_stamp_value({'wht_aware': False}) != base
    # ...pixel-neutral settings do not.
    assert bkgsub_stamp_value({'keep_pre_bkgsub': False}) == base
    assert bkgsub_stamp_value({'split_extensions': False}) == base
