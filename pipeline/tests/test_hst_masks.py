"""What keeps pixels and exposures out of an HST mosaic besides cosmic rays:
satellite trails (found, ACS/WFC), hand-drawn region masks, and the review
previews they are drawn on.

The trail finder itself (acstools.findsat_mrt) is exercised on real exposures
(docs/hst/design.md, section 8b); here its catalog is replaced by a fixed one,
and what the step does with it is checked.
"""

import os

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.config import get_hst_step_config
from campfire_pipeline.hst import orchestrate
from campfire_pipeline.hst.exposure import MASK_EXT, open_exposure
from campfire_pipeline.hst.steps import apply_masks, preview, trails

from test_hst_exposure import _acs_file


# ---------------------------------------------------------------------------
# trails
# ---------------------------------------------------------------------------

def test_a_binned_mask_goes_back_to_the_chip_shape():
    binned = np.zeros((3, 4), bool)
    binned[1, 2] = binned[2, 3] = True
    full = trails._expand(binned, (7, 9))         # 2x binning dropped a row and a column
    assert full.shape == (7, 9)
    assert full[2:4, 4:6].all() and full[:2].sum() == 0
    assert full[4:7, 6:9].all()                   # the dropped edge follows its neighbor
    with pytest.raises(ValueError, match='does not bin'):
        trails._expand(binned, (6, 16))


def _fake_finder(found):
    """A stand-in for chip_trails: ``found[extver]`` is (mask, trails)."""
    def chip_trails(exposure_file, extver, params, processes=1):
        return found[extver]
    return chip_trails


def _trail(snr, persistence, masked):
    return {'snr': snr, 'persistence': persistence, 'width': 12.0,
            'theta': 55.0, 'masked': masked}


def test_trails_are_flagged_with_bit_16384_and_replaced_on_a_rerun(tmp_path, monkeypatch):
    path = str(tmp_path / 'jtest01aaq_flc.fits')
    ny, nx, _t, _sky = _acs_file(path)
    with fits.open(path, mode='update') as hdul:      # a stale trail flag
        hdul['DQ', 2].data[5, 5] |= trails.TRAIL_BIT

    m1 = np.zeros((ny, nx), bool)
    m1[3, :] = True
    found = {1: (m1, [_trail(280.0, 1.0, True)]),
             2: (np.zeros((ny, nx), bool), [_trail(6.0, 0.6, False)])}
    monkeypatch.setattr(trails, 'chip_trails', _fake_finder(found))

    result = trails.trails_step(path)
    assert [t['masked'] for t in result[1]] == [True]
    with fits.open(path) as hdul:
        a, b = hdul['DQ', 1].data, hdul['DQ', 2].data
        assert a.dtype.kind == 'i' and a.dtype.itemsize == 2
        assert ((a[3] & trails.TRAIL_BIT) != 0).all()
        assert a[0, 0] == 64 and a[0, 2] == 4096      # other bits untouched
        assert ((b & trails.TRAIL_BIT) == 0).all()    # stale flag cleared
        assert hdul[0].header['CFP_TRL'] == f'findsat n=1 px={nx}'
    # the adapter leaves the trail out of everything downstream
    assert not open_exposure(path)[0].load().good[3].any()
    # stamped: nothing to do
    assert trails.trails_step(path) is None


def test_an_ignored_exposure_keeps_its_pixels(tmp_path, monkeypatch):
    path = str(tmp_path / 'jtest01aaq_flc.fits')
    ny, nx, _t, _sky = _acs_file(path)
    m1 = np.zeros((ny, nx), bool)
    m1[3, :] = True
    found = {1: (m1, [_trail(15.0, 0.9, True)]),
             2: (np.zeros((ny, nx), bool), [])}
    monkeypatch.setattr(trails, 'chip_trails', _fake_finder(found))
    trails.trails_step(path, step_config={'ignore': ['JTEST01AAQ']})
    with fits.open(path) as hdul:
        assert ((hdul['DQ', 1].data & trails.TRAIL_BIT) == 0).all()
        assert hdul[0].header['CFP_TRL'] == 'findsat n=0 px=0 ignored=1'


