"""HST orchestration: settings, the mode gate, overlap groups, and the combine
phase end to end on synthetic WFC3/IR-shaped exposures.

The combine test drizzles three 80 x 80 pixel exposures onto a small tile; it
uses no HST software (the steps that need it, calibrate / wcs_init / crreject,
are covered by their own tests).
"""

import json
import os

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS
from click.testing import CliRunner

from campfire_pipeline.common import cfp
from campfire_pipeline.config import load_config
from campfire_pipeline.hst import orchestrate
from campfire_pipeline.hst.exposure import OUTLIER_EXT
from campfire_pipeline.hst.field import HstField

from test_hst_outlier import _ir_file

FIELDS = """
[tf]
tangent_point = [34.4, -5.2]

[tf.T1]
rotation = 0
[tf.T1.30mas]
crpix = [250.5, 250.5]
naxis = [500, 500]

[tf.far]
tangent_point = [40.0, 10.0]
rotation = 0
[tf.far.30mas]
crpix = [50.5, 50.5]
naxis = [100, 100]

[tf.wfc3ir]
filters = ["f160w"]
files = ["ites*"]
[tf.wfc3ir.outlier]
protect_radius = 0.6
[tf.wfc3ir.resample]
background_subtract = false
plot = false
"""


@pytest.fixture
def field(tmp_path, monkeypatch):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'fields.toml').write_text(FIELDS)
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    f = HstField.load('tf', 'wfc3ir')
    f.setup_workspace()
    return f


@pytest.fixture
def config():
    cfg = load_config(None)
    cfg.setdefault('pipeline', {})['version'] = 'test-version'
    return cfg


def _exposures(field, n=3, hit=True):
    rng = np.random.default_rng(5)
    d = field.filter_dir('f160w')
    crpix = ((40.0, 40.0), (43.4, 38.7), (37.2, 42.6), (41.0, 44.0))[:n]
    files = [_ir_file(os.path.join(d, f'itest01a{k}q_flt.fits'), f'itest01a{k}q',
                      c, rng, mjd=57000.0 + k)
             for k, c in enumerate(crpix)]
    if hit:
        with fits.open(files[0], mode='update') as hdul:
            hdul['SCI', 1].data[30:33, 50:53] += 50.0
    return files


# -- settings -----------------------------------------------------------------

def test_step_settings_are_the_config_with_the_fields_on_top(field, config):
    cfg = orchestrate.step_config('outlier', field, config)
    assert cfg['protect_radius'] == 0.6                 # the field's
    assert cfg['scale'] == '5.0 0.7'                    # the config's
    config['wfc3ir']['outlier']['scale'] = '6.0 0.7'    # a user config value
    assert orchestrate.step_config('outlier', field, config)['scale'] == '6.0 0.7'


def test_reference_files_have_their_own_cache(field, config):
    refs = orchestrate.reference_settings(config, field)
    assert refs['crds_context'] == config['environment']['HST_CRDS_CONTEXT']
    assert refs['crds_server'] == 'https://hst-crds.stsci.edu'
    assert refs['crds_path'] == os.path.join(field.campfire_root, 'cache', 'crds_hst')
    config['environment']['HST_CRDS_PATH'] = '/somewhere/else'
    assert orchestrate.reference_settings(config, field)['crds_path'] == '/somewhere/else'
    # the JWST cache is never used, whatever $CRDS_PATH says
    config['environment']['CRDS_PATH'] = '/shared/jwst-crds'
    del config['environment']['HST_CRDS_PATH']
    assert 'jwst' not in orchestrate.reference_settings(config, field)['crds_path']


@pytest.mark.parametrize('cards, reason', [
    ({'FILTER': 'F160W'}, None),
    ({'FILTER': 'F125W'}, 'its header says filter f125w'),
    ({'FILTER': 'F160W', 'SUBARRAY': True}, 'subarray'),
    ({'FILTER': 'F160W', 'SCAN_TYP': 'C'}, 'spatial scan'),
    ({'FILTER': 'F160W', 'MTFLAG': 'T'}, 'moving target'),
    ({'FILTER': 'F160W', 'IMAGETYP': 'DARK'}, 'not a sky exposure'),
])
def test_only_direct_imaging_is_reduced(cards, reason):
    header = fits.Header(cards)
    got = orchestrate.unsupported_mode_reason(header, 'f160w')
    assert (got is None) if reason is None else (reason in got)
    grism = fits.Header({'FILTER': 'G141'})
    assert 'dispersed' in orchestrate.unsupported_mode_reason(grism, 'g141')


def test_a_visit_is_solved_again_when_the_catalog_changed(field):
    (f,) = _exposures(field, n=1, hit=False)

    def stamp(value):
        with fits.open(f, mode='update') as hdul:
            for key, val in cfp.format(keyset=cfp.HST, CFP_ALGN=value).items():
                hdul[0].header[key] = val

    stamp('fit=rigid pool=visit rc=abcd1234')
    assert not orchestrate._algn_pending([f], 'abcd1234', False)
    assert orchestrate._algn_pending([f], 'ffff0000', False)     # new catalog
    assert orchestrate._algn_pending([f], 'abcd1234', True)      # --overwrite
    stamp(cfp.NOT_ALIGNED)
    assert orchestrate._algn_pending([f], 'abcd1234', False)     # retried


