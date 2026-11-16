"""`cfpipe query`: what MAST holds, by program, field or position, for JWST and
HST. No network: the catalog service is replaced by a function returning rows.
"""

import json

import numpy as np
import pytest
from click.testing import CliRunner

from campfire_pipeline.common import mast_query as mq


def _square(ra, dec, half=0.02):
    """A CAOM footprint string for a square of ``half`` degrees about a point."""
    c = np.cos(np.radians(dec))
    pts = [(ra - half / c, dec - half), (ra + half / c, dec - half),
           (ra + half / c, dec + half), (ra - half / c, dec + half)]
    return 'POLYGON ' + ' '.join(f'{r % 360.0:.6f} {d:.6f}' for r, d in pts)


def _row(obsid, program, filt, ra, dec, *, mjd='57615.5', kind='image',
         exptime='500.0', rights='PUBLIC', pi='Coe, Dan'):
    return {'observationid': obsid, 'prpid': program, 'prppi': pi,
            'trgname': 'TARGET', 'intent': 'science', 'enrbandpassname': filt,
            'timexposure': exptime, 'timmin': mjd, 'datarights': rights,
            'dataproducttype': kind, 'posboundsstcs': _square(ra, dec)}


_HST_ROWS = [
    _row('jczgo1aaq', '14096', 'F814W', 289.27, -33.52),
    _row('jczgo1abq', '14096', 'F814W', 289.27, -33.52),
    _row('jczgo1acq', '14096', 'F435W', 289.27, -33.52),
    _row('jczgo1acq', '14096', 'F435W', 289.27, -33.52),          # listed twice
    _row('jd0001aaq', '15000', 'F814W', 289.60, -33.52),          # 0.3 deg away
    _row('jd0002aaq', '15000', 'G800L', 289.27, -33.52, kind='spectrum'),
    dict(_row('jd0003aaq', '15000', 'F606W', 289.27, -33.52), posboundsstcs=''),
]


def _tap(rows):
    seen = []

    def run(adql):
        seen.append(adql)
        return list(rows)
    run.seen = seen
    return run


def test_an_instrument_implies_its_telescope_and_a_mismatch_is_refused():
    assert [i.token for i in mq.resolve_instruments('hst')] == ['acswfc', 'wfc3ir']
    assert [i.token for i in mq.resolve_instruments('jwst')] == ['nircam', 'nirspec']
    assert [i.token for i in mq.resolve_instruments(None, ['WFC3IR'])] == ['wfc3ir']
    assert [i.token for i in mq.resolve_instruments(
        'hst', ['acswfc', 'acswfc'])] == ['acswfc']
    with pytest.raises(ValueError, match='not on HST'):
        mq.resolve_instruments('hst', ['nircam'])
    with pytest.raises(ValueError, match='telescope or an instrument'):
        mq.resolve_instruments()
    with pytest.raises(ValueError, match='unknown instrument'):
        mq.resolve_instruments(None, ['miri'])


def test_the_query_text_names_what_was_asked_for():
    acs = mq.INSTRUMENTS['acswfc']
    adql = mq.build_adql(acs, programs=[14096, 16729])
    assert "o.collection = 'HST'" in adql and "o.insname LIKE 'ACS/WFC'" in adql
    assert "o.prpid IN ('14096', '16729')" in adql
    assert "p.calibrationlevel = 1" in adql and "o.intent = 'science'" in adql
    assert 'trgposra' not in adql
    assert "o.intent = " not in mq.build_adql(acs, programs=[1], science_only=False)

    box = mq.build_adql(acs, regions=[mq.cone_polygon(150.0, 2.0, 0.05)],
                        pad_deg=0.3)
    assert 'o.trgposdec BETWEEN 1.650000 AND 2.350000' in box
    assert 'o.trgposra BETWEEN 149.6' in box and 'AND 150.3' in box
    # a region across RA = 0 is asked for as two ranges
    wrap = mq.build_adql(acs, regions=[mq.cone_polygon(0.02, 10.0, 0.05)],
                         pad_deg=0.1)
    assert 'o.trgposra >= 359.' in wrap and 'OR o.trgposra <= 0.' in wrap