def test_trails_is_an_acs_step(tmp_path):
    path = str(tmp_path / 'itest01aaq_flt.fits')
    primary = fits.PrimaryHDU()
    primary.header.update({'INSTRUME': 'WFC3', 'DETECTOR': 'IR'})
    primary.writeto(path)
    with pytest.raises(ValueError, match='ACS/WFC'):
        trails.trails_step(path)
    assert 'trails' in [n for n, _ in orchestrate.steps_for('acswfc')]
    assert 'trails' not in [n for n, _ in orchestrate.steps_for('wfc3ir')]
    # the cuts ship in the config, nowhere else
    cfg = get_hst_step_config('trails', 'acswfc')
    assert cfg['min_persistence'] == 0.8 and cfg['min_snr'] == 8.0
    assert cfg['grow'] == 12 and cfg['ignore'] == []
    assert get_hst_step_config('trails', 'wfc3ir') == {}


def test_only_confident_trails_are_masked(tmp_path, monkeypatch):
    """The finder's catalog holds a strong trail, a weak one that passed its
    own cuts, and a rejected candidate: only the first is masked."""
    findsat = pytest.importorskip('acstools.findsat_mrt')
    from astropy.table import Table

    path = str(tmp_path / 'jtest01aaq_flc.fits')
    hdul = fits.HDUList([fits.PrimaryHDU()])
    for v in (1, 2):
        for name, dtype in (('SCI', 'float32'), ('ERR', 'float32'), ('DQ', 'int16')):
            hdul.append(fits.ImageHDU(np.zeros((80, 120), dtype), name=name, ver=v))
    hdul.writeto(path)

    class Finder:
        def __init__(self, image_file, extension=None, binsize=None, **kw):
            assert extension == 1 and binsize == 2
            assert kw['min_persistence'] == 0.8 and kw['threshold'] == 5.0
            self.image = np.zeros((40, 60))
            self.source_list = Table({
                'id': [1, 2, 3], 'status': [2, 2, 0],
                'snr': [50.0, 6.0, 30.0], 'persistence': [1.0, 0.9, 0.0],
                'width': [4.0, 4.0, 4.0], 'theta': [90.0, 0.0, 45.0],
                'endpoints': [[(0.0, 20.0), (59.0, 20.0)],
                              [(30.0, 0.0), (30.0, 39.0)],
                              [(0.0, 0.0), (39.0, 39.0)]],
            })

        def run_mrt(self):
            pass

        find_mrt_sources = filter_sources = run_mrt

    monkeypatch.setattr(findsat, 'WfcWrapper', Finder)
    params = dict(get_hst_step_config('trails', 'acswfc'), grow=0)
    mask, found = trails.chip_trails(path, 1, params)
    assert [(t['snr'], t['masked']) for t in found] == [(50.0, True), (6.0, False)]
    assert found[0]['width'] == 8.0                 # binned pixels -> native
    assert mask.shape == (80, 120)
    rows = np.flatnonzero(mask.any(axis=1))
    assert rows.min() >= 34 and rows.max() <= 46 and 40 in rows   # the row-20 trail
    assert mask[40].mean() > 0.9
    assert not mask[:30].any() and not mask[50:].any()      # the weak one is not masked
    grown, _ = trails.chip_trails(path, 1, dict(params, grow=3))
    assert grown.sum() > mask.sum() and grown[rows.min() - 3, 60]


# ---------------------------------------------------------------------------
# apply_masks
# ---------------------------------------------------------------------------

