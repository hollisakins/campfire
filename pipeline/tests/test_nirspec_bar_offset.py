"""The bar offset from the background edges (``nirspec.bar_offset``): the offset of synthetic empty-shutter
edges is recovered through the fit, with or without a wider profile; the file keeps what the profile fit
applies; a measurement is redone only when its inputs change; the pass-1 cache signature follows it."""

import numpy as np
import pytest
from scipy.special import erf

from campfire_pipeline.nirspec import bar_offset as BO
from campfire_pipeline.nirspec import optics as O
from campfire_pipeline.nirspec import profile_contam as CT
from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH

MASK = (-1, 0, 1)


def _uniform(mask=MASK, blur=0.08, half=0.5):
    """(LAM, Y) uniform light through hard-edged open shutters blurred by a Gaussian (slit units)."""
    y = O.Y[None, :]
    s = blur * np.sqrt(2)
    u = sum(0.5 * (erf((y - (k * MSA_SLIT_PITCH - half)) / s) - erf((y - (k * MSA_SLIT_PITCH + half)) / s))
            for k in mask)
    return np.repeat(u, O.LAM.size, axis=0)


def _samples(true_offset, half=0.5, n_per=40, seed=0):
    """Pixels of the outer shutters' far halves (the ``samples`` layout), their ratio the profile with
    open half-height ``half`` at y + ``true_offset`` over its shutter centre, with 1 % noise."""
    rng = np.random.default_rng(seed)
    prof_true, prof_model = _uniform(half=half), _uniform()
    pitch = MSA_SLIT_PITCH
    u = np.tile(np.linspace(-0.74, 0.74, 400), n_per)
    keep = np.abs(u) > 0.21
    u = u[keep]
    kk = np.where(np.arange(u.size) % 2 == 0, -1, 1)
    u = np.abs(u) * kk                                              # the half away from the source
    lam = rng.uniform(1.0, 5.0, u.size)
    y = (kk + u) * pitch
    num = BO.uniform_at(prof_true, y + true_offset, lam)
    cu = np.linspace(-BO.CENTRE, BO.CENTRE, 7) * pitch
    den = np.mean([BO.uniform_at(prof_true, kk * pitch + c + true_offset, lam) for c in cu], axis=0)
    r = num / den * (1 + 0.01 * rng.standard_normal(u.size))
    edges = np.geomspace(1.0, 5.0, BO.N_BANDS + 1)
    return dict(u=u.astype(np.float32), band=(np.digitize(lam, edges) - 1).astype(np.int8), r=r.astype(np.float32),
                T=BO.model_ratio(prof_model, y, lam, kk, pitch), quadrant=np.full(u.size, 1, np.int8),
                exposure=np.full(u.size, 'a'))


@pytest.mark.parametrize('true_offset', [-0.03, 0.0, 0.017])
def test_offset_recovered(true_offset):
    off, lo, hi = BO.fit(_samples(true_offset), min_pixels=100)
    assert off == pytest.approx(true_offset, abs=0.002)
    assert lo == pytest.approx(hi, abs=0.004)


def test_a_wider_profile_moves_the_halves_apart_not_the_offset():
    off, lo, hi = BO.fit(_samples(0.01, half=0.52), min_pixels=100)
    assert off == pytest.approx(0.01, abs=0.002)
    assert abs(lo - hi) > 0.02


def test_too_few_pixels_give_no_offset():
    s = _samples(0.0, n_per=1)
    assert np.isnan(BO.fit(s, min_pixels=10 ** 6)[0])


def test_open_mask_from_the_state_string():
    assert BO.open_mask('1x1') == (-1, 0, 1)
    assert BO.open_mask('10x11') == (-2, 0, 1, 2)


def _measurement(offset, n=10, version=BO.MEASURE_VERSION):
    return dict(version=version, optics=O.version(), n_canonicals=n, offset=offset, lower=offset, upper=offset,
                n_pixels=1000, exposures={}, spread=np.nan,
                applied={str(q): offset + BO.QUADRANT_PATTERN[q] for q in (1, 2, 3, 4)} if np.isfinite(offset) else {})


def test_file_round_trip(tmp_path):
    BO.write(str(tmp_path), {'PRISM/CLEAR': _measurement(0.012), 'G395M/F290LP': _measurement(np.nan)})
    t = BO.table(str(tmp_path))
    assert set(t) == {'PRISM/CLEAR'}                    # an unmeasured disperser applies nothing
    assert t['PRISM/CLEAR'][3] == pytest.approx(0.012 + BO.QUADRANT_PATTERN[3])


class _Can:
    def __init__(self, sid):
        self.grating, self.filter, self.source_id = 'PRISM', 'CLEAR', sid


def test_ensure_measures_only_what_changed(tmp_path, monkeypatch):
    ws = tmp_path / 'products' / 'nirspec' / 'obs'
    ws.mkdir(parents=True)
    cans = [_Can(i) for i in range(10)]
    monkeypatch.setattr(BO.B, 'discover_workspace', lambda workspace: list(cans))
    calls = []

    def fake_measure(cs, workspace, **kw):
        calls.append(len(cs))
        return _measurement(0.01, n=len(cs))
    monkeypatch.setattr(BO, 'measure', fake_measure)
    assert BO.ensure(str(ws))['PRISM/CLEAR'][1] == pytest.approx(0.01 + BO.QUADRANT_PATTERN[1])
    BO.ensure(str(ws))
    assert calls == [10]                                 # kept
    cans.append(_Can(10))
    BO.ensure(str(ws))
    assert calls == [10, 11]                             # another set of canonicals
    BO.write(str(ws), {'PRISM/CLEAR': _measurement(0.01, n=11, version='0')})
    BO.ensure(str(ws))
    assert calls == [10, 11, 11]                         # an older measurement


def test_pass1_signature_follows_the_offsets():
    a = CT.pass1_settings({})
    b = CT.pass1_settings({'bar_offsets': {'PRISM/CLEAR': {1: 0.01, 2: 0.01}}})
    c = CT.pass1_settings({'bar_offsets': {'PRISM/CLEAR': {1: 0.02, 2: 0.01}}})
    assert 'bars' not in a and b['bars'] != c['bars']
