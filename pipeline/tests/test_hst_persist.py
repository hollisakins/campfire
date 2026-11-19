"""WFC3/IR persistence masks (``hst/steps/persist.py``) and the download of
the archive's predictions (``common/wfc3_persist.py``, ``cfpipe download``).

The predictions are synthetic and laid out as the software that writes the
real ones lays them out: a copy of the calibrated exposure whose first
extension holds the prediction in e-/s, inside ``<proposal>.Visit<VV>.tar.gz``.
The "archive" is a directory read through a ``file://`` address, so nothing
here touches the network.
"""

import os
import shutil
import tarfile

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.common import cfp, mast_query as mq, wfc3_persist
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
filters = ["f125w", "f160w"]
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


def _exposure(directory, root, seed=5, mjd=57000.0, star=None):
    """A calibrated exposure through the bkg step, of program 12101; ``star``
    is a pixel (y, x) that collects 100 000 electrons."""
    os.makedirs(directory, exist_ok=True)
    path = _ir_file(os.path.join(directory, f'{root}_flt.fits'), root,
                    (40.0, 40.0), np.random.default_rng(seed), mjd=mjd)
    with fits.open(path, mode='update') as hdul:
        hdul[0].header['PROPOSID'] = PROPOSAL
        for key, val in cfp.format(keyset=cfp.HST, CFP_BKG=None).items():
            hdul[0].header[key] = val
        if star is not None:
            hdul['SCI', 1].data[star] = 100000.0 / hdul[0].header['EXPTIME']
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


def _flagged(path):
    return (fits.getdata(path, ('DQ', 1)) & persist.PERSIST_BIT) != 0


# -- the archive's prediction ---------------------------------------------------

def test_pixels_predicted_above_the_threshold_are_flagged_and_left_out(tmp_path):
    exposure = _exposure(tmp_path, 'ites01a0q')
    before = fits.getdata(exposure, ('SCI', 1)).copy()
    product = _prediction(exposure, tmp_path / 'ites01a0q_persist.fits',
                          bright=[(10, 12), (10, 13)], faint=[(30, 30)])

    assert persist.persist_step(exposure, product) == (persist.ARCHIVE, 2)
    assert sorted(zip(*np.nonzero(_flagged(exposure)))) == [(10, 12), (10, 13)]
    np.testing.assert_array_equal(fits.getdata(exposure, ('SCI', 1)), before)
    stamp = cfp.step_value(exposure, 'CFP_PERS', keyset=cfp.HST)
    assert stamp == 'archive >0.01 px=2 max=0.050'
    # the adapter leaves them out of the outlier pass and the mosaic
    good = open_exposure(exposure)[0].load().good
    assert not good[10, 12] and not good[10, 13] and good[30, 30]

    # stamped: nothing to do
    assert persist.persist_step(exposure, product) is None
    # another threshold replaces the first mask, in both directions
    assert persist.persist_step(exposure, product, (), {'threshold': 0.001},
                                overwrite=True)[1] == 3
    assert persist.persist_step(exposure, product, (), {'threshold': 0.5},
                                overwrite=True)[1] == 0
    assert not _flagged(exposure).any()


def test_a_prediction_of_another_shape_is_refused(tmp_path):
    exposure = _exposure(tmp_path, 'ites01a0q')
    product = _prediction(exposure, tmp_path / 'ites01a0q_persist.fits')
    with fits.open(product, mode='update') as hdul:
        hdul[1].data = np.zeros((10, 10), dtype='float32')
    with pytest.raises(ValueError, match='not a prediction for this exposure'):
        persist.persist_step(exposure, product)
    assert not cfp.has_step(exposure, 'CFP_PERS', keyset=cfp.HST)
    assert not os.path.exists(exposure + '.persist.tmp')


# -- the in-field rule ------------------------------------------------------------

def test_without_a_prediction_a_saturated_pixel_is_masked_in_what_follows(field):
    """NIRCam's rule, across the filters of the field: the star saturates in
    the F125W exposure, and the F160W exposures that start within 2500 s are
    masked there; one a day later is not."""
    minute = 60.0 / 86400.0
    stim = _exposure(field.filter_dir('f125w'), 'ites01a0q', mjd=57000.0,
                     star=(20, 21))
    soon = _exposure(field.filter_dir('f160w'), 'ites01a1q', seed=1,
                     mjd=57000.0 + 15 * minute)
    later = _exposure(field.filter_dir('f160w'), 'ites02a2q', seed=2,
                      mjd=57001.0)

    orchestrate.run_step('persist', field, {})

    assert sorted(zip(*np.nonzero(_flagged(soon)))) == [(20, 21)]
    assert not _flagged(later).any() and not _flagged(stim).any()
    stamp = cfp.step_value(soon, 'CFP_PERS', keyset=cfp.HST)
    assert stamp.startswith('in-field n=1 px=1 [')
    assert cfp.step_value(later, 'CFP_PERS', keyset=cfp.HST).startswith(
        'in-field n=0 px=0 [')
    # the stamp and its comment fit the card: nothing is truncated
    with fits.open(soon) as hdul:
        assert 'persistence flagged' in hdul[0].header.comments['CFP_PERS']
    assert orchestrate.without_prediction(field, 'f160w') == [
        'ites01a1q', 'ites02a2q']
    assert orchestrate.unmasked_for_persistence(field, 'f160w') == []

    # nothing changed: a second run rewrites nothing
    stamps = [os.path.getmtime(p) for p in (stim, soon, later)]
    orchestrate.run_step('persist', field, {})
    assert [os.path.getmtime(p) for p in (stim, soon, later)] == stamps

    # another exposure before `soon` appears: its mask is made again
    _exposure(field.filter_dir('f125w'), 'ites01a3q', seed=3,
              mjd=57000.0 + 5 * minute, star=(40, 41))
    orchestrate.run_step('persist', field, {})
    assert sorted(zip(*np.nonzero(_flagged(soon)))) == [(20, 21), (40, 41)]

    # the prediction arrives: it replaces the in-field mask
    product = wfc3_persist.product_path(field.raw_root, PROPOSAL, 'ites01a1q')
    os.makedirs(os.path.dirname(product))
    _prediction(soon, product, bright=[(7, 7)])
    orchestrate.run_step('persist', field, {})
    assert sorted(zip(*np.nonzero(_flagged(soon)))) == [(7, 7)]
    assert orchestrate.without_prediction(field, 'f160w') == ['ites02a2q']


