"""Tests for the per-filter align step wiring (nircam/orchestrate._run_align).

Align now runs as a per-filter step inside the process loop (replacing jhat),
driven through ``run_step('align', ...)`` — the same path the CLI uses. The
gating and pool-splitting tests are fast (no FITS). The end-to-end tests build a
real synthetic field (canonical exposures with a persistable gwcs + injected
offset, plus a written refcat) and run the step.
"""

import os

import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.table import Table

from _align_gwcs import HAVE_PERSISTABLE_WCS, build_canonical, make_persistable_wcs
from campfire_pipeline.nircam.association import (
    ExposureGroup, ExposureMember, split_pools,
)
from campfire_pipeline.nircam.field import Field
from campfire_pipeline.nircam.orchestrate import _active_process_steps, run_step
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
    assert not _visit_membership_matches(manifest, 'jw001', all_members[:1])
    assert not _visit_membership_matches(
        manifest, 'jw001', all_members + ['/w/jw001_001_003_nrca1.fits'])


def test_active_process_steps_swaps_jhat_for_align():
    off = [n for n, _ in _active_process_steps({}, _field(False))]
    on = [n for n, _ in _active_process_steps({}, _field(True))]
    # align off (default): the JHAT path runs, no align step
    assert 'jhat' in off and 'align' not in off
    # align on: jhat is swapped out for align (exactly one alignment engine)
    assert 'jhat' not in on and 'align' in on
    # wcs_shift is KEPT in both (manual offsets for corrupted metadata)
    assert 'wcs_shift' in off and 'wcs_shift' in on
    # everything else untouched
    assert 'bkg' in on and 'detector1' in on


def test_align_cfp_key_resolves_and_shows_in_status():
    # `reset --from align` and the `status` CFP_ALGN column must work now that
    # align is a process-loop step (it's not in the static ALL_STEPS list).
    from campfire_pipeline.nircam.cli import _step_to_cfp_key
    from campfire_pipeline.nircam.orchestrate import CFP_STEPS
    assert _step_to_cfp_key('align') == 'CFP_ALGN'
    assert _step_to_cfp_key('jhat') == 'CFP_JHAT'      # jhat still resolvable
    assert ('align', 'CFP_ALGN') in CFP_STEPS
    on = [k for n, k in _active_process_steps({}, _field(True)) if k]
    assert 'CFP_ALGN' in on and 'CFP_JHAT' not in on   # align field's status column


# --- pool splitting (fast, no FITS) -----------------------------------------

def _mem(det, filt='f200w'):
    return ExposureMember(path=f'/w/tok_{det}.fits', filter_name=filt,
                          rootname=f'tok_{det}', detector=det)


def test_split_pools_per_module():
    g = ExposureGroup('tok', tuple(_mem(d)
                                   for d in ('nrca1', 'nrca2', 'nrcb1', 'nrcb2')))
    pools = split_pools([g], pool_modules=False)
    assert sorted(p.key for p in pools) == ['tok:a', 'tok:b']
    a = next(p for p in pools if p.key == 'tok:a')
    assert sorted(m.detector for m in a.members) == ['nrca1', 'nrca2']


def test_split_pools_pooled_keeps_group_whole():
    g = ExposureGroup('tok', tuple(_mem(d) for d in ('nrca1', 'nrcb1')))
    pools = split_pools([g], pool_modules=True)
    assert len(pools) == 1 and pools[0].key == 'tok'


def test_split_pools_single_module_not_split():
    g = ExposureGroup('tok', tuple(_mem(d) for d in ('nrca1', 'nrca2')))
    pools = split_pools([g], pool_modules=False)
    assert len(pools) == 1 and pools[0].key == 'tok'


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
    """A field with one exposure spanning two filters: nrca1 in f200w +
    nrcalong in f444w (one token), each carrying a 2 arcsec offset from the
    written reference catalog. Align now solves each filter independently."""
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


def _align(field, **kw):
    run_step('align', field, {}, filters=['f200w', 'f444w'], n_processes=1, **kw)


