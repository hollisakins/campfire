"""Stage 2a single-call rework: the edited full metafile and the split.

The per-source metafile edit (``MetaFile.rows_for_source``) is the row operation
both arrangements share; ``for_sources`` stacks many of them into one metafile;
``_plan_rounds`` keeps two sources of one slitlet apart; ``_split_spec2_result``
turns one multi-slit product into the per-source canonicals plus the
background-slitlet file. Synthetic metafile and slits throughout — the
bit-identity of the real per-slit arrays against the per-source loop is checked
on real data (docs/design-nirspec-master-background.md §1.7).
"""
import os

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table

pytest.importorskip("campfire_pipeline")
from campfire_pipeline.nirspec.metafile import MetaFile  # noqa: E402
from campfire_pipeline.nirspec import stage2  # noqa: E402


MSAMETID = 7


def _shutter_rows(slitlet, source_id, columns, source_col, dither_points=(1, 2, 3)):
    """Rows of one slitlet for every dither point; the source sits in
    ``source_col`` at dither point 1 and moves one shutter per nod."""
    rows = []
    cols = list(columns)
    for dp in dither_points:
        src_col = cols[(cols.index(source_col) + dp - 1) % len(cols)]
        for c in cols:
            is_src = source_id is not None and c == src_col
            rows.append(dict(
                slitlet_id=slitlet, msa_metadata_id=MSAMETID, shutter_quadrant=1,
                shutter_row=100, shutter_column=c, source_id=source_id if is_src else 0,
                background='N' if is_src else 'Y', shutter_state='OPEN',
                estimated_source_in_shutter_x=0.5 if is_src else np.nan,
                estimated_source_in_shutter_y=0.5 if is_src else np.nan,
                dither_point_index=dp, primary_source='Y' if is_src else 'N',
                fixed_slit='NONE',
            ))
    return rows


def _metafile(tmp_path, extra_rows=()):
    rows = []
    rows += _shutter_rows(11, 1001, [20, 21, 22], 21)          # source 1001
    rows += _shutter_rows(12, 1002, [30, 31, 32], 31)          # source 1002
    rows += _shutter_rows(12, 1003, [30, 31, 32], 31)[:0]      # (placeholder)
    rows += _shutter_rows(13, None, [40, 41, 42], 40)          # background-only
    rows += list(extra_rows)
    shutter = Table(rows=rows)
    source = Table(rows=[
        dict(program=7076, source_id=1001, source_name='7076_1001', alias='a', ra=1.0, dec=1.0, preimage_id='x', stellarity=0.2),
        dict(program=7076, source_id=1002, source_name='7076_1002', alias='b', ra=2.0, dec=2.0, preimage_id='x', stellarity=0.2),
        dict(program=7076, source_id=1003, source_name='7076_1003', alias='c', ra=3.0, dec=3.0, preimage_id='x', stellarity=0.2),
    ])
    hdul = fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.zeros((2, 2)), name='SHUTTER_IMAGE'),
                         fits.BinTableHDU(shutter.as_array(), name='SHUTTER_INFO'),
                         fits.BinTableHDU(source.as_array(), name='SOURCE_INFO')])
    path = str(tmp_path / 'jw07076001001_01_msa.fits')
    hdul.writeto(path)
    return MetaFile(fits.open(path), os.path.basename(path), MSAMETID)


def _two_sources_one_slitlet():
    """Source 1003 shares slitlet 12 with 1002, 0.5" apart (not a duplicate)."""
    rows = []
    for dp in (1, 2, 3):
        rows.append(dict(
            slitlet_id=12, msa_metadata_id=MSAMETID, shutter_quadrant=1, shutter_row=100,
            shutter_column=32, source_id=1003, background='N', shutter_state='OPEN',
            estimated_source_in_shutter_x=0.4, estimated_source_in_shutter_y=0.4,
            dither_point_index=dp, primary_source='Y', fixed_slit='NONE'))
    return rows


