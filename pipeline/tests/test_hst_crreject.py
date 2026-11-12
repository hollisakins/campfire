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