def _algn_rc(value):
    """The rc= refcat-hash token from a CFP_ALGN value string, or None."""
    return next((t[3:] for t in str(value).split() if t.startswith('rc=')), None)


@pytestmark_e2e
def test_align_step_end_to_end(tmp_path):
    field, refcat, xy_by_path = _build_field(tmp_path, enabled=True)
    _align(field)

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
def test_align_step_disabled_is_noop(tmp_path):
    field, _, xy_by_path = _build_field(tmp_path, enabled=False)
    _align(field)
    for path in xy_by_path:
        with fits.open(path) as h:
            assert 'CFP_ALGN' not in h[0].header


@pytestmark_e2e
def test_align_step_idempotent(tmp_path):
    field, _, xy_by_path = _build_field(tmp_path, enabled=True)
    _align(field)
    mtimes = {p: os.path.getmtime(p) for p in xy_by_path}
    _align(field)
    assert all(os.path.getmtime(p) == mtimes[p] for p in xy_by_path)


@pytestmark_e2e
def test_align_step_resolves_stale_refcat(tmp_path):
    # Changing the refcat (its content hash) must re-solve on a normal re-run,
    # not silently keep the stale solution — the rc= provenance staleness check.
    from astropy.table import vstack
    field, refcat, xy_by_path = _build_field(tmp_path, enabled=True)
    _align(field)
    stamps = {}
    for p in xy_by_path:
        with fits.open(p) as h:
            stamps[p] = h[0].header['CFP_ALGN']
        assert 'rc=' in stamps[p]                          # provenance stamped
    # rewrite the refcat with a far (out-of-footprint) decoy row: different file
    # hash (-> stale), but the solve itself is unchanged (row is clipped out).
    extra = Table({'RA': [81.0], 'DEC': [-31.0]})
    extra['mag'] = np.zeros(1, 'float32')
    extra['mag_err'] = np.ones(1, 'float32')
    write_refcat(vstack([refcat, extra]),
                 os.path.join(field.refcat_dir, 'test.ecsv'), overwrite=True)
    _align(field)
    for p in xy_by_path:
        with fits.open(p) as h:
            new = h[0].header['CFP_ALGN']
        assert _algn_rc(new) != _algn_rc(stamps[p])        # re-solved, new rc


@pytestmark_e2e
def test_align_step_warns_and_reattempts_not_aligned(tmp_path, capsys):
    field, _, xy_by_path = _build_field(tmp_path, enabled=True)
    # Clobber the refcat with too few sources -> every pool rejects to
    # NOT_ALIGNED (the solve's <3-source guard).
    tiny = Table({'RA': [80.0, 80.001], 'DEC': [-30.0, -30.001]})
    tiny['mag'] = np.zeros(2, 'float32')
    tiny['mag_err'] = np.ones(2, 'float32')
    write_refcat(tiny, os.path.join(field.refcat_dir, 'test.ecsv'), overwrite=True)

    _align(field)
    out1 = capsys.readouterr().out
    for path in xy_by_path:
        with fits.open(path) as h:
            assert h[0].header.get('CFP_ALGN') == 'NOT_ALIGNED'
    assert 'FAILED alignment' in out1                 # loud warning
    assert _TOKEN in out1                              # lists the failed exposure

    # Re-run WITHOUT --overwrite: the NOT_ALIGNED pool is re-attempted (the user
    # may have retuned params), not silently skipped, and warned about again.
    _align(field)
    out2 = capsys.readouterr().out
    assert 're-attempting' in out2
    assert 'FAILED alignment' in out2


@pytestmark_e2e
def test_align_step_hardstops_unsupported_mode(tmp_path):
    # An exposure in an unsupported observing mode must stop the align step (the
    # user has to exclude it explicitly), not fall through generic logic.
    field, _, xy_by_path = _build_field(tmp_path, enabled=True)
    for path in xy_by_path:
        with fits.open(path, mode='update') as h:
            h[0].header['EXP_TYPE'] = 'NRC_CORON'
            h.flush()
    with pytest.raises(RuntimeError, match='observing mode'):
        _align(field)
    for path in xy_by_path:
        with fits.open(path) as h:
            assert 'CFP_ALGN' not in h[0].header
