"""The table built from a Legacy Surveys DR10 result (no network)."""

import numpy as np
import pytest
from astropy.table import MaskedColumn, Table

from campfire_pipeline.imaging.refcat.motion import has_proper_motion
from campfire_pipeline.imaging.refcat.query import _ls_dr10_table, query_ls_dr10


def _result():
    """Seven rows as ``ls_dr10.tractor`` returns them. In order: a Gaia star
    with a proper motion; a Gaia star without one; a Gaia galaxy without one;
    a bright point source Gaia does not list (S/N 40); a faint point source
    (S/N 8), the survey's fallback type; a galaxy; a galaxy with no usable
    flux."""
    return Table({
        'ra': [150.0, 150.01, 150.02, 150.03, 150.04, 150.05, 150.06],
        'dec': [2.0, 2.01, 2.02, 2.03, 2.04, 2.05, 2.06],
        'mag_i': [18.0, 20.5, 18.5, 21.7, 23.5, 22.0, np.nan],
        'flux_i': [63.0, 6.3, 40.0, 2.0, 0.4, 1.6, 0.0],
        'flux_ivar_i': [400.0, 400.0, 400.0, 400.0, 400.0, 400.0, 0.0],
        'type': ['PSF', 'PSF', 'SER', 'PSF', 'PSF', 'REX', 'EXP'],
        'ref_cat': MaskedColumn(['GE', 'GE', 'GE', '', '', '', ''],
                                mask=[False, False, False, True, True, True, True]),
        'ref_id': [3836262246168776960, 3836262246168776961, 3836262246168776962, 0, 0, 0, 0],
        'ref_epoch': [2016.0, 2016.0, 2016.0, 0.0, 0.0, 0.0, 0.0],
        'pmra': [-3.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        'pmdec': [1.25, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        'parallax': [0.4, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        'pmra_ivar': [4.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        'pmdec_ivar': [16.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    })


def test_keep_lists_every_source_where_the_survey_has_it():
    table, info = _ls_dr10_table(_result(), 'i', 'keep')
    assert table.colnames == ['RA', 'DEC', 'mag', 'mag_err']
    assert len(table) == 6                      # the row with no flux is gone
    assert not has_proper_motion(table)
    assert info == {'stars': 'keep'}


def test_gaia_drops_recognizable_stars_that_have_no_proper_motion():
    table, info = _ls_dr10_table(_result(), 'i', 'gaia')
    # Kept: the Gaia star with a proper motion, the Gaia galaxy, the faint
    # point source (not recognizable as a star) and the galaxy. Dropped: the
    # Gaia star without a proper motion and the bright unlisted point source.
    np.testing.assert_allclose(table['RA'], [150.0, 150.02, 150.04, 150.05])
    assert info['n_gaia_stars'] == 1
    assert info['n_dropped_no_motion'] == 1
    assert info['n_dropped_point'] == 1
    assert info['n_faint_point'] == 1
    assert info['n_extended'] == 2
    # The star carries what the align step needs to move it to an exposure's date.
    assert has_proper_motion(table)
    star = table[0]
    assert star['source_id'] == 3836262246168776960
    assert star['ref_epoch'] == 2016.0
    assert (star['pmra'], star['pmdec']) == (-3.5, 1.25)
    assert star['pmra_err'] == pytest.approx(0.5)
    assert star['pmdec_err'] == pytest.approx(0.25)
    # Nothing else carries any of it, so nothing else is ever moved.
    for name in ('ref_epoch', 'pmra', 'pmdec', 'parallax', 'pmra_err', 'pmdec_err'):
        assert np.all(np.isnan(np.asarray(table[name][1:], float)))
    assert list(table['source_id'][1:]) == [0, 0, 0]


def test_the_signal_to_noise_limit_decides_which_point_sources_are_stars():
    # The bright unlisted point source has S/N 40, the faint one S/N 8.
    table, info = _ls_dr10_table(_result(), 'i', 'gaia', star_snr_min=50.0)
    assert info['n_dropped_point'] == 0 and info['n_faint_point'] == 2
    table, info = _ls_dr10_table(_result(), 'i', 'gaia', star_snr_min=5.0)
    assert info['n_dropped_point'] == 2 and info['n_faint_point'] == 0
    # A Gaia point source without a proper motion goes whatever the limit.
    assert info['n_dropped_no_motion'] == 1


def test_unknown_stars_mode_is_refused_before_any_query():
    with pytest.raises(ValueError, match='stars must be one of'):
        query_ls_dr10((150.0, 2.0), 0.01, stars='drop')
