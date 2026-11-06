"""Stage 3's same-slitlet revision (p1 922323, 2026-11-02).

* The decision does not depend on the background an earlier run subtracted: the joint
  first pass it is measured on reads the override files' nod differences, rebuilt from
  the frames before subtraction (``profile_fit.load_group``'s ``pre_bkgsub``).
* Spec3 runs after the revision, so a revised source's s2d and EXPOSURES table come
  from the background actually subtracted.

The canonicals here are small FITS files (live SCI / DQ, ``PRE_BKGSUB_SCI`` / ``_DQ``,
the stage-2b cards) read through a stand-in for jwst's ``MultiSlitModel``.
"""
import json
import os
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.common import cfp
from campfire_pipeline.nirspec import bar_offset as BO
from campfire_pipeline.nirspec import canonical as C
from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import slitlet_neighbours as SN
from campfire_pipeline.nirspec import stage3 as S3

OBS = 'ember_cosmos_p1'
ROOT = 'jw07076001001_05101'
FG = 'g395m_f290lp'
SID, NBR = 922323, 922322
NY, NX = 10, 40
PL = 0.8                         # the point-source path loss stage 2b removes and re-applies
SKY = 100.0
NODS = (1, 2, 3)
REASON = f'neighbor {NBR} at 0.021 of background'


# ---------------------------------------------------------------------------
# the arithmetic
# ---------------------------------------------------------------------------
def test_rebuilt_nod_differences_follow_jwsts_average():
    """Each exposure minus the mean of its partners' values: NaN left out, 0 where none
    has one (jwst's ``average_background``), 0 outside a partner's cutout (stage 2b pads
    with zeros); DQ the OR of all; an excluded exposure keeps its frame."""
    f1, f2, f3 = (np.full((2, 3), v) for v in (10.0, 4.0, 6.0))
    f2[0, 0] = np.nan                                     # one partner has no value
    f2[0, 1] = f3[0, 1] = np.nan                          # neither has one
    f3[1, 2] = 0.0                                        # outside exposure 3's cutout
    d = [np.zeros((2, 3), np.uint32) for _ in range(3)]
    d[1][1, 1] = 1
    d[2][1, 0] = 4
    out = PF.rebuild_nod_differences([f1, f2, f3], d, [[1, 2], [0, 2], None])
    diff, dq = out[0]
    assert diff[0, 0] == 10.0 - 6.0 and diff[0, 1] == 10.0 and diff[1, 2] == 10.0 - 2.0
    assert diff[1, 1] == 10.0 - 5.0
    assert dq[1, 1] == 1 and dq[1, 0] == 4 and dq[0, 0] == 0
    assert np.isnan(out[1][0][0, 0])                      # its own frame has no value there
    assert out[2][0] is f3 and out[2][1] is d[2]


# ---------------------------------------------------------------------------
# canonicals
# ---------------------------------------------------------------------------
def _raw_frames():
    """Frames with the path loss removed: the background, and a neighbor's light in the
    rows 6-7 of exposures 1 (a little) and 3. Exposure 3's nod difference then puts it at
    0.0195 of the background with exposure 1 as its only partner, at 0.0205 with both."""
    raw = {n: np.full((NY, NX), SKY) for n in NODS}
    raw[1][6:8] += 0.2
    raw[3][6:8] += 2.15
    return raw


def _path(ws, nod, sid=SID):
    return os.path.join(ws, f'{ROOT}_{nod:05d}_nrs1_{sid}.fits')


