"""HST calibrate step: command construction, environment isolation, atomicity.

``crds`` and the ``hstcal`` executables are replaced by stand-ins; the real
chain was run by hand on ACS/WFC exposures (docs/hst/design.md, section 8b).
"""

import os
import subprocess

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.hst.steps import calibrate


def _raw(path, instrume='ACS', detector='WFC', rootname='jcal01aaq'):
    hdu = fits.PrimaryHDU()
    hdu.header.update({'INSTRUME': instrume, 'DETECTOR': detector,
                       'ROOTNAME': rootname})
    fits.HDUList([hdu, fits.ImageHDU(np.zeros((4, 4), 'int16'), name='SCI')]
                 ).writeto(path)


@pytest.fixture
def fake_tools(monkeypatch, tmp_path):
    """Record subprocess calls; make the 'calibration program' write a product."""
    calls = []
    state = {'fail': None, 'suffix': 'flc'}

    def fake_run(cmd, cwd=None, env=None, capture_output=False, text=False):
        calls.append({'cmd': list(cmd), 'cwd': cwd, 'env': dict(env)})
        kind = 'crds' if 'crds.bestrefs' in cmd else 'cal'
        if state['fail'] == kind:
            return subprocess.CompletedProcess(cmd, 1, stdout='boom', stderr='bad')
        if kind == 'cal':
            raw = os.path.join(cwd, cmd[-1])
            product = raw.replace('_raw.fits', f"_{state['suffix']}.fits")
            hdu = fits.PrimaryHDU()
            hdu.header['CAL_VER'] = '10.4.1 (08-Aug-2025)'
            hdu.writeto(product)
            fits.PrimaryHDU().writeto(raw.replace('_raw.fits', '_flt_extra.fits'))
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

    monkeypatch.setattr(calibrate.subprocess, 'run', fake_run)
    monkeypatch.setattr(calibrate.shutil, 'which', lambda name: f'/fake/bin/{name}')
    return calls, state


def test_acs_raw_becomes_a_stamped_flc(fake_tools, monkeypatch, tmp_path):
    calls, _ = fake_tools
    monkeypatch.setenv('CRDS_SERVER_URL', 'https://jwst-crds.stsci.edu')
    monkeypatch.setenv('CRDS_CONTEXT', 'jwst_1481.pmap')
    raw = str(tmp_path / 'jcal01aaq_raw.fits')
    _raw(raw)
    out_dir = tmp_path / 'acswfc-f606w'
    crds_path = tmp_path / 'crds_hst'
    (crds_path / 'references' / 'hst' / 'acs').mkdir(parents=True)

    path = calibrate.calibrate_step(raw, str(out_dir), crds_context='hst_1372.pmap',
                                    crds_path=str(crds_path), nthreads=4)

    assert path == str(out_dir / 'jcal01aaq_flc.fits') and os.path.exists(path)
    assert cfp.step_value(path, 'CFP_CAL', keyset=cfp.HST) == 'raw 10.4.1 hst_1372.pmap'

    crds_call, cal_call = calls
    # reference selection: pinned HST context, HST server, HST cache ...
    assert '--new-context' in crds_call['cmd'] and 'hst_1372.pmap' in crds_call['cmd']
    assert '--update-bestrefs' in crds_call['cmd']
    assert crds_call['env']['CRDS_SERVER_URL'] == calibrate.HST_CRDS_SERVER
    assert crds_call['env']['CRDS_CONTEXT'] == 'hst_1372.pmap'
    assert crds_call['env']['CRDS_PATH'] == str(crds_path)
    # ... and only inside the subprocess: the pipeline's JWST settings survive
    assert os.environ['CRDS_SERVER_URL'] == 'https://jwst-crds.stsci.edu'
    assert os.environ['CRDS_CONTEXT'] == 'jwst_1481.pmap'
    assert 'jref' not in os.environ

    # calibration: calacs with an explicit thread count and the jref directory
    assert cal_call['cmd'][:3] == ['/fake/bin/calacs.e', '--nthreads', '4']
    assert cal_call['cmd'][-1] == 'jcal01aaq_raw.fits'
    assert cal_call['env']['jref'] == os.path.join(
        str(crds_path), 'references', 'hst', 'acs', '')

    # nothing but the canonical file is left behind, and the RAW is untouched
    assert sorted(os.listdir(out_dir)) == ['jcal01aaq_flc.fits']
    assert os.path.exists(raw)

    # stamped: a second call does not run anything
    assert calibrate.calibrate_step(raw, str(out_dir), crds_context='hst_1372.pmap',
                                    crds_path=str(crds_path)) == path
    assert len(calls) == 2


