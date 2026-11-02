"""The master-background spectrum (``mbkg_extraction``): the frames, the single-frame fit
at a fixed shape, the self-subtraction share, the ``MBKGPREF`` / ``MBKGWHY`` rule and the
``_spec.fits`` extensions (``stage3.opt_ext_single_source``)."""

import os

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.metadata.science_hash import science_hash
from campfire_pipeline.nirspec import mbkg_extraction as MX
from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import stage3 as S3

from test_nirspec_profile_fit import NX, NY, TRUTH, _exposures, _spectrum, _truth_result


# ---------------------------------------------------------------------------
# the fit on single frames
# ---------------------------------------------------------------------------
def _single_frames(params, flux_scale, noise, seed=2, src_y=0.1):
    """The three exposures of ``_exposures`` as single frames (nothing subtracted), each
    the source's own light plus noise, and the truth."""
    exps = _exposures(src_y=src_y)
    for e in exps:
        e.partners = []
    R = _truth_result(exps, params, [0.0, 0.0, 0.0], flux_scale)
    rng = np.random.default_rng(seed)
    for k, e in enumerate(exps):
        e.diff = PF.model_frame(R, k, applied_pathloss=False) + noise * rng.normal(size=(NY, NX))
        e.var = np.full((NY, NX), noise ** 2)
    return exps, R


def test_single_frames_at_a_fixed_shape_recover_the_spectrum():
    exps, truth = _single_frames(TRUTH, flux_scale=200.0, noise=1.0)
    M = PF.ProfileModel(exps, chromatic=True)
    assert np.all(M.dof_weight() == 1.0)                      # one independent value per pixel
    np.testing.assert_array_equal(M.diff_variance(0), exps[0].var)
    shape = dict(centre_offset=TRUTH['centre'] - M.c_prior, drift=TRUTH['drift'], width=TRUTH['width'],
                 width_change=TRUTH['width_change'])
    R = PF.fit_fixed(model=M, shape=shape)
    good = np.isfinite(R.flux) & (R.lam > 1.0) & (R.lam < 4.8)
    assert abs(np.median(R.flux[good] / (200.0 * _spectrum(R.lam[good]))) - 1) < 0.01
    pull = (R.flux[good] - 200.0 * _spectrum(R.lam[good])) / R.flux_err[good]
    assert 0.7 < np.std(pull) < 1.3
    assert R.params['centre'] == pytest.approx(TRUTH['centre'])


def test_self_subtraction_is_small_for_a_compact_source_and_large_for_a_wide_one():
    exps = _exposures(src_y=0.0)
    compact = _truth_result(exps, dict(centre=0.0, drift=0.0, width=PF.W_FLOOR, width_change=0.0), [0, 0, 0], 1.0)
    wide = _truth_result(exps, dict(centre=0.0, drift=0.0, width=1.2, width_change=0.0), [0, 0, 0], 1.0)
    assert MX.self_subtraction(compact, exps) < 0.02
    assert MX.self_subtraction(wide, exps) > 0.10


# ---------------------------------------------------------------------------
# MBKGPREF / MBKGWHY, NEXP / NEXPBKG
# ---------------------------------------------------------------------------
def test_exposure_counts():
    assert MX.exposure_counts(dict(filename=['a', 'b', 'c'], bkg_nods=['2,3', '3', '1,2'])) == (3, 1)
    assert MX.exposure_counts(dict(filename=['a'], bkg_nods=['excluded'])) == (1, 0)
    assert MX.exposure_counts(dict(filename=['a', 'b'], bkg_nods=['N/A', 'N/A'])) == (2, None)


def test_preferred_takes_the_first_reason_that_applies():
    clear = dict(filename=['a', 'b'], bkg_nods=['2', '1'], bkg_why=['', 'neighbor 9 at 0.030 of background'])
    flagged = dict(clear, bkg_why=['every background exposure: neighbor 9 at 0.050 of background', ''])
    assert MX.preferred(clear, 0.02, 0.10) == (False, '')
    assert MX.preferred(clear, 0.15, 0.10) == (True, 'extended')
    assert MX.preferred(flagged, 0.15, 0.10) == (True, 'no_clear_background')
    assert MX.preferred(dict(filename=['a'], bkg_nods=['excluded'], bkg_why=['']), 0.0, 0.1) == (True, 'single_exposure')
    assert MX.preferred(clear, None, 0.10) == (False, '')


