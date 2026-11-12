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


# --- Gaia proper motions for a position-only catalog --------------------------

def _mosaic_catalog_and_gaia(epoch=2025.8, frame_offset_mas=(0.0, 0.0)):
    """A catalog extracted at ``epoch`` (40 moving stars, 60 galaxies, no motion
    columns) and the Gaia table of 30 of those stars plus 5 stars not in the
    catalog. The catalog frame can sit off Gaia's by ``frame_offset_mas``."""
    from astropy.time import Time

    rng = np.random.default_rng(3)
    n_star, n_gal = 40, 60
    ra16 = 150.0 + rng.uniform(-0.03, 0.03, n_star)
    dec16 = 2.0 + rng.uniform(-0.03, 0.03, n_star)
    pmra = rng.normal(-2.0, 4.0, n_star)
    pmdec = rng.normal(-5.0, 4.0, n_star)
    gaia_all = Table({'RA': ra16, 'DEC': dec16, 'ref_epoch': np.full(n_star, 2016.0),
                      'pmra': pmra, 'pmdec': pmdec, 'source_id': np.arange(1, n_star + 1),
                      'pmra_err': np.full(n_star, 0.1), 'pmdec_err': np.full(n_star, 0.1),
                      'parallax': np.full(n_star, 0.5)})
    at_epoch = propagate_to_epoch(gaia_all, Time(epoch, format='jyear').mjd)
    cosd = np.cos(np.radians(2.0))
    off_ra = frame_offset_mas[0] / 3.6e6 / cosd
    off_dec = frame_offset_mas[1] / 3.6e6
    cat = Table({
        'RA': np.concatenate([np.asarray(at_epoch['RA']) + off_ra,
                              150.0 + rng.uniform(-0.03, 0.03, n_gal) + off_ra]),
        'DEC': np.concatenate([np.asarray(at_epoch['DEC']) + off_dec,
                               2.0 + rng.uniform(-0.03, 0.03, n_gal) + off_dec]),
        'mag': np.full(n_star + n_gal, 20.0, 'float32'),
        'mag_err': np.full(n_star + n_gal, 0.02, 'float32'),
    })
    gaia = gaia_all[:30]
    extra = Table({'RA': 150.2 + rng.uniform(0, 0.01, 5), 'DEC': np.full(5, 2.2),
                   'ref_epoch': np.full(5, 2016.0), 'pmra': np.full(5, 1.0),
                   'pmdec': np.full(5, 1.0), 'source_id': np.arange(900, 905),
                   'pmra_err': np.full(5, 0.1), 'pmdec_err': np.full(5, 0.1),
                   'parallax': np.full(5, 0.5)})
    from astropy.table import vstack
    return cat, vstack([gaia, extra]), gaia_all, (off_ra, off_dec)


def test_attach_gaia_motion_puts_stars_back_at_an_earlier_epoch():
    from astropy.time import Time
    from campfire_pipeline.imaging.refcat.motion import attach_gaia_motion

    cat, gaia, truth, _ = _mosaic_catalog_and_gaia()
    out, info = attach_gaia_motion(cat, gaia, 2025.8)
    assert info['n_matched'] == 30 and info['n_with_motion'] == 30
    assert has_proper_motion(out)
    moving = np.isfinite(np.asarray(out['pmra'], float))
    assert moving.sum() == 30 and np.all(moving[:30]) and not np.any(moving[30:])
    assert np.all(np.asarray(out['ref_epoch'], float)[moving] == 2025.8)
    # positions are the catalog's own, untouched
    assert np.array_equal(np.asarray(out['RA']), np.asarray(cat['RA']))

    # An exposure from 2016.6: the 30 stars land where Gaia says they were,
    # the 10 stars Gaia did not supply and every galaxy stay put.
    mjd = Time(2016.6, format='jyear').mjd
    moved = propagate_to_epoch(out, mjd)
    want = propagate_to_epoch(truth, mjd)
    cosd = np.cos(np.radians(2.0))
    dra = (np.asarray(moved['RA'])[:30] - np.asarray(want['RA'])[:30]) * cosd * 3.6e6
    dde = (np.asarray(moved['DEC'])[:30] - np.asarray(want['DEC'])[:30]) * 3.6e6
    assert np.max(np.hypot(dra, dde)) < 0.05            # mas
    assert np.array_equal(np.asarray(moved['RA'])[30:], np.asarray(cat['RA'])[30:])


def test_attach_gaia_motion_allows_a_frame_offset_and_keeps_existing_motion():
    from campfire_pipeline.imaging.refcat.motion import attach_gaia_motion

    cat, gaia, _, _ = _mosaic_catalog_and_gaia(frame_offset_mas=(120.0, -90.0))
    out, info = attach_gaia_motion(cat, gaia, 2025.8, match_radius=0.05)
    assert info['n_with_motion'] == 30                  # offset removed before matching
    assert abs(info['offset_mas'][0] - 120.0) < 1.0
    assert abs(info['offset_mas'][1] + 90.0) < 1.0

    # A row that already has a proper motion keeps it.
    out['pmra'][0], out['pmdec'][0], out['ref_epoch'][0] = 99.0, -99.0, 2020.0
    again, info2 = attach_gaia_motion(out, gaia, 2025.8, match_radius=0.05)
    assert again['pmra'][0] == 99.0 and again['ref_epoch'][0] == 2020.0
    assert info2['n_with_motion'] == 0


def test_attach_gaia_motion_rejects_an_mjd_epoch():
    import pytest
    from campfire_pipeline.imaging.refcat.motion import attach_gaia_motion

    cat, gaia, _, _ = _mosaic_catalog_and_gaia()
    with pytest.raises(ValueError, match='Julian year'):
        attach_gaia_motion(cat, gaia, 60953.0)
