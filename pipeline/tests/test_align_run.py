"""Tests for the field-level align phase wiring (nircam/orchestrate.run_align).

The gating test is fast (no FITS). The end-to-end tests build a real synthetic
field — canonical exposures with a persistable gwcs + injected offset across two
filter dirs sharing one exposure token, plus a written refcat — and run the
phase through the same path the CLI uses.
"""

import os

import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.table import Table

from _align_gwcs import HAVE_PERSISTABLE_WCS, build_canonical, make_persistable_wcs
from campfire_pipeline.nircam.field import Field
from campfire_pipeline.nircam.orchestrate import _active_process_steps, run_align
from campfire_pipeline.nircam.refcat.io import write_refcat

_TOKEN = 'jw01727028001_04101_00003'
_SHAPE = (2048, 1024)
_COSD = float(np.cos(np.deg2rad(-30.0)))


# --- gating (fast, no FITS) -------------------------------------------------

def _field(enabled, **kw):
    return Field(name='cosmos', filters=['f200w'], files=['jw01727*'],
                 tangent_point=(80.0, -30.0), tiles={},
                 step_overrides={'align': {'enabled': enabled, **kw}})


def test_visit_membership_detects_dropped_member():
    # The cheap outlier pre-scan must re-run a visit whose membership changed
    # (e.g. a NOT_ALIGNED quarantine dropped an exposure) — otherwise resample
    # reuses CR masks computed with the now-absent exposure still pooled.
    from campfire_pipeline.nircam.orchestrate import _visit_membership_matches
    manifest = {'inputs': [
        {'filename': 'jw001_001_001_nrca1.fits'},
        {'filename': 'jw001_001_002_nrca1.fits'},
        {'filename': 'jw002_001_001_nrca1.fits'},     # cross-visit overlap
    ]}
    all_members = ['/w/jw001_001_001_nrca1.fits', '/w/jw001_001_002_nrca1.fits']
    assert _visit_membership_matches(manifest, 'jw001', all_members)  # unchanged
    # a member dropped (quarantined) -> mismatch -> force re-run
    assert not _visit_membership_matches(manifest, 'jw001',
                                         all_members[:1])
    # a member added -> mismatch too
    assert not _visit_membership_matches(
        manifest, 'jw001', all_members + ['/w/jw001_001_003_nrca1.fits'])


def test_active_process_steps_gates_jhat_and_wcs_shift():
    off = [n for n, _ in _active_process_steps({}, _field(False))]
    on = [n for n, _ in _active_process_steps({}, _field(True))]
    # align off (default): the JHAT path runs
    assert 'jhat' in off and 'wcs_shift' in off
    # align on: the JHAT path is removed (exactly one alignment path)
    assert 'jhat' not in on and 'wcs_shift' not in on
    # everything else is untouched
    assert 'variance' in on and 'sky' in on and 'detector1' in on


# --- end-to-end -------------------------------------------------------------

pytestmark_e2e = pytest.mark.skipif(
    not HAVE_PERSISTABLE_WCS, reason="jwst persistable-gwcs builder unavailable")


def _inject(shape, xy, rng, noise=1.0, fwhm=2.5, amp=400.0):
    sigma = fwhm / 2.3548
    img = rng.normal(0.0, noise, shape).astype(float)
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    for cx, cy in zip(*xy):
        img += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2))
    return img


def _build_field(tmp_path, enabled=True):
    """A field with one exposure: nrca1 in f200w + nrcalong in f444w (one token),
    each carrying a 2 arcsec offset from the written reference catalog."""
    field = Field(name='cosmos', filters=['f200w', 'f444w'], files=['jw01727*'],
                  tangent_point=(80.0, -30.0), tiles={},
                  step_overrides={'align': {'enabled': enabled,
                                            'refcat': 'test.ecsv'}})
    field.setup_workspace(campfire_root=str(tmp_path))
    rng = np.random.default_rng(0)
    ref_ra, ref_dec, xy_by_path = [], [], {}
    for filt, det, base_ra in [('f200w', 'nrca1', 80.0),
                               ('f444w', 'nrcalong', 80.05)]:
        x = rng.uniform(50, 950, 40)
        y = rng.uniform(50, 2000, 40)
        truth = make_persistable_wcs(ra_ref=base_ra, dec_ref=-30.0)
        rt, dt = truth(x, y)
        ref_ra.append(np.asarray(rt, float))
        ref_dec.append(np.asarray(dt, float))
        off_ra = base_ra + 2.0 / 3600.0 / _COSD          # inject 2 arcsec
        sci = _inject(_SHAPE, (x, y), rng)
        path = os.path.join(field.filter_dir(filt), f'{_TOKEN}_{det}.fits')
        build_canonical(path, sci, ra_ref=off_ra, dec_ref=-30.0)
        xy_by_path[path] = (x, y)
    refcat = Table({'RA': np.concatenate(ref_ra), 'DEC': np.concatenate(ref_dec)})
    refcat['mag'] = np.zeros(len(refcat), 'float32')
    refcat['mag_err'] = np.ones(len(refcat), 'float32')
    write_refcat(refcat, os.path.join(field.refcat_dir, 'test.ecsv'),
                 overwrite=True)
    return field, refcat, xy_by_path


