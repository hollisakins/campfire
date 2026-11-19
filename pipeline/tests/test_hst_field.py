"""The HST instruments' part of a field: what fields.toml says, where the files
are, and which exposures may go into a mosaic.

The files here are empty FITS headers with the step stamps under test; no HST
software is needed.
"""

import os
import time

import pytest
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.hst.field import HstField
from campfire_pipeline.nircam.field import Field

FIELDS = """
[pg]
filters = ["f444w"]
files = ["jw06882*"]
tangent_point = [289.27, -33.52]
refcat = "pg_f444w.ecsv"

[pg.A1]
rotation = 0
[pg.A1.30mas]
crpix = [500.5, 500.5]
naxis = [1000, 1000]

[pg.wfc3ir]
filters = ["F160W", "f105w"]
files = ["iczg*", "iepl{71,72}*"]
skip = ["iepl72i1q"]
[pg.wfc3ir.outlier]
protect_radius = 0.5

[pg.acswfc]
filters = ["f814w"]
files = ["jczg*"]

[hstonly]
tangent_point = [10.0, 2.0]
[hstonly.T]
rotation = 0
[hstonly.T.30mas]
crpix = [50.5, 50.5]
naxis = [100, 100]
[hstonly.acswfc]
filters = ["f606w"]
files = ["jabc*"]
"""


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'fields.toml').write_text(FIELDS)
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    return tmp_path


