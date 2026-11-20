"""Field-level tie of NIRCam visits: one frame for a whole field.

The ``align`` step fits each visit to a reference catalog on its own, so two
visits agree only as well as the catalog allows over the sky they share. An
external catalog (Legacy Surveys galaxies at ~50 mas per source, a few dozen
Gaia stars) leaves visit-to-visit offsets of several mas to tens of mas, which
is what limits epoch-to-epoch astrometry. The JWST detections themselves are
good to a few mas each and every pair of overlapping visits shares hundreds to
thousands of them. This module uses them.

**The solve.** For the whole field at once, with x and y independent:

``unknowns``
    one linear map per *unit* (shift + 2x2 matrix about the unit's centre; a
    unit is one visit in one filter, so no geometry is shared between filters
    or assumed for the instrument), one sky position per source and, when
    several filters are tied, one position per (source, filter).
``ties``
    every (source, unit) mean position, moved by its unit's map, equals the
    source's position in that filter.
``chromatic term``
    a source's position in each filter equals its position within
    ``sig_chromatic`` (a galaxy's centroid moves with wavelength); ties within
    one filter are therefore stronger than ties across filters.
``stars``
    a detection of a catalog star with a proper motion, moved by its unit's
    map, equals the star's catalog position **propagated to that unit's
    epoch**, plus the star's own proper-motion error times the time since the
    catalog epoch. That error is one unknown per star, held to zero within the
    catalog's quoted proper-motion error, so a star seen by ten visits is not
    counted as ten independent measurements of the frame. Stars are not sources
    of the solve: they move between visits. Only an unsaturated star can be
    used (a saturated core is masked and its fragments sit ~0.2 arcsec off),
    which ``star_radius`` and the clipping select; on a long-wavelength field
    that leaves the faint end of Gaia, a few tens of stars.
``galaxies``
    a source with a stationary catalog counterpart equals the catalog position
    within ``sig_catalog``. Weak per source; it places the field where there
    are no stars.
``prior``
    the four matrix terms of each unit are zero within ``sig_linear``, so a
    unit with few ties stays sane.

No visit is a reference. The catalog fixes where the field sits and the
low-order shape of the mosaic; the JWST ties fix how visits sit against each
other. A unit that shares few or no sources with other units is still solved:
it is held by its stars, its catalog galaxies and the prior, and is reported as
weakly tied (``weak`` in the unit table).

**Outputs.** The solved source positions as a reference catalog (the
*consensus* catalog, ``campfire-refcat-v1``), which the ``align`` step then
uses for every filter of the field; and one row per unit with its map, its
formal errors and what tied it. A first solve holds half of the shared sources
out and measures unit-to-unit agreement on them before and after, so the
quality of the tie is measured on sources that did not make it.

Measured on Abell 2744 (2026-11, 27 F444W visits over 3 years): unit-to-unit
agreement on held-out sources 0.9 mas rms; epoch-pair offsets of the mosaics in
12 arcsec cells 3.1 mas at the 90th percentile, against 15 mas when every visit
is fitted to Legacy Surveys DR10 on its own.

This module does no pipeline I/O beyond reading ``ALGNCAT`` tables: the
orchestration layer (``orchestrate.run_tie``) chooses the exposures, loads the
catalog and writes the results.
"""

import os
from dataclasses import dataclass, field as _dc_field

import numpy as np

from campfire_pipeline.common.io import log

#: Defaults of ``[nircam.align.tie]`` (the config file is the authority; these
#: serve direct callers and tests).
DEFAULTS = dict(
    unit='visit',          # 'visit' | 'module': what gets its own map, per filter
    snr_min=50.0,          # detections used
    link_radius=0.10,      # arcsec: detections closer than this are one source
    star_radius=0.08,      # arcsec: detection to catalog star at the unit epoch
    min_detections=2,      # exposures of a unit that must see a source (units of 2+ exposures)
    anchor_radius=0.20,    # arcsec: source to stationary catalog row
    sig_detection=8.0,     # mas per detection
    sig_floor=2.0,         # mas, floor of a (source, unit) mean
    sig_catalog=60.0,      # mas per stationary catalog row
    sig_star=5.0,          # mas floor per star detection (saturation, crowding)
    sig_pm_floor=0.1,      # mas/yr added to the catalog's proper-motion error
    sig_linear=5.0,        # mas per arcmin, prior on a unit's matrix terms
    sig_chromatic=3.0,     # mas, a source's position from filter to filter
    min_shared=30,         # shared sources below which a unit is 'weak'
    holdout=True,          # measure the tie on held-out sources first
    seed=0,
)

_MAS = 3.6e6 * 180.0 / np.pi     # radians -> mas


