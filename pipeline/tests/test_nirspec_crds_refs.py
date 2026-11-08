"""The boundary with jwst's path-loss and bar-shadow references (``nirspec.crds_refs``) and the manifest of
CAMPFIRE's own calibrations (``data/nirspec_calibrations.toml``).

* The path-loss reader checks the assumptions the profile fit makes about the reference and names the one
  that fails; the y ratio and the separability measure are exact on a synthetic product cube.
* The switches hand each direction of the path loss back to jwst: with ``slit_loss_y = "jwst"`` a point
  source at its planned position gets jwst's path loss exactly; with ``slit_loss_x = "jwst"`` the
  correction along the dispersion is left out.
* Every shipped calibration file is in the manifest with its bytes' sha256 and its own version, every
  switch the manifest names exists in the default config, and a reduction with another path-loss
  reference than a calibration was derived against is reported.
* With the reference on disk (``crds_reference``), the live file still meets the assumptions and
  ``compare`` runs (the standing comparison a CRDS_CONTEXT bump should be checked with)."""

import hashlib
import json
import tomllib
import warnings
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nirspec import crds_refs as CR
from campfire_pipeline.nirspec import optics as O
from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH

from optics_standin import Analytic
from test_nirspec_profile_fit import _exposures

DATA = Path(CR.__file__).parent.parent / 'data'
DEFAULT_CONFIG = DATA / 'config_default.toml'


# ---------------------------------------------------------------------------
# the path-loss reference
# ---------------------------------------------------------------------------
def _pathloss_file(tmp_path, monkeypatch, y0=-1.5, aperture='MOS1x3', ny=91, separable=True):
    """A path-loss reference in jwst's layout: P(lam, y, x) = a(lam) * gy(y) * gx(x) (plus a cross term
    when not separable), y and x in shutter pitches, wavelengths in metres."""
    lam = 0.6e-6 + 0.27e-6 * np.arange(21)
    y = y0 + (1.0 / 30) * np.arange(ny)
    x = -0.5 + (1.0 / 30) * np.arange(31)
    gy = np.exp(-0.5 * (y / 0.4) ** 2)
    gx = np.exp(-0.5 * (x / 0.5) ** 2)
    a = 0.9 - 0.05 * lam * 1e6
    cube = a[:, None, None] * gy[None, :, None] * gx[None, None, :]
    if not separable:
        cube = cube * np.exp(0.2 * y[None, :, None] * x[None, None, :])
    hdr = fits.Header()
    for n, (v0, dv, size) in enumerate(((x[0], x[1] - x[0], x.size), (y[0], y[1] - y[0], y.size),
                                        (lam[0], lam[1] - lam[0], lam.size)), start=1):
        hdr[f'CRVAL{n}'], hdr[f'CDELT{n}'] = v0, dv
    hdr['APERTURE'] = aperture
    h1 = fits.ImageHDU(np.ones((21, 31, 31)), name='PS')
    h1.header['APERTURE'] = 'MOS1x1'
    h1.ver = 1
    h2 = fits.ImageHDU(cube, header=hdr, name='PS')
    h2.ver = 2
    path = tmp_path / 'jwst_nirspec_pathloss_9999.fits'
    fits.HDUList([fits.PrimaryHDU(), h1, h2]).writeto(path)
    monkeypatch.setattr(CR, '_locate', lambda name: str(path))
    CR.pathloss_cube.cache_clear()
    return path.name, lam * 1e6, y, gy


def test_point_y_ratio_divides_out_the_y_dependence(tmp_path, monkeypatch):
    name, lam, y, gy = _pathloss_file(tmp_path, monkeypatch)
    for srcy in (0.0, 0.2, -0.35):
        r = CR.point_y_ratio(name, lam[[2, 10]], srcy)
        assert r == pytest.approx(1.0 / np.interp(srcy, y, gy), rel=2e-3)
    lam_s, sep = CR.separability(name)
    assert sep.max() < 1e-9


def test_separability_sees_a_cross_term(tmp_path, monkeypatch):
    name, *_ = _pathloss_file(tmp_path, monkeypatch, separable=False)
    assert CR.separability(name)[1].max() > 0.02


