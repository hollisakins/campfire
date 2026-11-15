"""HST settings live in the default config, per detector, and a field names
one reference catalog for every instrument.
"""

import pytest

from campfire_pipeline.config import _load_package_defaults, get_hst_step_config
from campfire_pipeline.nircam.field import Field
from campfire_pipeline.nircam.orchestrate import (check_align_config,
                                                  field_refcat_name)

_DETECTORS = ('acswfc', 'wfc3ir')
_STEPS = ('sky', 'crreject', 'align', 'outlier')


def test_every_step_of_every_detector_has_its_block():
    hst = _load_package_defaults()['hst']
    assert hst['crds_context'].startswith('hst_') and hst['crds_server']
    for detector in _DETECTORS:
        for step in _STEPS:
            assert hst[detector][step], f'[hst.{detector}.{step}] is empty'
    # the two detectors carry the same keys, written out for each
    for step in _STEPS:
        assert set(hst['acswfc'][step]) == set(hst['wfc3ir'][step])


def test_the_shipped_values_are_the_measured_ones():
    assert get_hst_step_config('crreject', 'acswfc')['driz_cr_snr'] == '3.5 3.0'
    assert get_hst_step_config('crreject', 'wfc3ir')['driz_cr_scale'] == '2.5 0.7'
    assert get_hst_step_config('outlier', 'wfc3ir')['scale'] == '5.0 0.7'
    assert get_hst_step_config('outlier', 'acswfc')['protect_radius'] == 0.25
    assert get_hst_step_config('align', 'acswfc')['detect_fwhm'] == 2.2
    assert get_hst_step_config('align', 'wfc3ir')['detect_fwhm'] == 1.3
    for detector in _DETECTORS:
        cfg = get_hst_step_config('align', detector)
        assert cfg['pool'] == 'visit' and cfg['fit'] == 'auto'
        assert cfg['chip_fits'] is False


def test_user_config_and_field_overrides_take_precedence():
    user = {'hst': {'wfc3ir': {'outlier': {'snr': '9 6', 'grow': 1}}}}
    # a user config replaces the shipped file as the base
    assert get_hst_step_config('outlier', 'wfc3ir', user) == {'snr': '9 6', 'grow': 1}
    # the field's table wins over both, and other keys are kept
    got = get_hst_step_config('outlier', 'wfc3ir', user, overrides={'grow': 2})
    assert got == {'snr': '9 6', 'grow': 2}
    # overrides on the shipped defaults leave the rest alone and do not stick
    got = get_hst_step_config('outlier', 'wfc3ir', overrides={'grow': 3})
    assert got['grow'] == 3 and got['scale'] == '5.0 0.7'
    assert get_hst_step_config('outlier', 'wfc3ir')['grow'] == 1


def test_an_unknown_detector_is_refused():
    with pytest.raises(KeyError, match='wfc3uvis'):
        get_hst_step_config('sky', 'wfc3uvis')
    with pytest.raises(KeyError):
        get_hst_step_config('sky', 'crds_context')      # a scalar, not a detector


_FIELDS = """
[cosmos]
    filters = ['f200w']
    files = ['jw01727*']
    tangent_point = [150.1, 2.2]
    refcat = 'cosmos_nircam.ecsv'

    [cosmos.A1]
        rotation = 20
        [cosmos.A1.30mas]
            crpix = [10, 10]
            naxis = [100, 100]

    [cosmos.acswfc]
        filters = ['f814w']
        files = ['jczg*']
        [cosmos.acswfc.align]
            fit = 'rigid'
    [cosmos.wfc3ir]
        filters = ['f160w']
        files = ['iczg*']
"""


def test_the_nircam_loader_reads_the_field_refcat_and_ignores_hst_tables(tmp_path):
    ff = tmp_path / 'fields.toml'
    ff.write_text(_FIELDS)
    field = Field.load('cosmos', fields_file=str(ff))
    assert field.refcat == 'cosmos_nircam.ecsv'
    assert list(field.tiles) == ['A1']                  # not acswfc / wfc3ir
    assert field.filters == ['f200w'] and field.files == ['jw01727*']
    assert 'acswfc' not in field.step_overrides
    assert field.step_overrides.get('align') is None    # HST's align is not NIRCam's


def _field(top=None, old=None):
    overrides = {'align': {'refcat': old}} if old else {}
    return Field(name='cosmos', filters=['f200w'], files=['jw01727*'],
                 tangent_point=(80.0, -30.0), tiles={}, step_overrides=overrides,
                 refcat=top)


def test_one_reference_catalog_per_field(tmp_path):
    assert field_refcat_name(_field()) is None
    assert field_refcat_name(_field(top='a.ecsv')) == 'a.ecsv'
    # the older place is still read
    assert field_refcat_name(_field(old='b.ecsv'), {'refcat': 'b.ecsv'}) == 'b.ecsv'
    # the same file in both places is fine; two different files are refused
    assert field_refcat_name(_field(top='a.ecsv'), {'refcat': 'a.ecsv'}) == 'a.ecsv'
    with pytest.raises(ValueError, match='two reference catalogs'):
        field_refcat_name(_field(top='a.ecsv'), {'refcat': 'b.ecsv'})

    # the align preflight takes the field-level key
    (tmp_path / 'a.ecsv').write_text('')
    field = _field(top='a.ecsv')
    field.refcat_dir = str(tmp_path)
    check_align_config({}, field)
    missing = _field(top='gone.ecsv')
    missing.refcat_dir = str(tmp_path)
    with pytest.raises(ValueError, match='not found'):
        check_align_config({}, missing)