def test_footprint_strings_and_overlap():
    (one,) = mq.parse_stcs('POLYGON ICRS 10.0 1.0 10.1 1.0 10.1 1.1 10.0 1.1')
    assert one.shape == (4, 2) and one[2].tolist() == [10.1, 1.1]
    two = mq.parse_stcs('POLYGON 1 1 2 1 2 2 POLYGON 5 5 6 5 6 6 5 6')
    assert [p.shape for p in two] == [(3, 2), (4, 2)]
    assert mq.parse_stcs('') == [] and mq.parse_stcs('CIRCLE 1 2 3') == []

    a = mq.cone_polygon(150.0, 2.0, 0.05)
    assert mq.polygons_overlap(a, mq.cone_polygon(150.06, 2.0, 0.05))
    assert not mq.polygons_overlap(a, mq.cone_polygon(150.2, 2.0, 0.05))
    assert mq.polygons_overlap(a, mq.cone_polygon(150.0, 2.0, 0.5))    # inside
    # two regions on either side of RA = 0
    assert mq.polygons_overlap(mq.cone_polygon(359.99, 0.0, 0.03),
                               mq.cone_polygon(0.01, 0.0, 0.03))


def test_exposures_are_selected_by_footprint_filter_and_kind():
    tap = _tap(_HST_ROWS)
    region = [mq.cone_polygon(289.27, -33.52, 0.05)]
    messages = []
    got = mq.query_exposures([mq.INSTRUMENTS['acswfc']], regions=region,
                             tap=tap, log=messages.append)
    # the far one, the grism one and the one without a footprint are out; the
    # one listed twice counts once
    assert [e.obs_id for e in got] == ['jczgo1aaq', 'jczgo1abq', 'jczgo1acq']
    assert 'without a footprint' in messages[0]
    e = got[0]
    assert (e.instrument, e.telescope, e.program) == ('acswfc', 'hst', '14096')
    assert e.filter == 'f814w' and e.exptime == 500.0 and e.date == '2016-08-15'
    assert e.glob == 'jczgo1*' and e.exposure == 'jczgo1aaq'    # the visit

    only = mq.query_exposures([mq.INSTRUMENTS['acswfc']], regions=region,
                              filters=['F435W'], tap=tap, log=messages.append)
    assert [x.obs_id for x in only] == ['jczgo1acq']
    # by program alone nothing is tested on the sky
    every = mq.query_exposures([mq.INSTRUMENTS['acswfc']], programs=[15000],
                               tap=tap, log=messages.append)
    assert {x.obs_id for x in every} >= {'jd0001aaq', 'jd0003aaq'}
    with pytest.raises(ValueError, match='program, a field or a position'):
        mq.query_exposures([mq.INSTRUMENTS['acswfc']], tap=tap)


def test_jwst_detector_files_count_as_one_exposure():
    rows = [_row(f'jw06882060001_02101_0000{k}_{det}', '6882', 'F200W', 289.27,
                 -33.52, mjd='60953.0', exptime='311.0', pi='Fujimoto, Seiji')
            for k in (1, 2) for det in ('nrca1', 'nrca2', 'nrcb1')]
    rows.append(dict(_row('jw06882060001_02101_00003_nrca1', '6882', 'F200W',
                          289.27, -33.52), timmin=''))             # not taken
    got = mq.query_exposures([mq.INSTRUMENTS['nircam']], programs=[6882],
                             tap=_tap(rows), log=lambda m: None)
    assert len(got) == 7 and got[0].glob == 'jw06882060*'
    assert got[0].exposure == 'jw06882060001_02101_00001'
    (row,) = mq.summarize(got)
    assert row['n_files'] == 7 and row['n_exposures'] == 3
    assert row['exptime'] == pytest.approx(311.0 * 2 + 500.0)
    untaken = [e for e in got if not np.isfinite(e.mjd)]
    assert len(untaken) == 1 and untaken[0].date == ''
    assert untaken[0].to_dict()['mjd'] is None


def test_the_summary_ends_with_the_globs_to_paste():
    got = mq.query_exposures([mq.INSTRUMENTS['acswfc']], programs=[14096, 15000],
                             tap=_tap(_HST_ROWS), log=lambda m: None)
    text = mq.format_summary(got)
    assert 'acswfc: 5 exposures in 2 programs' in text
    # one visit of 14096 is named, not the program: its other visits are
    # other clusters
    assert "'jczgo1*',   # 14096  Coe, Dan  f435w f814w  3 exposures" in text
    assert "'jd00{01,03}*',   # 15000" in text
    assert mq.brace_globs({'jw06882060*', 'jw06882061*', 'jw01727001*'}) == [
        'jw01727001*', 'jw06882{060,061}*']
    assert mq.format_summary([]) == 'no exposures found'
    lines = mq.format_exposures(got).splitlines()
    assert len(lines) == 5 and lines[0].split()[:3] == ['acswfc', '14096', 'jczgo1aaq']
    listed = json.loads(mq.to_json(got))
    assert listed[0]['glob'] == 'jczgo1*' and len(listed[0]['footprint'][0]) == 4


