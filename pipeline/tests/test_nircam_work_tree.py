"""Combine working-tree tests (epic #261, N7).

The combine phase must not mutate the canonical per-exposure FITS. These cover
the two new pieces of machinery:

* ``_prime_work_copy`` — fuse CFMASK -> DO_NOT_USE, reset combine-phase stamps.
* ``Field.materialize_work`` — copy/freshness/incrementality, canonical frozen.

Synthetic FITS only (no jwst / CRDS), so the module imports cleanly.
"""

import os

import numpy as np
from astropy.io import fits

from campfire_pipeline.nircam.field import Field, _prime_work_copy

_DO_NOT_USE = 1
_ROOT = 'jw01727028001_04101_00003_nrcalong'


def _write_canonical(path, *, sci=None, cfmask=None,
                     stamps=('CFP_JHAT', 'CFP_MASK', 'CFP_BPIX', 'CFP_OUT')):
    """Write a minimal canonical exposure (primary + SCI + DQ [+ CFMASK])."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if sci is None:
        sci = np.arange(16, dtype=np.float32).reshape(4, 4)
    dq = np.zeros((4, 4), dtype=np.uint32)
    prim = fits.PrimaryHDU()
    for k in stamps:
        prim.header[k] = '2026-07-03T00:00:00'
    hdus = [prim, fits.ImageHDU(sci, name='SCI'), fits.ImageHDU(dq, name='DQ')]
    if cfmask is not None:
        hdus.append(fits.ImageHDU(cfmask.astype(np.uint8), name='CFMASK'))
    fits.HDUList(hdus).writeto(path, overwrite=True)


def test_prime_fuses_mask_and_clears_combine_stamps(tmp_path):
    cf = np.zeros((4, 4), np.uint8)
    cf[1, 1] = 1
    cf[2, 3] = 1
    p = str(tmp_path / f'{_ROOT}.fits')
    _write_canonical(p, cfmask=cf)

    _prime_work_copy(p)

    with fits.open(p) as hdul:
        dq = hdul['DQ'].data
        hdr = hdul[0].header
    # DO_NOT_USE set on exactly the masked pixels, nowhere else.
    assert np.array_equal((dq & _DO_NOT_USE).astype(bool), cf.astype(bool))
    # Combine-phase stamps are cleared so the fresh copy re-runs those steps;
    # the mask + process stamps are retained.
    assert 'CFP_BPIX' not in hdr and 'CFP_OUT' not in hdr
    assert 'CFP_MASK' in hdr and 'CFP_JHAT' in hdr


def test_prime_without_cfmask_leaves_dq_untouched(tmp_path):
    p = str(tmp_path / f'{_ROOT}.fits')
    _write_canonical(p, cfmask=None, stamps=('CFP_JHAT',))
    _prime_work_copy(p)
    with fits.open(p) as hdul:
        assert not hdul['DQ'].data.any()


def _make_field(tmp_path):
    f = Field(name='cosmos', filters=['f444w'], files=['jw01727*'],
              tangent_point=(150.0, 2.0), tiles={})
    f.setup_workspace(campfire_root=str(tmp_path))
    return f


def test_materialize_copies_to_work_tree_and_freezes_canonical(tmp_path):
    f = _make_field(tmp_path)
    cf = np.zeros((4, 4), np.uint8)
    cf[0, 0] = 1
    sci = np.arange(16, dtype=np.float32).reshape(4, 4)
    canon = os.path.join(f.filter_dir('f444w'), f'{_ROOT}.fits')
    _write_canonical(canon, sci=sci, cfmask=cf)

    work_paths = f.materialize_work('f444w')

    assert len(work_paths) == 1
    wp = work_paths[0]
    assert os.path.exists(wp) and 'nircam_work' in wp
    with fits.open(wp) as hdul:
        assert hdul['DQ'].data[0, 0] & _DO_NOT_USE       # mask fused on the work copy
        np.testing.assert_array_equal(hdul['SCI'].data, sci)
    # The canonical DQ stays frozen — no DO_NOT_USE baked in.
    with fits.open(canon) as hdul:
        assert not hdul['DQ'].data.any()


def test_materialize_is_incremental(tmp_path):
    f = _make_field(tmp_path)
    canon = os.path.join(f.filter_dir('f444w'), f'{_ROOT}.fits')
    _write_canonical(canon, cfmask=None, stamps=('CFP_JHAT', 'CFP_MASK'))
    wp = f.materialize_work('f444w')[0]

    # Simulate outlier stamping CFP_OUT onto the work copy.
    with fits.open(wp, mode='update') as hdul:
        hdul[0].header['CFP_OUT'] = '2026-07-03T01:00:00'
        hdul.flush()

    # Canonical unchanged -> work copy kept -> its CFP_OUT survives (combine skips).
    f.materialize_work('f444w')
    with fits.open(wp) as hdul:
        assert 'CFP_OUT' in hdul[0].header

    # Canonical re-processed (newer mtime) -> work copy re-copied fresh, dropping
    # CFP_OUT so combine re-runs on it.
    wm = os.path.getmtime(wp)
    os.utime(canon, (wm + 10, wm + 10))
    f.materialize_work('f444w')
    with fits.open(wp) as hdul:
        assert 'CFP_OUT' not in hdul[0].header


def _stamp_algn(path, value):
    with fits.open(path, mode='update') as hdul:
        hdul[0].header['CFP_ALGN'] = value
        hdul.flush()


def test_materialize_quarantines_not_aligned(tmp_path):
    # An align-enabled combine only admits exposures with a completed alignment.
    # BOTH failure modes are dropped (they'd drizzle with a raw WCS): a
    # CFP_ALGN=NOT_ALIGNED reject, and an exposure with no CFP_ALGN stamp at all
    # (align never solved it). Only the dof=... solution survives.
    f = _make_field(tmp_path)
    d = f.filter_dir('f444w')
    solved = os.path.join(d, f'{_ROOT}.fits')
    rej_root = 'jw01727028001_04101_00004_nrcalong'
    unstamped_root = 'jw01727028001_04101_00005_nrcalong'
    rejected = os.path.join(d, f'{rej_root}.fits')
    unstamped = os.path.join(d, f'{unstamped_root}.fits')
    _write_canonical(solved, stamps=())
    _write_canonical(rejected, stamps=())
    _write_canonical(unstamped, stamps=())            # no CFP_ALGN written
    _stamp_algn(solved, 'dof=shared res=0.01 n=30')
    _stamp_algn(rejected, 'NOT_ALIGNED')

    # Default: no quarantine -> all three exposures materialized.
    both = f.materialize_work('f444w', overwrite=True)
    assert len(both) == 3

    # With the quarantine, only the solved exposure survives; the rejected and
    # unstamped ones are dropped AND their stale work copies removed, so the
    # ensemble glob can't see them.
    kept = f.materialize_work('f444w', overwrite=True, exclude_not_aligned=True)
    assert len(kept) == 1
    assert os.path.basename(kept[0]) == f'{_ROOT}.fits'
    work_dir = f.filter_dir('f444w', work=True)
    assert not os.path.exists(os.path.join(work_dir, f'{rej_root}.fits'))
    assert not os.path.exists(os.path.join(work_dir, f'{unstamped_root}.fits'))
    assert os.path.exists(os.path.join(work_dir, f'{_ROOT}.fits'))