def _same_rows(a, b):
    """Row-for-row equality of two shutter tables (NaN-tolerant)."""
    if len(a) != len(b) or a.colnames != b.colnames:
        return False
    for col in a.colnames:
        x, y = np.asarray(a[col]), np.asarray(b[col])
        if x.dtype.kind == 'f':
            if not np.array_equal(x, y, equal_nan=True):
                return False
        elif not np.array_equal(x, y):
            return False
    return True


def test_rows_for_source_matches_filter_by_source_id(tmp_path):
    mf = _metafile(tmp_path)
    rows = mf.rows_for_source(1001)
    per_source = mf.filter_by_source_id(1001, set_stellarity=1.0)
    assert _same_rows(rows, per_source.shutter_table)
    # one primary per dither point, everything else an empty background shutter
    for dp in (1, 2, 3):
        sub = rows[rows['dither_point_index'] == dp]
        assert (sub['primary_source'] == 'Y').sum() == 1
        assert set(sub['source_id'][sub['primary_source'] == 'N']) == {0}
        assert set(sub['background'][sub['primary_source'] == 'N']) == {'Y'}


def test_background_slitlet_ids_excludes_source_slitlets(tmp_path):
    mf = _metafile(tmp_path)
    assert mf.background_slitlet_ids() == [13]
    assert mf.slitlets_of(1001) == [11]
    bkg = mf.background_slitlet_rows()
    assert set(bkg['slitlet_id']) == {13}
    assert len(bkg) == 9


def test_for_sources_stacks_edits_and_keeps_source_table(tmp_path):
    mf = _metafile(tmp_path)
    rows = {sid: mf.rows_for_source(sid) for sid in (1001, 1002)}
    edited = mf.for_sources(rows, set_stellarity=1.0, filename='x_msa_r1.fits',
                            background_slitlets=mf.background_slitlet_ids())
    assert edited.filename == 'x_msa_r1.fits'
    assert set(edited.shutter_table['slitlet_id']) == {11, 12, 13}
    # each source's rows are exactly its per-source edit
    for sid, slit in ((1001, 11), (1002, 12)):
        got = edited.shutter_table[edited.shutter_table['slitlet_id'] == slit]
        assert _same_rows(got, rows[sid])
    # background rows untouched
    got = edited.shutter_table[edited.shutter_table['slitlet_id'] == 13]
    assert _same_rows(got, mf.background_slitlet_rows())
    # SOURCE_INFO kept whole, stellarity forced like the per-source file
    assert set(edited.source_table['source_id']) == {1001, 1002, 1003}
    assert np.all(edited.source_table['stellarity'] == 1.0)
    assert np.all(mf.source_table['stellarity'] == 0.2)  # original untouched
    # it writes (the HDU sync round-trips the stacked table)
    edited.write(str(tmp_path), overwrite=True)
    with fits.open(tmp_path / 'x_msa_r1.fits') as h:
        assert len(h[2].data) == len(edited.shutter_table)


def test_plan_rounds_separates_sources_sharing_a_slitlet(tmp_path):
    mf = _metafile(tmp_path, extra_rows=_two_sources_one_slitlet())
    assert mf.slitlets_of(1003) == [12]
    rounds = stage2._plan_rounds(mf, [1001, 1002, 1003])
    assert [r['source_ids'] for r in rounds] == [[1001, 1002], [1003]]
    assert rounds[0]['slitlets'] == {11, 12}
    assert all(r['fixed_slit'] is False for r in rounds)
    # the normal case: one round
    assert [r['source_ids'] for r in stage2._plan_rounds(mf, [1001, 1002])] == [[1001, 1002]]
    assert stage2._plan_rounds(mf, []) == []


def test_plan_rounds_splits_fixed_slit_sources_only_when_asked(tmp_path, monkeypatch):
    mf = _metafile(tmp_path)
    monkeypatch.setattr(MetaFile, 'is_fixed_slit', lambda self, sid: sid == 1002)
    assert [r['source_ids'] for r in stage2._plan_rounds(mf, [1001, 1002])] == [[1001, 1002]]
    rounds = stage2._plan_rounds(mf, [1001, 1002], split_fixed_slit=True)
    assert [(r['source_ids'], r['fixed_slit']) for r in rounds] == [([1001], False), ([1002], True)]