def write_group(ws, partners, why=None, sid=SID):
    """The canonicals of one background group as stage 2b leaves them: the live SCI the
    nod difference with ``partners`` ({nod: [nods]}; path loss re-applied), the frames
    before subtraction as ``PRE_BKGSUB_*``, the ``BKGNODS`` cards. ``why`` {nod: reason}
    marks an automatic override."""
    raw = _raw_frames()
    paths = []
    for nod in NODS:
        part = partners[nod]
        live = (raw[nod] - np.mean([raw[j] for j in part], axis=0)) / PL
        h = fits.Header()
        h.update({'FILTER': 'F290LP', 'GRATING': 'G395M', 'PATTTYPE': 'NONE', 'PATT_NUM': nod, 'NUMDTHPT': 3,
                  'PRIDTPTS': 3, 'SUBPXPTS': 1, 'NOD_TYPE': '3-SHUTTER-SLITLET', 'EFFEXPTM': 1000.0,
                  'SRCFLUX': 'T'})
        for k, v in cfp.format(keyset=cfp.NIRSPEC, CFP_BKG=None).items():
            h[k] = v
        overridden = why is not None and str(nod) in why
        for k, v in C.bkg_nods_cards(part, overridden, auto=overridden,
                                     why=(why or {}).get(str(nod), '')).items():
            h[k] = v
        sci = fits.ImageHDU(live.astype(np.float32), name='SCI', ver=1)
        sci.header.update({'SHUTTRID': 1, 'SHUTSTA': '1x1'})
        hdus = [fits.PrimaryHDU(header=h), sci, fits.ImageHDU(np.zeros((NY, NX), np.uint32), name='DQ', ver=1)]
        hdus += C.make_prefixed_hdus({'SCI': (raw[nod] / PL).astype(np.float32),
                                      'DQ': np.zeros((NY, NX), np.uint32)}, C.PRE_BKGSUB_PREFIX)
        p = _path(ws, nod, sid)
        fits.HDUList(hdus).writeto(p, overwrite=True)
        paths.append(p)
    return paths


class _Transform:
    def __call__(self, xx, yy):
        return np.zeros(np.shape(xx)), (np.asarray(yy, float) - NY / 2) * 0.1, 3.0 + 0.01 * np.asarray(xx, float)


class FakeMultiSlitModel:
    """What ``profile_fit.load_group`` reads of a canonical: its live slit (from the
    file), a path loss, the slit-frame transform, the stage-2b state."""

    def __init__(self, path):
        with fits.open(path) as h:
            data = np.array(h['SCI', 1].data, float)
            dq = np.array(h['DQ', 1].data, np.uint32)
        wcs = SimpleNamespace(get_transform=lambda a, b: _Transform())
        self.slits = [SimpleNamespace(
            data=data, dq=dq, var_poisson=np.ones((NY, NX)), var_rnoise=np.ones((NY, NX)), var_flat=np.zeros((NY, NX)),
            wavelength=3.0 + 0.01 * np.mgrid[:NY, :NX][1], pathloss_point=np.full((NY, NX), PL),
            pathloss_uniform=np.full((NY, NX), PL), pathloss_correction_type='POINT', source_ypos=0.0,
            shutter_state='1x1', slit_ymin=-1.0, slit_ymax=1.0, quadrant=1, ystart=1, xstart=1, ysize=NY, xsize=NX,
            meta=SimpleNamespace(wcs=wcs))]
        self.meta = SimpleNamespace(cal_step=SimpleNamespace(bkg_subtract='COMPLETE', pathloss='COMPLETE'),
                                    instrument=SimpleNamespace(detector='NRS1', grating='G395M', filter='F290LP'))

    def close(self):
        pass


@pytest.fixture
def fake_models(monkeypatch):
    monkeypatch.setattr('jwst.datamodels.MultiSlitModel', FakeMultiSlitModel)


def _nod3(exps):
    return next(e for e in exps if f'_{3:05d}_' in e.name)


def test_live_load_follows_bkgnods_the_rebuilt_load_does_not(tmp_path, fake_models):
    """The cause of the flip: the live nod difference is whatever stage 2b last
    subtracted. Rebuilt from the frames before subtraction with the override files'
    partners it is the same from either state, and equal to the live one of that state."""
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    pa = write_group(str(a), {1: [2, 3], 2: [1, 3], 3: [1]}, why={'3': REASON})
    pb = write_group(str(b), {1: [2, 3], 2: [1, 3], 3: [1, 2]})
    live_a, live_b = (PF.load_group(p) for p in (pa, pb))
    assert _nod3(live_a).partners == [0] and _nod3(live_b).partners == [0, 1]
    assert not np.allclose(_nod3(live_a).diff, _nod3(live_b).diff)
    pre_a, pre_b = (PF.load_group(p, pre_bkgsub=True) for p in (pa, pb))
    for ea, eb, el in zip(pre_a, pre_b, live_b):
        assert ea.partners == eb.partners == el.partners
        np.testing.assert_allclose(ea.diff, eb.diff)
        np.testing.assert_allclose(ea.diff, el.diff, rtol=1e-6)
        np.testing.assert_array_equal(ea.ok, el.ok)
    # a hand override is the override files': it is kept
    hand = PF.load_group(pa, bkg_overrides={'3': [2]}, pre_bkgsub=True)
    assert _nod3(hand).partners == [1]


