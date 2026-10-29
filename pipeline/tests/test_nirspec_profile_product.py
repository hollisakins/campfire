"""The profile-fit product's fallbacks, failures and the 1-D combine's choice of the best
spectrum (extraction v2, ``profile_product.run_product``, ``stage3``)."""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import profile_product as PP
from campfire_pipeline.nirspec import stage3 as S3


def _run(tmp_path, sid=7):
    return PP.run_product('obs_prism_7', str(tmp_path), [[]], [None], [], sid, {}, True, cleanup_crfs=False)


def _with_crf(tmp_path, sid=7):
    (tmp_path / f'obs_prism_7_s{sid:09d}_crf.fits').write_bytes(b'')


def test_no_crf_falls_back(tmp_path):
    r = _run(tmp_path)
    assert r['fallback'] == PP.FALLBACK_NO_CRF and r['source_id'] == 7 and r['scan'] is None
    h = fits.getheader(tmp_path / 'obs_prism_7_pf.fits')
    assert h['EXTRACT'] == 'boxcar3' and h['EXTRWHY'] == 'no_crf'


@pytest.mark.parametrize('exc', [PF.NotBackgroundSubtracted, PF.NoUsablePixels, PF.TooFewWavelengths])
def test_named_conditions_fall_back(tmp_path, monkeypatch, exc):
    _with_crf(tmp_path)

    def boom(*a, **k):
        raise exc('profile fit: test')
    monkeypatch.setattr(PP, 'analyse_product', boom)
    r = _run(tmp_path)
    assert r['fallback'] == exc.reason
    assert exc.reason in S3.EXTRACT_REASONS
    assert fits.getheader(tmp_path / 'obs_prism_7_pf.fits')['EXTRWHY'] == exc.reason


@pytest.mark.parametrize('exc', [RuntimeError, ValueError, KeyError])
def test_other_exceptions_propagate(tmp_path, monkeypatch, exc):
    """Anything that is not a named condition fails the stage, and no _pf.fits is left
    that the extraction could mistake for a result."""
    _with_crf(tmp_path)

    def boom(*a, **k):
        raise exc('unexpected')
    monkeypatch.setattr(PP, 'analyse_product', boom)
    with pytest.raises(exc):
        _run(tmp_path)
    assert not (tmp_path / 'obs_prism_7_pf.fits').exists()


def test_fallback_count_by_reason():
    res = [dict(fallback=None), dict(fallback='no_crf'), dict(fallback='no_crf'), dict(fallback='no_usable_pixels')]
    assert S3.log_fallbacks(res) == {'no_crf': 2, 'no_usable_pixels': 1}


def _groups(contam_a, contam_b, extract=('profile', 'profile'), state=('predicted', 'predicted')):
    wave = np.linspace(1.0, 2.0, 40)
    prof = dict(wavelengths=[wave, wave], fluxes=[np.full(40, 10.0), np.full(40, 10.0)],
                errors=[np.full(40, 0.1), np.full(40, 0.1)])
    groups = [dict(contam=c, contam_err=0.1 * np.nan_to_num(c), extract=x, reason='r', state=s)
              for c, x, s in zip((contam_a, contam_b), extract, state)]
    return wave, prof, groups


def test_combine_chooses_the_best_spectrum_again():
    """Per-group fnu_contam combined with the profile's exposure-time weights; a group
    with nothing predicted adds zero light; PFCONSIG recomputed on the combined
    spectra decides the subtraction."""
    a = np.full(40, 2.0)
    b = np.full(40, np.nan)                                  # nothing predicted in the second group
    wave, prof, groups = _groups(a, b)
    out = S3._combined_profile_fit(prof, groups, [1.0, 3.0], wave, np.full(40, 10.0), np.full(40, 0.05))
    c = out['columns']['fnu_contam'][0]
    np.testing.assert_allclose(c[2:-2], 0.25 * 2.0, rtol=1e-6)      # exposure-time weights 1:3
    np.testing.assert_allclose(out['columns']['fnu_contam_err'][0][2:-2], 0.25 * 0.2, rtol=1e-6)   # linear
    assert out['choice'] == 'profile_decontam' and out['header']['PFCONSIG'] > 3
    assert out['header']['EXTRACT'] == 'profile_decontam' and out['header']['CONTSTAT'] == 'predicted'
    # a faint prediction: not subtracted
    wave, prof, groups = _groups(np.full(40, 1e-4), np.full(40, 1e-4))
    out = S3._combined_profile_fit(prof, groups, [1.0, 1.0], wave, np.full(40, 10.0), np.full(40, 0.05))
    assert (out['choice'], out['reason']) == ('profile', 'contam_insignificant')


def test_combine_group_fallback_gives_the_boxcar():
    wave, prof, groups = _groups(np.full(40, 2.0), np.full(40, np.nan), extract=('profile', 'boxcar3'),
                                 state=('predicted', 'off'))
    out = S3._combined_profile_fit(prof, groups, [1.0, 1.0], wave, np.full(40, 10.0), np.full(40, 0.05))
    assert (out['choice'], out['reason']) == ('boxcar3', 'group_fallback')


