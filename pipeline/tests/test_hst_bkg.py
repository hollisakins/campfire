"""HST bkg step: NIRCam's background chain on HST chips.

Synthetic files, no HST software or data: an ACS/WFC exposure with an offset
between the amplifier halves of each chip, and a WFC3/IR exposure with a band
of extra light down the left of the detector (scattered earthlight looks like
that).
"""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.hst.exposure import BKG_KEY, VARSCALE_KEY, open_exposure
from campfire_pipeline.hst.steps.bkg import SRCMASK_EXT, amplifier_regions, bkg_step
from campfire_pipeline.imaging.bkg_chain import region_pedestal

_NY, _NX, _T = 300, 400, 400.0
_SKY, _TRUE_SIGMA, _READ = 40.0, 9.0, 4.0          # electrons
_AMP_STEP = {1: (0.0, 3.0), 2: (-2.0, 0.0)}        # offset per amplifier half
_NO_PLOT = {'plot': False}


def _wcs(scale, extver=1):
    s = scale / 3600.0
    return {'CTYPE1': 'RA---TAN-SIP', 'CTYPE2': 'DEC--TAN-SIP',
            'CRPIX1': 200.0, 'CRPIX2': 150.0, 'CRVAL1': 40.0,
            'CRVAL2': 26.0 + 0.004 * extver, 'CD1_1': -s, 'CD1_2': 0.0,
            'CD2_1': 0.0, 'CD2_2': s, 'A_ORDER': 2, 'B_ORDER': 2,
            'A_2_0': 0.0, 'B_0_2': 0.0}


