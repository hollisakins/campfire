"""Tests for the field-level visit tie (``nircam/align/tie.py``).

A synthetic field: static sources and moving stars on a patch of sky, seen by
overlapping "visits" at different epochs, each visit displaced by a known
linear map. The tie has to recover the maps from the sources the visits share,
place the field on the catalog, and use the stars at each visit's epoch.
"""

from __future__ import annotations

import numpy as np
import pytest
from astropy.table import Table
from astropy.time import Time

from campfire_pipeline.nircam.align import tie

RA0, DEC0 = 3.55, -30.37
ARCMIN = 6.0e4          # mas


def _sky(x_mas, y_mas):
    return tie._deproject(np.asarray(x_mas, float), np.asarray(y_mas, float), RA0, DEC0)


def _field(seed=0, n_src=1500, n_star=40, n_visit=6, noise=2.0, pm_error=0.0,
           filters=('f444w',), chromatic=0.0, visit_shift=20.0):
    """Exposure dicts (as ``tie.read_exposure`` returns them), the catalog, and
    the truth. Visits are 4x4 arcmin tiles in a row, half overlapping, two
    exposures each; visit i was taken 0.5 * i yr after 2022.5."""
    rng = np.random.default_rng(seed)
    width = (n_visit + 1) * 2.0 * ARCMIN
    sx = rng.uniform(-0.5 * width, 0.5 * width, n_src)
    sy = rng.uniform(-2 * ARCMIN, 2 * ARCMIN, n_src)
    gx0 = rng.uniform(-0.5 * width, 0.5 * width, n_star)
    gy0 = rng.uniform(-2 * ARCMIN, 2 * ARCMIN, n_star)
    pm = rng.normal(0, 8.0, (n_star, 2))                     # mas/yr, true
    pm_cat = pm + rng.normal(0, pm_error, (n_star, 2)) if pm_error else pm.copy()
    colour = rng.normal(0, chromatic, (len(filters), n_src, 2)) if chromatic else None

    exposures, truth = [], {}
    for v in range(n_visit):
        cx = (v - 0.5 * (n_visit - 1)) * 2.0 * ARCMIN
        year = 2022.5 + 0.5 * v
        mjd = Time(year, format='jyear').mjd
        for fi, filt in enumerate(filters):
            shift = rng.normal(0, visit_shift, 2)
            lin = rng.normal(0, 1.0, (2, 2))                  # mas / arcmin
            truth[f'jw{v:011d}:{filt}'] = (shift, lin, cx)
            ins = np.abs(sx - cx) < 2 * ARCMIN
            gx = gx0 + pm[:, 0] * (year - 2016.0)
            gy = gy0 + pm[:, 1] * (year - 2016.0)
            ing = np.abs(gx - cx) < 2 * ARCMIN
            px = np.r_[sx[ins] + (colour[fi, ins, 0] if chromatic else 0.0), gx[ing]]
            py = np.r_[sy[ins] + (colour[fi, ins, 1] if chromatic else 0.0), gy[ing]]
            # what the visit measures: truth displaced by MINUS its map
            xp, yp = (px - cx) / ARCMIN, py / ARCMIN
            mx = px - (shift[0] + lin[0, 0] * xp + lin[0, 1] * yp)
            my = py - (shift[1] + lin[1, 0] * xp + lin[1, 1] * yp)
            for e in range(2):
                ra, dec = _sky(mx + rng.normal(0, noise, len(mx)),
                               my + rng.normal(0, noise, len(my)))
                exposures.append(dict(
                    name=f'jw{v:011d}_02101_{e + 1:05d}_nrcalong', visit=f'jw{v:011d}',
                    exposure=f'jw{v:011d}_02101_{e + 1:05d}', module='a', filt=filt,
                    mjd=mjd, ra=ra, dec=dec, mag=np.full(len(ra), 22.0),
                    snr=np.full(len(ra), 100.0)))
    # catalog: half of the static sources with 50 mas errors; all stars at 2016
    cat = rng.random(n_src) < 0.5
    cra, cdec = _sky(sx[cat] + rng.normal(0, 50.0, cat.sum()),
                     sy[cat] + rng.normal(0, 50.0, cat.sum()))
    gra, gdec = _sky(gx0, gy0)
    nan = np.full(cat.sum(), np.nan)
    refcat = Table(dict(
        RA=np.r_[cra, gra], DEC=np.r_[cdec, gdec],
        mag=np.full(cat.sum() + n_star, 20.0), mag_err=np.full(cat.sum() + n_star, 0.1),
        ref_epoch=np.r_[nan, np.full(n_star, 2016.0)],
        pmra=np.r_[nan, pm_cat[:, 0]], pmdec=np.r_[nan, pm_cat[:, 1]],
        pmra_err=np.r_[nan, np.full(n_star, max(pm_error, 0.05))],
        pmdec_err=np.r_[nan, np.full(n_star, max(pm_error, 0.05))]))
    return exposures, refcat, truth, (sx, sy)


def _map_errors(result, truth):
    """Solved minus true shift per unit, mas."""
    out = []
    for row in result.units:
        shift, lin, cx = truth[row['unit']]
        out.append((row['dx_mas'] - shift[0], row['dy_mas'] - shift[1]))
    return np.array(out)


def test_projection_round_trip():
    x = np.array([0.0, 3.0e5, -7.0e5, 1.0])
    y = np.array([0.0, -2.0e5, 4.0e5, -1.0])
    ra, dec = tie._deproject(x, y, RA0, DEC0)
    x2, y2 = tie._project(ra, dec, RA0, DEC0)
    assert np.allclose(x2, x, atol=1e-4) and np.allclose(y2, y, atol=1e-4)


