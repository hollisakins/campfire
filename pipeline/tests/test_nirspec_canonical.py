"""Tests for the NIRSpec canonical spectrum-exposure primitives (issue #212).

Uses synthetic single-slit FITS (mimicking a MultiSlitModel: PRIMARY + per-slit
SCI/ERR/DQ/VAR_* with EXTVER=1 + an ASDF blob) and a FakeModel whose .save()
drops non-schema HDUs — the exact behaviour the real DataModel.save() has and
that save_canonical must work around. The PR-3 gate validated the real-jwst end
of this (Spec3Pipeline ignores the appended HDUs); these tests lock the FITS
plumbing: revert round-trip, preserve-across-save, replace-or-append.
"""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nirspec import canonical as C


def _slit_fits(path, sci=None, extras=None, **cards):
    """Write a synthetic single-slit canonical-like FITS file."""
    rng = np.arange(12, dtype='f4').reshape(3, 4)
    prim = fits.PrimaryHDU()
    prim.header['INSTRUME'] = 'NIRSPEC'
    for k, v in cards.items():
        prim.header[k] = v
    hdus = [prim]
    arrays = {
        'SCI': rng if sci is None else sci,
        'ERR': rng * 0.1,
        'DQ': np.zeros((3, 4), dtype='i4'),
        'VAR_RNOISE': rng * 0.01,
        'VAR_POISSON': rng * 0.02,
        'VAR_FLAT': rng * 0.03,
    }
    for name, data in arrays.items():
        h = fits.ImageHDU(np.asarray(data), name=name)
        h.ver = 1
        hdus.append(h)
    # ASDF blob (datamodels write this last); restore/save must tolerate it.
    asdf = fits.BinTableHDU.from_columns(
        [fits.Column(name='ASDF_METADATA', format='B', array=np.zeros(4, 'u1'))])
    asdf.name = 'ASDF'
    hdus.append(asdf)
    for h in (extras or []):
        hdus.append(h)
    fits.HDUList(hdus).writeto(str(path), overwrite=True)
    return str(path)


class FakeModel:
    """Stand-in for a jwst MultiSlitModel: .save() writes only the schema HDUs
    (PRIMARY + slit exts + ASDF), dropping any custom non-schema HDUs — exactly
    like DataModel.save()."""
    def __init__(self, src_path):
        self._src = src_path

    def save(self, path):
        with fits.open(self._src, memmap=False) as hdul:
            keep = fits.HDUList([h.copy() for h in hdul if not C.is_custom_hdu(h)])
        keep.writeto(path, overwrite=True)


# --- helpers ---------------------------------------------------------------

def test_read_slit_arrays(tmp_path):
    p = _slit_fits(tmp_path / 'c.fits')
    arrs = C.read_slit_arrays(p)
    assert set(arrs) == set(C.SLIT_ARRAY_EXTS)            # VAR_FLAT excluded
    assert arrs['SCI'].shape == (3, 4)
    assert arrs['SCI'][0, 0] == 0 and arrs['SCI'][-1, -1] == 11


def test_make_prefixed_hdus():
    arrays = {'SCI': np.ones((2, 2)), 'DQ': np.zeros((2, 2), 'i4')}
    hdus = C.make_prefixed_hdus(arrays, C.PRE_BKGSUB_PREFIX)
    names = sorted(h.name for h in hdus)
    assert names == ['PRE_BKGSUB_DQ', 'PRE_BKGSUB_SCI']
    assert all(h.ver == 1 for h in hdus)


def test_is_custom_hdu(tmp_path):
    p = _slit_fits(tmp_path / 'c.fits',
                   extras=[fits.ImageHDU(np.zeros((2, 2)), name='S2D_SCI')])
    with fits.open(p) as hdul:
        customs = {h.name for h in hdul if C.is_custom_hdu(h)}
    assert customs == {'S2D_SCI'}                         # not SCI/ERR/ASDF


# --- append_extras ---------------------------------------------------------

def test_append_extras_adds_hdu_and_card(tmp_path):
    p = _slit_fits(tmp_path / 'c.fits')
    C.append_extras(p, extra_hdus=[fits.ImageHDU(np.full((3, 4), 7.0), name='S2D_SCI')],
                    header_updates={'CFP_S2D': ('t', 'campfire: s2d')})
    with fits.open(p) as hdul:
        assert hdul[0].header['CFP_S2D'] == 't'
        assert np.all(hdul['S2D_SCI'].data == 7.0)
        assert np.array_equal(hdul['SCI'].data, np.arange(12).reshape(3, 4))  # live unchanged


def test_append_extras_replaces_same_name(tmp_path):
    p = _slit_fits(tmp_path / 'c.fits',
                   extras=[fits.ImageHDU(np.ones((3, 4)), name='S2D_SCI')])
    C.append_extras(p, extra_hdus=[fits.ImageHDU(np.full((3, 4), 9.0), name='S2D_SCI')])
    with fits.open(p) as hdul:
        s2d = [h for h in hdul if (h.name or '') == 'S2D_SCI']
        assert len(s2d) == 1 and np.all(s2d[0].data == 9.0)   # replaced, not duplicated


