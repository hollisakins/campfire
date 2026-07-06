"""Tests for the A/B astrometry driver (scripts/nircam_ab_astrometry.py).

Exercises the pure ``run_ab`` core (the three-way comparison over in-memory
catalogs) with a synthetic reference and two arms carrying known injected
offsets, so it needs no mosaics or SEP extraction. The mosaic→catalog half is
already covered by the refcat extract tests.
"""

import importlib.util
import os

import astropy.units as u
import numpy as np
import pytest
from astropy.table import Table

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'scripts', 'nircam_ab_astrometry.py',
)


@pytest.fixture(scope='module')
def ab():
    spec = importlib.util.spec_from_file_location('nircam_ab_astrometry', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _grid(n=5, ra0=150.0, dec0=2.1, step=0.002):
    ra, dec = np.meshgrid(np.arange(n) * step + ra0,
                          np.arange(n) * step + dec0)
    return Table({'RA': ra.ravel(), 'DEC': dec.ravel(),
                  'mag': np.full(ra.size, 22.0),
                  'mag_err': np.full(ra.size, 0.1)})


def _offset_dec(cat, mas):
    out = cat.copy()
    out['DEC'] = out['DEC'] + mas / 3.6e6  # mas → deg
    return out


def test_run_ab_recovers_injected_offsets(ab):
    cat_ref = _grid()
    cat_jhat = _offset_dec(cat_ref, 100.0)   # +100 mas in Dec vs reference
    cat_align = _offset_dec(cat_ref, 20.0)    # +20 mas in Dec vs reference

    result = ab.run_ab(cat_jhat, cat_align, cat_ref,
                       match_radius=0.5 * u.arcsec)

    assert set(result) == {'jhat_vs_align', 'jhat_vs_ref', 'align_vs_ref'}
    for r in result.values():
        assert r['n_matched'] == len(cat_ref)
        for k in ('dra_stats', 'ddec_stats', 'sep_stats'):
            assert set(r[k]) == {'mean', 'median', 'mad'}

    # Absolute Dec residuals recover the injections; align is the tighter arm.
    assert result['jhat_vs_ref']['ddec_stats']['median'] == pytest.approx(100.0,
                                                                          abs=1)
    assert result['align_vs_ref']['ddec_stats']['median'] == pytest.approx(20.0,
                                                                           abs=1)
    # Method-to-method: jhat is +80 mas in Dec relative to align.
    assert result['jhat_vs_align']['ddec_stats']['median'] == pytest.approx(80.0,
                                                                            abs=1)


def test_summarize_strips_arrays(ab):
    cat_ref = _grid()
    result = ab.run_ab(cat_ref, cat_ref, cat_ref)
    summary = ab.summarize(result)
    for pairing in result:
        keys = set(summary[pairing])
        assert 'dra_mas' not in keys and 'sep_mas' not in keys
        assert {'n_matched', 'dra_stats', 'ddec_stats', 'sep_stats'} <= keys
