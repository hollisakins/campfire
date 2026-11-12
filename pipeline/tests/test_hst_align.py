"""HST align step on synthetic exposures: solve, write-back, backup, re-run.

Two ACS-shaped exposures (two chips each) with compact sources rendered through
a true WCS; the headers carry a WCS that is off by a known shift and roll, and
the second exposure by a further 30 mas of its own. The reference catalog is the
truth.
"""

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table
from astropy.wcs import WCS

pytest.importorskip('tweakwcs')
pytest.importorskip('sep')

from campfire_pipeline.common import cfp  # noqa: E402
from campfire_pipeline.hst.exposure import open_exposure  # noqa: E402
from campfire_pipeline.hst.steps.align import (  # noqa: E402
    ALIGN_SUFFIX, _BACKUP, align_visit,
)

_NX, _NY, _S, _T = 500, 400, 0.05, 500.0
_ERR = dict(dra=0.35, ddec=-0.20, roll=0.02)        # arcsec, arcsec, degrees
_EXTRA = (0.024, -0.018)                            # second exposure only, arcsec


def _cards(crpix, crval, roll_deg, perturbed, extra=(0.0, 0.0)):
    s = _S / 3600.0
    ra0, dec0 = crval
    if perturbed:
        ra0 += (_ERR['dra'] + extra[0]) / 3600.0 / np.cos(np.radians(dec0))
        dec0 += (_ERR['ddec'] + extra[1]) / 3600.0
        roll_deg += _ERR['roll']
    t = np.radians(roll_deg)
    return {
        'CTYPE1': 'RA---TAN-SIP', 'CTYPE2': 'DEC--TAN-SIP',
        'CRPIX1': crpix[0], 'CRPIX2': crpix[1], 'CRVAL1': ra0, 'CRVAL2': dec0,
        'CD1_1': -s * np.cos(t), 'CD1_2': s * np.sin(t),
        'CD2_1': s * np.sin(t), 'CD2_2': s * np.cos(t),
        'A_ORDER': 2, 'B_ORDER': 2, 'A_2_0': 1e-6, 'A_0_2': 0.0, 'A_1_1': 0.0,
        'B_2_0': 0.0, 'B_0_2': 1e-6, 'B_1_1': 0.0,
    }


def _true_wcs(crpix, crval):
    h = dict(_cards(crpix, crval, 20.0, perturbed=False), NAXIS=2,
             NAXIS1=_NX, NAXIS2=_NY)
    return WCS(h)


def _exposure(path, rootname, dither, rng, sky_sources, extra=(0.0, 0.0)):
    """Write one two-chip exposure; headers carry the perturbed WCS."""
    crval = (40.0 + dither[0] / 3600.0, 26.0 + dither[1] / 3600.0)
    primary = fits.PrimaryHDU()
    primary.header.update({'INSTRUME': 'ACS', 'DETECTOR': 'WFC',
                           'ROOTNAME': rootname, 'FILTER1': 'CLEAR1L',
                           'FILTER2': 'F814W', 'EXPTIME': _T,
                           'EXPSTART': 59000.0, 'EXPEND': 59000.006})
    hdul = fits.HDUList([primary])
    yy, xx = np.mgrid[:_NY, :_NX]
    for extver, crpix in ((1, (250.0, 420.0)), (2, (250.0, -20.0))):
        w = _true_wcs(crpix, crval)
        x, y = w.all_world2pix(sky_sources[:, 0], sky_sources[:, 1], 0)
        sci = np.full((_NY, _NX), 50.0) + rng.normal(0, 7.0, (_NY, _NX))
        for xs, ys, f in zip(x, y, sky_sources[:, 2]):
            if -5 < xs < _NX + 5 and -5 < ys < _NY + 5:
                sci += f * np.exp(-0.5 * ((xx - xs) ** 2 + (yy - ys) ** 2) / 1.1 ** 2)
        err = np.sqrt(np.clip(sci, 0, None) + 16.0)
        for name, data in (('SCI', sci.astype('float32')),
                           ('ERR', err.astype('float32')),
                           ('DQ', np.zeros((_NY, _NX), 'int16'))):
            hdu = fits.ImageHDU(data=data, name=name, ver=extver)
            if name == 'SCI':
                hdu.header.update(_cards(crpix, crval, 20.0, perturbed=True,
                                         extra=extra))
                hdu.header.update({'BUNIT': 'ELECTRONS', 'PHOTFLAM': 7.0e-20,
                                   'PHOTPLAM': 8045.0, 'IDCSCALE': 0.05,
                                   'WCSNAME': 'IDC_test', 'CFSKY': 50.0})
            hdul.append(hdu)
    hdul.writeto(path)
    return crval