# --- save_canonical: preserve customs across the model.save() drop ----------

def test_save_canonical_preserves_existing_customs(tmp_path):
    # Existing canonical carries an S2D_SCI view from stage2a's resample.
    p = _slit_fits(tmp_path / 'c.fits', extras=[
        fits.ImageHDU(np.full((3, 4), 5.0), name='S2D_SCI')])
    model = FakeModel(p)  # its .save() would drop S2D_SCI
    # stage2b re-saves the (bkgsub) model and attaches PRE_BKGSUB_* + S2D_BKGSUB_*.
    pre = C.make_prefixed_hdus({'SCI': np.full((3, 4), 1.0)}, C.PRE_BKGSUB_PREFIX)
    s2db = [fits.ImageHDU(np.full((3, 4), 2.0), name='S2D_BKGSUB_SCI')]
    C.save_canonical(model, p, extra_hdus=pre + s2db,
                     header_updates={'CFP_BKG': ('t', 'campfire: bkgsub')})
    with fits.open(p) as hdul:
        names = [h.name for h in hdul]
        assert 'S2D_SCI' in names          # preserved from stage2a
        assert 'PRE_BKGSUB_SCI' in names   # new revert array
        assert 'S2D_BKGSUB_SCI' in names   # new bkgsub view
        assert hdul[0].header['CFP_BKG'] == 't'
        assert np.all(hdul['S2D_SCI'].data == 5.0)
        assert np.all(hdul['PRE_BKGSUB_SCI'].data == 1.0)


def test_save_canonical_extra_wins_over_preserved(tmp_path):
    # Existing S2D_SCI must be overwritten when stage2b passes a fresh one.
    p = _slit_fits(tmp_path / 'c.fits', extras=[
        fits.ImageHDU(np.full((3, 4), 5.0), name='S2D_SCI')])
    model = FakeModel(p)
    C.save_canonical(model, p, extra_hdus=[fits.ImageHDU(np.full((3, 4), 8.0), name='S2D_SCI')])
    with fits.open(p) as hdul:
        s2d = [h for h in hdul if (h.name or '') == 'S2D_SCI']
        assert len(s2d) == 1 and np.all(s2d[0].data == 8.0)


def test_save_canonical_drops_customs_without_preservation_flag(tmp_path):
    p = _slit_fits(tmp_path / 'c.fits', extras=[
        fits.ImageHDU(np.full((3, 4), 5.0), name='S2D_SCI')])
    C.save_canonical(FakeModel(p), p, preserve_existing_custom=False)
    with fits.open(p) as hdul:
        assert 'S2D_SCI' not in [h.name for h in hdul]   # dropped (model.save behaviour)


# --- restore_pre_bkgsub round-trip -----------------------------------------

def test_restore_pre_bkgsub_round_trip(tmp_path):
    cal_sci = np.arange(12, dtype='f4').reshape(3, 4)
    bkgsub_sci = cal_sci - 100.0
    # Canonical in the bkgsub'd state: live SCI = bkgsub, revert arrays = cal,
    # plus a stale bkgsub view + state cards.
    pre_hdus = C.make_prefixed_hdus(
        {'SCI': cal_sci, 'ERR': cal_sci * 0.1, 'DQ': np.zeros((3, 4), 'i4'),
         'VAR_RNOISE': cal_sci * 0.01, 'VAR_POISSON': cal_sci * 0.02},
        C.PRE_BKGSUB_PREFIX)
    p = _slit_fits(tmp_path / 'c.fits', sci=bkgsub_sci,
                   extras=pre_hdus + [fits.ImageHDU(bkgsub_sci, name='S2D_BKGSUB_SCI'),
                                      fits.ImageHDU(cal_sci, name='S2D_SCI')],
                   CFP_BKG='t', CFP_S2D='t', CFP_CAL='t')
    assert C.has_pre_bkgsub(p)
    C.restore_pre_bkgsub(p)
    with fits.open(p) as hdul:
        assert np.array_equal(hdul['SCI'].data, cal_sci)      # live restored to cal
        names = [h.name for h in hdul]
        assert not any(n.startswith('PRE_BKGSUB') for n in names)   # revert dropped
        assert 'S2D_BKGSUB_SCI' not in names                  # stale bkgsub view dropped
        assert 'S2D_SCI' in names                             # un-bkgsub view kept
        assert 'CFP_BKG' not in hdul[0].header and 'CFP_S2D' not in hdul[0].header
        assert 'CFP_CAL' in hdul[0].header                    # cal state card kept


def test_restore_pre_bkgsub_raises_without_revert(tmp_path):
    p = _slit_fits(tmp_path / 'c.fits')
    assert not C.has_pre_bkgsub(p)
    with pytest.raises(RuntimeError):
        C.restore_pre_bkgsub(p)
