"""WFC3/IR persistence masks from the archive's per-exposure predictions
(``hst/steps/persist.py``).

The products are synthetic and laid out as the software that writes the real
ones lays them out: a copy of the calibrated exposure whose first extension
holds the prediction in e-/s, inside ``<proposal>.Visit<VV>.tar.gz``. The
"archive" is a directory read through a ``file://`` address, so nothing here
touches the network.
"""

import os
import shutil
import tarfile

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.config import get_hst_step_config
from campfire_pipeline.hst import orchestrate
from campfire_pipeline.hst.exposure import open_exposure
from campfire_pipeline.hst.field import HstField
from campfire_pipeline.hst.steps import persist

from test_hst_outlier import _ir_file

PROPOSAL = 12101
FIELDS = """
[tf]
tangent_point = [34.4, -5.2]

[tf.T1]
rotation = 0
[tf.T1.30mas]
crpix = [250.5, 250.5]
naxis = [500, 500]

[tf.wfc3ir]
filters = ["f160w"]
files = ["ites*"]

[tf.acswfc]
filters = ["f814w"]
files = ["jtes*"]
"""


@pytest.fixture
def field(tmp_path, monkeypatch):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'fields.toml').write_text(FIELDS)
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    f = HstField.load('tf', 'wfc3ir')
    f.setup_workspace()
    return f


def _exposure(directory, root, seed=5):
    """A calibrated exposure through the sky step, of program 12101."""
    path = _ir_file(os.path.join(directory, f'{root}_flt.fits'), root,
                    (40.0, 40.0), np.random.default_rng(seed), mjd=57000.0)
    with fits.open(path, mode='update') as hdul:
        hdul[0].header['PROPOSID'] = PROPOSAL
        for key, val in cfp.format(keyset=cfp.HST, CFP_SKY=None).items():
            hdul[0].header[key] = val
    return path


def _prediction(exposure_file, out_file, bright=(), faint=()):
    """The archive's product for an exposure: its copy, with the predicted
    persistence (e-/s) in the first extension."""
    shutil.copyfile(exposure_file, out_file)
    with fits.open(out_file, mode='update') as hdul:
        model = np.zeros(hdul[1].data.shape, dtype='float32')
        for y, x in bright:
            model[y, x] = 0.05
        for y, x in faint:
            model[y, x] = 0.005
        hdul[1].data = model
    return str(out_file)


def _archive(tmp_path, visit, members):
    """``<base>/<proposal>/Visit<VV>/<proposal>.Visit<VV>.tar.gz`` holding
    ``members`` (``{name inside the archive: file}``); returns the base
    address."""
    base = tmp_path / 'archive'
    d = base / str(PROPOSAL) / f'Visit{visit}'
    d.mkdir(parents=True)
    with tarfile.open(d / f'{PROPOSAL}.Visit{visit}.tar.gz', 'w:gz') as tar:
        for name, path in members.items():
            tar.add(path, arcname=name)
    return base.as_uri()


def test_pixels_predicted_above_the_threshold_are_flagged_and_left_out(tmp_path):
    exposure = _exposure(tmp_path, 'ites01a0q')
    before = fits.getdata(exposure, ('SCI', 1)).copy()
    product = _prediction(exposure, tmp_path / 'ites01a0q_persist.fits',
                          bright=[(10, 12), (10, 13)], faint=[(30, 30)])

    assert persist.persist_step(exposure, product) == 2
    with fits.open(exposure) as hdul:
        flagged = (hdul['DQ', 1].data & persist.PERSIST_BIT) != 0
        np.testing.assert_array_equal(hdul['SCI', 1].data, before)
    assert sorted(zip(*np.nonzero(flagged))) == [(10, 12), (10, 13)]
    stamp = cfp.step_value(exposure, 'CFP_PERS', keyset=cfp.HST)
    assert stamp.startswith('archive model >0.01 e-/s px=2 ')
    # the adapter leaves them out of the outlier pass and the mosaic
    good = open_exposure(exposure)[0].load().good
    assert not good[10, 12] and not good[10, 13] and good[30, 30]

    # stamped: nothing to do
    assert persist.persist_step(exposure, product) is None
    # another threshold replaces the first mask, in both directions
    assert persist.persist_step(exposure, product, {'threshold': 0.001},
                                overwrite=True) == 3
    assert persist.persist_step(exposure, product, {'threshold': 0.02},
                                overwrite=True) == 2
    assert persist.persist_step(exposure, product, {'threshold': 0.5},
                                overwrite=True) == 0
    assert not (fits.getdata(exposure, ('DQ', 1)) & persist.PERSIST_BIT).any()