@pytest.fixture
def visit(tmp_path):
    rng = np.random.default_rng(5)
    # sources over the ~25" x 40" footprint of the two chips
    n = 160
    dra = rng.uniform(-22, 22, n)
    ddec = rng.uniform(-26, 26, n)
    flux = rng.uniform(800, 6000, n)
    sky = np.column_stack([40.0 + dra / 3600.0 / np.cos(np.radians(26.0)),
                           26.0 + ddec / 3600.0, flux])
    files, crvals = [], {}
    for root, dither, extra in (('jal01aaq', (0.0, 0.0), (0.0, 0.0)),
                                ('jal01abq', (0.6, 0.35), _EXTRA)):
        path = str(tmp_path / f'{root}_flc.fits')
        crvals[path] = _exposure(path, root, dither, rng, sky, extra=extra)
        files.append(path)
    refcat = Table({'RA': sky[:, 0], 'DEC': sky[:, 1]})
    return files, refcat, crvals


def _offset_from_truth(path, crval):
    """Largest sky offset (arcsec) between the file's WCS and the true WCS,
    sampled over each chip."""
    worst = 0.0
    for chip, crpix in zip(open_exposure(path), ((250.0, 420.0), (250.0, -20.0))):
        x = np.array([20.0, 480.0, 20.0, 480.0, 250.0])
        y = np.array([20.0, 20.0, 380.0, 380.0, 200.0])
        ra, dec = chip.pixel_to_world(x, y)
        tra, tdec = _true_wcs(crpix, crval).all_pix2world(x, y, 0)
        off = np.hypot((ra - tra) * np.cos(np.radians(26.0)), dec - tdec) * 3600.0
        worst = max(worst, float(off.max()))
    return worst


def test_align_visit_corrects_and_records(visit):
    files, refcat, crvals = visit
    assert all(0.3 < _offset_from_truth(f, crvals[f]) < 0.6 for f in files)

    sol = align_visit(files, refcat, key='jal01', refcat_hash='abc123')
    assert sol.status == 'SOLVED'
    assert len(sol.detectors) == 4                 # 2 exposures x 2 chips, one pool

    tokens = []
    for f in files:
        assert _offset_from_truth(f, crvals[f]) < 0.005          # 5 mas
        value = cfp.step_value(f, 'CFP_ALGN', keyset=cfp.HST)
        assert value.startswith('dof=') and 'rc=abc123' in value
        tokens.append(value.split('rel=')[1].split()[0])
        with fits.open(f) as hdul:
            for v in (1, 2):
                h = hdul['SCI', v].header
                assert h['WCSNAME'] == 'IDC_test' + ALIGN_SUFFIX
                assert all(k in h for k in _BACKUP.values())
                # the backup holds the pre-alignment value, which was moved
                assert h[_BACKUP['CRVAL1']] != h['CRVAL1']
                assert h['A_2_0'] == 1e-6                # distortion untouched
    # one exposure is the reference, the other was tied to it on common sources
    assert 'ref' in tokens
    assert int(next(t for t in tokens if t != 'ref')) > 50

    # stamped: nothing to do
    assert align_visit(files, refcat, key='jal01') is None

    # a forced re-run starts from the backed-up WCS and lands in the same place
    with fits.open(files[0]) as hdul:
        first = [hdul['SCI', 1].header[k] for k in _BACKUP]
        backup = [hdul['SCI', 1].header[k] for k in _BACKUP.values()]
    align_visit(files, refcat, key='jal01', overwrite=True)
    with fits.open(files[0]) as hdul:
        h = hdul['SCI', 1].header
        np.testing.assert_allclose([h[k] for k in _BACKUP], first, rtol=0, atol=1e-9)
        assert [h[k] for k in _BACKUP.values()] == backup
        assert h['WCSNAME'].count(ALIGN_SUFFIX) == 1