def _mad(x):
    x = np.asarray(x, float)
    if x.size == 0:
        return np.nan
    return 1.4826 * np.nanmedian(np.abs(x - np.nanmedian(x)))


# --- tangent plane ------------------------------------------------------------

def _project(ra, dec, ra0, dec0):
    """Gnomonic projection about (ra0, dec0); returns (xi, eta) in mas.

    A real tangent plane, not ``(ra - ra0) cos(dec0)``: over a 5 arcmin visit
    the latter departs from a plane by tens of mas, which a linear map cannot
    absorb.
    """
    a, d = np.radians(ra), np.radians(dec)
    a0, d0 = np.radians(ra0), np.radians(dec0)
    cosc = np.sin(d0) * np.sin(d) + np.cos(d0) * np.cos(d) * np.cos(a - a0)
    xi = np.cos(d) * np.sin(a - a0) / cosc
    eta = (np.cos(d0) * np.sin(d) - np.sin(d0) * np.cos(d) * np.cos(a - a0)) / cosc
    return xi * _MAS, eta * _MAS


def _deproject(xi, eta, ra0, dec0):
    """Inverse of :func:`_project`; returns (ra, dec) in degrees."""
    x, y = np.asarray(xi, float) / _MAS, np.asarray(eta, float) / _MAS
    a0, d0 = np.radians(ra0), np.radians(dec0)
    rho = np.hypot(x, y)
    c = np.arctan(rho)
    with np.errstate(invalid='ignore', divide='ignore'):
        dec = np.arcsin(np.cos(c) * np.sin(d0)
                        + np.where(rho > 0, y * np.sin(c) * np.cos(d0) / rho, 0.0))
        ra = a0 + np.arctan2(x * np.sin(c),
                             rho * np.cos(d0) * np.cos(c) - y * np.sin(d0) * np.sin(c))
    ra = np.where(rho > 0, ra, a0)
    return np.degrees(ra) % 360.0, np.degrees(dec)


# --- reading exposures --------------------------------------------------------

def read_exposure(path, snr_min=50.0):
    """Detections of one aligned exposure from its ``ALGNCAT`` table.

    Returns ``None`` when the file has no ``ALGNCAT`` (not aligned) or carries
    ``CFP_ALGN = NOT_ALIGNED``. Sky positions are the ones ``align`` wrote,
    under the exposure's accepted WCS.
    """
    from astropy.io import fits
    name = os.path.basename(path)
    name = name[:-5] if name.endswith('.fits') else name
    with fits.open(path, memmap=False) as h:
        if 'ALGNCAT' not in h:
            return None
        hdr = h[0].header
        if str(hdr.get('CFP_ALGN', '')).upper().startswith('NOT_ALIGNED'):
            return None
        c = h['ALGNCAT'].data
        snr = np.asarray(c['snr'], float)
        ra, dec = np.asarray(c['ra'], float), np.asarray(c['dec'], float)
        k = (snr > snr_min) & np.isfinite(ra) & np.isfinite(dec)
        mag = (np.asarray(c['mag'], float)[k] if 'mag' in c.names
               else np.full(k.sum(), np.nan))
        mjd = None
        for card in ('EXPMID', 'MJD-AVG'):
            if hdr.get(card) is not None:
                mjd = float(hdr[card])
                break
        if mjd is None and hdr.get('EXPSTART') is not None and hdr.get('EXPEND') is not None:
            mjd = 0.5 * (float(hdr['EXPSTART']) + float(hdr['EXPEND']))
        filt = str(hdr.get('FILTER', '') or '').lower()
    if not filt:
        filt = os.path.basename(os.path.dirname(path)).lower()
    # jwPPPPPOOOVVV_GGSAA_EEEEE_<detector>
    parts = name.split('_')
    detector = parts[-1].lower() if len(parts) > 1 else ''
    module = detector[3:4] if detector.startswith('nrc') else ''
    return dict(name=name, visit=parts[0], exposure='_'.join(parts[:3]),
                module=module, filt=filt, mjd=mjd,
                ra=ra[k], dec=dec[k], mag=mag, snr=snr[k])