def _acs_file(path):
    """Two chips, an offset between the amplifier halves of each, sources,
    cosmic rays already flagged (the step runs after crreject)."""
    rng = np.random.default_rng(7)
    primary = fits.PrimaryHDU()
    primary.header.update({
        'INSTRUME': 'ACS', 'DETECTOR': 'WFC', 'ROOTNAME': 'jbkg01aaq',
        'FILTER1': 'CLEAR1L', 'FILTER2': 'F814W', 'EXPTIME': _T,
        'CCDAMP': 'ABCD'})
    hdul = fits.HDUList([primary])
    yy, xx = np.mgrid[:_NY, :_NX]
    sources = {}
    for extver in (1, 2):
        sci = _SKY + rng.normal(0.0, _TRUE_SIGMA, (_NY, _NX))
        left, right = _AMP_STEP[extver]
        sci[:, :_NX // 2] += left
        sci[:, _NX // 2:] += right
        where = []
        for _ in range(8):
            y0, x0 = rng.uniform(30, _NY - 30), rng.uniform(30, _NX - 30)
            sci += rng.uniform(1500, 4000) * np.exp(
                -0.5 * ((yy - y0) ** 2 + (xx - x0) ** 2) / 2.0 ** 2)
            where.append((int(round(y0)), int(round(x0))))
        sources[extver] = where
        dq = np.zeros((_NY, _NX), 'int16')
        hits = (rng.integers(0, _NY, 150), rng.integers(0, _NX, 150))
        sci[hits] += rng.uniform(100, 2000, 150)
        dq[hits] = 4096                                   # flagged cosmic rays
        dq[5, :] = 128                                    # a bad row
        # ERR as calacs writes it: Poisson of the counts plus read noise. It
        # knows nothing of the extra noise in this frame (true sigma is 9 e-).
        err = np.sqrt(np.clip(sci, 0, None) + _READ ** 2)
        for name, data in (('SCI', sci.astype('float32')),
                           ('ERR', err.astype('float32')), ('DQ', dq)):
            hdu = fits.ImageHDU(data=data, name=name, ver=extver)
            if name == 'SCI':
                hdu.header.update(_wcs(0.05, extver))
                hdu.header.update({'BUNIT': 'ELECTRONS', 'PHOTFLAM': 7.0e-20,
                                   'PHOTPLAM': 8045.0, 'IDCSCALE': 0.05})
            hdul.append(hdu)
    hdul.writeto(path)
    return sources


_IR_N, _IR_T, _IR_SKY, _IR_SIGMA = 300, 500.0, 0.8, 0.045   # e-/s
_IR_STAR = (150, 220)


def _ir_file(path, band=0.0):
    """One WFC3/IR chip: a flat background, a few compact sources, and a band
    of extra light of amplitude ``band`` on the left third, fading to nothing
    by column 150."""
    rng = np.random.default_rng(11)
    primary = fits.PrimaryHDU()
    primary.header.update({'INSTRUME': 'WFC3', 'DETECTOR': 'IR',
                           'ROOTNAME': 'ibkg01aaq', 'FILTER': 'F140W',
                           'EXPTIME': _IR_T})
    yy, xx = np.mgrid[:_IR_N, :_IR_N]
    light = band / (1.0 + np.exp((xx - 110.0) / 12.0))
    sci = _IR_SKY + light + rng.normal(0.0, _IR_SIGMA, (_IR_N, _IR_N))
    stars = [_IR_STAR] + [(int(rng.uniform(30, 270)), int(rng.uniform(30, 270)))
                          for _ in range(6)]
    truth = np.zeros_like(sci)
    for y0, x0 in stars:
        truth += 6.0 * np.exp(-0.5 * ((yy - y0) ** 2 + (xx - x0) ** 2) / 1.2 ** 2)
    sci += truth
    err = np.sqrt(np.clip(sci, 0, None) / _IR_T + 0.0004)
    hdus = []
    for name, data in (('SCI', sci.astype('float32')), ('ERR', err.astype('float32')),
                       ('DQ', np.zeros((_IR_N, _IR_N), 'int16'))):
        hdu = fits.ImageHDU(data=data, name=name, ver=1)
        if name == 'SCI':
            hdu.header.update(_wcs(0.128))
            hdu.header.update({'BUNIT': 'ELECTRONS/S', 'PHOTFNU': 1.5e-07,
                               'IDCSCALE': 0.128})
        hdus.append(hdu)
    hdus.append(fits.ImageHDU(data=np.full((_IR_N, _IR_N), _IR_T, 'float32'),
                              name='TIME', ver=1))
    fits.HDUList([primary] + hdus).writeto(path)
    return truth


def test_the_pedestal_rule_is_nircams_for_any_amplifier_layout():
    """One level per amplifier: on NIRCam's four column strips the shared rule
    gives what NIRCam's own function gives."""
    from campfire_pipeline.nircam.constants import NIR_AMPS
    from campfire_pipeline.nircam.oneoverf import peramp_pedestal

    rng = np.random.default_rng(2)
    data = rng.normal(0.0, 1.0, (2048, 2048))
    mask = rng.random(data.shape) < 0.1
    regions = []
    for i, amp in enumerate('ABCD'):
        _, _, c0, c1 = NIR_AMPS[amp]['data']
        data[:, c0:c1] += 0.3 * i
        regions.append((slice(0, 2048), slice(c0, c1)))
    ours, levels = region_pedestal(data, mask, regions)
    theirs, per_amp = peramp_pedestal(data, mask)
    assert np.array_equal(ours, theirs)
    assert levels == [per_amp[a] for a in 'ABCD']


def test_amplifier_regions():
    class Chip:
        shape = (2048, 4096)

        class detector:
            token = 'acswfc'
    assert amplifier_regions(Chip, {'CCDAMP': 'ABCD'}) == [
        (slice(0, 2048), slice(0, 2048)), (slice(0, 2048), slice(2048, 4096))]
    # a subarray is read through one amplifier
    assert amplifier_regions(Chip, {'CCDAMP': 'B'}) == [
        (slice(0, 2048), slice(0, 4096))]


def test_acs_background_is_subtracted_per_amplifier(tmp_path):
    path = str(tmp_path / 'jbkg01aaq_flc.fits')
    sources = _acs_file(path)
    with fits.open(path) as hdul:
        before = {v: hdul['SCI', v].data.astype('float64') for v in (1, 2)}
        err_before = hdul['ERR', 1].data.copy()

    results = bkg_step(path, step_config=_NO_PLOT)
    assert set(results) == {'jbkg01aaq_sci1', 'jbkg01aaq_sci2'}

    with fits.open(path) as hdul:
        assert 'CFP_BKG' in hdul[0].header
        assert 'subtract_2d=True' in hdul[0].header['CFP_BKG']
        assert np.array_equal(hdul['ERR', 1].data, err_before)   # ERR untouched
        for v in (1, 2):
            h = hdul['SCI', v].header
            sci = hdul['SCI', v].data.astype('float64')
            mask = hdul[SRCMASK_EXT, v].data.astype(bool)
            left, right = _AMP_STEP[v]
            # the level is out of the pixels, and recorded
            assert h[BKG_KEY] == pytest.approx(_SKY + 0.5 * (left + right), abs=0.5)
            blank = ~mask
            half = _NX // 2
            m_left = np.median(sci[:, :half][blank[:, :half]])
            m_right = np.median(sci[:, half:][blank[:, half:]])
            assert abs(m_left) < 0.4 and abs(m_right) < 0.4
            # ... with the step between the two amplifiers (before: 3 and 2 e-)
            assert abs(m_right - m_left) < 0.5
            # the source mask covers the sources, and not the whole frame
            for y0, x0 in sources[v]:
                assert mask[y0, x0]
            assert 0.3 < blank.mean() < 0.95
            # a source keeps its flux: what was removed under it is the level
            removed = before[v] - sci
            y0, x0 = sources[v][0]
            box = removed[y0 - 3:y0 + 4, x0 - 3:x0 + 4]
            assert np.all(np.abs(box - h[BKG_KEY]) < 4.0)
            # variance: the true scatter, not what ERR claims (white noise, so
            # NIRCam's block measurement gives the single-pixel ratio)
            modelled = _SKY + _READ ** 2
            assert h[VARSCALE_KEY] == pytest.approx(_TRUE_SIGMA ** 2 / modelled,
                                                   rel=0.12)

    # the adapter hands the pixels over as they are, with the measured noise
    for chip in open_exposure(path):
        a = chip.load()
        f = chip.sb_per_rate
        assert chip.provenance()['sky_src'] == 'bkg'
        assert chip.provenance()['sky'] == pytest.approx(_SKY, abs=2.0)
        assert np.sqrt(np.median(a.var_rnoise)) == pytest.approx(
            _TRUE_SIGMA / _T * f, rel=0.07)
        assert abs(np.median(a.sci[a.good])) < 0.1 * _TRUE_SIGMA / _T * f


def test_a_band_of_light_in_one_exposure_is_removed(tmp_path):
    """Scattered light on a third of the detector, six times the pixel noise:
    after the step the background is level across the detector, with no edge
    where the band was, and a star in the clean part keeps its flux."""
    path = str(tmp_path / 'ibkg01aaq_flt.fits')
    truth = _ir_file(path, band=0.27)
    bkg_step(path, step_config=_NO_PLOT)
    with fits.open(path) as hdul:
        sci = hdul['SCI', 1].data.astype('float64')
        mask = hdul[SRCMASK_EXT, 1].data.astype(bool)
        level = hdul['SCI', 1].header[BKG_KEY]
    blank = ~mask
    strips = [np.median(sci[:, a:a + 50][blank[:, a:a + 50]])
              for a in range(0, _IR_N, 50)]
    # before: +0.27 on the left, 0 on the right. After: level to a tenth of
    # the pixel noise, strip by strip
    assert np.max(np.abs(strips)) < 0.1 * _IR_SIGMA
    assert level > _IR_SKY                              # the band is in the level
    y0, x0 = _IR_STAR
    got = sci[y0 - 4:y0 + 5, x0 - 4:x0 + 5].sum()
    want = truth[y0 - 4:y0 + 5, x0 - 4:x0 + 5].sum()
    assert got == pytest.approx(want, rel=0.03)


def test_bkg_step_runs_once_and_can_be_run_again(tmp_path):
    path = str(tmp_path / 'ibkg01aaq_flt.fits')
    _ir_file(path)
    bkg_step(path, step_config=_NO_PLOT)
    with fits.open(path) as hdul:
        first = hdul['SCI', 1].data.copy()
        level = hdul['SCI', 1].header[BKG_KEY]
    assert level == pytest.approx(_IR_SKY, abs=0.01)

    assert bkg_step(path, step_config=_NO_PLOT) is None   # stamped: nothing happens
    with fits.open(path) as hdul:
        assert np.array_equal(hdul['SCI', 1].data, first)

    bkg_step(path, step_config=_NO_PLOT, overwrite=True)  # on subtracted pixels
    with fits.open(path) as hdul:
        # nothing more to remove (a second fit follows the noise of its mesh
        # boxes, a small fraction of the pixel noise), and the recorded level
        # is still the total
        moved = hdul['SCI', 1].data - first
        assert np.std(moved) < 0.2 * _IR_SIGMA
        assert np.max(np.abs(moved)) < _IR_SIGMA
        assert hdul['SCI', 1].header[BKG_KEY] == pytest.approx(level, abs=0.01)
        assert len([h for h in hdul if h.name == SRCMASK_EXT]) == 1
    assert cfp.has_step(path, 'CFP_BKG', keyset=cfp.HST)


def test_the_cosmic_ray_step_compares_leveled_copies(tmp_path):
    """A band of light in one exposure would stand above the visit's median
    over its whole area. The cosmic-ray step levels its working copy first:
    the band's shape goes, the level of the counts stays, a star is kept."""
    from campfire_pipeline.config import get_hst_step_config
    from campfire_pipeline.hst.steps.crreject import _level_background

    path = str(tmp_path / 'ibkg01aaq_flt.fits')
    truth = _ir_file(path, band=0.27)
    with fits.open(path) as hdul:
        before = hdul['SCI', 1].data.astype('float64')
    _level_background(path, get_hst_step_config('bkg', 'wfc3ir'))
    with fits.open(path) as hdul:
        after = hdul['SCI', 1].data.astype('float64')
    left, right = np.median(after[:, :60]), np.median(after[:, 200:])
    assert np.median(before[:, :60]) - np.median(before[:, 200:]) > 0.2
    # 0.27 before; the coarse fit leaves about a ninth of it at the very edge
    # of this small frame, under one pixel sigma
    assert abs(left - right) < _IR_SIGMA
    # still at the counts of the exposure (its background of 0.8), not at zero
    assert np.median(after) == pytest.approx(_IR_SKY, abs=_IR_SIGMA)
    y0, x0 = _IR_STAR
    assert after[y0, x0] - np.median(after) == pytest.approx(truth[y0, x0], rel=0.1)

    # switched off with the detrend
    off = get_hst_step_config('bkg', 'wfc3ir', overrides={'detrend': {'enabled': False}})
    _level_background(path, off)
    with fits.open(path) as hdul:
        assert np.array_equal(hdul['SCI', 1].data.astype('float64'), after)


def test_the_cosmic_ray_step_gets_the_counts_with_their_background(tmp_path):
    """Run again after bkg, the cosmic-ray step works on a copy with the
    recorded level added back; the exposure itself is not touched."""
    import shutil

    from campfire_pipeline.hst.steps.crreject import _restore_background

    path = str(tmp_path / 'ibkg01aaq_flt.fits')
    _ir_file(path)
    with fits.open(path) as hdul:
        original = hdul['SCI', 1].data.astype('float64')
    bkg_step(path, step_config=_NO_PLOT)
    copy = str(tmp_path / 'copy.fits')
    shutil.copyfile(path, copy)
    _restore_background(copy)
    with fits.open(copy) as hdul:
        restored = hdul['SCI', 1].data.astype('float64')
        assert BKG_KEY not in hdul['SCI', 1].header
    assert np.median(restored) == pytest.approx(np.median(original), abs=0.01)
    assert BKG_KEY in fits.getheader(path, ('SCI', 1))