def test_the_fallback_is_said(field, capsys):
    _exposure(field.filter_dir('f160w'), 'ites01a0q')
    orchestrate.run_step('persist', field, {})
    out = capsys.readouterr().out
    assert 'no archive prediction on disk' in out and 'ites01a0q' in out
    assert 'cfpipe download --telescope hst --instrument wfc3ir --field tf' in out


def test_only_the_ir_detector_has_the_step(field):
    names = [n for n, _ in orchestrate.steps_for('wfc3ir')]
    assert names.index('crreject') < names.index('persist') < names.index('preview')
    assert 'persist' not in [n for n, _ in orchestrate.steps_for('acswfc')]
    acs = HstField.load('tf', 'acswfc')
    acs.setup_workspace()
    assert orchestrate.unmasked_for_persistence(acs, 'f814w') == []
    assert orchestrate.without_prediction(acs, 'f814w') == []


# -- the download -----------------------------------------------------------------

def _ir_exposure(root):
    return mq.Exposure('wfc3ir', 'hst', root, str(PROPOSAL), '', '', 'f160w',
                       500.0, 57000.0, 'PUBLIC', ())


@pytest.fixture
def no_raw(monkeypatch):
    """The RAW half of the download, without the network."""
    from campfire_pipeline.common import query as jwst_query

    def fake(files, token=None, workers=4, desc=''):
        for f in files:
            f['_path'].parent.mkdir(parents=True, exist_ok=True)
            f['_path'].write_bytes(b'x' * f['size'])
        return 0
    monkeypatch.setattr(jwst_query, 'download_files', fake)
    monkeypatch.setattr(mq, 'raw_products', lambda exposures, tap=None: {
        e.obs_id: {'filename': f'{e.obs_id}_raw.fits', 'size': 3,
                   'uri': 'x', 'checksum': ''} for e in exposures})
    monkeypatch.setattr(mq, '_write_hst_manifest',
                        lambda directory, rows: directory / 'manifest.ecsv')


def test_the_download_fetches_a_visit_once_and_keeps_only_predictions(
        tmp_path, no_raw):
    made = tmp_path / 'made'
    a = _exposure(made, 'ites01a0q')
    base = _archive(tmp_path, '01', {
        f'{PROPOSAL}.Visit01/ites01a0q_persist.fits':
            _prediction(a, made / 'a.fits', bright=[(5, 5)]),
        f'{PROPOSAL}.Visit01/ites01a1q_persist.fits':
            _prediction(a, made / 'b.fits'),
        f'{PROPOSAL}.Visit01/ites01a0q_extper.fits':
            _prediction(a, made / 'x.fits'),
        # a member that names a place outside the archive's own directory
        '../../outside_persist.fits': _prediction(a, made / 'o.fits'),
    })
    root = tmp_path / 'root'
    exposures = [_ir_exposure(f'ites01a{k}q') for k in range(3)]
    lines = []

    dry = mq.download_hst(exposures, root, dry_run=True, log=lines.append,
                          persist_url=base)
    assert dry['persist_missing'] == 0 and not (root / 'raw').exists()
    assert any('would fetch' in m and 'Visit01' in m for m in lines)

    counts = mq.download_hst(exposures, root, log=lines.append, persist_url=base)

    # the third exposure is not in the visit's archive: named, and counted
    # apart from the errors of the data download
    assert counts['errors'] == 0 and counts['persist_missing'] == 1
    assert any('ites01a2q' in m and 'has no prediction for it' in m for m in lines)
    kept = root / 'raw' / 'wfc3ir' / str(PROPOSAL) / 'persist'
    assert sorted(p.name for p in kept.iterdir()) == [
        'ites01a0q_persist.fits', 'ites01a1q_persist.fits',
        'outside_persist.fits']
    assert not list(root.rglob('*extper*'))
    assert [p for p in tmp_path.rglob('outside_persist.fits')] == [
        kept / 'outside_persist.fits']
    assert wfc3_persist.product_path(root / 'raw' / 'wfc3ir', PROPOSAL,
                                     'ites01a0q') == str(kept / 'ites01a0q_persist.fits')

    # a second run fetches nothing that is there
    again = wfc3_persist.fetch_predictions(
        {str(PROPOSAL): ['ites01a0q', 'ites01a1q']}, root / 'raw' / 'wfc3ir',
        base_url='file:///nowhere')
    assert again == {'present': 2, 'fetched': 0, 'missing': {}}


def test_an_archive_that_does_not_answer_is_reported_not_an_error(tmp_path, no_raw):
    lines = []
    counts = mq.download_hst([_ir_exposure('ites01a0q')], tmp_path / 'root',
                             log=lines.append,
                             persist_url=(tmp_path / 'nothing-here').as_uri())
    assert counts['errors'] == 0 and counts['downloaded'] == 1
    assert counts['persist_missing'] == 1
    text = '\n'.join(lines)
    assert 'WARNING: no persistence prediction' in text
    assert 'could not be fetched' in text and 'ites01a0q' in text