@pytest.mark.parametrize('kw, says', [(dict(aperture='MOS1x1'), 'APERTURE'), (dict(y0=-0.5, ny=31), 'y runs')])
def test_the_reader_names_a_broken_assumption(tmp_path, monkeypatch, kw, says):
    name, *_ = _pathloss_file(tmp_path, monkeypatch, **kw)
    with pytest.raises(ValueError, match=says):
        CR.pathloss_cube(name)


def test_point_to_uniform():
    p, u = np.full(3, 0.8), np.full(3, 0.5)
    assert np.allclose(CR.point_to_uniform('POINT', p, u), 1.6)
    assert CR.point_to_uniform('UNIFORM', p, u) is None
    assert CR.point_to_uniform('POINT', None, u) is None


def test_slit_position_in_slit_units():
    assert CR.slit_position(0.2) == pytest.approx(0.2 * MSA_SLIT_PITCH)


# ---------------------------------------------------------------------------
# the switches
# ---------------------------------------------------------------------------
def test_slit_loss_from_config_and_bad_values():
    assert CR.SlitLoss.from_config({}).is_default
    assert CR.SlitLoss.from_config({'slit_loss_y': 'JWST', 'slit_loss_x': 'jwst'}) == CR.SlitLoss('jwst', 'jwst')
    with pytest.raises(ValueError, match='slit_loss_y'):
        CR.SlitLoss(y='crds')
    opts = PF.load_options({'bar_offsets': {'PRISM/CLEAR': {1: 0.01}}, 'slit_loss_x': 'jwst'})
    assert opts['bar_offsets'] == {'PRISM/CLEAR': {1: 0.01}} and opts['slit_loss'] == CR.SlitLoss(x='jwst')


class _Corrected(Analytic):
    def x_correction(self, lam, srcx):
        return np.full(np.shape(lam), 0.9)


def _tau(exps, sl, p):
    for e in exps:
        e.slit_loss = sl
        e.src_y_jwst, e.pl_ref, e.src_x_jwst = e.src_y / MSA_SLIT_PITCH, 'ref.fits', 0.2
    M = PF.ProfileModel(exps, optics=[_Corrected()] * 3)
    lam, dy = exps[0].wav[:, 40], exps[0].dy[:, 40]
    return M, lam, M.throughput(p, np.zeros(3), 0, lam, dy)


def test_slit_loss_y_jwst_gives_a_point_source_jwsts_loss(monkeypatch):
    monkeypatch.setattr(CR, 'point_y_ratio', lambda ref, lam, srcy: np.full(np.shape(lam), 1.25))
    p = np.array([0.25, 0.0, np.log(PF.W_FLOOR), 0.0])            # a point at its planned position
    M, lam, tau = _tau(_exposures(src_y=0.25), CR.SlitLoss(y='jwst', x='jwst'), p)
    t_here = Analytic().transmission(PF.REF_APERTURE, lam, 0.25, PF.W_FLOOR)
    assert np.allclose(tau * t_here, 0.8)                           # jwst's point loss there, exactly
    M, lam, tau_o = _tau(_exposures(src_y=0.25), CR.SlitLoss(), p)
    t0 = Analytic().transmission(PF.REF_APERTURE, lam, 0.0, PF.W_FLOOR)
    assert np.allclose(tau_o, 0.8 * 1.25 * 0.9 / t0)               # the default: moved to the centre, corrected


def test_slit_loss_x_jwst_leaves_out_the_correction(monkeypatch):
    monkeypatch.setattr(CR, 'point_y_ratio', lambda ref, lam, srcy: np.ones(np.shape(lam)))
    p = np.array([0.1, 0.0, np.log(0.05), 0.0])
    _, _, a = _tau(_exposures(), CR.SlitLoss(x='corrected'), p)
    _, _, b = _tau(_exposures(), CR.SlitLoss(x='jwst'), p)
    assert np.allclose(a, 0.9 * b)