def test_wfc3ir_uses_calwf3_and_iref(fake_tools, tmp_path):
    calls, state = fake_tools
    state['suffix'] = 'flt'
    raw = str(tmp_path / 'ical01aaq_raw.fits')
    _raw(raw, 'WFC3', 'IR', 'ical01aaq')
    crds_path = tmp_path / 'crds_hst'
    (crds_path / 'references' / 'hst' / 'iref').mkdir(parents=True)
    path = calibrate.calibrate_step(raw, str(tmp_path / 'out'),
                                    crds_context='hst_1372.pmap',
                                    crds_path=str(crds_path), nthreads=4)
    assert path.endswith('ical01aaq_flt.fits')
    cal = calls[1]
    assert cal['cmd'] == ['/fake/bin/calwf3.e', 'ical01aaq_raw.fits']   # no --nthreads
    assert cal['env']['iref'].endswith(os.path.join('hst', 'iref', ''))


def test_keep_intermediates_moves_the_other_products(fake_tools, tmp_path):
    raw = str(tmp_path / 'jcal01aaq_raw.fits')
    _raw(raw)
    out_dir = tmp_path / 'out'
    calibrate.calibrate_step(raw, str(out_dir), crds_context='hst_1372.pmap',
                             crds_path=str(tmp_path), keep_intermediates=True)
    assert sorted(os.listdir(out_dir)) == ['jcal01aaq_flc.fits',
                                           'jcal01aaq_flt_extra.fits']


@pytest.mark.parametrize('failing', ['crds', 'cal'])
def test_a_failing_subprocess_leaves_no_product(fake_tools, tmp_path, failing):
    _, state = fake_tools
    state['fail'] = failing
    raw = str(tmp_path / 'jcal01aaq_raw.fits')
    _raw(raw)
    out_dir = tmp_path / 'out'
    with pytest.raises(RuntimeError, match='failed'):
        calibrate.calibrate_step(raw, str(out_dir), crds_context='hst_1372.pmap',
                                 crds_path=str(tmp_path))
    assert os.listdir(out_dir) == []


def test_a_crds_server_failure_is_retried_and_other_failures_are_not(monkeypatch):
    waits, calls = [], []
    monkeypatch.setattr(calibrate.time, 'sleep', waits.append)

    def flaky(cmd, *, cwd, env, what):
        calls.append(cmd)
        if len(calls) < 3:
            raise RuntimeError("crds failed (exit 1):\nServiceError: CRDS jsonrpc "
                               "failure 'get_mapping_names' HTTP Error 504: "
                               "Gateway Time-out")
        return 'ok'
    monkeypatch.setattr(calibrate, '_run', flaky)
    assert calibrate._run_crds(['x'], cwd='.', env={}, what='crds') == 'ok'
    assert len(calls) == 3 and waits == [30.0, 60.0]

    def broken(cmd, *, cwd, env, what):
        calls.append(cmd)
        raise RuntimeError('crds failed (exit 1):\nno match for this file')
    calls.clear()
    monkeypatch.setattr(calibrate, '_run', broken)
    with pytest.raises(RuntimeError, match='no match'):
        calibrate._run_crds(['x'], cwd='.', env={}, what='crds')
    assert len(calls) == 1                         # not the server's fault


def test_requirements_are_explicit(monkeypatch, tmp_path):
    raw = str(tmp_path / 'jcal01aaq_raw.fits')
    _raw(raw)
    with pytest.raises(ValueError, match='crds_context'):
        calibrate.calibrate_step(raw, str(tmp_path / 'o'), crds_context=None,
                                 crds_path=str(tmp_path))
    monkeypatch.setattr(calibrate.shutil, 'which', lambda name: None)
    with pytest.raises(RuntimeError, match='hstcal'):
        calibrate.calibrate_step(raw, str(tmp_path / 'o'),
                                 crds_context='hst_1372.pmap', crds_path=str(tmp_path))
    uvis = str(tmp_path / 'ical02aaq_raw.fits')
    _raw(uvis, 'WFC3', 'UVIS', 'ical02aaq')
    with pytest.raises(ValueError, match='Unsupported'):
        calibrate.calibrate_step(uvis, str(tmp_path / 'o'),
                                 crds_context='hst_1372.pmap', crds_path=str(tmp_path))
