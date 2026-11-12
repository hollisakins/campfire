"""HST sky step: sky level, ACS amplifier offsets, variance scale."""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.hst.exposure import SKY_KEY, VARSCALE_KEY, open_exposure
from campfire_pipeline.hst.steps.sky import (
    AMP_KEYS, SKYSIG_KEY, sky_step, source_mask,
)

_NY, _NX, _T = 300, 400, 400.0
_SKY, _TRUE_SIGMA, _READ = 40.0, 9.0, 4.0          # electrons
_AMP_STEP = {1: (0.0, 0.6), 2: (-0.4, 0.0)}        # offset per amplifier half


def _acs_file(path):
    rng = np.random.default_rng(7)
    primary = fits.PrimaryHDU()
    primary.header.update({
        'INSTRUME': 'ACS', 'DETECTOR': 'WFC', 'ROOTNAME': 'jsky01aaq',
        'FILTER1': 'CLEAR1L', 'FILTER2': 'F814W', 'EXPTIME': _T,
        'CCDAMP': 'ABCD'})
    hdul = fits.HDUList([primary])
    yy, xx = np.mgrid[:_NY, :_NX]
    for extver in (1, 2):
        sci = _SKY + rng.normal(0.0, _TRUE_SIGMA, (_NY, _NX))
        left, right = _AMP_STEP[extver]
        sci[:, :_NX // 2] += left
        sci[:, _NX // 2:] += right
        for _ in range(12):                               # sources
            y0, x0 = rng.uniform(20, _NY - 20), rng.uniform(20, _NX - 20)
            sci += rng.uniform(200, 3000) * np.exp(
                -0.5 * ((yy - y0) ** 2 + (xx - x0) ** 2) / 2.0 ** 2)
        hits = (rng.integers(0, _NY, 150), rng.integers(0, _NX, 150))
        sci[hits] += rng.uniform(100, 2000, 150)          # cosmic rays
        # ERR as calacs writes it: Poisson of the counts plus read noise. It
        # knows nothing of the extra noise in this frame (true sigma is 9 e-).
        err = np.sqrt(np.clip(sci, 0, None) + _READ ** 2)
        dq = np.zeros((_NY, _NX), 'int16')
        dq[5, :] = 128                                    # a bad row
        s = 0.05 / 3600.0
        for name, data in (('SCI', sci.astype('float32')),
                           ('ERR', err.astype('float32')), ('DQ', dq)):
            hdu = fits.ImageHDU(data=data, name=name, ver=extver)
            if name == 'SCI':
                hdu.header.update({
                    'CTYPE1': 'RA---TAN-SIP', 'CTYPE2': 'DEC--TAN-SIP',
                    'CRPIX1': 200.0, 'CRPIX2': 150.0, 'CRVAL1': 40.0,
                    'CRVAL2': 26.0 + 0.004 * extver, 'CD1_1': -s, 'CD1_2': 0.0,
                    'CD2_1': 0.0, 'CD2_2': s, 'A_ORDER': 2, 'B_ORDER': 2,
                    'A_2_0': 0.0, 'B_0_2': 0.0, 'BUNIT': 'ELECTRONS',
                    'PHOTFLAM': 7.0e-20, 'PHOTPLAM': 8045.0, 'IDCSCALE': 0.05})
            hdul.append(hdu)
    hdul.writeto(path)


def test_source_mask_finds_sources_and_keeps_sky():
    rng = np.random.default_rng(3)
    sci = rng.normal(10.0, 1.0, (200, 200))
    sci[90:110, 90:110] += 50.0
    good = np.ones(sci.shape, bool)
    good[0, :] = False
    mask = source_mask(sci, good)
    assert mask[0].all()                       # bad pixels stay masked
    assert mask[90:110, 90:110].all()          # the source
    # ... grown by 3 px along the axes (the growth is diamond-shaped)
    assert mask[87, 100] and mask[112, 100] and mask[100, 87] and mask[100, 112]
    assert 0.93 < (~mask).mean() < 0.99        # nearly all blank sky survives
    # the unmasked pixels are an unbiased sample of the sky
    assert np.median(sci[~mask]) == pytest.approx(10.0, abs=0.05)


def test_sky_step_measures_and_records(tmp_path):
    path = str(tmp_path / 'jsky01aaq_flc.fits')
    _acs_file(path)
    with fits.open(path) as hdul:
        before = {v: hdul['SCI', v].data.astype('float64') for v in (1, 2)}

    results = sky_step(path)
    assert set(results) == {'jsky01aaq_sci1', 'jsky01aaq_sci2'}

    with fits.open(path) as hdul:
        assert 'CFP_SKY' in hdul[0].header
        for v in (1, 2):
            h = hdul['SCI', v].header
            left, right = _AMP_STEP[v]
            # the step between the two amplifier halves is recovered and removed
            got = h[AMP_KEYS[1]] - h[AMP_KEYS[0]]
            assert got == pytest.approx(right - left, abs=0.15)
            removed = before[v] - hdul['SCI', v].data
            assert np.median(removed[:, :_NX // 2]) == pytest.approx(
                h[AMP_KEYS[0]], abs=1e-4)
            assert np.median(removed[:, _NX // 2:]) == pytest.approx(
                h[AMP_KEYS[1]], abs=1e-4)
            # sky recorded, not subtracted
            assert h[SKY_KEY] == pytest.approx(_SKY + 0.5 * (left + right), abs=0.3)
            assert np.median(hdul['SCI', v].data) == pytest.approx(h[SKY_KEY], abs=0.5)
            # measured scatter is the true one, not what ERR claims
            assert h[SKYSIG_KEY] == pytest.approx(_TRUE_SIGMA, rel=0.05)
            modelled = _SKY + _READ ** 2
            assert h[VARSCALE_KEY] == pytest.approx(_TRUE_SIGMA ** 2 / modelled,
                                                   rel=0.08)

    # the adapter now reports the measured noise as its background variance
    for chip in open_exposure(path):
        a = chip.load()
        f = chip.sb_per_rate
        assert chip.provenance()['sky_src'] == 'header'
        assert np.sqrt(np.median(a.var_rnoise)) == pytest.approx(
            _TRUE_SIGMA / _T * f, rel=0.05)
        assert abs(np.median(a.sci[a.good])) < 0.05 * _TRUE_SIGMA / _T * f


def test_sky_step_is_idempotent(tmp_path):
    path = str(tmp_path / 'jsky01aaq_flc.fits')
    _acs_file(path)
    sky_step(path)
    with fits.open(path) as hdul:
        first = hdul['SCI', 1].data.copy()
        offsets = [hdul['SCI', 1].header[k] for k in AMP_KEYS]

    assert sky_step(path) is None                  # stamped: nothing happens
    with fits.open(path) as hdul:
        assert np.array_equal(hdul['SCI', 1].data, first)

    sky_step(path, overwrite=True)                 # re-measure on corrected data
    with fits.open(path) as hdul:
        # nothing more to remove: pixels and recorded totals barely move
        assert np.max(np.abs(hdul['SCI', 1].data - first)) < 0.1
        for k, old in zip(AMP_KEYS, offsets):
            assert hdul['SCI', 1].header[k] == pytest.approx(old, abs=0.1)
    assert cfp.has_step(path, 'CFP_SKY', keyset=cfp.HST)
