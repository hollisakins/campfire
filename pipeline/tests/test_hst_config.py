"""Each HST detector is an instrument with its own blocks in the default config
(``[acswfc.*]``, ``[wfc3ir.*]``), and a field names one reference catalog for
every instrument.
"""

import os

import pytest

from campfire_pipeline.config import _load_package_defaults, get_hst_step_config
from campfire_pipeline.nircam.field import Field
from campfire_pipeline.nircam.orchestrate import (check_align_config,
                                                  field_refcat_name)

_DETECTORS = ('acswfc', 'wfc3ir')
_STEPS = ('sky', 'crreject', 'align', 'outlier')


def test_every_step_of_every_detector_has_its_block():
    shipped = _load_package_defaults()
    assert 'hst' not in shipped                 # no telescope-level block
    env = shipped['environment']
    assert env['HST_CRDS_CONTEXT'].startswith('hst_') and env['HST_CRDS_SERVER_URL']
    for detector in _DETECTORS:
        for step in _STEPS:
            assert shipped[detector][step], f'[{detector}.{step}] is empty'
        assert 'crds_context' not in shipped[detector]     # a pin, unset
    # the two instruments carry the same keys, written out for each; the
    # single-exposure cosmic-ray settings exist for the CCD only (a WFC3/IR
    # ramp rejects cosmic rays itself)
    for step in _STEPS:
        acs = {k for k in shipped['acswfc'][step] if not k.startswith('single_')}
        assert acs == set(shipped['wfc3ir'][step])
    assert {k for k in shipped['acswfc']['crreject'] if k.startswith('single_')} == {
        'single_sigclip', 'single_sigfrac', 'single_objlim', 'single_niter',
        'single_grow'}


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
    user = {'wfc3ir': {'outlier': {'snr': '9 6', 'grow': 1}}}
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
    with pytest.raises(KeyError, match='not an HST instrument'):
        get_hst_step_config('align', 'nircam')          # an instrument, not HST's


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


# --- CRDS: one context series per observatory, pins per instrument -----------

from campfire_pipeline.config import (check_crds_context, check_crds_server,  # noqa: E402
                                      crds_context_for, crds_server_for,
                                      setup_environment)

_JWST, _HST = 'jwst_1481.pmap', 'hst_1372.pmap'


def test_a_context_of_the_other_observatory_is_refused_loudly():
    assert check_crds_context(_JWST, 'jwst', 'x') == _JWST
    assert check_crds_context(_HST, 'hst', 'x') == _HST
    with pytest.raises(ValueError, match='is a HST CRDS context'):
        check_crds_context(_HST, 'jwst', '[environment].CRDS_CONTEXT')
    with pytest.raises(ValueError, match='is a JWST CRDS context'):
        check_crds_context(_JWST, 'hst', '[environment].HST_CRDS_CONTEXT')
    with pytest.raises(ValueError, match='not a HST CRDS context'):
        check_crds_context('latest.pmap', 'hst', 'x')
    with pytest.raises(ValueError, match='no CRDS context'):
        check_crds_context('', 'hst', 'x')
    with pytest.raises(ValueError, match='JWST CRDS server'):
        check_crds_server('https://jwst-crds.stsci.edu', 'hst', 'x')
    with pytest.raises(ValueError, match='HST CRDS server'):
        check_crds_server('https://hst-crds.stsci.edu', 'jwst', 'x')