# ---------------------------------------------------------------------------
# the decision, run twice from either state (stage3.run_profile_fit_phase)
# ---------------------------------------------------------------------------
class FakeObs:
    def __init__(self, ws):
        self.name = OBS
        self.workspace_dir = ws
        self.directories_setup = True
        self.stuck_closed_shutters = None

    def effective_bkg_overrides(self):
        return {}, {}

    def discover_files(self, ext='canonical', source_ids='all'):
        paths = sorted(p for p in os.listdir(self.workspace_dir) if p.startswith(ROOT) and p.endswith(f'_{SID}.fits'))
        t = Table()
        t['path'] = [os.path.join(self.workspace_dir, p) for p in paths]
        t['name'] = paths
        t['source_id'] = [SID] * len(paths)
        t['detector'] = ['nrs1'] * len(paths)
        t['obs'] = [ROOT.split('_')[0]] * len(paths)
        t['dither_pattern_type'] = ['NONE'] * len(paths)
        t['nod_type'] = ['3-SHUTTER-SLITLET'] * len(paths)
        t['subpixel_dither_points'] = [1] * len(paths)
        t['dither_position'] = [int(p.split('_')[2]) for p in paths]
        t['filter_grating'] = [FG] * len(paths)
        t['config'] = [ROOT.split('_')[1]] * len(paths)
        t['root'] = [ROOT] * len(paths)
        return t


def _brightness(exps):
    """Stand-in for the neighbor's modeled light over the background: its light in
    exposure 3's nod difference (rows 6-7) over the background there. In the pipeline it
    is the neighbor's joint-pass spectrum, which the target's nod differences enter."""
    return float(np.median(np.sum(_nod3(exps).diff[6:8], axis=0))) / (2 * SKY)


def _decide(frac):
    """``slitlet_neighbours.decide`` on nod 3, whose partner 2 puts the neighbor on it."""
    slit = SN.Slitlet(slitlet_id=1, quadrant=1, row=1, open_u={}, target_u={1: 0.0, 2: 5.0, 3: 10.0}, ref_u={})
    nb = SN.Neighbour(NBR, where={2: 10.0}, sky_frac=frac)
    d = {x.nod: x for x in SN.decide(ROOT, slit, [nb], {1: 1, 2: 2, 3: 3}, {1: [2, 3], 2: [1, 3], 3: [1, 2]})}
    return d[3].keep, d[3].why


@pytest.fixture
def phase(monkeypatch, fake_models):
    """``run_profile_fit_phase`` with the joint first pass replaced by ``_brightness`` on
    the exposures it loads, the revision by ``_decide`` and a stage 2b that rewrites the
    canonicals, and Spec3 and the products recorded."""
    from campfire_pipeline.nirspec import joint_first_pass as JF
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec import profile_product as PP
    from campfire_pipeline.nirspec import stage2 as S2

    calls = []
    state = {}

    def joint_run(tasks, ws, sc, is_prism, n_processes=1, overwrite=False, cache_size=60, pre_bkgsub=False,
                  targets=None, stuck_keys=None):
        t = tasks[SID]
        exps = PF.load_source(t['groups'], bkg_overrides=t['overrides'], science=set(t['science']),
                              **({'pre_bkgsub': True} if pre_bkgsub else {}))
        calls.append(('pass1', pre_bkgsub, targets))
        if targets is None:
            state['frac'] = _brightness(exps)
        return []

    def revise(obs, s2c, sids, fg, prefix, n_processes=1):
        calls.append(('revise',))
        keep, why = _decide(state['frac'])
        partners = {1: [2, 3], 2: [1, 3], 3: keep}
        have = fits.getheader(_path(obs.workspace_dir, 3))['BKGNODS']
        if have == ','.join(str(n) for n in keep):
            return set()
        write_group(obs.workspace_dir, partners, why={'3': REASON} if why == 'dropped' else None)
        return {SID}

    monkeypatch.setattr(JF, 'run', joint_run)
    monkeypatch.setattr(S2, 'revise_slitlet_overrides', revise)
    monkeypatch.setattr(CT, 'exposures_without_maps', lambda *a: [])
    monkeypatch.setattr(CT, 'stuck_keys_by_root', lambda *a: {})
    monkeypatch.setattr(S3, 'run_stage3_single_source', lambda *a, **k: calls.append(('spec3', a[2])))
    monkeypatch.setattr(PP, 'run_product', lambda *a: calls.append(('product', a[0])) or None)
    monkeypatch.setattr(BO, 'ensure', lambda *a, **k: {})
    return calls, state


