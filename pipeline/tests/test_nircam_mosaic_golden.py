"""Golden-pixel test of the production NIRCam combine path.

Four small synthetic exposures (real ``ImageModel`` FITS with a persisted
JWST-structured gwcs, compact sources, noise and cosmic rays) go through the
two steps every production field runs in the combine phase:

1. ``outlier_detect_for_visit`` — the campfire-native per-visit outlier step;
2. ``drizzle_tile`` — the campfire-native tile drizzle.

The resulting mosaic planes (SCI, ERR, WHT, EXPTIME, NEXP) and each input's
OUTLIER mask are compared with a stored reference,
``data/nircam_mosaic_golden.npz``.

Why it exists: these two modules are about to be generalized so HST exposures
can share them (docs/hst/design.md, section 3.3). The unit tests around them
check helpers and contracts; nothing else pins the pixels that come out. Two
comparisons are made:

* **Tolerance** (always): float planes agree to ``rtol=1e-5``, with identical
  NaN patterns; integer planes and outlier masks are equal. This is what CI
  checks on any platform.
* **Bit-identical** (only on the platform that wrote the reference): SHA-256 of
  every array equals the stored digest. A refactor that is meant to move code
  without changing arithmetic must keep this passing on the machine it is
  developed on.

Regenerate the reference (only when a change to the pixels is intended, and
say so in the changelog)::

    CAMPFIRE_GOLDEN_UPDATE=1 pytest pipeline/tests/test_nircam_mosaic_golden.py

Keep the produced files for inspection or for ``scripts/mosaic_baseline.py``::

    CAMPFIRE_GOLDEN_KEEP=/some/dir pytest pipeline/tests/test_nircam_mosaic_golden.py
"""

import hashlib
import os
import platform
import shutil
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from _align_gwcs import HAVE_PERSISTABLE_WCS, make_persistable_wcs

pytest.importorskip("drizzle.resample")
pytestmark = pytest.mark.skipif(
    not HAVE_PERSISTABLE_WCS, reason="needs jwst + gwcs for a persistable WCS")

GOLDEN = Path(__file__).parent / 'data' / 'nircam_mosaic_golden.npz'

# --- scene -------------------------------------------------------------------
_NY = _NX = 96                    # input "detector"
_IN_SCALE = 0.031                 # arcsec / pixel
_TILE = dict(crval=(80.0, -30.0), crpix=(74.5, 74.5), shape=(150, 150),
             rotation=0.0, pixel_scale=0.03)
_SIGMA = 0.02                     # per-pixel background noise (MJy/sr)
_DITHERS = [(0.0, 0.0), (0.093, 0.047), (-0.062, 0.109), (0.031, -0.078)]  # arcsec
_FLOAT_PLANES = ('sci', 'err', 'wht', 'exptime')
_PLATFORM = f'{platform.system()}-{platform.machine()}'


def _sregion(w):
    xs = np.array([-0.5, _NX - 0.5, _NX - 0.5, -0.5])
    ys = np.array([-0.5, -0.5, _NY - 0.5, _NY - 0.5])
    ra, dec = w(xs, ys, with_bounding_box=False)
    return 'POLYGON ICRS ' + ' '.join(f'{a:.9f} {d:.9f}' for a, d in zip(ra, dec))


def _build_exposure(path, index, rng, sky_sources):
    """One synthetic cal-stage exposure. Returns the pixels given cosmic rays."""
    from jwst.datamodels import ImageModel

    dra, ddec = _DITHERS[index]
    cosd = np.cos(np.deg2rad(-30.0))
    ra_ref = 80.0 + dra / 3600.0 / cosd
    dec_ref = -30.0 + ddec / 3600.0
    w = make_persistable_wcs(ra_ref=ra_ref, dec_ref=dec_ref,
                             crpix=(_NX / 2, _NY / 2),
                             scale=_IN_SCALE / 3600.0)
    w.bounding_box = ((-0.5, _NX - 0.5), (-0.5, _NY - 0.5))
    w.array_shape = (_NY, _NX)

    yy, xx = np.mgrid[:_NY, :_NX]
    sci = np.zeros((_NY, _NX))
    for ra, dec, flux in sky_sources:
        x0, y0 = w.invert(ra, dec, with_bounding_box=False)
        sci += flux * np.exp(-0.5 * ((xx - x0) ** 2 + (yy - y0) ** 2) / 1.3 ** 2)
    var_poisson = 1e-4 * np.clip(sci, 0, None)
    sci = sci + rng.normal(0.0, _SIGMA, sci.shape)

    # Cosmic rays: isolated hits plus one short streak, different per exposure.
    cr = np.zeros((_NY, _NX), bool)
    cr[rng.integers(4, _NY - 4, 12), rng.integers(4, _NX - 4, 12)] = True
    r, c = rng.integers(10, _NY - 10), rng.integers(10, _NX - 10)
    cr[r, c:c + 4] = True
    sci[cr] += 60 * _SIGMA

    dq = np.zeros((_NY, _NX), 'uint32')
    dq[:, 0] = 1                                  # a DO_NOT_USE column
    var_rnoise = np.full((_NY, _NX), _SIGMA ** 2)
    var_poisson[5, 7] = np.inf                    # exercises variance masking
    var_flat = np.zeros((_NY, _NX))

    m = ImageModel((_NY, _NX))
    m.data = sci.astype('float32')
    m.dq = dq
    m.var_rnoise = var_rnoise.astype('float32')
    m.var_poisson = var_poisson.astype('float32')
    m.var_flat = var_flat.astype('float32')
    with np.errstate(invalid='ignore'):
        m.err = np.sqrt(var_rnoise + var_poisson + var_flat).astype('float32')
    for k, v in [('v2_ref', 120.0), ('v3_ref', 500.0), ('roll_ref', 30.0),
                 ('ra_ref', ra_ref), ('dec_ref', dec_ref),
                 ('v3yangle', 0.0), ('vparity', -1)]:
        setattr(m.meta.wcsinfo, k, v)
    m.meta.wcsinfo.s_region = _sregion(w)
    m.meta.wcs = w
    m.meta.exposure.exposure_time = 1000.0
    m.meta.exposure.effective_exposure_time = 900.0 + 10.0 * index
    m.meta.photometry.pixelarea_arcsecsq = _IN_SCALE ** 2
    m.meta.photometry.pixelarea_steradians = (
        _IN_SCALE ** 2 * (np.pi / (180.0 * 3600.0)) ** 2)
    m.meta.bunit_data = 'MJy/sr'
    m.meta.bunit_err = 'MJy/sr'
    m.meta.filename = os.path.basename(path)
    m.save(path)
    m.close()
    return cr


