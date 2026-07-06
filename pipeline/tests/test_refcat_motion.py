"""Tests for the refcat epoch/proper-motion contract (refcat/io + refcat/motion).

Pure astropy — no jwst/tweakwcs. Covers the optional motion schema (detection,
round-trip, alias canonicalization) and the epoch propagation (a star moves, a
galaxy and a motion-less catalog do not).
"""

import numpy as np
from astropy.table import Table

from campfire_pipeline.nircam.refcat.io import (
    has_proper_motion,
    read_refcat,
    write_refcat,
)
from campfire_pipeline.nircam.refcat.motion import propagate_to_epoch


def _pm_table():
    """A refcat with a fast star (row 0) and a motion-less galaxy (row 1)."""
    return Table({
        'RA': [150.0, 150.5], 'DEC': [2.0, 2.0],
        'mag': np.array([18.0, 20.0], 'float32'),
        'mag_err': np.array([0.01, 0.02], 'float32'),
        'source_id': [1, 2], 'ref_epoch': [2016.0, 2016.0],
        'pmra': [3600.0, np.nan], 'pmdec': [0.0, np.nan],   # 3600 mas/yr = 3.6"/yr
        'parallax': [1.0, np.nan],
        'pmra_err': [0.1, np.nan], 'pmdec_err': [0.1, np.nan],
    })


def _plain_table():
    return Table({'RA': [1.0], 'DEC': [2.0],
                  'mag': np.array([18.0], 'float32'),
                  'mag_err': np.array([0.01], 'float32')})


# --- schema -----------------------------------------------------------------

def test_has_proper_motion():
    assert has_proper_motion(_pm_table())
    assert not has_proper_motion(_plain_table())
    t = _pm_table()
    t.remove_column('pmdec')                        # missing one required column
    assert not has_proper_motion(t)


def test_write_read_roundtrip_preserves_motion(tmp_path):
    p = str(tmp_path / 'rc.ecsv')
    write_refcat(_pm_table(), p, overwrite=True)
    back = read_refcat(p)
    for c in ('source_id', 'ref_epoch', 'pmra', 'pmdec', 'parallax'):
        assert c in back.colnames
    assert has_proper_motion(back)


def test_read_canonicalizes_motion_aliases(tmp_path):
    # An external Gaia dump using pmra_error/pmdec_error / SOURCE_ID.
    t = Table({'RA': [150.0], 'DEC': [2.0],
               'mag': np.array([18.0], 'float32'),
               'mag_err': np.array([0.01], 'float32'),
               'ref_epoch': [2016.0], 'pmra': [10.0], 'pmdec': [5.0],
               'pmra_error': [0.1], 'pmdec_error': [0.1], 'SOURCE_ID': [42]})
    p = str(tmp_path / 'ext.ecsv')
    t.write(p, format='ascii.ecsv', overwrite=True)
    back = read_refcat(p)
    assert {'pmra_err', 'pmdec_err', 'source_id'}.issubset(back.colnames)
    assert has_proper_motion(back)


# --- propagation ------------------------------------------------------------

def test_propagate_moves_star_keeps_galaxy():
    t = _pm_table()
    out = propagate_to_epoch(t, 60000.0)            # MJD 60000 ~ jyear 2023.15
    # star: 3.6"/yr over ~7.15 yr ~ 25.7" in RA*cos(dec)
    dra = (out['RA'][0] - t['RA'][0]) * 3600.0 * np.cos(np.radians(2.0))
    assert 24.0 < dra < 27.0
    # galaxy (NaN proper motion) is left exactly where it was
    assert out['RA'][1] == t['RA'][1] and out['DEC'][1] == t['DEC'][1]
    # input is not mutated
    assert t['RA'][0] == 150.0


def test_propagate_noop_without_motion():
    t = _plain_table()
    assert propagate_to_epoch(t, 60000.0) is t      # same object, no work done
