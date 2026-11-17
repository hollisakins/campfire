"""NIRCam's outlier pass on the instrument-neutral code.

NIRCam used to have its own implementation of the per-visit outlier pass. It
now runs ``imaging/outlier.py::detect_outliers``, the routine the HST
instruments run, through :class:`campfire_pipeline.nircam.exposure.NircamChip`.
These tests use the synthetic scene of ``test_nircam_mosaic_golden`` (four
dithered exposures with known cosmic rays) and check what the pass is for:
the cosmic rays are flagged, real sources and blank sky are not, and the file
is left as the JWST outlier step leaves one.
"""

import os

import numpy as np
import pytest
from astropy.io import fits

import test_nircam_mosaic_golden as golden
from _align_gwcs import HAVE_PERSISTABLE_WCS

pytest.importorskip("drizzle.resample")
pytestmark = pytest.mark.skipif(
    not HAVE_PERSISTABLE_WCS, reason="needs jwst + gwcs for a persistable WCS")

_DNU, _OUTLIER = 1, 16


def _scene(workdir, n=4):
    """The golden scene's exposures on disk; returns (files, cosmic-ray masks)."""
    rng = np.random.default_rng(20261111)
    cosd = np.cos(np.deg2rad(-30.0))
    sky_sources = [
        (80.0 + dx / 3600.0 / cosd, -30.0 + dy / 3600.0, f)
        for dx, dy, f in zip(rng.uniform(-1.1, 1.1, 10),
                             rng.uniform(-1.1, 1.1, 10),
                             rng.uniform(0.3, 3.0, 10))
    ]
    files, hits = [], []
    for i in range(len(golden._DITHERS)):
        path = os.path.join(str(workdir), f'jw01727001001_02101_0000{i + 1}_nrca1.fits')
        hits.append(golden._build_exposure(path, i, rng, sky_sources))
        files.append(path)
    return files[:n], hits[:n]


def test_the_adapter_hands_over_a_jwst_exposure_as_it_is(tmp_path):
    from jwst.datamodels import ImageModel

    from campfire_pipeline.imaging.exposure import is_chip
    from campfire_pipeline.nircam.exposure import open_exposure

    (path,), _ = _scene(tmp_path, n=1)
    chip = open_exposure(path)
    assert is_chip(chip)
    assert chip.instrument == 'nircam'
    assert chip.key == os.path.basename(path).removesuffix('.fits')
    assert chip.shape == (golden._NY, golden._NX)
    assert chip.exptime == 1000.0 and chip.xposure == 900.0
    assert chip.pixel_area_arcsec2 == pytest.approx(golden._IN_SCALE ** 2)

    arrays = chip.load()
    with ImageModel(path) as m:
        assert np.array_equal(arrays.sci, m.data)
        assert np.array_equal(arrays.var_rnoise, m.var_rnoise)
        # the DO_NOT_USE column of the scene is the only bad one
        assert not arrays.good[:, 0].any() and arrays.good[:, 1:].all()
        ra, dec = m.meta.wcs(10.0, 20.0, with_bounding_box=False)
    got = chip.pixel_to_world(np.array([10.0]), np.array([20.0]))
    assert float(got[0][0]) == pytest.approx(float(ra), abs=1e-12)
    assert float(got[1][0]) == pytest.approx(float(dec), abs=1e-12)
    # positions outside the detector still map (no bounding box)
    out = chip.pixel_to_world(np.array([-5.0]), np.array([200.0]))
    assert np.isfinite(out[0]).all() and np.isfinite(out[1]).all()