def test_the_command(monkeypatch):
    from campfire_pipeline import cli
    from campfire_pipeline.common import query as jwst_query

    tap = _tap(_HST_ROWS)
    monkeypatch.setattr(jwst_query, '_tap_query', tap)
    runner = CliRunner()

    res = runner.invoke(cli.main, ['query', '--instrument', 'acswfc', '--target',
                                   '289.27 -33.52', '--radius', '3'])
    assert res.exit_code == 0, res.output
    assert "'jczgo1*'" in res.output and 'f814w' in res.output
    assert 'trgposra' in tap.seen[-1]

    res = runner.invoke(cli.main, ['query', '--telescope', 'hst', '--program',
                                   '14096', '--format', 'json'])
    assert res.exit_code == 0, res.output
    assert len(tap.seen) == 3                    # one query per HST instrument

    for args, message in (
            (['query', '--program', '1'], 'telescope or an instrument'),
            (['query', '--telescope', 'hst'], '--program, --field or --target'),
            (['query', '--telescope', 'hst', '--instrument', 'nircam',
              '--program', '1'], 'not on HST'),
            (['query', '--telescope', 'hst', '--field', 'x', '--target', '1 2'],
             'not both'),
            (['query', '--telescope', 'hst', '--target', '400 2'], 'RA must be')):
        res = runner.invoke(cli.main, args)
        assert res.exit_code != 0 and message in res.output, res.output


# --- HST download -------------------------------------------------------------

def _archive(rows, without_raw=()):
    """A catalog stand-in answering both queries: exposures, and their RAW
    files (30 bytes each)."""
    def run(adql):
        run.seen.append(adql)
        if 'caomartifact' not in adql:
            return list(rows)
        out = []
        for r in rows:
            obs = r['observationid']
            if f"'{obs}'" in adql and obs not in without_raw:
                out.append({'observationid': obs, 'productfilename': f'{obs}_raw.fits',
                            'datauri': f'mast:HST/product/{obs}_raw.fits',
                            'contentlength': '30', 'contentchecksum': 'md5:x'})
                out.append({'observationid': obs, 'productfilename': f'{obs}_spt.fits',
                            'datauri': 'x', 'contentlength': '5',
                            'contentchecksum': ''})
        return out
    run.seen = []
    return run


@pytest.fixture
def fetched(monkeypatch):
    """Replace the network download: write each file at its stated size."""
    from campfire_pipeline.common import query as jwst_query

    calls = []

    def fake(files, token=None, workers=4, desc=''):
        for f in files:
            f['_path'].parent.mkdir(parents=True, exist_ok=True)
            f['_path'].write_bytes(b'x' * f['size'])
        calls.append([f['filename'] for f in files])
        return 0
    monkeypatch.setattr(jwst_query, 'download_files', fake)
    return calls


def test_hst_raw_files_land_where_the_layout_says(tmp_path, fetched):
    from astropy.table import Table

    tap = _archive(_HST_ROWS, without_raw=('jd0001aaq',))
    exposures = mq.query_exposures([mq.INSTRUMENTS['acswfc']],
                                   programs=[14096, 15000], tap=tap,
                                   log=lambda m: None)
    messages = []
    counts = mq.download_hst(exposures, tmp_path, tap=tap, log=messages.append)
    assert counts == {'selected': 5, 'missing': 1, 'present': 0,
                      'downloaded': 4, 'errors': 0}
    assert 'no RAW file in the archive: jd0001aaq' in messages[0]
    raw = tmp_path / 'raw' / 'acswfc'
    assert (raw / '14096' / 'f814w' / 'jczgo1aaq_raw.fits').stat().st_size == 30
    assert (raw / '14096' / 'f435w' / 'jczgo1acq_raw.fits').exists()
    assert (raw / '15000' / 'f606w' / 'jd0003aaq_raw.fits').exists()
    assert not list(raw.rglob('*_spt.fits'))
    manifest = Table.read(raw / '14096' / 'manifest.ecsv')
    assert list(manifest['rootname']) == ['jczgo1aaq', 'jczgo1abq', 'jczgo1acq']
    assert manifest['filter'][2] == 'f435w' and str(manifest['program'][0]) == '14096'
    assert manifest['s_region'][0].startswith('POLYGON ICRS 289.')

    # a second run downloads nothing and keeps the manifest rows
    again = mq.download_hst(exposures, tmp_path, tap=tap, log=messages.append)
    assert again['present'] == 4 and again['downloaded'] == 0
    assert fetched == [['jczgo1aaq_raw.fits', 'jczgo1abq_raw.fits',
                        'jczgo1acq_raw.fits', 'jd0003aaq_raw.fits'], []]
    assert len(Table.read(raw / '14096' / 'manifest.ecsv')) == 3