def _run_phase(ws):
    obs = FakeObs(ws)
    files = obs.discover_files()
    from campfire_pipeline.nirspec.observation import Observation
    files = Observation.group_files(files)
    product = f'{OBS}_{FG}_{SID}'
    pf = [S3._profile_fit_task(obs, files, files, product, SID)]
    s3 = [(list(files['name']), ws, product, SID)]
    sc = dict(profile_fit=True, contam_predict=True, scan_neighbours=False, slitlet_neighbor_brightness='model')
    S3.run_profile_fit_phase(obs, sc, pf, stage2_config={}, s3_tasks=s3, s3_kwargs={})
    return fits.getheader(_path(ws, 3))['BKGNODS']


def test_the_decision_does_not_depend_on_the_previous_run(tmp_path, phase):
    """922323 nod 3 on p1: started from canonicals where it used only exposure 1 (run A)
    the neighbor read 0.020 and both were kept; started from both (run B) it read 0.021
    and exposure 2 was dropped; each run undid the last. Now both starts give one answer,
    and a run from its own result keeps it."""
    calls, state = phase
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    write_group(str(a), {1: [2, 3], 2: [1, 3], 3: [1]}, why={'3': REASON})
    write_group(str(b), {1: [2, 3], 2: [1, 3], 3: [1, 2]})
    first_a = _run_phase(str(a))
    frac_a = state['frac']
    first_b = _run_phase(str(b))
    assert state['frac'] == pytest.approx(frac_a) == pytest.approx(0.0205)
    assert first_a == first_b == '1'
    assert _run_phase(str(a)) == '1' and _run_phase(str(b)) == '1'
    assert ('pass1', True, None) in calls


def test_spec3_runs_after_the_revision_and_the_pass_again_for_its_source(tmp_path, phase):
    calls, _ = phase
    write_group(str(tmp_path), {1: [2, 3], 2: [1, 3], 3: [1, 2]})
    _run_phase(str(tmp_path))
    names = [c[0] for c in calls]
    assert names == ['pass1', 'revise', 'pass1', 'spec3', 'product']
    assert calls[2] == ('pass1', False, [SID])           # on its own nod differences, after the revision


# ---------------------------------------------------------------------------
# the EXPOSURES table of a revised source (stage3.run_stage3)
# ---------------------------------------------------------------------------
def _fake_spec3(asn_file, **kw):
    """Spec3 writes the s2d and x1d of the association's product."""
    with open(asn_file) as f:
        product = json.load(f)['products'][0]['name']
    for ext in ('s2d', 'x1d'):
        fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.zeros((3, 4), np.float32), name='SCI')]).writeto(
            f'{product}_s{SID:09d}_{ext}.fits', overwrite=True)