def test_cosmic_rays_are_flagged_and_the_sky_is_not(tmp_path):
    from campfire_pipeline.nircam.outlier_detect import outlier_detect_for_visit

    files, hits = _scene(tmp_path)
    outlier_detect_for_visit(files, files, plot=False, tempdir=str(tmp_path))

    for path, cr in zip(files, hits):
        with fits.open(path) as hdul:
            dq = hdul['DQ'].data
            sci = hdul['SCI'].data
            header = hdul[0].header
        flagged = (dq & _OUTLIER) != 0
        # A hit is tested where the comparison is solid: a few pixels inside
        # the edge of the covered sky, since the blot interpolates over six
        # pixels and must not reach an empty one. With dithers of 2 to 4
        # pixels that leaves out a border of the detector here.
        testable = np.zeros_like(cr)
        testable[8:-8, 8:-8] = cr[8:-8, 8:-8]
        assert testable.sum() >= 8
        # every injected hit (60 sigma) there is found
        assert flagged[testable].all()
        # and little else: the pass may take the pixels touching a hit, not
        # blank sky or sources (4 exposures, 16 hits each)
        from scipy.ndimage import binary_dilation
        near = binary_dilation(cr, structure=np.ones((3, 3), bool))
        assert (flagged & ~near).sum() <= 5
        # the file is left as the JWST step leaves one: OUTLIER comes with
        # DO_NOT_USE, and the pixel is blanked
        assert ((dq[flagged] & _DNU) != 0).all()
        assert np.isnan(sci[flagged]).all()
        assert 'CFP_OUT' in header


def test_only_the_visit_is_flagged_and_stamped(tmp_path):
    from campfire_pipeline.nircam.outlier_detect import outlier_detect_for_visit

    files, hits = _scene(tmp_path)
    visit, context = files[:2], files[2:]
    before = {f: open(f, 'rb').read() for f in context}
    outlier_detect_for_visit(files, visit, plot=False, tempdir=str(tmp_path))
    for f in visit:
        with fits.open(f) as hdul:
            assert ((hdul['DQ'].data & _OUTLIER) != 0).sum() >= 8
            assert 'CFP_OUT' in hdul[0].header
    # the exposures it was compared with are comparison material only
    assert all(open(f, 'rb').read() == before[f] for f in context)


def test_protected_pixels_are_not_tested(tmp_path):
    from campfire_pipeline.nircam.outlier_detect import outlier_detect_for_visit

    files, hits = _scene(tmp_path)
    rows, cols = np.nonzero(hits[0][:, 1:])
    r, c = int(rows[0]), int(cols[0]) + 1            # one injected hit
    mask = np.zeros(hits[0].shape, bool)
    mask[max(r - 3, 0):r + 4, max(c - 3, 0):c + 4] = True
    outlier_detect_for_visit(files, files, plot=False, tempdir=str(tmp_path),
                             protect={files[0]: mask})
    with fits.open(files[0]) as hdul:
        dq = hdul['DQ'].data
        stamp = hdul[0].header['CFP_OUT']
    assert not ((dq[mask] & _OUTLIER) != 0).any()
    assert ((dq & _OUTLIER) != 0).sum() >= 8         # the other hits still are
    assert 'star_protect' in stamp and 'not tested' in stamp


def test_one_exposure_alone_gets_a_stamp_and_no_flags(tmp_path):
    from campfire_pipeline.nircam.outlier_detect import outlier_detect_for_visit

    (path,), _ = _scene(tmp_path, n=1)
    outlier_detect_for_visit([path], [path], plot=False, tempdir=str(tmp_path))
    with fits.open(path) as hdul:
        assert ((hdul['DQ'].data & _OUTLIER) != 0).sum() == 0
        assert 'CFP_OUT' in hdul[0].header


def test_two_exposures_keep_the_clean_pixel_under_a_hit(tmp_path):
    """Where only two exposures overlap, the median is their mean: a strong
    hit in one raises it by half the hit, and the clean pixel of the other
    then stands as far below the comparison as the hit stands above it. The
    shared code takes the minimum there, so the hit is flagged and the clean
    pixel is kept."""
    from campfire_pipeline.nircam.outlier_detect import outlier_detect_for_visit

    files, hits = _scene(tmp_path, n=2)
    outlier_detect_for_visit(files, files, plot=False, tempdir=str(tmp_path))
    flagged = []
    for path in files:
        with fits.open(path) as hdul:
            flagged.append((hdul['DQ'].data & _OUTLIER) != 0)
    for mine, cr in zip(flagged, hits):
        testable = np.zeros_like(cr)
        testable[8:-8, 8:-8] = cr[8:-8, 8:-8]
        assert mine[testable].mean() > 0.9
    # pixels flagged away from any hit of their own exposure: the clean
    # counterparts of the other exposure's hits would show up here
    from scipy.ndimage import binary_dilation
    for mine, cr in zip(flagged, hits):
        near = binary_dilation(cr, structure=np.ones((3, 3), bool))
        assert (mine & ~near).sum() <= 5