# ---------------------------------------------------------------------------
# the frames
# ---------------------------------------------------------------------------
def test_write_frame_subtracts_the_master_background(tmp_path, monkeypatch):
    from jwst.datamodels import MultiSlitModel, SlitModel
    from campfire_pipeline.nirspec import canonical as C

    ny, nx = 6, 8
    m = MultiSlitModel()
    m.slits.append(SlitModel(data=np.full((ny, nx), 99.0, np.float32), err=np.ones((ny, nx), np.float32),
                             dq=np.zeros((ny, nx), np.uint32), var_poisson=np.full((ny, nx), 9.0, np.float32),
                             var_rnoise=np.full((ny, nx), 1.0, np.float32),
                             var_flat=np.zeros((ny, nx), np.float32)))
    canonical = str(tmp_path / 'jw07076001001_03101_00001_nrs1_7.fits')
    m.save(canonical)
    pre = {'SCI': np.full((ny, nx), 5.0, np.float32), 'VAR_POISSON': np.full((ny, nx), 2.0, np.float32),
           'VAR_RNOISE': np.full((ny, nx), 1.0, np.float32), 'DQ': np.zeros((ny, nx), np.uint32)}
    C.append_extras(canonical, extra_hdus=C.make_prefixed_hdus(pre, C.PRE_BKGSUB_PREFIX))
    sky = np.full((ny, nx), 3.0)
    sky[0, 0] = np.nan                                     # a pixel the master background does not cover
    monkeypatch.setattr('campfire_pipeline.nirspec.stage2.master_background_on',
                        lambda path: (sky, np.full((ny, nx), 0.5)))
    out = MX.write_frame(canonical, str(tmp_path))
    assert out == MX.copy_path(str(tmp_path), canonical) and out.endswith('_nrs1_7_mbkgsub.fits')
    with MultiSlitModel(out) as c:
        s = c.slits[0]
        assert c.meta.filename == os.path.basename(out)   # what the crf drizzle matches on
        assert str(c.meta.cal_step.bkg_subtract) == 'COMPLETE'
        np.testing.assert_allclose(s.data[1:, :], 2.0)    # the calibrated frame, not the nod difference
        assert np.isnan(s.data[0, 0]) and (s.dq[0, 0] & PF.DO_NOT_USE)
        np.testing.assert_allclose(s.var_poisson[1:, :], 2.5)
        np.testing.assert_allclose(s.err[1:, :], np.sqrt(3.5))
    kept = C.read_slit_arrays(out, prefix=C.PRE_BKGSUB_PREFIX)
    np.testing.assert_array_equal(kept['SCI'], pre['SCI'])
    assert fits.getheader(out)['CFMBKGSB']


def test_write_frame_without_a_master_background(tmp_path, monkeypatch):
    monkeypatch.setattr('campfire_pipeline.nirspec.stage2.master_background_on', lambda path: None)
    with pytest.raises(MX.NoMasterBackground):
        MX.write_frame(str(tmp_path / 'jw07076001001_03101_00001_nrs1_7.fits'), str(tmp_path))


# ---------------------------------------------------------------------------
# the _spec.fits
# ---------------------------------------------------------------------------
NAMES = ['jw07076001001_03101_00001_nrs1_7.fits', 'jw07076001001_03101_00002_nrs1_7.fits']


def _inputs(tmp_path, product, amp, where, contam=0.1, wave=None, bkg_why=('', ''), x1d_shift=0.0):
    """A minimal s2d / x1d pair (a source of ``amp`` uJy with a Gaussian profile) and its
    _pf.fits under ``where`` (the workspace or its ``mbkg`` subdirectory)."""
    ny, nx = 30, 60
    rows = np.arange(ny)[:, None]
    prof = np.exp(-0.5 * ((rows - 15) / 1.2) ** 2) * np.ones((1, nx))
    prof = prof / prof[:, :1].sum()
    wave = np.linspace(1.0, 5.0, nx) if wave is None else wave
    os.makedirs(where, exist_ok=True)
    exp = fits.table_to_hdu(Table(dict(filename=NAMES, bkg_nods=['2', '1'], bkg_why=list(bkg_why))))
    exp.name = 'EXPOSURES'
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(amp * prof / 1e12, name='SCI'),
                  fits.ImageHDU(np.full((ny, nx), 0.01 / 1e12), name='ERR'),
                  fits.ImageHDU(np.tile(wave, (ny, 1)), name='WAVELENGTH'),
                  fits.ImageHDU(np.ones((ny, nx)), name='WHT'), exp]).writeto(os.path.join(where, f'{product}_s2d.fits'))
    x1 = fits.BinTableHDU.from_columns([fits.Column(name='WAVELENGTH', format='D', array=wave + x1d_shift)],
                                       name='EXTRACT1D')
    x1.header['EXTRYSTR'] = 13
    x1.header['EXTRYSTP'] = 18
    fits.HDUList([fits.PrimaryHDU(), x1]).writeto(os.path.join(where, f'{product}_x1d.fits'))
    h = fits.Header()
    h['PFNCONT'] = 1
    h['PFSELFSB'] = 0.02
    fits.HDUList([fits.PrimaryHDU(header=h), fits.ImageHDU(prof / 1e12, name='PROF2D'),
                  fits.ImageHDU(contam * amp * prof / 1e12, name='CONTAM2D'),
                  fits.ImageHDU(0.0 * prof, name='CONTERR2D')]).writeto(os.path.join(where, f'{product}_pf.fits'))


def _master_backgrounds(tmp_path, sources=('pool', 'exposure')):
    from campfire_pipeline.nirspec.profile_contam import exposure_key
    for name, src in zip(NAMES, sources):
        h = fits.Header()
        h['MBKGSRC'] = src
        h['CFMBKGV'] = '1.5'
        fits.PrimaryHDU(header=h).writeto(tmp_path / f'{exposure_key(name)}_mbkg.fits')


