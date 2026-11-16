"""HST crreject step: the parts that do not need drizzlepac.

AstroDrizzle's rejection sequence itself is exercised on real exposures (see
docs/hst/design.md, section 8b); here the mask reading, the DQ transfer and the
step's guards are checked.
"""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.hst.steps import crreject


def _flc(path, dq1, dq2):
    hdul = fits.HDUList([fits.PrimaryHDU()])
    for v, dq in ((1, dq1), (2, dq2)):
        hdul.append(fits.ImageHDU(np.zeros(dq.shape, 'float32'), name='SCI', ver=v))
        hdul.append(fits.ImageHDU(dq.astype('int16'), name='DQ', ver=v))
    hdul.writeto(path)


def test_transfer_sets_replaces_and_preserves(tmp_path):
    path = str(tmp_path / 'jcr01aaq_flc.fits')
    dq1 = np.zeros((4, 5), 'int16')
    dq1[0, 0] = 64                    # warm pixel: must survive
    dq1[1, 1] = 4096                  # an old cosmic-ray flag: must be cleared
    dq1[2, 2] = 4096 + 16             # old flag on a hot pixel
    _flc(path, dq1, np.zeros((4, 5), 'int16'))

    new1 = np.zeros((4, 5), bool)
    new1[0, 0] = new1[3, 4] = True
    new2 = np.zeros((4, 5), bool)
    new2[2, 3] = True
    counts = crreject._transfer_flags(path, {1: new1, 2: new2})
    assert counts == [2, 1]

    with fits.open(path) as hdul:
        a, b = hdul['DQ', 1].data, hdul['DQ', 2].data
        assert a.dtype.kind == 'i' and a.dtype.itemsize == 2      # still 16-bit
        assert a[0, 0] == 64 + 4096           # kept its bit, gained the CR bit
        assert a[3, 4] == 4096
        assert a[1, 1] == 0                   # stale flag removed
        assert a[2, 2] == 16                  # stale flag removed, hot bit kept
        assert b[2, 3] == 4096 and b.sum() == 4096

    with pytest.raises(RuntimeError, match='shape'):
        crreject._transfer_flags(path, {1: np.zeros((2, 2), bool)})


def test_crmasks_are_read_per_chip_and_inverted(tmp_path):
    # driz_cr convention: 1 = pixel passed, 0 = cosmic ray
    for v, bad in ((1, (0, 0)), (2, (1, 2))):
        m = np.ones((3, 4), 'uint8')
        m[bad] = 0
        fits.PrimaryHDU(m).writeto(tmp_path / f'jcr01aaq_sci{v}_crmask.fits')
    fits.PrimaryHDU(np.zeros((3, 4), 'uint8')).writeto(
        tmp_path / 'jcr01aaq_sci1_blt.fits')                 # unrelated product
    masks = crreject._read_crmasks(str(tmp_path), 'jcr01aaq', [1, 2])
    assert masks[1][0, 0] and masks[1].sum() == 1
    assert masks[2][1, 2] and masks[2].sum() == 1
    with pytest.raises(RuntimeError, match='expected one'):
        crreject._read_crmasks(str(tmp_path), 'jcr02bbq', [1])


def test_step_guards(tmp_path):
    a = str(tmp_path / 'jcr01aaq_flc.fits')
    _flc(a, np.zeros((4, 5), 'int16'), np.zeros((4, 5), 'int16'))
    # one exposure cannot be cleaned against others
    with pytest.raises(ValueError, match='at least two'):
        crreject.crreject_step([a])
    # every file stamped already: nothing to do
    b = str(tmp_path / 'jcr01bbq_flc.fits')
    _flc(b, np.zeros((4, 5), 'int16'), np.zeros((4, 5), 'int16'))
    for f in (a, b):
        with fits.open(f, mode='update') as hdul:
            for k, v in cfp.format(keyset=cfp.HST, CFP_CR='adz n=2').items():
                hdul[0].header[k] = v
    assert crreject.crreject_step([a, b]) is None


def _lone_acs(path, rate=False):
    """A 120 x 120 two-chip exposure: sky with noise, one star 2.2 pixels wide
    at (60, 60) and, on chip 1, four single-pixel hits and a three-pixel track."""
    from test_hst_exposure import _imset, _wcs_cards

    ny = nx = 120
    rng = np.random.default_rng(4)
    primary = fits.PrimaryHDU()
    primary.header.update({
        'INSTRUME': 'WFC3' if rate else 'ACS', 'DETECTOR': 'IR' if rate else 'WFC',
        'ROOTNAME': 'jlone01aq', 'EXPTIME': 500.0, 'READNSEA': 5.0,
        'READNSEB': 5.0, 'READNSEC': 5.0, 'READNSED': 5.0})
    if rate:
        primary.header['FILTER'] = 'F160W'
    else:
        primary.header.update({'FILTER1': 'CLEAR1L', 'FILTER2': 'F814W'})
    yy, xx = np.mgrid[0:ny, 0:nx]
    star = 3000.0 * np.exp(-((yy - 60) ** 2 + (xx - 60) ** 2) / (2 * (2.2 / 2.355) ** 2))
    hits = [(20, 30), (40, 90), (85, 25), (100, 100)]
    hdul = fits.HDUList([primary])
    for extver in ((1,) if rate else (1, 2)):
        sci = rng.poisson(80.0 + star).astype(float) + rng.normal(0, 5.0, (ny, nx))
        if extver == 1:
            for i, j in hits:
                sci[i, j] += 800.0
            sci[70, 40:43] += 600.0
        err = np.sqrt(np.clip(sci, 1, None) + 25.0)
        extra = {'BUNIT': 'ELECTRONS/S' if rate else 'ELECTRONS', 'PHOTFNU': 1e-7,
                 'IDCSCALE': 0.05, 'WCSNAME': 'IDC_test'}
        hdul += _imset(extver, sci.astype('float32'), err.astype('float32'),
                       np.zeros((ny, nx), 'int16'),
                       _wcs_cards(0.05, (40.0 + 0.01 * extver, 26.0)), extra)
    hdul.writeto(path)
    return hits


def test_a_lone_exposure_is_cleaned_from_its_own_pixels(tmp_path):
    pytest.importorskip('astroscrappy')
    path = str(tmp_path / 'jlone01aq_flc.fits')
    hits = _lone_acs(path)
    n = crreject.crreject_single_step(path)
    with fits.open(path) as hdul:
        cr = (hdul['DQ', 1].data & crreject.CR_BIT) != 0
        other = (hdul['DQ', 2].data & crreject.CR_BIT) != 0
        stamp = hdul[0].header['CFP_CR']
    assert all(cr[i, j] for i, j in hits) and cr[70, 40:43].all()
    assert cr[hits[0][0] + 1, hits[0][1]]              # grown by one pixel
    # the star's core is as sharp as a hit on this detector and is left alone
    assert not cr[58:63, 58:63].any() and not other[58:63, 58:63].any()
    assert n == cr.sum() + other.sum() and n < 0.02 * cr.size
    assert stamp.startswith('lacosmic n=1 sigclip=4.5 objlim=10')
    # stamped: a second call does nothing
    assert crreject.crreject_single_step(path) is None


def test_the_single_exposure_method_is_for_ccds(tmp_path):
    path = str(tmp_path / 'ilone01aq_flt.fits')
    _lone_acs(path, rate=True)
    with pytest.raises(ValueError, match='ramp fit'):
        crreject.crreject_single_step(path)