def _stage3_inputs(tmp_path, product, contam_level=0.1):
    """A minimal s2d / x1d pair (a 10 uJy source with a Gaussian profile) and its _pf.fits."""
    from astropy.table import Table
    ny, nx = 30, 60
    rows = np.arange(ny)[:, None]
    prof = np.exp(-0.5 * ((rows - 15) / 1.2) ** 2) * np.ones((1, nx))
    prof = prof / prof[:, :1].sum()
    wave = np.linspace(1.0, 5.0, nx)
    exp = fits.table_to_hdu(Table(dict(filename=['a.fits'])))
    exp.name = 'EXPOSURES'
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(10.0 * prof / 1e12, name='SCI'),
                  fits.ImageHDU(np.full((ny, nx), 0.01 / 1e12), name='ERR'),
                  fits.ImageHDU(np.tile(wave, (ny, 1)), name='WAVELENGTH'),
                  fits.ImageHDU(np.ones((ny, nx)), name='WHT'), exp]).writeto(tmp_path / f'{product}_s2d.fits')
    x1 = fits.BinTableHDU.from_columns([fits.Column(name='WAVELENGTH', format='D', array=wave)], name='EXTRACT1D')
    x1.header['EXTRYSTR'] = 13
    x1.header['EXTRYSTP'] = 18
    fits.HDUList([fits.PrimaryHDU(), x1]).writeto(tmp_path / f'{product}_x1d.fits')
    h = fits.Header()
    h['PFNCONT'] = 1
    fits.HDUList([fits.PrimaryHDU(header=h), fits.ImageHDU(prof / 1e12, name='PROF2D'),
                  fits.ImageHDU(contam_level * 10.0 * prof / 1e12, name='CONTAM2D'),
                  fits.ImageHDU(0.0 * prof, name='CONTERR2D')]).writeto(tmp_path / f'{product}_pf.fits')
    return wave




def _cols(sfx):
    return [f'fnu{sfx}', f'fnu{sfx}_err', f'flam{sfx}', f'flam{sfx}_err']


def test_spec_fits_columns_and_cards(tmp_path):
    """The SPEC1D column set and the extraction cards of a profile-fit product; the
    _pf.fits is deleted once the _spec.fits is written."""
    _stage3_inputs(tmp_path, 'p')
    S3.opt_ext_single_source('p', str(tmp_path), profile_fit=True)
    with fits.open(tmp_path / 'p_spec.fits') as h:
        names = h['SPEC1D'].columns.names
        d = h['SPEC1D'].data
        hd = h[0].header
        ext = [x.name for x in h[1:]]
    assert names == (['wave'] + _cols('') + _cols('_opt') + _cols('_3px') + _cols('_4px') + _cols('_5px')
                     + _cols('_prof') + _cols('_contam'))
    assert (hd['EXTRACT'], hd['EXTRWHY'], hd['CONTSTAT']) == ('profile_decontam', 'contam_significant', 'predicted')
    np.testing.assert_allclose(d['fnu'][2:-2], 9.0, rtol=1e-5)
    np.testing.assert_allclose(d['fnu_prof'][2:-2], 10.0, rtol=1e-5)
    np.testing.assert_array_equal(d['fnu_err'], d['fnu_prof_err'])
    assert ext == ['SPEC1D', 'SCI', 'ERR', 'WAVELENGTH', 'WHT', 'PROF2D', 'CONTAM2D', 'PROF1D', 'EXPOSURES']
    assert not any(n.startswith(('fnu_pf', 'flam_pf', 'pf_')) for n in names)
    assert not (tmp_path / 'p_pf.fits').exists()


def test_spec_fits_fallback_and_profile_fit_off(tmp_path):
    _stage3_inputs(tmp_path, 'q')
    PP.write_fallback(str(tmp_path / 'q_pf.fits'), PP.FALLBACK_NO_CRF)
    S3.opt_ext_single_source('q', str(tmp_path), profile_fit=True)
    with fits.open(tmp_path / 'q_spec.fits') as h:
        d, hd = h['SPEC1D'].data, h[0].header
        assert 'fnu_prof' in h['SPEC1D'].columns.names
    assert (hd['EXTRACT'], hd['EXTRWHY']) == ('boxcar3', 'no_crf')
    np.testing.assert_array_equal(d['fnu'], d['fnu_3px'])
    assert not (tmp_path / 'q_pf.fits').exists()          # the reason is EXTRWHY in the _spec.fits
    np.testing.assert_array_equal(d['fnu_err'], d['fnu_3px_err'])
    S3.opt_ext_single_source('q', str(tmp_path), profile_fit=False)
    with fits.open(tmp_path / 'q_spec.fits') as h:
        d, hd = h['SPEC1D'].data, h[0].header
        assert 'fnu_prof' not in h['SPEC1D'].columns.names
    assert (hd['EXTRACT'], hd['EXTRWHY']) == ('optimal', 'profile_fit_off')
    np.testing.assert_array_equal(d['fnu'], d['fnu_opt'])