def test_spec_fits_carries_the_master_background_spectrum(tmp_path):
    _inputs(tmp_path, 'p', 10.0, str(tmp_path))
    _inputs(tmp_path, 'p_mbkg', 8.0, MX.subdir(str(tmp_path)))
    _master_backgrounds(tmp_path)
    S3.opt_ext_single_source('p', str(tmp_path), profile_fit=True, mbkg_spectrum=True)
    path = tmp_path / 'p_spec.fits'
    with fits.open(path) as h:
        ext = [x.name for x in h[1:]]
        assert ext[-4:] == list(S3.MBKG_EXTENSIONS)
        assert h['SPEC1D_MBKG'].columns.names == h['SPEC1D'].columns.names
        np.testing.assert_array_equal(h['SPEC1D_MBKG'].data['wave'], h['SPEC1D'].data['wave'])
        assert h['SCI_MBKG'].data.shape == h['SCI'].data.shape == h['ERR_MBKG'].data.shape
        assert h['PROF1D_MBKG'].columns.names == h['PROF1D'].columns.names
        np.testing.assert_allclose(h['SPEC1D_MBKG'].data['fnu'][2:-2], 8.0, rtol=1e-5)
        np.testing.assert_allclose(h['SPEC1D'].data['fnu'][2:-2], 10.0, rtol=1e-5)
        hd, hm = h[0].header, h['SPEC1D_MBKG'].header
        assert (hd['NEXP'], hd['NEXPBKG']) == (2, 1)
        assert (hd['MBKGPREF'], hd['MBKGWHY'], hd['MBKGNEXP']) == (False, '', 1)
        assert (hm['EXTRACT'], hm['MBKGVER'], hm['MBKGSRC']) == ('profile', '1.5', 'combined,exposure')
    assert not os.listdir(MX.subdir(str(tmp_path)))           # intermediates removed
    # the master-background spectrum is not part of the science identity (line fits stay fresh)
    with fits.open(path) as h:
        keep = fits.HDUList([x.copy() for x in h if x.name not in S3.MBKG_EXTENSIONS])
        for k in ('MBKGPREF', 'MBKGWHY', 'MBKGNEXP'):
            del keep[0].header[k]
        keep.writeto(tmp_path / 'nodded_only.fits')
    assert science_hash(path) == science_hash(tmp_path / 'nodded_only.fits')


def test_flagged_exposure_prefers_the_master_background(tmp_path):
    _inputs(tmp_path, 'p', 10.0, str(tmp_path), bkg_why=('every background exposure: neighbor 9 at 0.050 of '
                                                          'background', ''))
    _inputs(tmp_path, 'p_mbkg', 8.0, MX.subdir(str(tmp_path)))
    S3.opt_ext_single_source('p', str(tmp_path), profile_fit=True, mbkg_spectrum=True)
    hd = fits.getheader(tmp_path / 'p_spec.fits')
    assert (hd['MBKGPREF'], hd['MBKGWHY']) == (True, 'no_clear_background')


def test_a_different_grid_leaves_out_the_master_background_spectrum(tmp_path):
    _inputs(tmp_path, 'p', 10.0, str(tmp_path))
    _inputs(tmp_path, 'p_mbkg', 8.0, MX.subdir(str(tmp_path)), wave=np.linspace(1.0, 5.0, 60) + 1e-6)
    S3.opt_ext_single_source('p', str(tmp_path), profile_fit=True, mbkg_spectrum=True)
    with fits.open(tmp_path / 'p_spec.fits') as h:
        assert not any(x.name in S3.MBKG_EXTENSIONS for x in h)
        assert 'MBKGPREF' not in h[0].header and h[0].header['NEXP'] == 2


def test_rounding_of_the_one_dimensional_wave_is_accepted(tmp_path):
    """jwst's 1-D wavelengths of one 2-D grid differ by ~1e-15 um between two runs (p1):
    the master-background spectrum is kept and written on the nodded wave."""
    _inputs(tmp_path, 'p', 10.0, str(tmp_path))
    _inputs(tmp_path, 'p_mbkg', 8.0, MX.subdir(str(tmp_path)), x1d_shift=2e-15)
    S3.opt_ext_single_source('p', str(tmp_path), profile_fit=True, mbkg_spectrum=True)
    with fits.open(tmp_path / 'p_spec.fits') as h:
        np.testing.assert_array_equal(h['SPEC1D_MBKG'].data['wave'], h['SPEC1D'].data['wave'])


def test_no_master_background_spectrum_when_its_spec3_did_not_run(tmp_path):
    _inputs(tmp_path, 'p', 10.0, str(tmp_path))
    S3.opt_ext_single_source('p', str(tmp_path), profile_fit=True, mbkg_spectrum=True)
    with fits.open(tmp_path / 'p_spec.fits') as h:
        assert not any(x.name in S3.MBKG_EXTENSIONS for x in h)