def test_drop_stuck_removes_physical_shutters_and_reports_ordinals(tmp_path):
    mf = _metafile(tmp_path)
    rows = mf.rows_for_source(1001)          # q1 r100, columns 20, 21, 22
    out, ordinals, keys = stage2._drop_stuck(rows, {(1, 100, 20), (1, 100, 22), (1, 100, 41)})
    assert sorted(set(out['shutter_column'])) == [21]
    assert ordinals == [1, 3]
    assert keys == ['q1r100c20', 'q1r100c22']
    same, ordinals, keys = stage2._drop_stuck(rows, set())
    assert same is rows and ordinals is None and keys == []
    # all stuck -> empty, which the caller treats as "skip extraction"
    out, _, _ = stage2._drop_stuck(rows, {(1, 100, c) for c in (20, 21, 22)})
    assert len(out) == 0


def _slit(name, source_id, source_name, value, stellarity=1.0):
    from jwst.datamodels import SlitModel
    s = SlitModel(np.full((3, 4), float(value), dtype=np.float32))
    s.err = np.ones((3, 4), dtype=np.float32)
    s.dq = np.zeros((3, 4), dtype=np.uint32)
    s.name = str(name)
    s.source_id = int(source_id)
    s.source_name = source_name
    s.stellarity = stellarity
    s.source_type = 'POINT' if stellarity > 0.75 else 'EXTENDED'
    return s


def test_split_spec2_result_writes_canonicals_and_bkgslits(tmp_path, monkeypatch):
    from jwst.datamodels import MultiSlitModel

    monkeypatch.chdir(tmp_path)
    parent = MultiSlitModel()
    parent.meta.instrument.name = 'NIRSPEC'
    parent.meta.instrument.detector = 'NRS1'
    parent.meta.exposure.type = 'NRS_MSASPEC'
    parent.meta.observation.date = '2024-01-01'
    parent.meta.observation.time = '00:00:00'
    # source 1001, a two-slitlet source 1002 (two slits, same id), a background
    # slitlet whose slitlet_id collides with catalog id 1001, and a slit of a
    # source not in this round.
    parent.slits.append(_slit(11, 1001, '7076_1001', 1.0))
    parent.slits.append(_slit(12, 1002, '7076_1002', 2.0))
    parent.slits.append(_slit(15, 1002, '7076_1002', 2.5))
    parent.slits.append(_slit(1001, 1001, '7076_BKG1001', 9.0, stellarity=0.0))
    parent.slits.append(_slit(14, 1004, '7076_1004', 4.0))
    parent.save('base_spec2r1_cal.fits')
    parent.close()

    # No bar-shadow reference in a unit test: the step is replaced by identity.
    import jwst.barshadow
    monkeypatch.setattr(jwst.barshadow.BarShadowStep, 'call', staticmethod(lambda m: m))
    monkeypatch.setattr(stage2, 'log', lambda *a, **k: None)

    cards = {sid: [('STKSHTRS', 'N/A', 'Stuck shutters masked')] for sid in (1001, 1002, 1003)}
    bkg_path = str(tmp_path / 'bkgslits' / 'base_bkgslits.fits')
    slitlets = {1001: {11}, 1002: {12, 15}, 1003: {13}}
    produced, missing = stage2._split_spec2_result(
        'base_spec2r1_cal.fits', 'base', [1001, 1002, 1003], cards, bkg_path, 'orig_msa.fits',
        slitlets)
    assert produced == [1001, 1002]
    assert missing == [1003]          # off-detector in this round -> caller writes the marker

    with fits.open('base_1001.fits') as h:
        assert h['PRIMARY'].header['MSAMETFL'] == 'orig_msa.fits'
        assert h['PRIMARY'].header['STKSHTRS'] == 'N/A'
        assert 'CFP_CAL' in h['PRIMARY'].header
        from campfire_pipeline.nirspec import canonical
        assert h['PRIMARY'].header['CFSCHEMA'] == canonical.CANONICAL_SCHEMA_VERSION
        assert np.all(h[('SCI', 1)].data == 1.0)
        assert h[('SCI', 1)].header['SOURCEID'] == 1001
        assert ('SCI', 2) not in h
    with fits.open('base_1002.fits') as h:
        assert np.all(h[('SCI', 1)].data == 2.0)
        assert np.all(h[('SCI', 2)].data == 2.5)
    assert not os.path.exists('base_1003.fits')
    assert not os.path.exists('base_1004.fits')       # not in this round: ignored
    assert not os.path.exists('base_1001_cal.fits')   # promoted, not left behind

    with fits.open(bkg_path) as h:
        assert h['PRIMARY'].header['CFNBKGSL'] == 1
        assert h['PRIMARY'].header['CFBARSHA'] is True
        assert h['PRIMARY'].header['MSAMETFL'] == 'orig_msa.fits'
        assert np.all(h[('SCI', 1)].data == 9.0)
        assert h[('SCI', 1)].header['SRCNAME'] == '7076_BKG1001'