def test_link_one_point_per_source_per_group():
    # two groups, three sources; group 1 has two points near source 0
    x = np.array([0.0, 1000.0, 2000.0, 5.0, 30.0, 1003.0])
    y = np.zeros(6)
    group = np.array([0, 0, 0, 1, 1, 1])
    src, n = tie._link(x, y, group, 50.0)
    assert src[3] == src[0] and src[5] == src[1]
    assert src[4] not in (src[0], src[1], src[2])       # the farther one is new
    assert n == 4


def test_recovers_visit_maps_and_ties_visits():
    exposures, refcat, truth, _ = _field()
    r = tie.tie_field(exposures, refcat, field_name='t')
    err = _map_errors(r, truth)
    # visits were 20 mas apart (per axis); tied to well under the noise of a source
    assert r.stats['heldout_rms_before'] > 10.0
    assert r.stats['heldout_rms_after'] < 0.5
    # every visit within a few mas of truth: set by the catalog (50 mas / sqrt(N))
    assert np.abs(err).max() < 5.0
    # relative placement is far better than absolute
    assert np.std(err, axis=0).max() < 1.5
    assert not r.units['weak'].any()
    assert len(r.consensus) > 1000
    assert {'RA', 'DEC', 'mag', 'mag_err'} <= set(r.consensus.colnames)


def test_stars_are_used_at_each_visit_epoch():
    """With no stationary catalog, the stars alone place the field. They move
    8 mas/yr and the visits span 2.5 yr, so stars taken at the catalog epoch
    (or at one common epoch) would be tens of mas off, differently per visit."""
    exposures, refcat, truth, _ = _field(seed=1)
    stars_only = refcat[np.isfinite(np.asarray(refcat['pmra'], float))]
    r = tie.tie_field(exposures, stars_only, field_name='t')
    err = _map_errors(r, truth)
    assert r.stats['n_star_detections'] > 50
    assert np.abs(err).max() < 3.0
    assert abs(r.stats['star_median_mas'][0]) < 1.0
    assert abs(r.stats['star_median_mas'][1]) < 1.0

    # control: the same stars with their motions removed are wrong by ~50-70 mas
    # per visit, so they are either rejected or drag the field; either way the
    # result above is not reproduced by a catalog without motions.
    frozen = stars_only.copy()
    frozen['pmra'] = 0.0
    frozen['pmdec'] = 0.0
    r0 = tie.tie_field(exposures, frozen, field_name='t')
    assert np.abs(_map_errors(r0, truth)).max() > 10.0


def test_star_proper_motion_error_is_one_unknown_per_star():
    """Catalog proper motions off by 1 mas/yr: over 7-9 yr each star is 7-9 mas
    from where the catalog puts it, the same way in every visit. Solving for
    that error per star keeps the stars consistent with the JWST ties."""
    exposures, refcat, truth, _ = _field(seed=2, pm_error=1.0)
    r = tie.tie_field(exposures, refcat, field_name='t')
    assert r.stats['heldout_rms_after'] < 0.5
    assert r.stats['star_residual_mas'] < 4.0      # not the 8 mas the errors imply
    assert np.abs(_map_errors(r, truth)).max() < 6.0


def test_weak_unit_is_reported_and_still_placed_by_the_catalog():
    exposures, refcat, truth, _ = _field(seed=3, n_visit=3)
    # a fourth visit far from the others: shares nothing
    far, far_cat, far_truth, _ = _field(seed=4, n_visit=1, n_src=400)
    for e in far:
        ra, dec = e['ra'], e['dec']
        x, y = tie._project(ra, dec, RA0, DEC0)
        e['ra'], e['dec'] = _sky(x + 30 * ARCMIN, y)
        e['visit'] = 'jw99999999999'
        e['exposure'] = e['exposure'].replace('jw00000000000', 'jw99999999999')
    x, y = tie._project(np.asarray(far_cat['RA']), np.asarray(far_cat['DEC']), RA0, DEC0)
    far_cat['RA'], far_cat['DEC'] = _sky(x + 30 * ARCMIN, y)
    stationary = ~np.isfinite(np.asarray(far_cat['pmra'], float))
    from astropy.table import vstack
    r = tie.tie_field(exposures + far, vstack([refcat, far_cat[stationary]]),
                      field_name='t')
    row = r.units[r.units['visit'] == 'jw99999999999'][0]
    assert row['weak'] and row['n_shared'] == 0
    shift = far_truth['jw00000000000:f444w'][0]
    # placed by ~200 catalog rows at 50 mas: within ~4 sigma of 3.5 mas
    assert abs(row['dx_mas'] - shift[0]) < 15.0 and abs(row['dy_mas'] - shift[1]) < 15.0
    assert (~r.units['weak']).sum() == 3


def test_two_filters_get_separate_maps_and_one_frame():
    exposures, refcat, truth, _ = _field(seed=5, filters=('f277w', 'f444w'),
                                         chromatic=3.0, n_visit=4)
    r = tie.tie_field(exposures, refcat, field_name='t')
    assert len(r.units) == 8 and set(r.units['filter']) == {'f277w', 'f444w'}
    err = _map_errors(r, truth)
    assert np.abs(err).max() < 6.0
    assert r.stats['heldout_rms_after'] < 0.6
    # the two filters of one visit were displaced independently (20 mas) and
    # end on the same frame
    for v in set(r.units['visit']):
        rows = r.units[r.units['visit'] == v]
        d = [np.array([row['dx_mas'], row['dy_mas']]) - truth[row['unit']][0] for row in rows]
        assert np.abs(d[0] - d[1]).max() < 1.5


def test_unaligned_or_empty_input_is_an_error():
    with pytest.raises(ValueError):
        tie.tie_field([None], Table(dict(RA=[1.0], DEC=[1.0], mag=[1.0], mag_err=[1.0])))
