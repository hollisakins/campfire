"""Robustness guards for the refcat epoch/proper-motion subsystem.

Three latent-but-silent failure modes, each with a test that fails before the
guard was added:

1. A masked ``pmra``/``pmdec`` slot cast straight to float yields the fill
   sentinel (e.g. 1e20), not NaN — a masked filler row would be flagged
   "movable" and propagated ~1e20 mas off-sky. ``motion._float_col`` fixes this.
2. A ``ref_epoch`` given in the wrong unit (MJD, or decimal years since 2000)
   is silently read as a Julian year tens of thousands AD. ``propagate_to_epoch``
   now range-checks and raises ``ValueError``.
3. ``merge_refcats`` is first-wins, so a PM-free base catalog listed before a
   PM-bearing one silently strips proper motions from deduped rows. The merge
   now logs a WARNING.

Pure astropy — no jwst/tweakwcs.
"""

import numpy as np
import pytest
from astropy.table import MaskedColumn, Table

from campfire_pipeline.nircam.refcat.merge import merge_refcats
from campfire_pipeline.nircam.refcat.motion import propagate_to_epoch


def _pm_table(ref_epoch=2016.0):
    """A refcat with one fast star (row 0) and a motion-less galaxy (row 1)."""
    return Table({
        'RA': [150.0, 150.5], 'DEC': [2.0, 2.0],
        'mag': np.array([18.0, 20.0], 'float32'),
        'mag_err': np.array([0.01, 0.02], 'float32'),
        'source_id': [1, 2], 'ref_epoch': [ref_epoch, ref_epoch],
        'pmra': [3600.0, np.nan], 'pmdec': [0.0, np.nan],
        'parallax': [1.0, np.nan],
        'pmra_err': [0.1, np.nan], 'pmdec_err': [0.1, np.nan],
    })


def _plain_table(ra=150.0, dec=2.0):
    """A stationary (PM-free) catalog."""
    return Table({'RA': [ra], 'DEC': [dec],
                  'mag': np.array([18.0], 'float32'),
                  'mag_err': np.array([0.01], 'float32')})


# --- Fix 1: masked pm slots read as NaN, not the fill sentinel ---------------

def test_masked_pm_fill_value_stays_stationary():
    # A single source whose pmra/pmdec are masked with a large Gaia-style fill
    # value. A raw float cast would see 1e20 (finite, non-zero) -> "movable" ->
    # propagated ~1e20 mas off-sky. .filled(np.nan) -> NaN -> stationary.
    t = Table({
        'RA': [150.0], 'DEC': [2.0],
        'mag': np.array([18.0], 'float32'),
        'mag_err': np.array([0.01], 'float32'),
        'ref_epoch': [2016.0],
    })
    t['pmra'] = MaskedColumn([1e20], mask=[True], fill_value=1e20)
    t['pmdec'] = MaskedColumn([1e20], mask=[True], fill_value=1e20)

    out = propagate_to_epoch(t, 60000.0)
    # Position is untouched (to floating-point exactness): the row is stationary.
    assert out['RA'][0] == 150.0
    assert out['DEC'][0] == 2.0


# --- Fix 2: an out-of-range (wrong-unit) ref_epoch fails loud ----------------

def test_mjd_epoch_raises_valueerror():
    # ref_epoch = 57388.5 is an MJD, not a Julian year. A movable star carrying
    # it must raise rather than propagate to year ~57388 AD.
    t = _pm_table(ref_epoch=57388.5)
    with pytest.raises(ValueError, match="Julian year"):
        propagate_to_epoch(t, 60000.0)


def test_valueerror_names_offending_value():
    t = _pm_table(ref_epoch=57388.5)
    with pytest.raises(ValueError, match="57388.5"):
        propagate_to_epoch(t, 60000.0)


def test_in_range_epoch_does_not_raise():
    # A normal Gaia epoch propagates without complaint (guards against a
    # too-tight range rejecting valid catalogs).
    out = propagate_to_epoch(_pm_table(ref_epoch=2016.0), 60000.0)
    assert out['RA'][0] != 150.0                     # star actually moved


# --- Fix 3: PM-free base + PM-bearing later catalog warns --------------------

def test_merge_pmfree_first_warns(capsys):
    # Base catalog has no proper motions; the second (Gaia-like) one does.
    merged, _ = merge_refcats([_plain_table(ra=150.0), _pm_table()],
                              labels=['hsc', 'gaia'])
    out = capsys.readouterr().out
    assert 'WARNING' in out
    assert 'proper motion' in out.lower()
    assert 'gaia' in out                              # names the PM-bearing input


def test_merge_pm_first_does_not_warn(capsys):
    # Correct order (PM catalog first): no warning.
    merge_refcats([_pm_table(), _plain_table(ra=151.0)], labels=['gaia', 'hsc'])
    out = capsys.readouterr().out
    assert 'proper motion' not in out.lower()