def test_a_dry_run_writes_nothing_and_jwst_exposures_are_refused(tmp_path, fetched):
    tap = _archive(_HST_ROWS)
    exposures = mq.query_exposures([mq.INSTRUMENTS['acswfc']], programs=[14096],
                                   tap=tap, log=lambda m: None)
    lines = []
    counts = mq.download_hst(exposures, tmp_path, tap=tap, dry_run=True,
                             log=lines.append)
    assert counts['downloaded'] == 0 and fetched == []
    assert not (tmp_path / 'raw').exists()
    assert any('would download raw/acswfc/14096/f814w/jczgo1aaq_raw.fits' in m
               for m in lines)

    # what query wrote as JSON comes back as the same exposures
    assert mq.from_json(mq.to_json(exposures)) == exposures
    with pytest.raises(ValueError, match='not the JSON'):
        mq.from_json('acswfc 14096')

    jwst = mq.Exposure('nircam', 'jwst', 'jw06882060001_02101_00001_nrca1', '6882',
                       '', '', 'f200w', 311.0, 60953.0, 'PUBLIC', ())
    with pytest.raises(ValueError, match='JWST'):
        mq.download_hst([jwst], tmp_path, tap=tap)
    # an open filter-wheel position is not part of the directory name
    two = mq.Exposure('acswfc', 'hst', 'jx', '1', '', '', 'f606w;clear2l', 1.0,
                      1.0, '', ())
    assert two.filter_dir == 'f606w'


def test_the_download_command_takes_the_query_selection(tmp_path, monkeypatch,
                                                        fetched):
    from campfire_pipeline import cli
    from campfire_pipeline.common import query as jwst_query

    tap = _archive(_HST_ROWS)
    monkeypatch.setattr(jwst_query, '_tap_query', tap)
    runner = CliRunner()
    raw = str(tmp_path / 'raw')

    res = runner.invoke(cli.main, ['download', '--instrument', 'acswfc',
                                   '--program', '14096', '--download-dir', raw,
                                   '--dry-run'])
    assert res.exit_code == 0, res.output
    assert 'would download raw/acswfc/14096/f435w/jczgo1acq_raw.fits' in res.output
    assert fetched == []

    # the JSON of `cfpipe query`, from a file and from standard input
    listed = runner.invoke(cli.main, ['query', '--instrument', 'acswfc',
                                      '--program', '14096', '--format', 'json'])
    text = listed.output[listed.output.index('['):]
    (tmp_path / 'sel.json').write_text(text)
    res = runner.invoke(cli.main, ['download', '--from', str(tmp_path / 'sel.json'),
                                   '--download-dir', raw])
    assert res.exit_code == 0, res.output
    assert (tmp_path / 'raw/acswfc/14096/f814w/jczgo1aaq_raw.fits').exists()
    res = runner.invoke(cli.main, ['download', '--from', '-', '--download-dir', raw],
                        input=text)
    # (the stand-in archive does not apply the program cut, so all five come)
    assert res.exit_code == 0 and '5 already on disk' in res.output, res.output

    for args, message in (
            (['download', '--telescope', 'hst'], '--program, --field, --target'),
            (['download', '--from', 'x.json', '--program', '1'], 'as listed'),
            (['download', '--instrument', 'nircam'], '--program is required'),
            (['download', '--program', '1', '--field', 'x'], 'HST exposures')):
        res = runner.invoke(cli.main, args)
        assert res.exit_code != 0 and message in res.output, res.output
