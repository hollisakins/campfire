"""Tests for the per-instrument CFP provenance key sets (common/cfp.py).

Covers the issue #212 refactor of cfp.py from a single flat NIRCam-only
``CFP_KEYS`` list to separate ``NIRCAM`` / ``NIRSPEC`` key sets that share the
same mechanics. The central guarantees:

  * NIRCam call sites are unchanged (default ``keyset=NIRCAM``).
  * ``CFP_BKG`` / ``CFP_MASK`` are valid in BOTH namespaces independently
    (the NIRCam ``background``/``mask`` steps and the NIRSpec bkgsub/mask
    states collide on the keyword but not on semantics).
  * ``clear_from`` only ever slices within the selected key set, so a reset on
    one instrument never clears the other's keywords.
"""

import pytest
from astropy.io import fits

from campfire_pipeline.common import cfp


def _write_header(path, **cards):
    """Write a minimal single-HDU FITS file with the given primary cards."""
    hdu = fits.PrimaryHDU()
    for k, v in cards.items():
        hdu.header[k] = v
    hdu.writeto(path, overwrite=True)
    return str(path)


# --- back-compat: NIRCam is the default and unchanged ----------------------

def test_module_aliases_point_at_nircam():
    assert cfp.CFP_KEYS is cfp.NIRCAM.keys
    assert cfp.CFP_COMMENTS is cfp.NIRCAM.comments
    # The historical NIRCam chain head/tail are preserved.
    assert cfp.CFP_KEYS[0] == 'CFP_DET1'
    assert cfp.CFP_KEYS[-1] == 'CFP_OUT'


def test_format_defaults_to_nircam():
    out = cfp.format(CFP_DET1=None)
    val, comment = out['CFP_DET1']
    assert comment == cfp.NIRCAM.comments['CFP_DET1']
    # None -> ISO timestamp default.
    assert val and val[:2] == '20'


def test_format_passes_through_explicit_value():
    out = cfp.format(CFP_SKY='1.23e-04')
    assert out['CFP_SKY'][0] == '1.23e-04'


def test_format_rejects_unknown_key_for_nircam():
    with pytest.raises(ValueError):
        cfp.format(CFP_NOPE='x')
    # A NIRSpec-only key is unknown in the NIRCam namespace.
    with pytest.raises(ValueError):
        cfp.format(CFP_CAL=None)


# --- NIRSpec namespace ------------------------------------------------------

def test_nirspec_chain_order():
    assert cfp.NIRSPEC.keys == ['CFP_CAL', 'CFP_MASK', 'CFP_BKG', 'CFP_S2D']


def test_format_nirspec_keyset():
    out = cfp.format(keyset=cfp.NIRSPEC, CFP_BKG='skipped:nods=1')
    val, comment = out['CFP_BKG']
    assert val == 'skipped:nods=1'
    assert comment == cfp.NIRSPEC.comments['CFP_BKG']


def test_format_rejects_nircam_key_for_nirspec():
    with pytest.raises(ValueError):
        cfp.format(keyset=cfp.NIRSPEC, CFP_DET1=None)


# --- collision: shared keywords live in both namespaces independently -------
# On clean main the only NIRCam<->NIRSpec keyword collision is CFP_MASK; once
# the parked NIRCam background/artifact branch (PR #224) merges, CFP_BKG also
# collides. Derive the set dynamically so this stays correct across that merge.
_SHARED_KEYS = sorted(set(cfp.NIRCAM.keys) & set(cfp.NIRSPEC.keys))


def test_cfp_mask_is_a_current_collision():
    # Guards that the refactor actually separated a genuinely-shared keyword.
    assert 'CFP_MASK' in _SHARED_KEYS


@pytest.mark.parametrize('key', _SHARED_KEYS)
def test_shared_keyword_valid_in_both_keysets(key):
    assert key in cfp.NIRCAM.keys
    assert key in cfp.NIRSPEC.keys
    # Valid (no raise) under either keyset, with the keyset's own comment.
    n = cfp.format(keyset=cfp.NIRCAM, **{key: None})
    s = cfp.format(keyset=cfp.NIRSPEC, **{key: None})
    assert n[key][1] == cfp.NIRCAM.comments[key]
    assert s[key][1] == cfp.NIRSPEC.comments[key]
    assert n[key][1] != s[key][1]  # different semantics


# --- has_step / get_steps over a real file ---------------------------------