def _reg(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write('# Region file format: DS9\n' + text + '\n')
    return str(path)


def test_an_image_region_is_in_the_frame_of_the_stacked_preview(tmp_path):
    pytest.importorskip('regions')
    path = str(tmp_path / 'jtest01aaq_flc.fits')
    ny, nx, _t, _sky = _acs_file(path)                  # 24 x 32 per chip
    # DS9 image coordinates are 1-based. A box 4 wide and 2 tall centred at
    # (11.5, 31.5) covers canvas pixels x = 9..12, y = 30..31 (0-based): rows
    # 6 and 7 of the upper chip.
    reg = _reg(tmp_path / 'm' / 'jtest01aaq_flc.reg', 'image\nbox(11.5,31.5,4,2,0)')
    before = fits.getdata(path, ('SCI', 2)).copy()
    npix = apply_masks.apply_masks_step(path, reg)
    assert npix == 8
    with fits.open(path) as hdul:
        lower, upper = hdul[MASK_EXT, 1].data, hdul[MASK_EXT, 2].data
        assert lower.sum() == 0
        assert upper.sum() == 8 and upper[6:8, 9:13].all()
        assert np.array_equal(hdul['SCI', 2].data, before)      # pixels untouched
        assert (hdul['DQ', 2].data[6:8, 9:13] == 0).all()       # and DQ
        assert hdul[0].header['CFP_MASK'].startswith('reg ')
        assert hdul[0].header['CFP_MASK'].endswith(' px=8')
    chips = open_exposure(path)
    assert chips[0].load().good[6:8, 9:13].all()
    assert not chips[1].load().good[6:8, 9:13].any()
    # nothing changed on disk: nothing to do
    assert apply_masks.apply_masks_step(path, reg) is None


def test_a_sky_region_lands_on_the_chip_that_holds_it(tmp_path):
    pytest.importorskip('regions')
    path = str(tmp_path / 'jtest01aaq_flc.fits')
    _acs_file(path)
    chips = open_exposure(path)
    ra, dec = chips[0].pixel_to_world(20.0, 10.0)        # on [SCI,1]
    reg = _reg(tmp_path / 'm' / 'jtest01aaq_flc.reg',
               f'fk5\ncircle({float(ra):.7f},{float(dec):.7f},0.1")')
    apply_masks.apply_masks_step(path, reg)
    with fits.open(path) as hdul:
        m = hdul[MASK_EXT, 1].data
    yy, xx = np.nonzero(m)
    assert 9 <= len(yy) <= 16                            # radius 2 pixels
    assert abs(yy.mean() - 10.0) < 0.6 and abs(xx.mean() - 20.0) < 0.6


def test_a_mask_follows_its_region_file(tmp_path):
    pytest.importorskip('regions')
    path = str(tmp_path / 'jtest01aaq_flc.fits')
    _acs_file(path)
    reg = str(tmp_path / 'm' / 'jtest01aaq_flc.reg')

    # nobody drew on it: the file is not touched and carries no stamp
    stat = os.stat(path)
    assert apply_masks.is_current(path, reg)
    assert apply_masks.apply_masks_step(path, reg) is None
    assert os.stat(path).st_mtime_ns == stat.st_mtime_ns
    assert not cfp.has_step(path, 'CFP_MASK', keyset=cfp.HST)

    _reg(reg, 'image\nbox(5.5,5.5,2,2,0)')            # pixels 4..5 in x and y
    assert not apply_masks.is_current(path, reg)
    assert apply_masks.apply_masks_step(path, reg) == 4
    assert not open_exposure(path)[0].load().good[4:6, 4:6].any()
    # edited: redone, the old pixels released
    _reg(reg, 'image\nbox(20.5,10.5,2,2,0)')
    assert not apply_masks.is_current(path, reg)
    assert apply_masks.apply_masks_step(path, reg) == 4
    with fits.open(path) as hdul:
        assert hdul[MASK_EXT, 1].data[4:6, 4:6].sum() == 0
        assert len([h for h in hdul if h.name == MASK_EXT]) == 2
    # removed: the mask goes with it
    os.remove(reg)
    assert not apply_masks.is_current(path, reg)
    assert apply_masks.apply_masks_step(path, reg) == 0
    with fits.open(path) as hdul:
        assert not [h for h in hdul if h.name == MASK_EXT]
        assert hdul[0].header['CFP_MASK'] == apply_masks.NO_FILE
    assert apply_masks.is_current(path, reg)
    assert open_exposure(path)[0].load().good[4:6, 4:6].all()


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------

def test_the_preview_stacks_the_chips_and_blanks_what_is_left_out(tmp_path):
    path = str(tmp_path / 'jtest01aaq_flc.fits')
    ny, nx, _t, _sky = _acs_file(path, source=500.0)     # a source at (10, 10)
    with fits.open(path, mode='update') as hdul:
        hdul['SCI', 2].data[4, 4] += 900.0               # a cosmic ray, flagged
        hdul['DQ', 2].data[4, 4] |= 4096
    canvas = preview.snr_canvas(path)
    assert canvas.shape == (2 * ny, nx)
    assert canvas[10, 10] > 10 and canvas[ny + 10, 10] > 10     # both chips, in order
    assert canvas[ny + 4, 4] == 0.0                      # the flagged hit is blank
    assert canvas[0, 1] == 0.0 and canvas[0, 0] != 0.0   # hot pixel out, warm pixel in

    thumb, full = preview.preview_step(path, step_config={'max_dim': 16})
    assert thumb.endswith('jtest01aaq_flc_preview.png')
    assert full.endswith('jtest01aaq_flc_full.png')
    import matplotlib.image as mpimg
    assert mpimg.imread(full).shape[:2] == (2 * ny, nx)
    assert max(mpimg.imread(thumb).shape[:2]) <= 16
    assert fits.getheader(path)['CFP_PREV'] == preview.PREVIEW_KIND
    assert preview.preview_step(path) is None            # both there, stamped
    os.remove(thumb)
    assert preview.preview_step(path) is not None        # a missing PNG is redone