def test_each_instrument_resolves_its_own_context():
    cfg = {'environment': {'CRDS_CONTEXT': _JWST, 'HST_CRDS_CONTEXT': _HST,
                           'CRDS_SERVER_URL': 'https://jwst-crds.stsci.edu',
                           'HST_CRDS_SERVER_URL': 'https://hst-crds.stsci.edu'}}
    assert crds_context_for(cfg, 'nircam') == _JWST
    assert crds_context_for(cfg, 'nirspec') == _JWST
    assert crds_context_for(cfg, 'acswfc') == _HST
    assert crds_context_for(cfg, 'wfc3ir') == _HST
    assert crds_server_for(cfg, 'nircam') == 'https://jwst-crds.stsci.edu'
    assert crds_server_for(cfg, 'wfc3ir') == 'https://hst-crds.stsci.edu'
    # an instrument's own pin replaces its observatory's default, for that
    # instrument only
    pinned = dict(cfg, nircam={'crds_context': 'jwst_1500.pmap'},
                  wfc3ir={'crds_context': 'hst_1400.pmap'})
    assert crds_context_for(pinned, 'nircam') == 'jwst_1500.pmap'
    assert crds_context_for(pinned, 'nirspec') == _JWST
    assert crds_context_for(pinned, 'wfc3ir') == 'hst_1400.pmap'
    assert crds_context_for(pinned, 'acswfc') == _HST
    # HST never falls back to the JWST default
    with pytest.raises(ValueError, match='HST_CRDS_CONTEXT: no CRDS context'):
        crds_context_for({'environment': {'CRDS_CONTEXT': _JWST}}, 'acswfc')
    # and the wrong series is refused wherever it is put
    with pytest.raises(ValueError, match='JWST CRDS context'):
        crds_context_for({'acswfc': {'crds_context': _JWST}}, 'acswfc')
    with pytest.raises(ValueError, match='JWST CRDS context'):
        crds_context_for({'environment': {'HST_CRDS_CONTEXT': _JWST}}, 'wfc3ir')
    with pytest.raises(ValueError, match='HST CRDS context'):
        crds_context_for(dict(cfg, nirspec={'crds_context': _HST}), 'nirspec')
    with pytest.raises(ValueError, match='HST CRDS context'):
        crds_context_for({'environment': {'CRDS_CONTEXT': _HST}}, 'nircam')
    with pytest.raises(ValueError, match='JWST CRDS server'):
        crds_server_for({'environment': {
            'HST_CRDS_SERVER_URL': 'https://jwst-crds.stsci.edu'}}, 'acswfc')
    # the shipped file passes its own rules
    shipped = _load_package_defaults()
    assert crds_context_for(shipped, 'nircam').startswith('jwst_')
    for detector in _DETECTORS:
        assert crds_context_for(shipped, detector).startswith('hst_')
        assert 'hst-crds' in crds_server_for(shipped, detector)


def test_the_environment_takes_the_instrument_pin(monkeypatch):
    for key in ('CRDS_CONTEXT', 'CRDS_SERVER_URL', 'CRDS_PATH'):
        monkeypatch.setenv(key, 'placeholder')
    for key in ('HST_CRDS_CONTEXT', 'HST_CRDS_SERVER_URL'):
        monkeypatch.delenv(key, raising=False)
    cfg = {'environment': {'CRDS_CONTEXT': _JWST, 'HST_CRDS_CONTEXT': _HST,
                           'HST_CRDS_SERVER_URL': 'https://hst-crds.stsci.edu'},
           'nircam': {'crds_context': 'jwst_1500.pmap'}}
    setup_environment(cfg, instrument='nirspec')
    assert os.environ['CRDS_CONTEXT'] == _JWST
    setup_environment(cfg, instrument='nircam')
    assert os.environ['CRDS_CONTEXT'] == 'jwst_1500.pmap'
    # an HST command exports nothing of HST's and leaves the JWST context of
    # the process as the default
    setup_environment(cfg, instrument='acswfc')
    assert os.environ['CRDS_CONTEXT'] == _JWST
    assert 'HST_CRDS_CONTEXT' not in os.environ
    assert 'HST_CRDS_SERVER_URL' not in os.environ
    # a context of the wrong observatory stops the command, whatever runs
    with pytest.raises(ValueError, match='HST CRDS context'):
        setup_environment({'environment': {'CRDS_CONTEXT': _HST}})
    with pytest.raises(ValueError, match='JWST CRDS context'):
        setup_environment({'environment': {'HST_CRDS_CONTEXT': _JWST}},
                          instrument='nircam')
    # an HST command with no HST context anywhere stops at the start
    with pytest.raises(ValueError, match='no CRDS context'):
        setup_environment({'environment': {'CRDS_CONTEXT': _JWST}},
                          instrument='wfc3ir')
    with pytest.raises(ValueError, match='unknown instrument'):
        setup_environment(cfg, instrument='hst')


def test_hst_calibrate_refuses_a_jwst_context_before_running(tmp_path):
    from campfire_pipeline.hst.steps.calibrate import calibrate_step

    with pytest.raises(ValueError, match='JWST CRDS context'):
        calibrate_step(str(tmp_path / 'absent_raw.fits'), str(tmp_path),
                       crds_context=_JWST, crds_path=str(tmp_path))
    with pytest.raises(ValueError, match='JWST CRDS server'):
        calibrate_step(str(tmp_path / 'absent_raw.fits'), str(tmp_path),
                       crds_context=_HST, crds_path=str(tmp_path),
                       crds_server='https://jwst-crds.stsci.edu')
