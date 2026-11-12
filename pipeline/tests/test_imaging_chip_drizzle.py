"""The instrument-neutral drizzle route (Chip objects on a FITS tile grid).

Three checks, all on the synthetic scene of ``test_nircam_mosaic_golden``:

* the plain-FITS :class:`TileGrid` is the same tangent plane as the gwcs the
  NIRCam route builds through ``stcal``, including a rotated tile;
* the same exposures wrapped as :class:`Chip` objects drizzle to the same
  mosaic as the JWST-datamodel route;
* the seam's small contracts (weights, no mixing of input kinds).

The chip used here reads a JWST ``ImageModel``. It exists only to compare the
two routes on identical inputs; it is not the NIRCam production path.
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


class ModelChip:
    """A Chip backed by a JWST ImageModel file (test adapter)."""

    instrument = 'nircam'
    filter = 'f200w'

    def __init__(self, path):
        from jwst.datamodels import ImageModel
        self.path = path
        self.key = os.path.basename(path).removesuffix('.fits')
        with ImageModel(path) as m:
            self.shape = m.data.shape
            self.exptime = float(m.meta.exposure.exposure_time)
            self.xposure = float(m.meta.exposure.effective_exposure_time)
            self.pixel_area_arcsec2 = float(m.meta.photometry.pixelarea_arcsecsq)
            self._wcs = m.meta.wcs

    def load(self):
        from jwst.datamodels import ImageModel

        from campfire_pipeline.imaging.exposure import ChipArrays
        with ImageModel(self.path) as m:
            return ChipArrays(
                sci=np.array(m.data, 'float32'),
                var_rnoise=np.array(m.var_rnoise, 'float32'),
                var_poisson=np.array(m.var_poisson, 'float32'),
                var_flat=np.array(m.var_flat, 'float32'),
                good=(np.array(m.dq) & 1) == 0,
            )

    def pixel_to_world(self, x, y):
        return self._wcs(x, y, with_bounding_box=False)

    def provenance(self):
        return {'key': self.key, 'exptime': self.exptime, 'filter': self.filter}


@pytest.fixture(scope='module')
def scene(tmp_path_factory):
    """The golden scene's four exposures, outlier-flagged, on disk."""
    from campfire_pipeline.nircam.outlier_detect import outlier_detect_for_visit

    workdir = tmp_path_factory.mktemp('chipscene')
    rng = np.random.default_rng(20261111)
    cosd = np.cos(np.deg2rad(-30.0))
    sources = [(80.0 + dx / 3600.0 / cosd, -30.0 + dy / 3600.0, f)
               for dx, dy, f in zip(rng.uniform(-1.1, 1.1, 10),
                                    rng.uniform(-1.1, 1.1, 10),
                                    rng.uniform(0.3, 3.0, 10))]
    files = []
    for i in range(len(golden._DITHERS)):
        path = str(workdir / f'jw01727001001_02101_0000{i + 1}_nrca1.fits')
        golden._build_exposure(path, i, rng, sources)
        files.append(path)
    outlier_detect_for_visit(files, files, in_memory=True, plot=False,
                             tempdir=str(workdir))
    return workdir, files


@pytest.mark.parametrize('rotation', [0.0, 20.0, -51.5])
def test_tile_grid_matches_the_stcal_grid(scene, rotation):
    from campfire_pipeline.imaging.drizzle import _build_output_wcs
    from campfire_pipeline.imaging.grid import TileGrid

    _, files = scene
    tile = dict(golden._TILE, rotation=rotation)
    gw = _build_output_wcs(files, tile['crpix'], tile['crval'], tile['shape'],
                           tile['rotation'], tile['pixel_scale'])
    grid = TileGrid(crval=tile['crval'], crpix=tile['crpix'],
                    shape=tile['shape'], rotation=rotation,
                    pixel_scale=tile['pixel_scale'])

    nx, ny = tile['shape']
    xs, ys = np.meshgrid(np.linspace(-0.5, nx - 0.5, 9),
                         np.linspace(-0.5, ny - 0.5, 9))
    ra, dec = gw(xs, ys, with_bounding_box=False)
    gx, gy = grid.world_to_pixel(ra, dec)
    # Measured agreement is ~4e-9 px; 1e-6 px is 30 nano-arcsec at 30 mas.
    assert np.max(np.hypot(gx - xs, gy - ys)) < 1e-6       # pixels
    # and the other direction
    ra2, dec2 = grid.pixel_to_world(xs, ys)
    sx, sy = gw.invert(ra2, dec2, with_bounding_box=False)
    assert np.max(np.hypot(sx - xs, sy - ys)) < 1e-6


