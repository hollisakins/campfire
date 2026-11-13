"""HST field-level outlier step: flags into the CFOUT extension, honored by the
adapter, repeatable, and refused for exposures that are not aligned.

Synthetic WFC3/IR-shaped files: flat background plus noise, three dithers of
the same sky, a hit a few pixels across in the first.
"""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.hst.exposure import OUTLIER_EXT, SKY_KEY, open_exposure
from campfire_pipeline.hst.steps.outlier import outlier_step

_NY = _NX = 80
_T = 1000.0
_SKY = 1.0                       # e-/s
_SIGMA = np.sqrt(_SKY / _T + 0.001)


def _ir_file(path, root, crpix, rng, aligned='fit=rigid pool=visit'):
    primary = fits.PrimaryHDU()
    primary.header.update({'INSTRUME': 'WFC3', 'DETECTOR': 'IR', 'ROOTNAME': root,
                           'FILTER': 'F160W', 'EXPTIME': _T})
    if aligned is not None:
        for key, val in cfp.format(keyset=cfp.HST, CFP_ALGN=aligned).items():
            primary.header[key] = val
    sci = (_SKY + rng.normal(0, _SIGMA, (_NY, _NX))).astype('float32')
    err = np.full((_NY, _NX), _SIGMA, 'float32')
    s = 0.128 / 3600.0
    hdus = []
    for name, data in (('SCI', sci), ('ERR', err), ('DQ', np.zeros((_NY, _NX), 'int16'))):
        hdu = fits.ImageHDU(data=data, name=name, ver=1)
        if name == 'SCI':
            hdu.header.update({
                'CTYPE1': 'RA---TAN-SIP', 'CTYPE2': 'DEC--TAN-SIP',
                'CRPIX1': crpix[0], 'CRPIX2': crpix[1],
                'CRVAL1': 34.4, 'CRVAL2': -5.2,
                'CD1_1': -s, 'CD1_2': 0.0, 'CD2_1': 0.0, 'CD2_2': s,
                'A_ORDER': 2, 'B_ORDER': 2, 'A_2_0': 0.0, 'B_0_2': 0.0,
                'BUNIT': 'ELECTRONS/S', 'PHOTFNU': 1.5e-07, 'IDCSCALE': 0.128,
                'WCSNAME': 'IDC_test-CFALIGN', SKY_KEY: _SKY})
        hdus.append(hdu)
    hdus.append(fits.ImageHDU(data=np.full((_NY, _NX), _T, 'float32'), name='TIME', ver=1))
    fits.HDUList([primary] + hdus).writeto(path)
    return str(path)


def _visit(tmp_path, **kwargs):
    rng = np.random.default_rng(5)
    files = [_ir_file(tmp_path / f'itest01a{k}q_flt.fits', f'itest01a{k}q', crpix, rng,
                      **kwargs)
             for k, crpix in enumerate(((40.0, 40.0), (43.4, 38.7), (37.2, 42.6)))]
    with fits.open(files[0], mode='update') as hdul:
        hdul['SCI', 1].data[30:33, 50:53] += 50 * _SIGMA      # the hit
    return files


def test_flags_go_to_the_cfout_extension_and_the_adapter_honors_them(tmp_path):
    files = _visit(tmp_path)
    counts = outlier_step(files)
    assert counts[files[0]] >= 7                       # the 3x3 hit, or nearly
    assert counts[files[1]] + counts[files[2]] <= 3    # the others are clean

    with fits.open(files[0]) as hdul:
        flag = hdul[OUTLIER_EXT, 1].data
        assert flag.dtype == np.uint8 and flag.shape == (_NY, _NX)
        assert flag[30:33, 50:53].sum() >= 7
        assert np.all(hdul['DQ', 1].data == 0)         # DQ is not touched
        assert cfp.step_value(files[0], 'CFP_OUT', keyset=cfp.HST).startswith('field n=3')

    (chip,) = open_exposure(files[0])
    assert not chip.load().good[31, 51]                # dropped from the mosaic
    (raw,) = open_exposure(files[0], use_outlier_flags=False)
    assert raw.load().good[31, 51]                     # still there when asked


def test_rerun_replaces_the_flags_and_does_not_depend_on_them(tmp_path):
    files = _visit(tmp_path)
    outlier_step(files)
    first = fits.getdata(files[0], (OUTLIER_EXT, 1)).copy()
    assert outlier_step(files) is None                 # stamped: skipped
    outlier_step(files, overwrite=True)
    with fits.open(files[0]) as hdul:
        assert sum(h.name == OUTLIER_EXT for h in hdul) == 1
        assert np.array_equal(hdul[OUTLIER_EXT, 1].data, first)


def test_unaligned_exposures_are_refused(tmp_path):
    rng = np.random.default_rng(6)
    good = _ir_file(tmp_path / 'itest02a0q_flt.fits', 'itest02a0q', (40.0, 40.0), rng)
    never = _ir_file(tmp_path / 'itest02a1q_flt.fits', 'itest02a1q', (41.0, 40.0), rng,
                     aligned=None)
    failed = _ir_file(tmp_path / 'itest02a2q_flt.fits', 'itest02a2q', (42.0, 40.0), rng,
                      aligned=cfp.NOT_ALIGNED)
    for other in (never, failed):
        with pytest.raises(ValueError, match='no solved alignment'):
            outlier_step([good, other])
    with pytest.raises(ValueError, match='at least two'):
        outlier_step([good])
    with fits.open(good) as hdul:                      # nothing was written
        assert all(h.name != OUTLIER_EXT for h in hdul)
