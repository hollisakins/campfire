"""Tests for the checks of the diffraction model (scripts/nirspec_optics_checks.py): the stack
test per dataset and two runs compared on synthetic stack records, and the repeat test on
synthetic visits, including the refit of the along-dispersion correction."""

import importlib.util
import json
import os

import numpy as np
import pytest

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts',
                       'nirspec_optics_checks.py')


@pytest.fixture(scope='module')
def checks():
    spec = importlib.util.spec_from_file_location('nirspec_optics_checks', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rec(key, snr=50, c=0.0, sat=0.0, w=0.01, chi2r=1.0):
    return dict(key=key, detector='NRS1', ranges=[dict(range=2, lam=[1.5, 2.0], peak_snr=snr, c_node=c, sat=sat,
                                                       w=w, chi2=chi2r * 100.0, ndof=100.0)])


RECS = [_rec('a', c=0.1), _rec('b', snr=40, c=-0.2, chi2r=1.3), _rec('c', snr=30, chi2r=0.8),
        _rec('faint', snr=5), _rec('edge', c=0.4), _rec('saturated', sat=0.02),
        _rec('galaxy', w=0.15), _rec('odd', chi2r=8.0)]


def _write(run_dir, f=1.0):
    os.makedirs(run_dir / 'records', exist_ok=True)
    for r in RECS:
        r2 = json.loads(json.dumps(r))
        r2['ranges'][0]['chi2'] *= f
        with open(run_dir / 'records' / f"{r['key']}.json", 'w') as fh:
            json.dump(r2, fh)


def test_score_per_dataset_and_two_runs_compared(checks, tmp_path):
    _write(tmp_path)
    os.makedirs(tmp_path / 'samples')
    u = np.linspace(-4, 4, 4000)
    m = np.exp(-0.5 * (u / 0.6) ** 2)
    for k in ('a', 'b', 'c'):                          # the clean stars (clean_keys)
        np.savez(tmp_path / 'samples' / f'{k}.npz', r2_u=u, r2_d=m + 0.01, r2_m=m)
    assert checks.parse_groups('one=a,rest=') == {'one': 'a', 'rest': ''}
    per = checks.score_groups(str(tmp_path), checks.parse_groups('one=a,all='), log=lambda s: None)
    assert per['one'] == {} and per['all'][('NRS1', 2)]['n'] == 3       # one star is too few for a median
    _write(tmp_path / 'A')
    _write(tmp_path / 'B', 1.1)
    cmp = checks.compare(str(tmp_path / 'A'), str(tmp_path / 'B'), log=lambda s: None)
    n, ca, cb, ratio = cmp[('all', 'NRS1', 2)]
    assert n == 6 and ratio == pytest.approx(1.1) and cb == pytest.approx(1.1 * ca)   # 'edge', 'saturated' left out


def _visit(sid, det, pos, flux, along='y', snr=100.0, sat=0.0, bands=((1.0, 2.0), (3.0, 4.0))):
    return dict(sid=sid, det=det, srcx=pos if along == 'x' else 0.0, srcy=pos if along == 'y' else 0.0,
                bands=[list(b) for b in bands], flux=[(f, f / snr, snr) for f in flux], sat=[sat] * len(bands))


def test_repeat_pairs_against_the_most_central_visit(checks):
    rows = [_visit(1, 'NRS1', 0.02, [10.0, 20.0]), _visit(1, 'NRS1', 0.3, [11.0, 18.0]),
            _visit(1, 'NRS1', -0.2, [10.5, 20.0], snr=5.0),                      # too faint: not used
            _visit(1, 'NRS2', 0.3, [1.0, 1.0]),                                  # the other detector: its own star
            _visit(2, 'NRS1', 0.1, [5.0, 5.0]), _visit(2, 'NRS1', -0.4, [5.0, 4.0], sat=0.5),   # saturated
            dict(sid=3, det='NRS1', error='ValueError: x')]
    p = checks.repeat_pairs(rows)
    assert [len(b) for b in p] == [1, 1]
    assert p[0][0]['lnr'] == pytest.approx(np.log(1.1)) and p[1][0]['lnr'] == pytest.approx(np.log(0.9))
    assert p[0][0]['pos'] == 0.3 and p[0][0]['ref_pos'] == 0.02
    assert checks.repeat_pairs(rows, exclude=[1]) == [[], []]
    # the same source id in another program is another star
    other = [dict(r, program='01133') if r.get('srcy') == 0.3 and r.get('det') == 'NRS1' else dict(r, program='12497')
             for r in rows]
    assert checks.repeat_pairs(other) == [[], []]


def test_a_grating_band_is_the_same_wavelengths_in_every_visit(checks):
    b = checks.visit_bands('g395m_f290lp')
    assert len(b) == 4 and b[0][0] == pytest.approx(2.68) and b[-1][1] == pytest.approx(5.51)
    assert checks.visit_bands('prism_clear') == checks.VISIT_BANDS


def test_repeat_along_x_refits_the_correction(checks, tmp_path):
    """A scan whose fluxes carry the along-dispersion loss exp(-0.6 (lambda - 2)+ x^2) after a
    correction with b = 0.2 was applied: the refit undoes that one and finds 0.6."""
    rng = np.random.default_rng(3)
    bands = ((1.0, 1.5), (2.5, 3.5), (3.5, 5.0))
    rows = []
    for sid in range(40):
        for x in (0.0, -0.4, -0.2, 0.2, 0.4):
            f = [np.exp(-(0.6 - 0.2) * max(np.sqrt(a * b) - 2.0, 0.0) * x ** 2 + 0.005 * rng.normal())
                 for a, b in bands]
            rows.append(_visit(sid, 'NRS1', x, f, along='x', bands=bands))
    path = tmp_path / 'visits.json'
    path.write_text(json.dumps(dict(optics='test', x_loss=dict(b=0.2, lam0=2.0), rows=rows)))
    res = checks.repeat(str(path), along='x', log=lambda s: None)
    assert res[0]['slope'] == pytest.approx(0.0, abs=0.02)              # no loss below 2 um
    assert res[2]['slope'] == pytest.approx(-0.4 * (np.sqrt(3.5 * 5.0) - 2.0), rel=0.05)
    assert res[-1]['b'] == pytest.approx(0.6, abs=0.03) and res[-1]['b_used'] == 0.2