def test_align_needs_the_fields_reference_catalog(field, config):
    with pytest.raises(ValueError, match='names no reference catalog'):
        orchestrate.run_process(field, config)
    field.refcat = 'missing.ecsv'
    with pytest.raises(ValueError, match='reference catalog not found'):
        orchestrate.check_align_config(field)


def test_a_catalog_without_proper_motions_is_called_out(monkeypatch):
    from astropy.table import Table

    said = []
    monkeypatch.setattr(orchestrate, 'log', said.append)
    plain = Table({'RA': [1.0, 2.0], 'DEC': [1.0, 2.0]})
    orchestrate._warn_no_motions(plain, '/x/plain.ecsv')
    assert len(said) == 1 and 'plain.ecsv has no proper motions' in said[0]
    said.clear()
    # one star with a motion is enough: the catalog went through the rule
    moving = Table({'RA': [1.0, 2.0], 'DEC': [1.0, 2.0],
                    'pmra': [np.nan, 3.0], 'pmdec': [np.nan, -1.0]})
    orchestrate._warn_no_motions(moving, '/x/moving.ecsv')
    assert said == []


# -- combine --------------------------------------------------------------------

def test_exposures_are_grouped_by_overlap(field):
    files = _exposures(field, n=3, hit=False)
    rng = np.random.default_rng(1)
    far = _ir_file(os.path.join(field.filter_dir('f160w'), 'itest02a0q_flt.fits'),
                   'itest02a0q', (4000.0, 40.0), rng)     # 8 arcmin away
    groups = orchestrate.overlap_groups(files + [far])
    assert groups == [sorted(files), [far]]


def test_combine_flags_outliers_on_copies_and_drizzles_the_tile(field, config):
    files = _exposures(field)
    before = {f: open(f, 'rb').read() for f in files}
    orchestrate.run_combine(field, config)

    # the canonical exposures are untouched; the flags are on the copies
    assert all(open(f, 'rb').read() == before[f] for f in files)
    work = field.get_exposure_files('f160w', work=True)
    assert [os.path.basename(w) for w in work] == [os.path.basename(f) for f in files]
    assert fits.getdata(work[0], (OUTLIER_EXT, 1))[30:33, 50:53].sum() >= 7
    groups = json.load(open(os.path.join(os.path.dirname(work[0]),
                                         orchestrate.OUTLIER_MANIFEST)))['groups']
    assert [g['members'] for g in groups] == [[os.path.basename(w) for w in work]]

    # one mosaic, on the field's tile, named after the instrument
    d = field.filter_dir('f160w')
    name = 'mosaic_wfc3ir_f160w_tf_30mas_T1'
    mosaic = os.path.join(d, f'{name}_i2d.fits')
    assert os.path.exists(mosaic)
    assert not os.path.exists(os.path.join(d, 'mosaic_wfc3ir_f160w_tf_30mas_far_i2d.fits'))
    with fits.open(mosaic) as hdul:
        assert hdul[0].header['TELESCOP'] == 'HST'
        assert hdul[0].header['CMPFRVER'] == 'test-version'
        assert hdul['SCI'].data.shape == (500, 500)
        w = WCS(hdul['SCI'].header)
        crpix, crval, _shape, _rot = field.get_tile_wcs('T1', '30mas')
        assert np.allclose(w.wcs.crpix, np.array(crpix) + 1.0)
        assert np.allclose(w.wcs.crval, crval)
        sci = hdul['SCI'].data
        # the hit is gone: nothing in the mosaic stands far above the noise
        assert np.nanmax(np.abs(sci)) < 0.2 * 50.0 * 1.5e-7 * 1e-6 / (
            0.128 * np.pi / 648000.0) ** 2
        assert len(hdul['INPUTS'].data) == 3
    for suffix in ('_sci.fits', '_err.fits', '_wht.fits', '_exp.fits'):
        assert os.path.exists(os.path.join(d, name + suffix))
    manifest = json.load(open(os.path.join(d, f'{name}_manifest.json')))
    assert manifest['instrument'] == 'wfc3ir'
    assert [i['hash_kind'] for i in manifest['inputs']] == ['file'] * 3

    # a second run finds everything up to date and rewrites nothing
    stamp = os.path.getmtime(mosaic), [os.path.getmtime(w) for w in work]
    orchestrate.run_combine(field, config)
    assert (os.path.getmtime(mosaic), [os.path.getmtime(w) for w in work]) == stamp
    status = orchestrate.stale_tiles(field, config, 'f160w')
    assert [(r['tile'], r['stale'], r['n_inputs']) for r in status] == [
        ('T1', False, 3), ('far', False, 0)]

    # dropping an exposure from the field redoes the group and the tile
    field.skip = ['itest01a2q']
    orchestrate.run_combine(field, config)
    assert len(field.get_exposure_files('f160w', work=True)) == 2
    manifest = json.load(open(os.path.join(d, f'{name}_manifest.json')))
    assert len(manifest['inputs']) == 2
    assert cfp.step_value(work[0], 'CFP_OUT', keyset=cfp.HST).startswith('field n=2')