def test_chip_route_reproduces_the_jwst_route(scene):
    from campfire_pipeline.imaging.drizzle import drizzle_tile

    workdir, files = scene
    jwst_out = str(workdir / 'jwst_i2d.fits')
    chip_out = str(workdir / 'chip_i2d.fits')
    drizzle_tile(files, jwst_out, reduction_version='t', **golden._TILE)
    drizzle_tile([ModelChip(f) for f in files], chip_out,
                 reduction_version='t', **golden._TILE)

    with fits.open(jwst_out) as a, fits.open(chip_out) as b:
        assert [h.name for h in b][:5] == ['PRIMARY', 'SCI', 'ERR', 'CON', 'WHT']
        assert 'EXPTIME' in b and 'NEXP' in b and 'INPUTS' in b
        assert len(b['INPUTS'].data) == len(files)
        assert b[0].header['BUNIT'] == 'MJy/sr'
        assert b[0].header['CMPFRVER'] == 't'
        np.testing.assert_allclose(b[0].header['PIXAR_A2'], 0.03 ** 2)

        # Same exposure bookkeeping, exactly.
        assert np.array_equal(a['NEXP'].data, b['NEXP'].data)
        np.testing.assert_array_equal(a['EXPTIME'].data, b['EXPTIME'].data)

        for ext in ('SCI', 'ERR', 'WHT'):
            x, y = a[ext].data, b[ext].data
            assert np.array_equal(np.isfinite(x), np.isfinite(y)), ext
            ok = np.isfinite(x)
            # The two grids agree to ~4e-9 px, so pixel values agree to
            # float32 rounding (measured: 2e-7 relative) but not bit for bit.
            np.testing.assert_allclose(y[ok], x[ok], rtol=1e-5, atol=1e-6,
                                       err_msg=ext)

        # The FITS WCS of the chip mosaic is the tile grid.
        from astropy.wcs import WCS
        w = WCS(b['SCI'].header)
        ra, dec = w.all_pix2world([[golden._TILE['crpix'][0],
                                    golden._TILE['crpix'][1]]], 0)[0]
        np.testing.assert_allclose([ra, dec], golden._TILE['crval'], atol=1e-9)


def test_chip_weight_and_input_rules():
    from campfire_pipeline.imaging.drizzle import _all_chips
    from campfire_pipeline.imaging.exposure import ChipArrays, chip_weight, is_chip

    var = np.array([[0.04, 0.0], [np.inf, 0.01]], 'float32')
    arrays = ChipArrays(sci=np.zeros((2, 2), 'float32'), var_rnoise=var,
                        var_poisson=np.zeros((2, 2), 'float32'),
                        var_flat=np.zeros((2, 2), 'float32'),
                        good=np.array([[True, True], [True, False]]))
    w = chip_weight(arrays, 'ivm')
    # usable variance -> 1/var; zero / non-finite variance or a bad pixel -> 0
    np.testing.assert_allclose(w, [[25.0, 0.0], [0.0, 0.0]])
    np.testing.assert_allclose(chip_weight(arrays, 'exptime', exptime=400.0),
                               [[400.0, 400.0], [400.0, 0.0]])
    with pytest.raises(ValueError):
        chip_weight(arrays, 'nope')

    assert not is_chip('/some/file.fits')
    assert _all_chips(['a.fits', 'b.fits']) is False

    class Fake:
        key = 'k'

        def load(self):
            return arrays

        def pixel_to_world(self, x, y):
            return x, y

    assert is_chip(Fake()) and _all_chips([Fake(), Fake()]) is True
    with pytest.raises(TypeError):
        _all_chips([Fake(), 'a.fits'])