def test_without_the_relative_stage_the_exposures_stay_apart(visit):
    """One rigid fit of the visit cannot remove an offset between its exposures.
    The exposure-to-exposure stage does, and so does the per-chip refinement
    when the reference catalog is dense and precise (as this synthetic one is)."""
    files, refcat, crvals = visit
    sol = align_visit(files, refcat, key='jal01',
                      config={'relative': False, 'fine_fitgeom': 'none'})
    assert sol.status == 'SOLVED'
    worst = [_offset_from_truth(f, crvals[f]) for f in files]
    assert max(worst) > 0.010                       # ~half of the 30 mas each
    for f in files:
        value = cfp.step_value(f, 'CFP_ALGN', keyset=cfp.HST)
        assert value.startswith('dof=coarse,coarse') and 'rel=0' in value

    # per-chip refinement (the default) on its own, against the same catalog
    sol = align_visit(files, refcat, key='jal01', config={'relative': False},
                      overwrite=True)
    assert sol.status == 'SOLVED'
    assert max(_offset_from_truth(f, crvals[f]) for f in files) < 0.005
    assert any(d.dof != 'coarse' for d in sol.detectors)


def test_sources_on_saturated_pixels_are_not_used(visit):
    """A star with a saturated core is detected as biased pieces of its wings;
    detection drops anything on or next to DQ 256."""
    from campfire_pipeline.hst.steps.align import _detect

    files, _, _ = visit
    chip = open_exposure(files[0])[0]
    before = _detect(chip, {})
    x0, y0 = float(before['x'][0]), float(before['y'][0])       # the brightest
    with fits.open(files[0], mode='update') as hdul:
        j, i = int(round(y0)), int(round(x0))
        hdul['DQ', chip.extver].data[j - 1:j + 2, i - 1:i + 2] = 256
    after = _detect(open_exposure(files[0])[0], {})
    near = np.hypot(np.asarray(after['x']) - x0, np.asarray(after['y']) - y0) < 6
    assert not near.any()
    assert len(after) >= len(before) - 2            # nothing else was lost
    # the cut can be switched off, and then the star's remains come back
    kept = _detect(open_exposure(files[0])[0], {'saturation_grow': -1})
    assert len(kept) > len(after)


def test_a_failed_solve_preserves_the_wcs_and_says_so(visit):
    files, refcat, crvals = visit
    far = Table({'RA': refcat['RA'] + 1.0, 'DEC': refcat['DEC']})   # 1 degree away
    with fits.open(files[0]) as hdul:
        before = [hdul['SCI', 1].header[k] for k in _BACKUP]
    sol = align_visit(files, far, key='jal01')
    assert sol.status == 'NOT_ALIGNED'
    for f in files:
        assert cfp.step_value(f, 'CFP_ALGN', keyset=cfp.HST) == cfp.NOT_ALIGNED
    with fits.open(files[0]) as hdul:
        h = hdul['SCI', 1].header
        assert [h[k] for k in _BACKUP] == before
        assert h['WCSNAME'] == 'IDC_test'
    # a NOT_ALIGNED visit is attempted again on a normal re-run
    assert align_visit(files, refcat, key='jal01').status == 'SOLVED'