def test_split_keeps_a_primary_less_slitlet_as_its_sources_product(tmp_path, monkeypatch):
    """A source whose shutter at this nod was removed as stuck closed leaves a
    slitlet jwst extracts under the background name; the per-source call kept it
    as the source's nod product, so the split must route it by slitlet."""
    from jwst.datamodels import MultiSlitModel

    monkeypatch.chdir(tmp_path)
    parent = MultiSlitModel()
    parent.meta.instrument.name = 'NIRSPEC'
    parent.meta.instrument.detector = 'NRS1'
    parent.meta.exposure.type = 'NRS_MSASPEC'
    parent.meta.observation.date = '2024-01-01'
    parent.meta.observation.time = '00:00:00'
    parent.slits.append(_slit(12, 12, '7076_BKG12', 7.0, stellarity=0.0))   # slitlet of 1002
    parent.slits.append(_slit(40, 40, '7076_BKG40', 9.0, stellarity=0.0))   # dedicated background
    parent.save('base_spec2r1_cal.fits')
    parent.close()

    import jwst.barshadow
    monkeypatch.setattr(jwst.barshadow.BarShadowStep, 'call', staticmethod(lambda m: m))
    monkeypatch.setattr(stage2, 'log', lambda *a, **k: None)

    cards = {1002: [('STKSHTRS', '[1]', 'Stuck shutters masked')]}
    bkg_path = str(tmp_path / 'bkgslits' / 'base_bkgslits.fits')
    produced, missing = stage2._split_spec2_result(
        'base_spec2r1_cal.fits', 'base', [1002], cards, bkg_path, 'orig_msa.fits', {1002: {12}})
    assert produced == [1002] and missing == []
    with fits.open('base_1002.fits') as h:
        assert h[('SCI', 1)].header['SRCNAME'] == '7076_BKG12'
        assert np.all(h[('SCI', 1)].data == 7.0)
    with fits.open(bkg_path) as h:
        assert h['PRIMARY'].header['CFNBKGSL'] == 1
        assert h[('SCI', 1)].header['SRCNAME'] == '7076_BKG40'