def test_has_step_and_get_steps(tmp_path):
    # CFP_CAL is NIRSpec-only; CFP_MASK is shared by both namespaces.
    path = _write_header(tmp_path / 'x.fits', CFP_CAL='t0', CFP_MASK='t1')
    assert cfp.has_step(path, 'CFP_CAL', keyset=cfp.NIRSPEC) is True
    assert cfp.has_step(path, 'CFP_S2D', keyset=cfp.NIRSPEC) is False
    steps = cfp.get_steps(path, keyset=cfp.NIRSPEC)
    assert steps == {'CFP_CAL': 't0', 'CFP_MASK': 't1'}
    # The NIRCam keyset sees only the shared CFP_MASK, not NIRSpec-only CFP_CAL.
    assert cfp.get_steps(path, keyset=cfp.NIRCAM) == {'CFP_MASK': 't1'}


def test_has_step_accepts_header_object():
    hdr = fits.Header()
    hdr['CFP_BKG'] = 't'
    assert cfp.has_step(hdr, 'CFP_BKG', keyset=cfp.NIRSPEC) is True


def test_has_step_rejects_unknown_key():
    with pytest.raises(ValueError):
        cfp.has_step(fits.Header(), 'CFP_CAL', keyset=cfp.NIRCAM)


# --- should_skip ------------------------------------------------------------

def test_should_skip_overwrite_short_circuits(tmp_path):
    path = _write_header(tmp_path / 'x.fits', CFP_CAL='t0')
    assert cfp.should_skip(path, 'CFP_CAL', 'root', 'cal', None, True,
                           keyset=cfp.NIRSPEC) is False


def test_should_skip_live_read(tmp_path):
    path = _write_header(tmp_path / 'x.fits', CFP_CAL='t0')
    assert cfp.should_skip(path, 'CFP_CAL', 'root', 'cal', None, False,
                           keyset=cfp.NIRSPEC) is True
    assert cfp.should_skip(path, 'CFP_BKG', 'root', 'bkg', None, False,
                           keyset=cfp.NIRSPEC) is False


# --- clear_from never crosses namespaces -----------------------------------

def test_clear_from_slices_within_nirspec(tmp_path):
    path = _write_header(
        tmp_path / 'x.fits',
        CFP_CAL='t0', CFP_MASK='t1', CFP_BKG='t2', CFP_S2D='t3',
    )
    cfp.clear_from(path, 'CFP_BKG', keyset=cfp.NIRSPEC)
    with fits.open(path) as hdul:
        hdr = hdul[0].header
    assert 'CFP_CAL' in hdr and 'CFP_MASK' in hdr     # before the cut: kept
    assert 'CFP_BKG' not in hdr and 'CFP_S2D' not in hdr  # from the cut: gone


def test_clear_from_nircam_does_not_touch_nirspec_keys(tmp_path):
    # A (hypothetical) file carrying both namespaces' keywords: clearing from a
    # NIRCam key must only remove NIRCam keys at/after it, never NIRSpec ones.
    path = _write_header(
        tmp_path / 'x.fits',
        CFP_DET1='t', CFP_IMG2='t', CFP_OUT='t',  # nircam chain
        CFP_CAL='t', CFP_S2D='t',                 # nirspec-only keys
    )
    cfp.clear_from(path, 'CFP_IMG2', keyset=cfp.NIRCAM)
    with fits.open(path) as hdul:
        hdr = hdul[0].header
    assert 'CFP_DET1' in hdr               # before the NIRCam cut
    assert 'CFP_IMG2' not in hdr           # at the cut
    assert 'CFP_OUT' not in hdr            # after the cut (nircam)
    # NIRSpec-only keys are untouched because they aren't in the NIRCam chain.
    assert 'CFP_CAL' in hdr and 'CFP_S2D' in hdr


def test_clear_from_rejects_key_not_in_keyset(tmp_path):
    path = _write_header(tmp_path / 'x.fits', CFP_CAL='t0')
    with pytest.raises(ValueError):
        cfp.clear_from(path, 'CFP_CAL', keyset=cfp.NIRCAM)  # CFP_CAL not nircam


# --- Keyset integrity -------------------------------------------------------

def test_keyset_requires_comment_for_every_key():
    with pytest.raises(ValueError):
        cfp.Keyset(name='broken', keys=['CFP_X'], comments={})


@pytest.mark.parametrize('ks', ['NIRCAM', 'NIRSPEC'])
def test_every_key_has_comment_and_fits_8char(ks):
    keyset = getattr(cfp, ks)
    for k in keyset.keys:
        assert k in keyset.comments
        assert len(k) <= 8  # FITS keyword limit