def test_a_region_drawn_after_a_mosaic_reaches_the_next_one(field, config):
    """The reviewer's loop: combine, draw a region on an exposure, combine
    again. No reset; only what the region touches is redone."""
    pytest.importorskip('regions')
    from campfire_pipeline.hst.exposure import MASK_EXT

    files = _exposures(field, hit=False)
    orchestrate.run_combine(field, config)
    d = field.filter_dir('f160w')
    exp = os.path.join(d, 'mosaic_wfc3ir_f160w_tf_30mas_T1_exp.fits')
    before = fits.getdata(exp).astype(float)
    work = field.get_exposure_files('f160w', work=True)
    times = [os.path.getmtime(w) for w in work]

    reg = field.mask_file(files[0], 'f160w')
    assert reg.endswith('reference/wfc3ir/tf/masks/f160w/itest01a0q_flt.reg')
    os.makedirs(os.path.dirname(reg))
    with open(reg, 'w') as f:
        f.write('# Region file format: DS9\nimage\nbox(40.5,40.5,20,20,0)\n')
    orchestrate.run_combine(field, config)

    assert fits.getdata(files[0], (MASK_EXT, 1)).sum() == 400
    assert not cfp.has_step(files[1], 'CFP_MASK', keyset=cfp.HST)   # untouched
    assert os.path.getmtime(work[0]) > times[0]                     # new copy
    assert fits.getdata(work[0], (MASK_EXT, 1)).sum() == 400
    # the mosaic lost that exposure's time under the region, nowhere else
    after = fits.getdata(exp).astype(float)
    lost = before - after
    assert lost.max() > 0 and lost.min() >= 0
    assert 0.02 < (lost > 0).mean() < 0.12
    status = orchestrate.stale_tiles(field, config, 'f160w')
    assert [(r['tile'], r['stale']) for r in status][0] == ('T1', False)


def test_status_reads_the_outlier_stamp_from_the_copy(field, config):
    _exposures(field, hit=False)
    orchestrate.run_combine(field, config, steps=('outlier',))
    rows = orchestrate.exposure_status(field, 'f160w')
    assert list(rows) == ['itest01a0q', 'itest01a1q', 'itest01a2q']
    assert all('CFP_ALGN' in s and 'CFP_OUT' in s for s in rows.values())


# -- commands -------------------------------------------------------------------

def _cli(monkeypatch):
    import campfire_pipeline.config as config_mod
    from campfire_pipeline.cli import main
    monkeypatch.setattr(config_mod, 'setup_environment', lambda *a, **k: None)
    return CliRunner(), main


def test_each_instrument_has_the_nircam_command_set(field, monkeypatch):
    runner, main = _cli(monkeypatch)
    for instrument in ('acswfc', 'wfc3ir'):
        out = runner.invoke(main, [instrument, '--help']).output
        for command in ('process', 'combine', 'run', 'status', 'check', 'reset',
                        'calibrate', 'wcs_init', 'sky', 'crreject', 'preview',
                        'align', 'apply_masks', 'outlier', 'resample'):
            assert command in out
    # satellite trails are an ACS/WFC step
    assert '  trails ' in runner.invoke(main, ['acswfc', '--help']).output
    assert '  trails ' not in runner.invoke(main, ['wfc3ir', '--help']).output


def test_status_and_reset_commands(field, monkeypatch):
    files = _exposures(field, hit=False)
    runner, main = _cli(monkeypatch)
    result = runner.invoke(main, ['wfc3ir', 'status', '--field', 'tf'])
    assert result.exit_code == 0, result.output
    assert 'itest01a0q' in result.output and '3/3 done' in result.output

    # a field without a table for the instrument is a plain message
    result = runner.invoke(main, ['acswfc', 'status', '--field', 'tf'])
    assert result.exit_code != 0 and 'has no [tf.acswfc] table' in result.output

    # steps that change pixels are not redone in place
    result = runner.invoke(main, ['wfc3ir', 'reset', '--field', 'tf', '--from', 'sky'])
    assert result.exit_code != 0 and '--raw' in result.output

    result = runner.invoke(main, ['wfc3ir', 'reset', '--field', 'tf',
                                  '--from', 'align', '--yes'])
    assert result.exit_code == 0, result.output
    assert not cfp.has_step(files[0], 'CFP_ALGN', keyset=cfp.HST)

    result = runner.invoke(main, ['wfc3ir', 'reset', '--field', 'tf', '--raw', '--yes'])
    assert result.exit_code == 0, result.output
    assert field.get_exposure_files('f160w') == []
