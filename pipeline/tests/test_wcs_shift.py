"""wcs_shift step: WCS rewrite must invalidate downstream alignment state.

wcs_shift and the alignment engines (jhat / align) coexist in the process loop:
wcs_shift applies a manual offset first, the engine solves from the shifted WCS
and records CFP_JHAT / CFP_ALGN (align also stashes its pre-align baseline in
ALGN_BAK). Retuning a wcs_shift rule and re-applying restores the original WCS
from WCS_BAK and applies the new shift — which silently discards the alignment
correction while the stamp (and ALGN_BAK) survive the datamodel round-trip. A
surviving stamp reads as "aligned" to the engines' skip checks and to the
combine quarantine, so the exposure would drizzle with an unaligned WCS.

These tests pin the fix: every actual wcs_shift apply clears CFP_JHAT/CFP_ALGN
and drops ALGN_BAK in the same atomic write, while leaving its own WCS_BAK and
the SRCMASK intact; files no rule rewrites are left alone.
"""

import numpy as np
import pytest
from astropy.io import fits

from _align_gwcs import HAVE_PERSISTABLE_WCS, build_canonical
from campfire_pipeline.nircam.steps.wcs_shift import wcs_shift_step

pytestmark = pytest.mark.skipif(
    not HAVE_PERSISTABLE_WCS, reason="jwst persistable-gwcs builder unavailable")

_TOKEN = 'jw01727028001_04101_00003'


def _rule(dra_arcsec):
    return {'files': [f'{_TOKEN}*'], 'filters': ['f200w'],
            'delta_ra': dra_arcsec / 3600.0, 'delta_dec': 0.0,
            'delta_roll': 0.0, 'scale': 1.0}


def _canonical(tmp_path):
    """A canonical in a filter-named dir (the step derives filtname from it)."""
    filt_dir = tmp_path / 'f200w'
    filt_dir.mkdir()
    path = str(filt_dir / f'{_TOKEN}_nrca1.fits')
    sm = np.zeros((64, 64), 'uint8')
    sm[10:12, 10:12] = 1
    build_canonical(path, np.zeros((64, 64), 'float32'), srcmask=sm)
    return path


def _stamp_aligned(path):
    """Simulate a completed alignment: CFP stamps, align's ALGN_BAK baseline,
    and the per-source ALGNCAT + scalar diagnostics a real solve writes."""
    from astropy.table import Table
    with fits.open(path, mode='update') as h:
        h[0].header['CFP_ALGN'] = 'dof=rshift res=0.01 n=12 rc=abcd1234'
        h[0].header['CFP_JHAT'] = 'gaia.ecsv'
        h[0].header['ALGNNMAT'] = 12
        h[0].header['ALGNGCON'] = 5.5
        h[0].header['ALGNSTAL'] = False
        h.append(fits.ImageHDU(data=np.arange(4, dtype=np.uint8),
                               name='ALGN_BAK'))
        h.append(fits.BinTableHDU(
            Table({'ra': [80.0, 80.1], 'dec': [-30.0, -30.1]}),
            name='ALGNCAT'))


def _probe_ra(path):
    from jwst.datamodels import ImageModel
    m = ImageModel(path, memmap=False)
    ra, _ = m.meta.wcs(32.0, 32.0)
    m.close()
    return float(ra)


def test_first_apply_invalidates_alignment(tmp_path):
    # A rule added AFTER align ran: the first wcs_shift apply rewrites the WCS,
    # so the (now stale) alignment stamps and ALGN_BAK must be cleared with it.
    path = _canonical(tmp_path)
    _stamp_aligned(path)

    wcs_shift_step(path, None, {'rules': [_rule(1.0)]})

    with fits.open(path) as h:
        hdr = h[0].header
        assert 'CFP_SHFT' in hdr                    # shift applied + stamped
        assert 'CFP_ALGN' not in hdr                # stale stamps cleared
        assert 'CFP_JHAT' not in hdr
        assert 'ALGN_BAK' not in h                  # stale baseline dropped
        # ALGNCAT holds sky positions under the WCS align solved, which this
        # step just replaced — it must not survive advertising them as current.
        assert 'ALGNCAT' not in h
        assert 'ALGNNMAT' not in hdr                # scalar diagnostics too
        assert 'ALGNGCON' not in hdr
        assert 'ALGNSTAL' not in hdr
        assert 'WCS_BAK' in h                       # own backup written
        assert 'SRCMASK' in h                       # untouched by the drop


def test_retune_overwrite_invalidates_and_reapplies(tmp_path):
    # The full retune flow: shift -> align -> retuned shift (--overwrite). The
    # re-apply must restore from WCS_BAK (declarative, not cumulative), clear
    # the alignment state again, and stamp the NEW rule.
    path = _canonical(tmp_path)
    ra0 = _probe_ra(path)

    wcs_shift_step(path, None, {'rules': [_rule(1.0)]})
    d1 = _probe_ra(path) - ra0
    _stamp_aligned(path)                            # align runs after the shift

    wcs_shift_step(path, None, {'rules': [_rule(2.0)]}, overwrite=True)

    with fits.open(path) as h:
        hdr = h[0].header
        assert 'CFP_ALGN' not in hdr
        assert 'CFP_JHAT' not in hdr
        assert 'ALGN_BAK' not in h
        assert 'WCS_BAK' in h
        assert 'dra=0.000555556' in hdr['CFP_SHFT']  # the retuned rule, 2"/3600
    # Restored-then-reapplied: the new offset is 2x the first, not 1x + 2x.
    d2 = _probe_ra(path) - ra0
    assert d1 != 0.0
    assert abs(d2 / d1 - 2.0) < 0.01


def test_no_matching_rule_leaves_alignment_alone(tmp_path):
    # Invalidation rides on an actual WCS rewrite; an exposure no rule touches
    # keeps its alignment state.
    path = _canonical(tmp_path)
    _stamp_aligned(path)
    rule = _rule(1.0)
    rule['files'] = ['jw99999*']                    # matches nothing

    wcs_shift_step(path, None, {'rules': [rule]})

    with fits.open(path) as h:
        assert 'CFP_ALGN' in h[0].header
        assert 'CFP_JHAT' in h[0].header
        assert 'ALGN_BAK' in h
        assert 'CFP_SHFT' not in h[0].header