def _run(workdir):
    """Build the scene, run outlier + drizzle, return ``{name: array}``."""
    from jwst.datamodels.dqflags import pixel as pixel_flags

    from campfire_pipeline.nircam.drizzle import drizzle_tile
    from campfire_pipeline.nircam.outlier_detect import outlier_detect_for_visit

    rng = np.random.default_rng(20261111)
    cosd = np.cos(np.deg2rad(-30.0))
    sky_sources = [
        (80.0 + dx / 3600.0 / cosd, -30.0 + dy / 3600.0, f)
        for dx, dy, f in zip(rng.uniform(-1.1, 1.1, 10),
                             rng.uniform(-1.1, 1.1, 10),
                             rng.uniform(0.3, 3.0, 10))
    ]
    files = []
    for i in range(len(_DITHERS)):
        path = os.path.join(workdir, f'jw01727001001_02101_0000{i + 1}_nrca1.fits')
        _build_exposure(path, i, rng, sky_sources)
        files.append(path)

    outlier_detect_for_visit(files, files, in_memory=True, plot=False,
                             tempdir=str(workdir))

    i2d = os.path.join(workdir, 'mosaic_nircam_f200w_golden_30mas_T_i2d.fits')
    drizzle_tile(files, i2d, reduction_version='golden', **_TILE)

    out = {}
    with fits.open(i2d) as hdul:
        out['sci'] = hdul['SCI'].data.astype('float32')
        out['err'] = hdul['ERR'].data.astype('float32')
        out['wht'] = hdul['WHT'].data.astype('float32')
        out['exptime'] = hdul['EXPTIME'].data.astype('float32')
        out['nexp'] = hdul['NEXP'].data.astype('int16')
    outlier_bit = int(pixel_flags['OUTLIER'])
    for i, f in enumerate(files):
        with fits.open(f) as hdul:
            out[f'outlier_{i}'] = (
                (hdul['DQ'].data & outlier_bit) != 0).astype('uint8')
    return out


def _digest(arr):
    return hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()


def test_combine_path_matches_golden(tmp_path):
    keep = os.environ.get('CAMPFIRE_GOLDEN_KEEP')
    workdir = Path(keep) if keep else tmp_path
    if keep:
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True)

    got = _run(str(workdir))

    # Sanity of the scene itself, independent of the stored reference: the
    # mosaic has coverage, and outlier detection found cosmic rays.
    assert np.isfinite(got['sci']).sum() > 5000
    assert got['nexp'].max() == len(_DITHERS)
    assert all(got[f'outlier_{i}'].sum() >= 8 for i in range(len(_DITHERS)))

    if os.environ.get('CAMPFIRE_GOLDEN_UPDATE'):
        GOLDEN.parent.mkdir(exist_ok=True)
        digests = {f'sha256_{k}': _digest(v) for k, v in got.items()}
        np.savez_compressed(GOLDEN, platform=_PLATFORM, **got, **digests)
        pytest.skip(f'golden reference rewritten for {_PLATFORM}')

    assert GOLDEN.exists(), (
        f'{GOLDEN} is missing; regenerate with CAMPFIRE_GOLDEN_UPDATE=1')
    ref = np.load(GOLDEN)

    for name in got:
        assert got[name].shape == ref[name].shape, name
        if name in _FLOAT_PLANES:
            assert np.array_equal(np.isnan(got[name]), np.isnan(ref[name])), (
                f'{name}: NaN pattern changed')
            np.testing.assert_allclose(got[name], ref[name], rtol=1e-5,
                                       atol=1e-8, equal_nan=True, err_msg=name)
        else:
            assert np.array_equal(got[name], ref[name]), f'{name} changed'

    if str(ref['platform']) == _PLATFORM:
        changed = [k for k, v in got.items()
                   if _digest(v) != str(ref[f'sha256_{k}'])]
        assert not changed, (
            f'not bit-identical to the golden reference on {_PLATFORM}: '
            f'{changed}')
