"""Tests for the NIRSpec grating/filter selection (issue #446).

``cfpipe nirspec <stage> --grating ... --filter ...`` sets a selection on the
Observation (``select_modes``); every stage then sees only matching exposures
through ``rate_files`` / ``discover_raw_files`` / ``discover_files`` /
``select_files``. These tests use header-only synthetic FITS files: no jwst.
"""

import click
import pytest
from astropy.io import fits
from click.testing import CliRunner

from campfire_pipeline.nirspec import cli as ncli
from campfire_pipeline.nirspec.observation import Observation

# (grating, filter) of three settings sharing one observation; G140M appears
# with two filters, which is why --grating alone cannot always pick a setting.
MODES = {
    '03101': ('G140M', 'F070LP'),
    '04101': ('G140M', 'F100LP'),
    '05101': ('G395M', 'F290LP'),
}
ROOT = 'jw07076020001'


def _primary(grating, filt, **extra):
    hdr = fits.Header()
    hdr['GRATING'] = grating
    hdr['FILTER'] = filt
    hdr['EXP_TYPE'] = 'NRS_MSASPEC'
    hdr['PATTTYPE'] = 'NONE'
    hdr['PATT_NUM'] = 1
    hdr['NUMDTHPT'] = 1
    hdr['NOD_TYPE'] = '3-SHUTTER-SLITLET'
    hdr['PRIDTPTS'] = 1
    hdr['SUBPXPTS'] = 1
    for k, v in extra.items():
        hdr[k] = v
    return fits.PrimaryHDU(header=hdr)


def _write(path, grating, filt, with_slit=False):
    hdus = [_primary(grating, filt)]
    if with_slit:
        ext = fits.ImageHDU(name='SCI')
        ext.header['SHUTTRID'] = 1
        hdus.append(ext)
    fits.HDUList(hdus).writeto(str(path), overwrite=True)


@pytest.fixture
def obs(tmp_path, monkeypatch):
    """An observation with raw uncals, rate files and canonicals of three settings."""
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    o = Observation(name='test_obs', field='egs', program='ember', program_id=7076,
                    data_subdir='7076', files=[f'{ROOT}*'])
    o.setup_workspace_directory(str(tmp_path / 'raw'), str(tmp_path / 'products'))
    import os
    os.makedirs(o.raw_dir, exist_ok=True)
    for config, (grating, filt) in MODES.items():
        stem = f'{ROOT}_{config}_00001_nrs1'
        _write(os.path.join(o.raw_dir, f'{stem}_uncal.fits'), grating, filt)
        _write(os.path.join(o.workspace_dir, f'{stem}_rate.fits'), grating, filt)
        for sid in (10, 20):
            _write(os.path.join(o.workspace_dir, f'{stem}_{sid}.fits'), grating, filt,
                   with_slit=True)
    # re-run setup so rate_files sees the files written above
    o.setup_workspace_directory(str(tmp_path / 'raw'), str(tmp_path / 'products'))
    return o


def _configs(table):
    return sorted(set(table['config']))


def test_no_selection_keeps_everything(obs):
    assert not obs.has_mode_selection
    assert len(obs.rate_files) == 3
    assert _configs(obs.discover_files()) == ['03101', '04101', '05101']
    assert obs.select_files(obs.rate_files) == obs.rate_files


def test_grating_selects_both_filters(obs):
    obs.select_modes(gratings=['g140m'])
    assert len(obs.rate_files) == 2
    assert _configs(obs.discover_files()) == ['03101', '04101']


def test_grating_and_filter_are_anded(obs):
    obs.select_modes(gratings=['G140M'], filters=['f100lp'])
    files = obs.discover_files()
    assert _configs(files) == ['04101']
    assert set(files['filter_grating']) == {'g140m_f100lp'}
    assert len(files) == 2   # both sources of that setting


def test_values_within_an_axis_are_ored(obs):
    obs.select_modes(filters=['F070LP', 'F290LP'])
    assert _configs(obs.discover_files()) == ['03101', '05101']


def test_source_ids_and_selection_combine(obs):
    obs.select_modes(gratings=['g395m'])
    files = obs.discover_files(source_ids=[20])
    assert list(files['source_id']) == [20]
    assert _configs(files) == ['05101']


def test_selected_files_still_group(obs):
    obs.select_modes(gratings=['g140m'], filters=['f070lp'])
    grouped = Observation.group_files(obs.discover_files())
    assert (grouped['bkg_group'] >= 0).all()