def _stamped(path, bkg=True, **stamps):
    """An exposure with these step stamps, and (unless ``bkg=False``) the
    background step's: a combine keeps out an exposure without it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    hdu = fits.PrimaryHDU()
    if bkg:
        stamps.setdefault('CFP_BKG', 'n_iter=3')
    for key, val in cfp.format(keyset=cfp.HST, **stamps).items():
        hdu.header[key] = val
    hdu.writeto(path, overwrite=True)
    return str(path)


def test_the_instrument_table_is_the_selection(root):
    f = HstField.load('pg', 'wfc3ir')
    assert f.filters == ['f160w', 'f105w']                 # lower case
    assert f.files == ['iczg*', 'iepl71*', 'iepl72*']      # braces expanded
    assert f.skip == ['iepl72i1q']
    assert f.step_overrides == {'outlier': {'protect_radius': 0.5}}
    assert f.refcat == 'pg_f444w.ecsv'                     # the field's catalog
    assert f.canonical_suffix == '_flt.fits'
    assert HstField.load('pg', 'acswfc').canonical_suffix == '_flc.fits'


def test_tiles_are_the_fields_and_shared_with_nircam(root):
    hst = HstField.load('pg', 'wfc3ir')
    nircam = Field.load('pg')
    assert list(hst.tiles) == list(nircam.tiles) == ['A1']
    assert hst.get_tile_wcs('A1', '30mas') == nircam.get_tile_wcs('A1', '30mas')
    assert hst.get_tile_corners('A1') == nircam.get_tile_corners('A1')


def test_a_field_needs_no_nircam_exposures(root):
    f = HstField.load('hstonly', 'acswfc')
    assert list(f.tiles) == ['T'] and f.refcat is None


@pytest.mark.parametrize('edit, message', [
    (('[pg.acswfc]', '[pg.acsxxx]'), 'has no [pg.acswfc] table'),
    (('files = ["jczg*"]', 'files = ["iczg*"]'), "starts with 'j'"),
    (('files = ["jczg*"]', 'files = ["j*"]'), 'program code'),
    (('files = ["jczg*"]', 'files = []'), 'files is empty'),
    (('files = ["jczg*"]', 'files = ["jczg*"]\ntiles = ["A1"]'), 'unknown key'),
])
def test_a_wrong_table_stops_with_the_reason(root, edit, message):
    path = root / 'config' / 'fields.toml'
    path.write_text(FIELDS.replace(*edit))
    with pytest.raises(ValueError, match=message.replace('[', r'\[')):
        HstField.load('pg', 'acswfc')


def test_raw_files_are_found_in_every_proposal_directory(root):
    f = HstField.load('pg', 'wfc3ir')
    f.setup_workspace()
    made = {}
    for pid, name in (('14096', 'iczgo2dlq'), ('16729', 'iepl71abq'),
                      ('16729', 'iepl72i1q'),          # skipped
                      ('15000', 'idxx01abq')):         # no glob selects it
        made[name] = _stamped(str(root / 'raw' / 'wfc3ir' / pid / 'f160w'
                                  / f'{name}_raw.fits'))
    _stamped(str(root / 'raw' / 'wfc3ir' / '14096' / 'f105w' / 'iczgo3aaq_raw.fits'))
    assert f.get_raw_files('f160w') == sorted([made['iczgo2dlq'], made['iepl71abq']])
    assert f.get_raw_files('f160w', skip=['iczgo2*']) == [made['iepl71abq']]
    assert len(f.get_raw_files('f105w')) == 1
    assert f.canonical_path(made['iczgo2dlq'], 'f160w') == str(
        root / 'products' / 'wfc3ir' / 'pg' / 'f160w' / 'iczgo2dlq_flt.fits')
    assert f.filter_dir('f160w', work=True) == str(
        root / 'products' / 'wfc3ir_work' / 'pg' / 'f160w')
    assert f.refcat_path() == str(
        root / 'reference' / 'nircam' / 'pg' / 'astrom_cats' / 'pg_f444w.ecsv')


def test_only_aligned_and_cleaned_exposures_get_a_working_copy(root):
    ir = HstField.load('pg', 'wfc3ir')
    ir.setup_workspace()
    d = ir.filter_dir('f160w')
    good = _stamped(f'{d}/iczgo2aaq_flt.fits', CFP_ALGN='fit=rigid rc=abc')
    _stamped(f'{d}/iczgo2abq_flt.fits', CFP_ALGN=cfp.NOT_ALIGNED)
    _stamped(f'{d}/iczgo2acq_flt.fits')
    # aligned, but its background is still in the pixels
    _stamped(f'{d}/iczgo2adq_flt.fits', bkg=False, CFP_ALGN='fit=rigid rc=abc')
    paths, held = ir.materialize_work('f160w')
    assert [os.path.basename(p) for p in paths] == ['iczgo2aaq_flt.fits']
    assert sorted(reason for _, reason in held) == [
        'alignment failed (NOT_ALIGNED)',
        'background not subtracted yet (bkg has not run)', 'not aligned yet']
    # an unaligned exposure can be forced in; one with its background cannot
    paths, held = ir.materialize_work('f160w', include_unaligned=True)
    assert len(paths) == 3
    assert [reason for _, reason in held] == [
        'background not subtracted yet (bkg has not run)']
    # and a copy of an exposure that is no longer ready is removed again
    paths, _ = ir.materialize_work('f160w')
    assert os.listdir(ir.filter_dir('f160w', work=True)) == ['iczgo2aaq_flt.fits']

    # a working copy is replaced only when the canonical file is newer
    work = paths[0]
    with fits.open(work, mode='update') as hdul:
        hdul[0].header['CFP_OUT'] = 'field n=2'
    ir.materialize_work('f160w')
    assert fits.getheader(work)['CFP_OUT'] == 'field n=2'
    later = time.time() + 5
    os.utime(good, (later, later))
    ir.materialize_work('f160w')
    assert 'CFP_OUT' not in fits.getheader(work)


def test_an_acs_exposure_without_cosmic_ray_flags_is_held_back(root):
    """Satellite trails are masks in the region file, not a condition: an
    exposure the trail step has not seen still goes in."""
    acs = HstField.load('pg', 'acswfc')
    acs.setup_workspace()
    d = acs.filter_dir('f814w')
    _stamped(f'{d}/jczgo1aaq_flc.fits', CFP_CR='adz n=2', CFP_ALGN='fit=linear')
    lone = _stamped(f'{d}/jczgo5aaq_flc.fits', CFP_TRL='findsat n=0 reg=none',
                    CFP_ALGN='fit=rigid')
    paths, held = acs.materialize_work('f814w')
    assert [os.path.basename(p) for p in paths] == ['jczgo1aaq_flc.fits']
    assert held == [(lone, 'no cosmic-ray flags yet (crreject has not run)')]


def test_everything_selected_that_is_not_in_the_mosaic_is_named(root, monkeypatch):
    """Held back, failed, or not even calibrated: each is reported with its
    reason, by the combine and by `check`."""
    from campfire_pipeline.hst import orchestrate

    ir = HstField.load('pg', 'wfc3ir')
    ir.setup_workspace()
    d = ir.filter_dir('f160w')
    _stamped(f'{d}/iczgo2aaq_flt.fits', CFP_ALGN='fit=linear')
    _stamped(f'{d}/iczgo2abq_flt.fits')
    _stamped(f'{d}/iczgo2acq_flt.fits', CFP_ALGN=cfp.NOT_ALIGNED)
    raw = root / 'raw' / 'wfc3ir' / '14096' / 'f160w'
    raw.mkdir(parents=True)
    for name in ('iczgo2aaq', 'iczgo2adq'):              # the second: RAW only
        fits.PrimaryHDU().writeto(raw / f'{name}_raw.fits')

    assert orchestrate.uncalibrated(ir, 'f160w') == ['iczgo2adq']
    reasons = orchestrate.held_back(ir, 'f160w')
    assert reasons == {
        'not calibrated yet (RAW file selected, no calibrated exposure)': ['iczgo2adq'],
        'not aligned yet': ['iczgo2abq'],
        'alignment failed (NOT_ALIGNED)': ['iczgo2acq'],
    }
    said = []
    monkeypatch.setattr(orchestrate, 'log', said.append)
    orchestrate._report_held_back(ir, 'f160w', reasons)
    text = '\n'.join(said)
    assert '3 selected wfc3ir f160w exposure(s) are NOT IN THE MOSAIC' in text
    for name in ('iczgo2adq', 'iczgo2abq', 'iczgo2acq'):
        assert name in text
    assert 'cfpipe wfc3ir process --field pg' in text
    said.clear()
    orchestrate._report_held_back(ir, 'f160w', {})
    assert said == []                                    # nothing held: no noise


def test_reviewer_exclusions_drop_exposures_like_skip(root):
    """reference/<instrument>/<field>/exposures.json, the NIRCam file: a
    name is the file name without .fits, or the bare rootname."""
    import json

    ir = HstField.load('pg', 'wfc3ir')
    ir.setup_workspace()
    d = ir.filter_dir('f160w')
    for name in ('iczgo2aaq', 'iczgo2abq', 'iczgo2acq'):
        _stamped(f'{d}/{name}_flt.fits', CFP_ALGN='fit=linear')
    assert len(ir.get_exposure_files('f160w')) == 3

    os.makedirs(ir.reference_dir, exist_ok=True)
    with open(os.path.join(ir.reference_dir, 'exposures.json'), 'w') as f:
        json.dump({'excluded': {'f160w': ['iczgo2abq_flt', 'iczgo2acq'],
                                'f125w': ['iczgo2aaq_flt']}}, f)
    ir.setup_workspace()
    assert [os.path.basename(p) for p in ir.get_exposure_files('f160w')] == [
        'iczgo2aaq_flt.fits']
    # the mask of an exposure is looked for under its file name
    assert ir.mask_file(f'{d}/iczgo2aaq_flt.fits', 'f160w').endswith(
        'reference/wfc3ir/pg/masks/f160w/iczgo2aaq_flt.reg')

    # an unreadable list is a warning, not a failed reduction
    with open(os.path.join(ir.reference_dir, 'exposures.json'), 'w') as f:
        f.write('{not json')
    ir.setup_workspace()
    assert len(ir.get_exposure_files('f160w')) == 3