def _link(x, y, group, radius_mas):
    """Group points into sources: greedy, group by group.

    *group* labels the points (an exposure, or a unit); within one group a
    source is taken by at most one point. The groups are visited largest first;
    each point joins the nearest existing source within *radius_mas* if it is
    also that source's nearest point of the group, else it starts a source.
    Returns the source index of every point and the number of sources.
    """
    from scipy.spatial import cKDTree
    x, y = np.asarray(x, float), np.asarray(y, float)
    src = -np.ones(len(x), int)
    sx, sy, sn = np.empty(0), np.empty(0), np.empty(0)
    labels, counts = np.unique(group, return_counts=True)
    for g in labels[np.argsort(-counts, kind='stable')]:
        idx = np.where(group == g)[0]
        if len(sx):
            mx, my = sx / sn, sy / sn
            d, j = cKDTree(np.c_[mx, my]).query(np.c_[x[idx], y[idx]])
            ok = d < radius_mas
            # one point per source within a group: keep the nearest
            order = np.argsort(d)
            taken = np.zeros(len(sx), bool)
            win = np.zeros(len(idx), bool)
            for i in order:
                if not ok[i]:
                    break
                if not taken[j[i]]:
                    taken[j[i]] = True
                    win[i] = True
            src[idx[win]] = j[win]
            np.add.at(sx, j[win], x[idx[win]])
            np.add.at(sy, j[win], y[idx[win]])
            np.add.at(sn, j[win], 1.0)
            new = idx[~win]
        else:
            new = idx
        if len(new):
            src[new] = len(sx) + np.arange(len(new))
            sx = np.r_[sx, x[new]]
            sy = np.r_[sy, y[new]]
            sn = np.r_[sn, np.ones(len(new))]
    return src, len(sx)


# --- the solve ----------------------------------------------------------------

@dataclass
class _Problem:
    """Rows of the least-squares problem (one axis shares everything but the
    measured values)."""
    nu: int
    ns: int
    # ties: (source, unit) means
    t_unit: np.ndarray
    t_src: np.ndarray
    t_band: np.ndarray           # index into (source, band) positions, or -1
    t_xp: np.ndarray             # arcmin from the unit centre
    t_yp: np.ndarray
    t_sig: np.ndarray            # mas
    t_x: np.ndarray              # mas, measured
    t_y: np.ndarray
    # (source, band) positions
    q_src: np.ndarray
    q_band: np.ndarray
    # stars: detections of catalog stars at the unit epoch
    s_unit: np.ndarray
    s_xp: np.ndarray
    s_yp: np.ndarray
    s_sig: np.ndarray
    s_dx: np.ndarray             # mas, measured minus catalog at epoch
    s_dy: np.ndarray
    s_star: np.ndarray           # index of the star
    s_dt: np.ndarray             # years since the catalog epoch
    pm_sig: np.ndarray           # mas/yr per star and axis: prior on its PM error
    # galaxies: stationary catalog rows on sources
    c_src: np.ndarray
    c_x: np.ndarray
    c_y: np.ndarray
    sig_catalog: float = 60.0
    sig_linear: float = 5.0
    sig_chromatic: float = 3.0