def test_a_failed_single_call_redoes_what_it_did_not_settle(tmp_path, monkeypatch):
    """The single call settles source 1, then fails. The fallback redoes source 2 although
    a canonical of an earlier run is on disk (it was pending: in ``rerun_source_ids``), with
    the observation's metafile for its MSAMETFL, and returns both."""
    import campfire_pipeline.config as cfg
    rate = tmp_path / 'jw01_03101_00001_nrs1_rate.fits'
    hdu = fits.PrimaryHDU()
    hdu.header.update(MSAMETFL='orig_msa.fits', EXP_TYPE='NRS_MSASPEC', GRATING='PRISM')
    hdu.writeto(rate)

    class Meta:
        filename = 'orig_msa.fits'
        unique_source_ids = np.array([1, 2])

    class Obs:
        workspace_dir = str(tmp_path)
        bkgslits_dir = str(tmp_path)

    fits.PrimaryHDU().writeto(tmp_path / 'jw01_03101_00001_nrs1_bkgslits.fits')     # done
    fits.PrimaryHDU().writeto(tmp_path / 'jw01_03101_00001_nrs1_2.fits')            # an earlier run
    monkeypatch.setattr(MetaFile, 'load_for_rate_file', staticmethod(lambda path: Meta()))
    monkeypatch.setattr(stage2, '_stuck_shutters', lambda obs, root, mf: frozenset())
    monkeypatch.setattr(cfg, 'get_empirical_barshadow_path', lambda grating: 'table.fits')
    monkeypatch.setattr(stage2, 'log', lambda *a, **k: None)

    def single(rate_file, obs, mf, pending, bkg, *, done, **kw):
        open('jw01_03101_00001_nrs1_1.fits', 'w').close()
        done.append(1)
        raise RuntimeError('round 2 failed')
    calls = []
    monkeypatch.setattr(stage2, '_run_spec2_single_call', single)
    monkeypatch.setattr(stage2, '_run_spec2_per_source',
                        lambda rate_file, obs, mf, sids, msametfl=None, **kw: calls.append((list(sids), msametfl))
                        or list(sids))
    cwd = os.getcwd()
    try:
        got = stage2.run_stage2a_single_rate(str(rate), Obs(), rerun_source_ids=frozenset({2}))
    finally:
        os.chdir(cwd)
    assert calls == [([2], 'orig_msa.fits')]
    assert sorted(got) == [1, 2]
    assert fits.getheader(rate)['MSAMETFL'] == 'orig_msa.fits'


def test_a_stamped_canonical_is_not_rewritten(tmp_path):
    # its modification time must not move: rewriting it on every run made the master
    # background stale every run
    p = tmp_path / 'c.fits'
    h = fits.PrimaryHDU()
    h.header['CFEXPGRP'] = 2
    h.writeto(p)
    os.utime(p, (1e9, 1e9))
    stage2.stamp_exp_group_single({'path': str(p), 'exp_group': 2})
    assert os.path.getmtime(p) == 1e9
    stage2.stamp_exp_group_single({'path': str(p), 'exp_group': 3})
    assert fits.getheader(p)['CFEXPGRP'] == 3


def test_a_canonical_made_with_other_stuck_shutters_is_rerun(tmp_path, monkeypatch):
    # what the canonical removed (STKSHPHY) against what the list names now for its source
    monkeypatch.setattr(stage2, '_drop_stuck', lambda rows, stuck: (rows, None, sorted(stuck)))

    class Meta:
        def rows_for_source(self, sid):
            return []

    def canonical(name, value):
        p = tmp_path / name
        h = fits.PrimaryHDU()
        if value is not None:
            h.header['STKSHPHY'] = value
        h.writeto(p)
        return str(p)

    same = canonical('a.fits', 'q1r2c3')
    assert not stage2._canonical_stuck_changed(same, Meta(), 1, {'q1r2c3'})
    assert stage2._canonical_stuck_changed(same, Meta(), 1, {'q1r2c3', 'q1r2c4'})   # step 1 added one
    none = canonical('b.fits', 'N/A')
    assert not stage2._canonical_stuck_changed(none, Meta(), 1, set())
    assert stage2._canonical_stuck_changed(none, Meta(), 1, {'q1r2c3'})
    assert not stage2._canonical_stuck_changed(canonical('c.fits', None), Meta(), 1, {'q1r2c3'})  # predates the card


def test_a_bkgslits_file_made_with_another_stuck_set_is_rerun(tmp_path):
    p = tmp_path / 'b_bkgslits.fits'
    h = fits.PrimaryHDU()
    h.header['STKSHSET'] = stage2._stuck_hash({(1, 2, 3)})
    h.writeto(p)
    assert not stage2._bkgslits_stuck_changed(str(p), {(1, 2, 3)})
    assert stage2._bkgslits_stuck_changed(str(p), {(1, 2, 3), (1, 2, 4)})
    assert stage2._stuck_hash(set()) == 'none'
    assert not stage2._bkgslits_stuck_changed(str(tmp_path / 'absent.fits'), {(1, 2, 3)})