def test_header_cards_record_the_choices(monkeypatch):
    from campfire_pipeline.nirspec import profile_chain as PC
    monkeypatch.setattr(CR, 'point_y_ratio', lambda ref, lam, srcy: np.ones(np.shape(lam)))
    exps = _exposures()
    for e in exps:
        e.slit_loss, e.pl_ref = CR.SlitLoss(y='jwst'), 'jwst_nirspec_pathloss_0010.fits'
    M = PF.ProfileModel(exps)
    R = PF.FitResult(params=dict(centre=0.1, centre_prior=0.1, drift=0.0, width=0.1, width_change=0.0, pivot=M.pivot),
                     eoff=np.zeros(3), lam=M.lam_grid, flux=np.ones(M.lam_grid.size), flux_err=np.ones(M.lam_grid.size),
                     chi2=0.0, npix=0, nfree=0, chromatic=True, model=M)
    c = PC.ChainResult(result=R, shape='own', chain='gated', reason='test').header_cards()
    assert c['PFSLY'][0] == 'jwst' and c['PFSLX'][0] == 'corrected'
    assert c['PFPLREF'][0] == 'jwst_nirspec_pathloss_0010.fits'


# ---------------------------------------------------------------------------
# the manifest
# ---------------------------------------------------------------------------
def test_every_calibration_file_is_in_the_manifest_with_its_bytes():
    man = CR.manifest()
    listed = {e['file'] for e in man.values() if e.get('file')}
    shipped = {p.name for p in DATA.glob('empirical_jwst_nirspec_barshadow_*.fits')} | {'nirspec_optics.json'}
    assert shipped <= listed, f'not in data/nirspec_calibrations.toml: {sorted(shipped - listed)}'
    for name, e in man.items():
        if e.get('file'):
            digest = hashlib.sha256((DATA / e['file']).read_bytes()).hexdigest()
            assert digest == e['sha256'], f'{name}: {e["file"]} changed; re-derive and update its manifest entry'


def test_manifest_versions_are_the_files_own():
    from campfire_pipeline.nirspec import bar_offset as BO
    man = CR.manifest()
    assert man['optics']['version'] == json.loads((DATA / 'nirspec_optics.json').read_text())['meta']['version']
    assert man['bar_offset']['version'] == BO.MEASURE_VERSION
    for name, e in man.items():
        if name.startswith('barshadow_'):
            assert e['version'] == fits.getheader(DATA / e['file'])['CFBSDATE']


def test_every_switch_named_exists_in_the_default_config():
    import re
    cfg = tomllib.loads(DEFAULT_CONFIG.read_text())['nirspec']
    for name, e in CR.manifest().items():
        if e['switch'].startswith('none:'):            # always used (the bar-shadow tables)
            continue
        section = re.search(r'\[nirspec\.(\w+)\]', e['switch']).group(1)
        for setting in re.findall(r'(\w+)\s*=', e['switch']):
            assert setting in cfg[section], f'{name}: {setting} not in [nirspec.{section}]'


def test_another_pathloss_reference_is_reported():
    assert CR.check_references('jwst_nirspec_pathloss_0010.fits', warn=False) == []
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter('always')
        msgs = CR.check_references('crds://jwst_nirspec_pathloss_0012.fits')
    assert msgs and 'optics' in msgs[0] and '0012' in msgs[0]
    assert any('nirspec_calibrations' in str(x.message) for x in w)


# ---------------------------------------------------------------------------
# the live reference (only where CRDS has it on disk)
# ---------------------------------------------------------------------------
@pytest.fixture
def crds_reference():
    """The path-loss reference the optics calibration was derived against, from the local CRDS cache;
    skips when it is not on disk (no download in tests)."""
    import os
    ref = CR.depends_on('pathloss')['optics']
    try:
        path = CR._locate(ref)
    except Exception as e:                       # noqa: BLE001 - no CRDS configuration
        pytest.skip(f'CRDS not configured: {e}')
    if not os.path.exists(path):
        pytest.skip(f'{ref} not in the local CRDS cache')
    CR.pathloss_cube.cache_clear()
    return ref


def test_live_reference_meets_the_assumptions(crds_reference):
    r = CR.compare(crds_reference)               # against the stand-in model here: the reference side is the test
    assert r['separability_max'] < 0.01          # point_y_ratio's x-times-y assumption, to 1 %
    for lam, rows in r['y_profile'].items():
        assert rows[0.1]['jwst'] == pytest.approx(1.0, abs=0.1)
    assert CR.compare_text(r)