def test_discover_raw_files_honours_selection(obs):
    obs.select_modes(filters=['f290lp'])
    obs.discover_raw_files()
    assert [p.rsplit('/', 1)[-1] for p in obs.uncal_files] == [
        f'{ROOT}_05101_00001_nrs1_uncal.fits']
    assert len(obs.rate_files) == 1


def test_discover_raw_files_rejects_empty_selection(obs):
    obs.select_modes(gratings=['prism'])
    with pytest.raises(RuntimeError, match='match grating prism'):
        obs.discover_raw_files()


def test_clearing_the_selection_restores_everything(obs):
    obs.select_modes(gratings=['g395m'])
    obs.select_modes()
    assert not obs.has_mode_selection
    assert len(obs.rate_files) == 3


def test_available_modes_reads_exposures_and_products(obs):
    import os
    open(os.path.join(obs.workspace_dir, 'test_obs_prism_clear_10_spec.fits'), 'wb').close()
    assert obs.available_modes() == [
        ('g140m', 'f070lp'), ('g140m', 'f100lp'), ('g395m', 'f290lp'), ('prism', 'clear')]


# --- CLI -------------------------------------------------------------------

def test_apply_modes_rejects_a_selection_matching_nothing(obs):
    with pytest.raises(click.UsageError, match='present: g140m/f070lp'):
        ncli._apply_modes(obs, ('g395h',), None)


def test_apply_modes_rejects_a_mismatched_pair(obs):
    """Both names exist in the observation, but never together."""
    with pytest.raises(click.UsageError, match='matches no exposure'):
        ncli._apply_modes(obs, ('g395m',), ('f100lp',))


def test_apply_modes_is_a_noop_without_flags(obs):
    ncli._apply_modes(obs, None, None)
    assert not obs.has_mode_selection


def test_apply_modes_sets_selection(obs):
    ncli._apply_modes(obs, ('g140m',), ('f100lp',))
    assert obs.mode_selection_label() == 'grating g140m and filter f100lp'


@pytest.mark.parametrize('command', [
    'stage1', 'stage2a', 'stage2b', 'stage3', 'zfit', 'linefit', 'detect-stuck', 'run'])
def test_every_stage_command_takes_the_flags(command):
    result = CliRunner().invoke(ncli.main, [command, '--help'])
    assert result.exit_code == 0
    assert '--grating' in result.output and '--filter' in result.output


def test_flag_values_are_validated_and_normalised():
    parse = ncli._parse_names(['g140m', 'g395m'], 'grating')
    assert parse(None, None, ('G140M,g395m', 'g140m')) == ('g140m', 'g395m')
    assert parse(None, None, ()) is None
    with pytest.raises(click.BadParameter, match="'g395' is not a NIRSpec grating"):
        parse(None, None, ('g395',))


def test_unknown_filter_is_a_usage_error():
    result = CliRunner().invoke(ncli.main, ['stage3', '--obs', 'x', '--filter', 'f444w'])
    assert result.exit_code == 2
    assert 'not a NIRSpec filter' in result.output


# --- zfit ------------------------------------------------------------------

def test_zfit_skips_gratings_without_spectra(tmp_path, monkeypatch):
    """An explicit grating with no *_spec.fits on disk builds no template grid."""
    from campfire_pipeline.nirspec import redshift_fitting as rf
    monkeypatch.setattr(rf, '_discover_gratings', lambda ws: ['g395m'])
    monkeypatch.setattr(rf, 'resolve_paths', lambda cfg: {'products_dir': str(tmp_path)},
                        raising=False)
    import campfire_pipeline.config as config
    monkeypatch.setattr(config, 'resolve_paths', lambda cfg: {'products_dir': str(tmp_path)})
    monkeypatch.setattr(config, 'resolve_template_grid_paths',
                        lambda cfg: pytest.fail('template grids resolved'))
    assert rf.fit_redshifts('obs', config={}, workspace_dir=str(tmp_path),
                            gratings=['G140M']) == 0


def test_spec_filter_parses_product_names():
    from campfire_pipeline.nirspec.redshift_fitting import _spec_filter
    assert _spec_filter('/w/my_obs_g140m_f100lp_123_spec.fits') == 'f100lp'
    assert _spec_filter('/w/garbage_spec.fits') is None