def test_exposures_table_of_a_revised_source_is_what_was_subtracted(tmp_path, monkeypatch, fake_models):
    """p1 run B: 922323's log says nod 3 used only exposure 1, its product said
    ``bkg_nods '1,2'``, override False (Spec3 ran before the revision, and with
    ``combine_method = "1d"`` never again). Now its s2d and EXPOSURES table are made after
    the revision, from the canonicals as subtracted."""
    from jwst.pipeline import Spec3Pipeline
    from campfire_pipeline.nirspec import joint_first_pass as JF
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec import profile_product as PP
    from campfire_pipeline.nirspec import stage2 as S2

    ws = str(tmp_path)
    write_group(ws, {1: [2, 3], 2: [1, 3], 3: [1, 2]})
    seen = {}

    def revise(obs, s2c, sids, fg, prefix, n_processes=1):
        write_group(obs.workspace_dir, {1: [2, 3], 2: [1, 3], 3: [1]}, why={'3': REASON})
        return {SID}

    def product(name, ws, *a):
        with fits.open(os.path.join(ws, f'{name}_s2d.fits')) as h:
            seen['exposures'] = Table(h['EXPOSURES'].data)
        return dict(product=name, source_id=SID, is_prism=False, scan=None, fallback=None)

    monkeypatch.setattr(Spec3Pipeline, 'call', _fake_spec3)
    monkeypatch.setattr(JF, 'run', lambda *a, **k: [])
    monkeypatch.setattr(S2, 'revise_slitlet_overrides', revise)
    monkeypatch.setattr(CT, 'exposures_without_maps', lambda *a: [])
    monkeypatch.setattr(CT, 'stuck_keys_by_root', lambda *a: {})
    monkeypatch.setattr(PP, 'run_product', product)
    monkeypatch.setattr(BO, 'ensure', lambda *a, **k: {})
    monkeypatch.setattr(S3, 'opt_ext_single_source', lambda *a, **k: None)
    monkeypatch.setattr('campfire_pipeline.config.get_stage_config', lambda *a, **k: {})
    sc = dict(method='nodded', combine_method='1d', profile_fit=True, contam_predict=True, scan_neighbours=False,
              slitlet_neighbor_brightness='model', mbkg_spectrum=True)
    S3.run_stage3(FakeObs(ws), sc, {'pipeline': {'version': 'test'}}, n_processes=1, overwrite=True)

    ex = seen['exposures']
    row = {int(str(f).split('_')[2]): r for f, r in zip(ex['filename'], ex)}
    for nod in NODS:
        h = fits.getheader(_path(ws, nod))
        r = row[nod]
        assert (r['bkg_nods'], r['bkg_override'], r['bkg_auto'], r['bkg_why']) == \
            (h['BKGNODS'], str(h['BKGOVR']), str(h['BKGAUTO']), h['BKGWHY'])
    assert (row[3]['bkg_nods'], row[3]['bkg_override'], row[3]['bkg_why']) == ('1', 'True', REASON)
    # and it is what the live SCI holds: exposure 3 minus exposure 1
    raw = _raw_frames()
    with fits.open(_path(ws, 3)) as h:
        np.testing.assert_allclose(h['SCI', 1].data * PL, raw[3] - raw[1], rtol=1e-6)


# ---------------------------------------------------------------------------
# the joint first pass: its member data and the boxcar fallback's Spec3
# ---------------------------------------------------------------------------
def test_member_cache_loads_the_override_files_nod_differences(monkeypatch):
    from campfire_pipeline.nirspec import joint_first_pass as JF
    seen = {}

    def load(groups, **kw):
        seen.update(kw)
        raise RuntimeError('loaded')
    monkeypatch.setattr(JF.PF, 'load_source', load)
    cache = JF.MemberCache({1: dict(groups=[['a']], overrides=[None], science=['a'], prism_product='')}, '.',
                           {}, False, pre_bkgsub=True)
    with pytest.raises(RuntimeError):
        cache.get(1)
    assert seen['pre_bkgsub'] is True
    # a pass 1 of one kind is never taken for the other
    assert JF.signature({}, [1, 2], pre_bkgsub=True) != JF.signature({}, [1, 2])


def test_boxcar_fallback_runs_its_spec3_when_the_s2d_is_stale(tmp_path, monkeypatch):
    from campfire_pipeline.nirspec import joint_first_pass as JF
    ran = []
    canon = tmp_path / 'c_nrs1_1.fits'
    canon.write_text('x')
    t = dict(product='p', groups=[[str(canon)]], overrides=[None], science=['c_nrs1_1.fits'],
             spec3=((['c_nrs1_1.fits'], str(tmp_path), 'p', 1), dict(cleanup_crfs=True)))
    monkeypatch.setattr(S3, 'run_stage3_single_source', lambda *a, **k: ran.append((a, k)))
    monkeypatch.setattr(S3, 'spec3_boxcar3', lambda ws, prod: None)
    cache = SimpleNamespace(workspace=str(tmp_path), is_prism=False)
    err = PF.NoUsablePixels('x')
    with pytest.raises(PF.NoUsablePixels):
        JF._boxcar(1, t, cache, {}, {}, err)
    assert ran == [((['c_nrs1_1.fits'], str(tmp_path), 'p', 1), dict(cleanup_crfs=True))]
    for ext in ('s2d', 'x1d'):
        (tmp_path / f'p_{ext}.fits').write_text('x')
        os.utime(tmp_path / f'p_{ext}.fits', (os.path.getmtime(canon) + 10,) * 2)
    with pytest.raises(PF.NoUsablePixels):
        JF._boxcar(1, t, cache, {}, {}, err)
    assert len(ran) == 1                                  # current: not run again
