"""Fully stuck sources and nodata markers (stage 2a / detect-stuck / stage 3).

1. Stage 2a retires a stale canonical when every shutter is stuck (or the
   detector has no data) and writes ``<prod>_nodata``.
2. ``detect_stuck_shutters`` returns ``{}`` on an empty files table.
3. Stage 3 finds finals whose source x grating is now all nodata.

None of these need the JWST stack.
"""

import os

import numpy as np
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.nirspec import nodata
from campfire_pipeline.nirspec.stuck_shutters import detect_stuck_shutters

ROOT = 'jw01234001001_03101'
OBS = 'ember_cosmos_p12'


def _touch(path, content=b''):
    with open(path, 'wb') as f:
        f.write(content)
    return path


def _fits(path, grating, filt):
    hdr = fits.Header()
    hdr['GRATING'] = grating
    hdr['FILTER'] = filt
    fits.PrimaryHDU(header=hdr).writeto(path)
    return path


# --- Bug 2 -------------------------------------------------------------------

def test_detect_stuck_shutters_empty_table():
    files = Table({'path': np.array([], dtype=str), 'grating': np.array([], dtype=str)})
    assert detect_stuck_shutters(None, files, {'detect_stuck_shutters_grating': True}) == {}


# --- Bug 1 -------------------------------------------------------------------

def test_mark_all_shutters_stuck_retires_stale_canonical(tmp_path):
    prod = f'{ROOT}_00001_nrs1_42'
    _touch(tmp_path / f'{prod}.fits')
    _touch(tmp_path / f'{prod}_cal.fits')
    _touch(tmp_path / f'{prod}.json')
    other = _touch(tmp_path / f'{ROOT}_00001_nrs1_420.fits')  # another source

    removed = nodata.mark_all_shutters_stuck(prod, directory=str(tmp_path))

    assert sorted(os.path.basename(p) for p in removed) == sorted(
        [f'{prod}.fits', f'{prod}_cal.fits', f'{prod}.json'])
    assert not (tmp_path / f'{prod}.fits').exists()
    assert (tmp_path / f'{prod}_nodata').read_text() == 'AllShuttersStuck\n'
    assert other.exists()


def test_mark_all_shutters_stuck_without_prior_canonical(tmp_path):
    prod = f'{ROOT}_00001_nrs2_42'
    assert nodata.mark_all_shutters_stuck(prod, directory=str(tmp_path)) == []
    assert (tmp_path / f'{prod}_nodata').read_text() == 'AllShuttersStuck\n'


def test_mark_nodata_on_detector_retires_stale_canonical(tmp_path):
    prod = f'{ROOT}_00001_nrs2_42'
    _touch(tmp_path / f'{prod}.fits')
    nodata.mark_nodata(prod, nodata.REASON_NO_DATA_ON_DETECTOR, directory=str(tmp_path))
    assert not (tmp_path / f'{prod}.fits').exists()
    assert (tmp_path / f'{prod}_nodata').read_text() == 'NoDataOnDetectorError\n'


# --- Bug 3 -------------------------------------------------------------------

def _workspace(tmp_path):
    """G395M of source 42 is all nodata; PRISM of 42 still has a canonical."""
    g395m = 'jw01234001001_03101'
    prism = 'jw01234001001_05101'
    for nod in ('00001', '00002'):
        _fits(tmp_path / f'{g395m}_{nod}_nrs1_rate.fits', 'G395M', 'F290LP')
        _fits(tmp_path / f'{prism}_{nod}_nrs1_rate.fits', 'PRISM', 'CLEAR')
        _touch(tmp_path / f'{g395m}_{nod}_nrs1_42_nodata', b'AllShuttersStuck\n')
        _fits(tmp_path / f'{prism}_{nod}_nrs1_42.fits', 'PRISM', 'CLEAR')
    base = f'{OBS}_g395m_f290lp_42'
    stale = [_touch(tmp_path / f'{base}{s}') for s in
             ('_spec.fits', '_s2d.fits', '_x1d.fits', '_prof.pdf', '_spec.pdf', '_zfit.fits')]
    stale.append(_touch(tmp_path / f'{base}_g1_spec.fits'))
    keep = [_touch(tmp_path / f'{OBS}_prism_clear_42_spec.fits'),
            _touch(tmp_path / f'{OBS}_g395m_f290lp_420_spec.fits'),
            _touch(tmp_path / f'{base}_gx_spec.fits')]  # not an exp-group part
    return stale, keep


def test_find_orphaned_finals(tmp_path):
    stale, keep = _workspace(tmp_path)
    orphans = nodata.find_orphaned_finals(str(tmp_path), OBS, [42, 420])
    assert list(orphans) == [(42, 'g395m_f290lp')]
    assert sorted(orphans[(42, 'g395m_f290lp')]) == sorted(str(p) for p in stale)

    removed = nodata.remove_orphaned_finals(orphans)
    assert sorted(removed) == sorted(str(p) for p in stale)
    assert all(not p.exists() for p in stale)
    assert all(p.exists() for p in keep)


def test_orphan_requires_unrequested_source_untouched(tmp_path):
    _workspace(tmp_path)
    assert nodata.find_orphaned_finals(str(tmp_path), OBS, [420]) == {}


def test_canonical_left_means_not_orphaned(tmp_path):
    _workspace(tmp_path)
    _fits(tmp_path / 'jw01234001001_03101_00003_nrs2_42.fits', 'G395M', 'F290LP')
    assert nodata.find_orphaned_finals(str(tmp_path), OBS, [42]) == {}


def test_final_without_markers_is_never_orphaned(tmp_path):
    """Missing inputs are not proof: no marker, no removal."""
    _touch(tmp_path / f'{OBS}_g395m_f290lp_7_spec.fits')
    assert nodata.find_orphaned_finals(str(tmp_path), OBS, [7]) == {}


def test_marker_without_rate_file_is_ignored(tmp_path):
    _touch(tmp_path / 'jw01234001001_03101_00001_nrs1_7_nodata')
    _touch(tmp_path / f'{OBS}_g395m_f290lp_7_spec.fits')
    assert nodata.find_orphaned_finals(str(tmp_path), OBS, [7]) == {}


def test_orphan_outside_mode_selection_is_left(tmp_path):
    _workspace(tmp_path)
    only_prism = lambda g, f: g.lower() == 'prism'
    assert nodata.find_orphaned_finals(str(tmp_path), OBS, [42], mode_matches=only_prism) == {}
