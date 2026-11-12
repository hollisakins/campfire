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


# --- a catalog that holds at any epoch: stars from Gaia -----------------------

_COSD = np.cos(np.radians(2.0))


def _mosaic_catalog_and_gaia(epoch=2025.8, with_size=True):
    """A catalog extracted from a mosaic observed at ``epoch``: 40 moving stars
    (compact) and 60 galaxies (extended), no motion columns. Gaia has the first
    30 stars with proper motions, the 31st without one, and 5 stars that are
    not in the catalog. Also returns the truth table of all 40 stars."""
    from astropy.table import vstack
    from astropy.time import Time

    rng = np.random.default_rng(3)
    n_star, n_gal = 40, 60
    truth = Table({'RA': 150.0 + rng.uniform(-0.03, 0.03, n_star),
                   'DEC': 2.0 + rng.uniform(-0.03, 0.03, n_star),
                   'ref_epoch': np.full(n_star, 2016.0),
                   'pmra': rng.normal(-2.0, 4.0, n_star),
                   'pmdec': rng.normal(-5.0, 4.0, n_star),
                   'source_id': np.arange(1, n_star + 1),
                   'pmra_err': np.full(n_star, 0.1), 'pmdec_err': np.full(n_star, 0.1),
                   'parallax': np.full(n_star, 0.5)})
    at_epoch = propagate_to_epoch(truth, Time(epoch, format='jyear').mjd)
    cat = Table({
        'RA': np.concatenate([np.asarray(at_epoch['RA']),
                              150.0 + rng.uniform(-0.03, 0.03, n_gal)]),
        'DEC': np.concatenate([np.asarray(at_epoch['DEC']),
                               2.0 + rng.uniform(-0.03, 0.03, n_gal)]),
        'mag': np.full(n_star + n_gal, 20.0, 'float32'),
        'mag_err': np.full(n_star + n_gal, 0.02, 'float32'),
    })
    if with_size:
        cat['rhalf'] = np.concatenate([rng.normal(0.070, 0.003, n_star),
                                       rng.uniform(0.15, 0.6, n_gal)])
    gaia = truth[:31].copy()
    gaia['pmra'][30] = np.nan                      # a two-parameter Gaia solution
    gaia['pmdec'][30] = np.nan
    extra = Table({'RA': 150.2 + rng.uniform(0, 0.01, 5), 'DEC': np.full(5, 2.2),
                   'ref_epoch': np.full(5, 2016.0), 'pmra': np.full(5, 1.0),
                   'pmdec': np.full(5, 1.0), 'source_id': np.arange(900, 905),
                   'pmra_err': np.full(5, 0.1), 'pmdec_err': np.full(5, 0.1),
                   'parallax': np.full(5, 0.5)})
    return cat, vstack([gaia, extra]), truth


def test_stars_come_from_gaia_and_other_point_sources_are_dropped():
    from astropy.time import Time
    from campfire_pipeline.imaging.refcat.motion import use_gaia_for_stars

    cat, gaia, truth = _mosaic_catalog_and_gaia()
    out, info = use_gaia_for_stars(cat, gaia)
    assert info['n_gaia_stars'] == 30
    assert info['n_dropped_no_motion'] == 1        # in Gaia, no proper motion
    assert info['n_dropped_compact'] == 9          # as compact as stars, not in Gaia
    assert info['n_kept'] == len(out) == 90        # 30 stars + 60 galaxies
    assert 0.07 < info['size_limit'] < 0.15

    stars = np.isfinite(np.asarray(out['pmra'], float))
    assert stars.sum() == 30
    # stars carry Gaia's own position and epoch, not the mosaic's
    assert np.all(np.asarray(out['ref_epoch'], float)[stars] == 2016.0)
    np.testing.assert_array_equal(np.asarray(out['RA'])[stars], np.asarray(truth['RA'])[:30])
    # galaxies are where the catalog had them
    np.testing.assert_array_equal(np.asarray(out['RA'])[~stars], np.asarray(cat['RA'])[40:])

    # at any exposure epoch the stars are where Gaia puts them
    for year in (2012.3, 2016.6, 2027.0):
        mjd = Time(year, format='jyear').mjd
        moved, want = propagate_to_epoch(out, mjd), propagate_to_epoch(truth, mjd)
        dra = (np.asarray(moved['RA'])[stars] - np.asarray(want['RA'])[:30]) * _COSD * 3.6e6
        dde = (np.asarray(moved['DEC'])[stars] - np.asarray(want['DEC'])[:30]) * 3.6e6
        assert np.max(np.hypot(dra, dde)) < 0.01                # mas


def test_nothing_is_dropped_on_size_without_a_size_or_enough_stars():
    from campfire_pipeline.imaging.refcat.motion import use_gaia_for_stars

    cat, gaia, _ = _mosaic_catalog_and_gaia(with_size=False)
    out, info = use_gaia_for_stars(cat, gaia)
    assert info['size_limit'] is None and info['n_dropped_compact'] == 0
    assert info['n_gaia_stars'] == 30 and len(out) == 99        # only the no-motion star

    cat, gaia, _ = _mosaic_catalog_and_gaia()
    out, info = use_gaia_for_stars(cat, gaia, min_stars=31)     # 30 stars is not enough
    assert info['size_limit'] is None and len(out) == 99


def test_epoch_hint_pairs_a_fast_star_and_reports_the_frame_offset():
    from campfire_pipeline.imaging.refcat.motion import use_gaia_for_stars

    cat, gaia, _ = _mosaic_catalog_and_gaia()
    gaia['pmra'][0] = 150.0                     # 1.5 arcsec in 9.8 years
    shift = 150.0 * 9.8 / 3.6e6 / _COSD
    cat['RA'][0] = gaia['RA'][0] + shift
    # the catalog's frame sits 20 mas north of Gaia's
    cat['DEC'] = np.asarray(cat['DEC']) + 20.0 / 3.6e6

    _, blind = use_gaia_for_stars(cat, gaia)
    out, info = use_gaia_for_stars(cat, gaia, epoch_hint=2025.8)
    assert blind['n_gaia_stars'] == 29 and info['n_gaia_stars'] == 30
    assert abs(info['frame_offset_mas'][0]) < 1.0
    assert abs(info['frame_offset_mas'][1] - 20.0) < 1.0
    assert 'epoch_hint' not in out.colnames
    assert np.all(np.asarray(out['ref_epoch'], float)[np.isfinite(out['pmra'])] == 2016.0)