def test_a_prediction_of_another_shape_is_refused(tmp_path):
    exposure = _exposure(tmp_path, 'ites01a0q')
    product = _prediction(exposure, tmp_path / 'ites01a0q_persist.fits')
    with fits.open(product, mode='update') as hdul:
        hdul[1].data = np.zeros((10, 10), dtype='float32')
    with pytest.raises(ValueError, match='not a prediction for this exposure'):
        persist.persist_step(exposure, product)
    assert not cfp.has_step(exposure, 'CFP_PERS', keyset=cfp.HST)
    assert not os.path.exists(exposure + '.persist.tmp')


def test_a_visit_is_fetched_once_and_only_its_predictions_are_kept(field, tmp_path):
    d = field.filter_dir('f160w')
    a, b, c = (_exposure(d, f'ites01a{k}q', seed=k) for k in range(3))
    made = tmp_path / 'made'
    made.mkdir()
    base = _archive(tmp_path, '01', {
        f'{PROPOSAL}.Visit01/ites01a0q_persist.fits':
            _prediction(a, made / 'a.fits', bright=[(5, 5)]),
        f'{PROPOSAL}.Visit01/ites01a1q_persist.fits':
            _prediction(b, made / 'b.fits'),
        f'{PROPOSAL}.Visit01/ites01a0q_extper.fits':
            _prediction(a, made / 'x.fits'),
        # a member that names a place outside the archive's own directory
        '../../outside_persist.fits': _prediction(a, made / 'o.fits'),
    })
    cfg = dict(get_hst_step_config('persist', 'wfc3ir'), archive_url=base)

    reasons = persist.persist_exposures([a, b, c], field.raw_root, cfg)

    # the third exposure is not in the visit's archive: named, not stamped
    assert list(reasons) == ['ites01a2q']
    assert 'has no prediction for it' in reasons['ites01a2q']
    assert not cfp.has_step(c, 'CFP_PERS', keyset=cfp.HST)
    assert ((fits.getdata(a, ('DQ', 1)) & persist.PERSIST_BIT) != 0).sum() == 1
    assert cfp.step_value(b, 'CFP_PERS', keyset=cfp.HST).startswith(
        'archive model >0.01 e-/s px=0 ')
    kept = persist.persist_dir(field.raw_root, PROPOSAL)
    assert sorted(os.listdir(kept)) == [
        'ites01a0q_persist.fits', 'ites01a1q_persist.fits',
        'outside_persist.fits']
    assert not os.path.exists(os.path.join(field.raw_root, 'outside_persist.fits'))


def test_no_prediction_is_said_and_holds_nothing_back(field, tmp_path, capsys):
    d = field.filter_dir('f160w')
    a = _exposure(d, 'ites01a0q')
    before = open(a, 'rb').read()
    config = {'wfc3ir': {'persist': {
        'archive_url': (tmp_path / 'nothing-here').as_uri()}}}

    orchestrate.run_step('persist', field, config)

    out = capsys.readouterr().out
    assert 'have NO persistence' in out and 'ites01a0q' in out
    assert 'could not be fetched' in out
    assert 'cfpipe wfc3ir persist --field tf' in out
    assert open(a, 'rb').read() == before
    assert orchestrate.unmasked_for_persistence(field, 'f160w') == ['ites01a0q']


def test_only_the_ir_detector_has_the_step(field):
    names = [n for n, _ in orchestrate.steps_for('wfc3ir')]
    assert names.index('crreject') < names.index('persist') < names.index('preview')
    assert 'persist' not in [n for n, _ in orchestrate.steps_for('acswfc')]
    acs = HstField.load('tf', 'acswfc')
    acs.setup_workspace()
    assert orchestrate.unmasked_for_persistence(acs, 'f814w') == []