def _solve_axis(p, meas, star, cat, wt, ws, wc, want_errors=False):
    """Solve one axis. Returns ``(unit params (nu, 3), source positions (ns,),
    band positions (nq,), formal errors of the unit params or None)``.

    A unit's map is ``corrected = measured + t + a * xp + b * yp``.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import splu

    nu, ns, nq, nstar = p.nu, p.ns, len(p.q_src), len(p.pm_sig)
    n_unk = 3 * nu + ns + nq + nstar
    rows, cols, vals, rhs = [], [], [], []
    r0 = 0

    def add(col_blocks, b):
        nonlocal r0
        n = len(b)
        r = r0 + np.arange(n)
        for c, v in col_blocks:
            rows.append(r)
            cols.append(c)
            vals.append(np.broadcast_to(v, (n,)))
        rhs.append(b)
        r0 += n

    # ties:  w (t + a xp + b yp - pos) = -w meas
    w = wt / p.t_sig
    pos_col = np.where(p.t_band >= 0, 3 * nu + ns + p.t_band, 3 * nu + p.t_src)
    add([(3 * p.t_unit, w), (3 * p.t_unit + 1, w * p.t_xp),
         (3 * p.t_unit + 2, w * p.t_yp), (pos_col, -w)], -w * meas)
    # chromatic term:  (q - p) / sig = 0
    if nq:
        wq = np.full(nq, 1.0 / p.sig_chromatic)
        add([(3 * nu + ns + np.arange(nq), wq), (3 * nu + p.q_src, -wq)],
            np.zeros(nq))
    # stars:  w (t + a xp + b yp - mu dt) = -w (meas - catalog at epoch)
    if len(p.s_unit):
        w = ws / p.s_sig
        add([(3 * p.s_unit, w), (3 * p.s_unit + 1, w * p.s_xp),
             (3 * p.s_unit + 2, w * p.s_yp),
             (3 * nu + ns + nq + p.s_star, -w * p.s_dt)], -w * star)
        add([(3 * nu + ns + nq + np.arange(nstar), 1.0 / p.pm_sig)],
            np.zeros(nstar))
    # galaxies:  pos / sig = catalog / sig
    if len(p.c_src):
        w = wc / p.sig_catalog
        add([(3 * nu + p.c_src, w)], w * cat)
    # prior on the matrix terms
    wl = np.full(nu, 1.0 / p.sig_linear)
    add([(3 * np.arange(nu) + 1, wl)], np.zeros(nu))
    add([(3 * np.arange(nu) + 2, wl)], np.zeros(nu))

    A = coo_matrix((np.concatenate(vals),
                    (np.concatenate(rows), np.concatenate(cols))),
                   shape=(r0, n_unk)).tocsr()
    b = np.concatenate(rhs)
    N = (A.T @ A).tocsc()
    # A unit, source or band position that nothing constrains keeps its start
    # value (zero correction; an unconstrained position is dropped later).
    N.setdiag(N.diagonal() + 1e-9)
    lu = splu(N)
    sol = lu.solve(A.T @ b)
    err = None
    if want_errors:
        err = np.empty(3 * nu)
        for i in range(3 * nu):
            e = np.zeros(n_unk)
            e[i] = 1.0
            err[i] = np.sqrt(max(lu.solve(e)[i], 0.0))
        err = err.reshape(nu, 3)
    return (sol[:3 * nu].reshape(nu, 3), sol[3 * nu:3 * nu + ns],
            sol[3 * nu + ns:3 * nu + ns + nq], err, sol[3 * nu + ns + nq:])


def _solve(p, use_src=None, want_errors=False, label='tie'):
    """Both axes with outlier clipping. *use_src* masks the sources whose ties
    and catalog rows enter. Returns a dict with the maps, positions, residuals
    and the kept-row masks."""
    kt = np.ones(len(p.t_src), bool) if use_src is None else use_src[p.t_src]
    kc = np.ones(len(p.c_src), bool) if use_src is None else use_src[p.c_src]
    wt, ws, wc = kt.astype(float), np.ones(len(p.s_unit)), kc.astype(float)
    out = None
    for it in range(6):
        last = it == 5
        Px, Sx, Qx, Ex, Mx = _solve_axis(p, p.t_x, p.s_dx, p.c_x, wt, ws, wc, want_errors)
        Py, Sy, Qy, Ey, My = _solve_axis(p, p.t_y, p.s_dy, p.c_y, wt, ws, wc, want_errors)

        def corr(P, unit, xp, yp):
            return P[unit, 0] + P[unit, 1] * xp + P[unit, 2] * yp

        posx = np.where(p.t_band >= 0, Qx[np.maximum(p.t_band, 0)] if len(Qx) else 0.0,
                        Sx[p.t_src])
        posy = np.where(p.t_band >= 0, Qy[np.maximum(p.t_band, 0)] if len(Qy) else 0.0,
                        Sy[p.t_src])
        rx = p.t_x + corr(Px, p.t_unit, p.t_xp, p.t_yp) - posx
        ry = p.t_y + corr(Py, p.t_unit, p.t_xp, p.t_yp) - posy
        if len(p.s_unit):
            sx = p.s_dx + corr(Px, p.s_unit, p.s_xp, p.s_yp) - Mx[p.s_star] * p.s_dt
            sy = p.s_dy + corr(Py, p.s_unit, p.s_xp, p.s_yp) - My[p.s_star] * p.s_dt
        else:
            sx, sy = np.empty(0), np.empty(0)
        cx, cy = Sx[p.c_src] - p.c_x, Sy[p.c_src] - p.c_y
        out = dict(Px=Px, Py=Py, Sx=Sx, Sy=Sy, Ex=Ex, Ey=Ey, Mx=Mx, My=My, rx=rx, ry=ry,
                   sx=sx, sy=sy, cx=cx, cy=cy, wt=wt > 0, ws=ws > 0, wc=wc > 0)
        if last:
            break
        good = wt > 0
        cut = max(4.0 * _mad(np.r_[rx[good], ry[good]]), 12.0) if good.any() else 12.0
        nwt = kt & (np.hypot(rx, ry) < cut)
        if len(sx):
            scut = np.maximum(4.0 * p.s_sig, 20.0)
            nws = np.hypot(sx, sy) < scut
        else:
            nws = ws > 0
        nwc = kc & (np.hypot(cx, cy) < 2.5 * p.sig_catalog)
        same = ((nwt == (wt > 0)).all() and (nws == (ws > 0)).all()
                and (nwc == (wc > 0)).all())
        wt, ws, wc = nwt.astype(float), nws.astype(float), nwc.astype(float)
        if same:
            break
    good = out['wt']
    log(f"{label}: {int(kt.sum())} ties ({int((kt & ~good).sum())} clipped), "
        f"{len(p.s_unit)} star detections ({int((~out['ws']).sum())} clipped), "
        f"{int(kc.sum())} catalog rows ({int((kc & ~out['wc']).sum())} clipped); "
        f"{len(p.pm_sig)} stars")
    return out


def _unit_agreement(p, src_mask, sol=None, min_pair=15):
    """Unit-to-unit agreement on the sources of *src_mask*: for every pair of
    units of the same filter, the median difference of the positions they give
    the sources they share. Returns ``(rms, largest, n_pairs)`` in mas over
    pairs with at least *min_pair* sources (per axis)."""
    k = src_mask[p.t_src]
    x, y = p.t_x[k].copy(), p.t_y[k].copy()
    unit, src = p.t_unit[k], p.t_src[k]
    if sol is not None:
        x += sol['Px'][unit, 0] + sol['Px'][unit, 1] * p.t_xp[k] + sol['Px'][unit, 2] * p.t_yp[k]
        y += sol['Py'][unit, 0] + sol['Py'][unit, 1] * p.t_xp[k] + sol['Py'][unit, 2] * p.t_yp[k]
    band = (p.q_band[p.t_band[k]] if len(p.q_band) else np.zeros(int(k.sum()), int))
    order = np.argsort(src, kind='stable')
    src, unit, x, y, band = src[order], unit[order], x[order], y[order], band[order]
    starts = np.r_[0, np.flatnonzero(np.diff(src)) + 1, len(src)]
    pairs = {}
    for a, b in zip(starts[:-1], starts[1:]):
        if b - a < 2:
            continue
        for i in range(a, b):
            for j in range(i + 1, b):
                # across filters a source's own colour gradient enters; the
                # agreement that matters for epoch pairs is within a filter
                if band[i] != band[j]:
                    continue
                u, v = (unit[i], unit[j]) if unit[i] < unit[j] else (unit[j], unit[i])
                s = 1.0 if unit[i] < unit[j] else -1.0
                pairs.setdefault((u, v), []).append((s * (x[i] - x[j]), s * (y[i] - y[j])))
    med = [np.median(np.array(v), axis=0) for v in pairs.values() if len(v) >= min_pair]
    if not med:
        return np.nan, np.nan, 0
    med = np.array(med)
    return float(np.sqrt(np.mean(med ** 2))), float(np.abs(med).max()), len(med)


@dataclass
class TieResult:
    """What :func:`tie_field` returns."""
    consensus: object                 # astropy Table: RA, DEC, mag, mag_err, n_units
    units: object                     # astropy Table, one row per unit
    stats: dict = _dc_field(default_factory=dict)


def tie_field(exposures, refcat, *, field_name='', config=None):
    """Tie the units of a field; return a :class:`TieResult`.

    Parameters
    ----------
    exposures : list of dict
        One per aligned exposure, as :func:`read_exposure` returns them.
    refcat : astropy Table
        The external catalog (``campfire-refcat-v1``): stationary rows anchor
        the field weakly; rows with ``pmra`` / ``pmdec`` / ``ref_epoch`` are
        stars and are used at each unit's epoch.
    config : dict, optional
        ``[nircam.align.tie]``; see :data:`DEFAULTS`.
    """
    from astropy.table import Table
    from scipy.spatial import cKDTree
    from campfire_pipeline.nircam.refcat.io import has_proper_motion
    from campfire_pipeline.nircam.refcat.motion import propagate_to_epoch

    cfg = {**DEFAULTS, **(config or {})}
    exposures = [e for e in exposures if e is not None and len(e['ra'])]
    if not exposures:
        raise ValueError("tie: no aligned exposures with detections")
    by_module = str(cfg['unit']).lower() == 'module'

    def unit_key(e):
        base = e['visit'] + (e['module'] if by_module else '')
        return f"{base}:{e['filt']}"

    ra0 = float(np.median(np.concatenate([e['ra'] for e in exposures])))
    dec0 = float(np.median(np.concatenate([e['dec'] for e in exposures])))
    bands = sorted({e['filt'] for e in exposures})
    multi_band = len(bands) > 1
    keys = sorted({unit_key(e) for e in exposures})
    uidx = {k: i for i, k in enumerate(keys)}
    nu = len(keys)

    # --- (source, unit) means: detections of a unit's exposures, linked -------
    U = dict(unit=[], x=[], y=[], n=[], mag=[])
    unit_info = []
    for k in keys:
        mem = [e for e in exposures if unit_key(e) == k]
        x, y = _project(np.concatenate([e['ra'] for e in mem]),
                        np.concatenate([e['dec'] for e in mem]), ra0, dec0)
        mag = np.concatenate([e['mag'] for e in mem])
        grp = np.concatenate([np.full(len(e['ra']), i) for i, e in enumerate(mem)])
        src, n = _link(x, y, grp, cfg['link_radius'] * 1e3)
        cnt = np.bincount(src, minlength=n).astype(float)
        mx = np.bincount(src, weights=x, minlength=n) / cnt
        my = np.bincount(src, weights=y, minlength=n) / cnt
        # a source one exposure alone saw, in a unit of several, is as often a
        # cosmic ray or a spike fragment as a source
        seen = cnt >= (int(cfg['min_detections']) if len(mem) > 1 else 1)
        fin = np.isfinite(mag)
        nm = np.bincount(src, weights=fin.astype(float), minlength=n)
        mm = np.where(nm > 0, np.bincount(src, weights=np.where(fin, mag, 0.0),
                                          minlength=n) / np.maximum(nm, 1), np.nan)
        U['unit'].append(np.full(int(seen.sum()), uidx[k]))
        U['x'].append(mx[seen])
        U['y'].append(my[seen])
        U['n'].append(cnt[seen])
        U['mag'].append(mm[seen])
        mx, my = mx[seen], my[seen]
        mjds = [e['mjd'] for e in mem if e['mjd'] is not None]
        unit_info.append(dict(key=k, visit=mem[0]['visit'], filt=mem[0]['filt'],
                              module=mem[0]['module'] if by_module else '',
                              n_exposures=len({e['exposure'] for e in mem}),
                              mjd=float(np.mean(mjds)) if mjds else np.nan,
                              x0=float(np.median(mx)), y0=float(np.median(my))))
    U = {k: np.concatenate(v) for k, v in U.items()}
    U['unit'] = U['unit'].astype(int)
    x0 = np.array([u['x0'] for u in unit_info])
    y0 = np.array([u['y0'] for u in unit_info])
    xp = (U['x'] - x0[U['unit']]) / 6.0e4          # arcmin from the unit centre
    yp = (U['y'] - y0[U['unit']]) / 6.0e4
    sig = np.sqrt(cfg['sig_detection'] ** 2 / U['n'] + cfg['sig_floor'] ** 2)

    # --- stars: catalog rows with a proper motion, at each unit's epoch ------
    is_star_obs = np.zeros(len(U['x']), bool)
    S = dict(unit=[], xp=[], yp=[], sig=[], dx=[], dy=[], star=[], dt=[])
    pm_sig = np.empty(0)
    n_stars_cat = 0
    if has_proper_motion(refcat):
        pmra = np.asarray(np.ma.filled(refcat['pmra'], np.nan), float)
        pmdec = np.asarray(np.ma.filled(refcat['pmdec'], np.nan), float)
        epoch = np.asarray(np.ma.filled(refcat['ref_epoch'], np.nan), float)
        star = np.isfinite(pmra) & np.isfinite(pmdec) & np.isfinite(epoch)
        n_stars_cat = int(star.sum())
        stars = refcat[star]

        def perr(name):
            if name in stars.colnames:
                return np.nan_to_num(np.asarray(np.ma.filled(stars[name], np.nan), float))
            return np.zeros(len(stars))
        pm_sig = (np.hypot(perr('pmra_err'), perr('pmdec_err')) / np.sqrt(2.0)
                  + float(cfg['sig_pm_floor']))
        from astropy.time import Time
        for i, u in enumerate(unit_info):
            if not np.isfinite(u['mjd']) or not len(stars):
                continue
            at = propagate_to_epoch(stars, u['mjd'])
            gx, gy = _project(np.asarray(at['RA'], float), np.asarray(at['DEC'], float),
                              ra0, dec0)
            mine = np.where(U['unit'] == i)[0]
            if not len(mine):
                continue
            d, j = cKDTree(np.c_[gx, gy]).query(np.c_[U['x'][mine], U['y'][mine]])
            ok = d < cfg['star_radius'] * 1e3
            m = mine[ok]
            is_star_obs[m] = True
            dt = Time(u['mjd'], format='mjd').jyear - epoch[star][j[ok]]
            S['unit'].append(np.full(len(m), i))
            S['xp'].append(xp[m])
            S['yp'].append(yp[m])
            S['sig'].append(np.sqrt(cfg['sig_star'] ** 2
                                    + cfg['sig_detection'] ** 2 / U['n'][m]))
            S['star'].append(j[ok])
            S['dt'].append(dt)
            S['dx'].append(U['x'][m] - gx[j[ok]])
            S['dy'].append(U['y'][m] - gy[j[ok]])
        stationary = refcat[~star]
    else:
        stationary = refcat
    S = {k: (np.concatenate(v) if v else np.empty(0)) for k, v in S.items()}
    S['unit'] = S['unit'].astype(int)
    S['star'] = S['star'].astype(int)

    # --- sources: (source, unit) means linked across units --------------------
    t = np.where(~is_star_obs)[0]
    src, ns = _link(U['x'][t], U['y'][t], U['unit'][t], cfg['link_radius'] * 1e3)
    band_idx = np.array([bands.index(u['filt']) for u in unit_info])
    t_bandid = band_idx[U['unit'][t]]
    if multi_band:
        qkey = src * len(bands) + t_bandid
        uq, t_band = np.unique(qkey, return_inverse=True)
        q_src, q_band = uq // len(bands), uq % len(bands)
    else:
        t_band = -np.ones(len(t), int)
        q_src, q_band = np.empty(0, int), np.empty(0, int)

    # stationary catalog rows on sources (at the mean measured position)
    cnt = np.bincount(src, minlength=ns).astype(float)
    mx = np.bincount(src, weights=U['x'][t], minlength=ns) / cnt
    my = np.bincount(src, weights=U['y'][t], minlength=ns) / cnt
    c_src, c_x, c_y = np.empty(0, int), np.empty(0), np.empty(0)
    if len(stationary):
        cxx, cyy = _project(np.asarray(stationary['RA'], float),
                            np.asarray(stationary['DEC'], float), ra0, dec0)
        d, j = cKDTree(np.c_[mx, my]).query(np.c_[cxx, cyy])
        d2, i2 = cKDTree(np.c_[cxx, cyy]).query(np.c_[mx, my])
        ok = (d < cfg['anchor_radius'] * 1e3) & (i2[j] == np.arange(len(cxx)))
        c_src, c_x, c_y = j[ok], cxx[ok], cyy[ok]

    # Solve for small numbers: every position is taken relative to the source's
    # mean measured position (mx, my), added back at the end. With raw
    # tangent-plane coordinates (1e6 mas) the normal equations lose the mas.
    p = _Problem(nu=nu, ns=ns, t_unit=U['unit'][t], t_src=src, t_band=t_band,
                 t_xp=xp[t], t_yp=yp[t], t_sig=sig[t],
                 t_x=U['x'][t] - mx[src], t_y=U['y'][t] - my[src],
                 q_src=q_src, q_band=q_band, s_unit=S['unit'], s_xp=S['xp'], s_yp=S['yp'],
                 s_sig=S['sig'], s_dx=S['dx'], s_dy=S['dy'],
                 s_star=S['star'], s_dt=S['dt'], pm_sig=pm_sig,
                 c_src=c_src, c_x=c_x - mx[c_src], c_y=c_y - my[c_src],
                 sig_catalog=float(cfg['sig_catalog']),
                 sig_linear=float(cfg['sig_linear']),
                 sig_chromatic=float(cfg['sig_chromatic']))

    # units a source is seen by (same filter counted once per unit)
    n_units_src = np.bincount(src, minlength=ns)
    shared = n_units_src >= 2
    log(f"tie[{field_name}]: {len(exposures)} exposures, {nu} units "
        f"({', '.join(bands)}), {ns} sources ({int(shared.sum())} seen by 2+ units), "
        f"{len(S['unit'])} detections of {len(np.unique(S['star']))} of "
        f"{n_stars_cat} catalog stars, "
        f"{len(c_src)} sources with a stationary catalog row")

    stats = dict(n_exposures=len(exposures), n_units=nu, filters=bands,
                 n_sources=int(ns), n_shared=int(shared.sum()),
                 n_star_detections=int(len(S['unit'])), n_catalog_rows=int(len(c_src)))

    # --- held-out validation ---------------------------------------------------
    if cfg['holdout'] and shared.sum() >= 40:
        rng = np.random.default_rng(int(cfg['seed']))
        hold = shared & (rng.random(ns) < 0.5)
        first = _solve(p, use_src=~hold, label=f"tie[{field_name}] (half held out)")
        before = _unit_agreement(p, hold)
        after = _unit_agreement(p, hold, first)
        stats.update(heldout_rms_before=before[0], heldout_max_before=before[1],
                     heldout_rms_after=after[0], heldout_max_after=after[1],
                     heldout_pairs=after[2])
        log(f"tie[{field_name}]: unit-to-unit agreement on held-out sources "
            f"({after[2]} pairs of units): rms {before[0]:.2f} -> {after[0]:.2f} mas, "
            f"largest {before[1]:.2f} -> {after[1]:.2f} mas")

    sol = _solve(p, want_errors=True, label=f"tie[{field_name}]")

    # --- unit table --------------------------------------------------------------
    kept = sol['wt']
    n_ties = np.bincount(p.t_unit[kept], minlength=nu)
    n_shared_u = np.bincount(p.t_unit[kept & shared[p.t_src]], minlength=nu)
    n_star_u = (np.bincount(p.s_unit[sol['ws']], minlength=nu)
                if len(p.s_unit) else np.zeros(nu, int))
    n_cat_u = np.zeros(nu, int)
    if len(c_src):
        has_cat = np.zeros(ns, bool)
        has_cat[c_src[sol['wc']]] = True
        n_cat_u = np.bincount(p.t_unit[kept & has_cat[p.t_src]], minlength=nu)
    star_res = np.full((nu, 2), np.nan)
    for i in range(nu):
        m = (p.s_unit == i) & sol['ws'] if len(p.s_unit) else np.zeros(0, bool)
        if m.sum() >= 3:
            star_res[i] = np.median(sol['sx'][m]), np.median(sol['sy'][m])
    Ex, Ey = sol['Ex'], sol['Ey']
    uc_ra, uc_dec = _deproject(x0, y0, ra0, dec0)     # where each map is centred
    units = Table(dict(
        unit=[u['key'] for u in unit_info],
        visit=[u['visit'] for u in unit_info],
        filter=[u['filt'] for u in unit_info],
        module=[u['module'] for u in unit_info],
        mjd=[u['mjd'] for u in unit_info],
        ra_center=uc_ra, dec_center=uc_dec,
        n_exposures=[u['n_exposures'] for u in unit_info],
        n_ties=n_ties, n_shared=n_shared_u, n_stars=n_star_u, n_catalog=n_cat_u,
        weak=(n_shared_u < int(cfg['min_shared'])),
        dx_mas=sol['Px'][:, 0], dy_mas=sol['Py'][:, 0],
        dx_err_mas=Ex[:, 0], dy_err_mas=Ey[:, 0],
        xx=sol['Px'][:, 1], xy=sol['Px'][:, 2], yx=sol['Py'][:, 1], yy=sol['Py'][:, 2],
        lin_err=np.sqrt(0.25 * (Ex[:, 1] ** 2 + Ex[:, 2] ** 2 + Ey[:, 1] ** 2 + Ey[:, 2] ** 2)),
        star_dx_mas=star_res[:, 0], star_dy_mas=star_res[:, 1]))
    units.meta.update(field=field_name, ra0=ra0, dec0=dec0,
                      note='corrected = measured + (dx, dy) + [[xx, xy], [yx, yy]] . '
                           '(offset from (ra_center, dec_center) in arcmin, in the '
                           'tangent plane about (ra0, dec0)); mas, mas/arcmin')
    weak = [u for u, w in zip(units['unit'], units['weak']) if w]
    if weak:
        log(f"tie[{field_name}]: {len(weak)} unit(s) share fewer than "
            f"{int(cfg['min_shared'])} sources with other units and rest on the "
            f"catalog: {', '.join(weak[:8])}{' ...' if len(weak) > 8 else ''}")
    if len(p.s_unit):
        g = sol['ws']
        stats.update(star_residual_mas=float(_mad(np.r_[sol['sx'][g], sol['sy'][g]])),
                     star_median_mas=(float(np.median(sol['sx'][g])),
                                      float(np.median(sol['sy'][g]))))
        log(f"tie[{field_name}]: stars at epoch after the tie: median "
            f"({np.median(sol['sx'][g]):+.2f}, {np.median(sol['sy'][g]):+.2f}) mas, "
            f"scatter {stats['star_residual_mas']:.1f} mas per axis")
    ks = kept & shared[p.t_src]
    stats['tie_residual_mas'] = float(_mad(np.r_[sol['rx'][ks], sol['ry'][ks]]))
    log(f"tie[{field_name}]: residual of shared sources {stats['tie_residual_mas']:.2f} mas per axis")
    stats['n_weak'] = len(weak)

    # --- consensus catalog -------------------------------------------------------
    # every source with at least one kept tie; a source only one unit saw is its
    # corrected position in that unit.
    n_kept = np.bincount(p.t_src[kept], minlength=ns)
    ok = n_kept >= 1
    ra, dec = _deproject(mx[ok] + sol['Sx'][ok], my[ok] + sol['Sy'][ok], ra0, dec0)
    with np.errstate(invalid='ignore'):
        magsum = np.bincount(src, weights=np.nan_to_num(U['mag'][t]), minlength=ns)
        magn = np.bincount(src, weights=np.isfinite(U['mag'][t]).astype(float), minlength=ns)
        mag = np.where(magn > 0, magsum / np.maximum(magn, 1), np.nan)
    consensus = Table(dict(RA=ra, DEC=dec, mag=mag[ok].astype('float32'),
                           mag_err=np.full(int(ok.sum()), np.nan, 'float32'),
                           n_units=n_kept[ok].astype('int32')))
    stats['n_consensus'] = int(ok.sum())
    return TieResult(consensus=consensus, units=units, stats=stats)