@pytestmark_e2e
def test_run_align_end_to_end(tmp_path):
    field, refcat, xy_by_path = _build_field(tmp_path, enabled=True)
    run_align(field, {}, filters=['f200w', 'f444w'], n_processes=1)

    from jwst.datamodels import ImageModel
    ref = SkyCoord(refcat['RA'], refcat['DEC'], unit='deg')
    assert len(xy_by_path) == 2
    for path, (x, y) in xy_by_path.items():
        with fits.open(path) as h:
            assert h[0].header.get('CFP_ALGN', '').startswith('dof=')
        m = ImageModel(path, memmap=False)
        ra, dec = m.meta.wcs(x, y)
        m.close()
        _, d2d, _ = SkyCoord(ra, dec, unit='deg').match_to_catalog_sky(ref)
        assert float(np.median(d2d.arcsec)) < 0.05     # corrected onto refcat


@pytestmark_e2e
def test_run_align_disabled_is_noop(tmp_path):
    field, _, xy_by_path = _build_field(tmp_path, enabled=False)
    run_align(field, {}, filters=['f200w', 'f444w'], n_processes=1)
    for path in xy_by_path:
        with fits.open(path) as h:
            assert 'CFP_ALGN' not in h[0].header


@pytestmark_e2e
def test_run_align_idempotent(tmp_path):
    field, _, xy_by_path = _build_field(tmp_path, enabled=True)
    run_align(field, {}, filters=['f200w', 'f444w'], n_processes=1)
    mtimes = {p: os.path.getmtime(p) for p in xy_by_path}
    run_align(field, {}, filters=['f200w', 'f444w'], n_processes=1)
    assert all(os.path.getmtime(p) == mtimes[p] for p in xy_by_path)


@pytestmark_e2e
def test_run_align_warns_and_reattempts_not_aligned(tmp_path, capsys):
    field, _, xy_by_path = _build_field(tmp_path, enabled=True)
    # Clobber the refcat with too few sources -> every exposure rejects to
    # NOT_ALIGNED (the solve's <3-source guard).
    tiny = Table({'RA': [80.0, 80.001], 'DEC': [-30.0, -30.001]})
    tiny['mag'] = np.zeros(2, 'float32')
    tiny['mag_err'] = np.ones(2, 'float32')
    write_refcat(tiny, os.path.join(field.refcat_dir, 'test.ecsv'), overwrite=True)

    run_align(field, {}, filters=['f200w', 'f444w'], n_processes=1)
    out1 = capsys.readouterr().out
    for path in xy_by_path:
        with fits.open(path) as h:
            assert h[0].header.get('CFP_ALGN') == 'NOT_ALIGNED'
    assert 'FAILED alignment' in out1                 # loud end-of-command warning
    assert _TOKEN in out1                              # lists the failed exposure

    # Re-run WITHOUT --overwrite: the NOT_ALIGNED exposure is re-attempted (the
    # user may have retuned params), not silently skipped, and warned about again.
    run_align(field, {}, filters=['f200w', 'f444w'], n_processes=1)
    out2 = capsys.readouterr().out
    assert 're-attempting' in out2
    assert 'FAILED alignment' in out2


@pytestmark_e2e
def test_run_align_cross_filter_closure_writes_all_members(tmp_path):
    # Cross-filter dependency closure: aligning with --filters f200w still pools
    # and WRITES the paired f444w (LW) member — one attitude corrects the whole
    # dither, so both canonicals get a real (dof=...) CFP_ALGN stamp.
    field, _, xy_by_path = _build_field(tmp_path, enabled=True)
    run_align(field, {}, filters=['f200w'], n_processes=1)
    assert len(xy_by_path) == 2                        # one f200w + one f444w member
    for path in xy_by_path:
        with fits.open(path) as h:
            assert h[0].header.get('CFP_ALGN', '').startswith('dof=')


@pytestmark_e2e
def test_run_align_hardstops_unsupported_mode(tmp_path):
    # An exposure in an unsupported observing mode must stop the whole align run
    # (the user has to exclude it explicitly), not fall through generic logic.
    field, _, xy_by_path = _build_field(tmp_path, enabled=True)
    for path in xy_by_path:
        with fits.open(path, mode='update') as h:
            h[0].header['EXP_TYPE'] = 'NRC_CORON'
            h.flush()
    with pytest.raises(RuntimeError, match='observing mode'):
        run_align(field, {}, filters=['f200w', 'f444w'], n_processes=1)
    # nothing was aligned
    for path in xy_by_path:
        with fits.open(path) as h:
            assert 'CFP_ALGN' not in h[0].header
