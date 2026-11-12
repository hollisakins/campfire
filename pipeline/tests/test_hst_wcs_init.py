"""HST wcs_init step: reference environment handling, stamping, skip logic.

``stwcs.updatewcs`` itself needs HST reference files, so it is replaced here by
a stand-in that writes what the real one writes (a WCSNAME and a SIP order).
The real call was exercised by hand on an ACS/WFC exposure (docs/hst/design.md,
section 8b).
"""

import os

import numpy as np
import pytest
from astropy.io import fits

pytest.importorskip('stwcs')

from campfire_pipeline.common import cfp  # noqa: E402
from campfire_pipeline.hst.steps import wcs_init  # noqa: E402


def _flc(path):
    hdul = fits.HDUList([fits.PrimaryHDU(),
                         fits.ImageHDU(np.zeros((4, 4), 'float32'), name='SCI', ver=1)])
    hdul.writeto(path)


def test_reference_environment_is_scoped(monkeypatch, tmp_path):
    monkeypatch.setenv('jref', '/somewhere/else/')
    monkeypatch.delenv('iref', raising=False)
    with wcs_init.reference_environment({'jref': tmp_path, 'iref': str(tmp_path)}):
        assert os.environ['jref'] == os.path.join(str(tmp_path), '')
        assert os.environ['iref'].endswith(os.sep)
    assert os.environ['jref'] == '/somewhere/else/'      # restored
    assert 'iref' not in os.environ                      # removed again


def test_step_runs_updatewcs_without_the_database_and_stamps(monkeypatch, tmp_path):
    path = str(tmp_path / 'jtest01aaq_flc.fits')
    _flc(path)
    calls = []

    def fake_updatewcs(filename, use_db=True):
        calls.append((filename, use_db, os.environ.get('jref')))
        with fits.open(filename, mode='update') as hdul:
            hdul['SCI', 1].header['WCSNAME'] = 'IDC_4bb1536mj'
            hdul['SCI', 1].header['A_ORDER'] = 5

    import stwcs.updatewcs
    monkeypatch.setattr(stwcs.updatewcs, 'updatewcs', fake_updatewcs)
    monkeypatch.delenv('jref', raising=False)

    name = wcs_init.wcs_init_step(path, reference_env={'jref': tmp_path})
    assert name == 'IDC_4bb1536mj'
    assert calls == [(path, False, os.path.join(str(tmp_path), ''))]
    assert 'jref' not in os.environ
    assert cfp.step_value(path, 'CFP_WCS', keyset=cfp.HST) == 'IDC_4bb1536mj'

    # stamped: a second call does nothing
    assert wcs_init.wcs_init_step(path, reference_env={'jref': tmp_path}) is None
    assert len(calls) == 1
    wcs_init.wcs_init_step(path, reference_env={'jref': tmp_path}, overwrite=True)
    assert len(calls) == 2


def test_step_refuses_a_file_left_without_distortion(monkeypatch, tmp_path):
    path = str(tmp_path / 'jtest01aaq_flc.fits')
    _flc(path)
    import stwcs.updatewcs
    monkeypatch.setattr(stwcs.updatewcs, 'updatewcs', lambda f, use_db=True: None)
    with pytest.raises(RuntimeError, match='no distortion'):
        wcs_init.wcs_init_step(path)
    assert not cfp.has_step(path, 'CFP_WCS', keyset=cfp.HST)
