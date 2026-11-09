"""Checks of a diffraction model of the MSA (``nirspec.optics``) on the calibration stars, beside the
calibration itself (``nirspec.optics_calibration``):

* ``score``: the stack test per dataset (``--groups name=key prefix,...``), from a stack run;
* ``compare``: two stack runs of the same stars (two models), their chi2 per range;
* ``visits`` + ``repeat``: the same star at several positions, each visit fitted by the profile fit
  with its defaults (``PF.fit``; stage 3's shape chain picks among such fits). Is its flux the same? Across configurations in y (check 1 on NGC 2506); along a scan in x (1133's
  slit grid, 12497 obs 4), which also refits the along-dispersion correction (``x_loss``);
* ``barshadow``: the model's uniform light against the empirical bar-shadow tables.

    python pipeline/scripts/nirspec_optics_checks.py score --stack D --groups 1133=nrscal_slitgrid_1133,ngc=nrscal_ngc2506
    python pipeline/scripts/nirspec_optics_checks.py compare --stack D --against D2 [--groups ...]
    python pipeline/scripts/nirspec_optics_checks.py visits --root R --obs a,b --out v.json [--by-group] [--optics M]
    python pipeline/scripts/nirspec_optics_checks.py repeat --visits v.json [--along x] [--exclude ids]
    python pipeline/scripts/nirspec_optics_checks.py barshadow [--optics M]
"""
import json
import os

import numpy as np

from campfire_pipeline.nirspec import optics_calibration as OC
from campfire_pipeline.nirspec import profile_fit as PF

VISIT_BANDS = ((0.7, 1.0), (1.0, 1.5), (1.5, 2.5), (2.5, 3.5), (3.5, 5.0))   # PRISM; a grating: visit_bands
REPEAT_SNR = 20.0                  # a band enters when its S/N (flux over error) is at least this
REPEAT_SAT = 0.01                  # ... and at most this share of its trace pixels is saturated
REPEAT_EDGE = 0.25                 # |SRCYPOS| of an edge visit
X_BINS = (-0.5, -0.35, -0.2, -0.07, 0.07, 0.2, 0.35, 0.5)


# ---------------------------------------------------------------------------
# The stack test per dataset, and two models compared
# ---------------------------------------------------------------------------
def parse_groups(text):
    """'name=prefix,name=prefix' -> {name: prefix} (star keys start with the observation name)."""
    return dict(item.split('=', 1) for item in text.split(',') if item) if text else {}


def score_groups(stack_dir, groups, log=print, **cuts):
    """The stack test per dataset (``groups``: {name: key prefix}): per detector and range,
    each group's clean stars, its largest |median residual| and the offset [rows] where it
    falls; a model can pass on all stars while one dataset does not."""
    keys = OC.clean_keys(OC.read_records(stack_dir), **cuts)
    met = {g: OC.stack_metric(os.path.join(stack_dir, 'samples'),
                              {k: [s for s in v if s.startswith(p)] for k, v in keys.items()})
           for g, p in groups.items()}
    for det in ('NRS1', 'NRS2'):
        for r, (a, b) in enumerate(OC.RANGES):
            cells = []
            for g in groups:
                m = met[g].get((det, r))
                if m is None:
                    cells.append(f'{g} -')
                    continue
                c = np.array(m['curve'])
                i = int(np.nanargmax(np.abs(c)))
                cells.append(f'{g} {m["n"]:3d} {c[i]:+.2f} % at {m["centre"][i]:+.2f}')
            log(f'{det} {a:.1f}-{b:.1f} um: ' + '; '.join(cells))
    return met


def _chi2_rows(stack_dir, edge_max=OC.EDGE_MAX):
    out = {}
    for rec in OC.read_records(stack_dir):
        for rg in rec['ranges']:
            if rg.get('chi2') is None or not np.isfinite(rg['chi2']) or rg.get('sat', 0) > 0 \
                    or abs(rg.get('c_node', np.inf)) > edge_max:
                continue
            out[(rec['key'], rg['range'])] = (rec['detector'], rg['range'], rg['chi2'])
    return out


def compare(dir_a, dir_b, groups=None, log=print):
    """Two stack runs of the same stars (two models): per group of stars (``groups``, {name:
    key prefix}; all stars when None), detector and range, the summed chi2 over the star-ranges
    both fitted (|centre| <= EDGE_MAX, unsaturated) and the median of each star-range's ratio
    b / a. Returns {(group, det, range): (n, chi2 a, chi2 b, median ratio)}."""
    a, b = _chi2_rows(dir_a), _chi2_rows(dir_b)
    common = sorted(set(a) & set(b))
    out = {}
    for g, prefix in (groups or {'all': ''}).items():
        for det in ('NRS1', 'NRS2'):
            for r, (lo, hi) in enumerate(OC.RANGES):
                ks = [k for k in common if k[0].startswith(prefix) and a[k][0] == det and a[k][1] == r]
                if len(ks) < 3:
                    continue
                ca, cb = sum(a[k][2] for k in ks), sum(b[k][2] for k in ks)
                ratio = float(np.median([b[k][2] / a[k][2] for k in ks]))
                out[(g, det, r)] = (len(ks), ca, cb, ratio)
                log(f'{g} {det} {lo:.1f}-{hi:.1f} um: {len(ks):4d} star-ranges, chi2 {ca:10.0f} -> {cb:10.0f} '
                    f'({100 * (cb / ca - 1):+5.1f} %), median ratio {ratio:.3f}')
    return out


# ---------------------------------------------------------------------------
# The same star at several positions
# ---------------------------------------------------------------------------
def band_fluxes(lam, flux, err, bands, min_points=5):
    """Per band: (inverse-variance mean flux, its error, median per-point S/N), NaN with fewer
    than ``min_points`` good points."""
    lam, f, e = np.asarray(lam, float), np.asarray(flux, float), np.asarray(err, float)
    ok = np.isfinite(f) & np.isfinite(e) & (e > 0)
    out = []
    for a, b in bands:
        m = ok & (lam >= a) & (lam < b)
        if m.sum() < min_points:
            out.append((np.nan, np.nan, np.nan))
            continue
        w = 1.0 / e[m] ** 2
        out.append((float(np.sum(w * f[m]) / np.sum(w)), float(1.0 / np.sqrt(np.sum(w))), float(np.median(f[m] / e[m]))))
    return out


def visit_bands(disperser):
    """The bands a visit's flux is measured in: ``VISIT_BANDS`` for PRISM, else four equal
    steps in log wavelength over the grating's range. Fixed per disperser, so a band is the
    same wavelengths in every visit (a visit's own pixel percentiles move with its x position)."""
    if disperser == 'prism_clear':
        return VISIT_BANDS
    from campfire_pipeline.nirspec.constants import GRATING_LIMITS
    lo, hi = GRATING_LIMITS[disperser.split('_')[0].lower()][:2]
    e = np.geomspace(lo, hi, 5)
    return tuple((float(a), float(b)) for a, b in zip(e[:-1], e[1:]))


def fit_visit(task):
    """One visit of a star (``OC.StarTask``: one detector of one configuration, or one
    background group of a scan) through the profile fit with its defaults (``PF.fit``, the
    model in use: the shipped one or the file ``CAMPFIRE_NIRSPEC_OPTICS`` names; not stage 3's
    shape chain): band fluxes (``visit_bands``), the fitted centre and width, chi2/dof, the
    planned position (median SRCXPOS / SRCYPOS of its science exposures), the program and the
    saturated share of each band. Errors are returned, not raised."""
    head = dict(key=task.key, obs=task.obs, sid=task.source_id, det=task.detector, group=task.group,
                config=task.config, srcx=task.srcx, srcy=task.srcy)
    try:
        exps = OC.load_star(task)
        head['program'] = os.path.basename(exps[0].path)[2:7]       # a star is the same source id in one program
        bands = visit_bands(task.disperser)
        R = PF.fit(exps)
        flags = OC.saturated_flags(exps)
        return dict(head, bands=[list(b) for b in bands], flux=band_fluxes(R.lam, R.flux, R.flux_err, bands),
                    sat=[OC.saturated_fraction(exps, flags, R.params['centre'], b) for b in bands],
                    centre=float(R.params['centre']), width=float(R.params['width']), chi2r=float(R.chi2_red))
    except Exception as err:                       # noqa: BLE001 - one visit's failure is reported, the others go on
        return dict(head, error=f'{type(err).__name__}: {err}')


def visits(root, observations, out_path, disperser='prism_clear', by_group=False, optics_path=None,
           min_visits=2, processes=1, log=print):
    """Every visit of every star seen in ``min_visits`` or more (``fit_visit``), written to
    ``out_path`` (JSON: optics version and x_loss, rows). A visit is one detector of one
    observation; with ``by_group`` one background group (a scan inside one observation).
    ``optics_path``: a candidate model in place of the shipped one."""
    from multiprocessing import Pool
    from campfire_pipeline.nirspec import optics as O
    if optics_path:
        os.environ['CAMPFIRE_NIRSPEC_OPTICS'] = os.path.abspath(optics_path)    # the workers' PF.fit reads it
        O._MODELS.clear()
    tasks = OC.tasks_for(root, observations, (disperser,), by_group=by_group)
    seen = {}
    for t in tasks:
        seen.setdefault(t.source_id, set()).add((t.obs, t.group, t.detector))
    tasks = [t for t in tasks if len(seen[t.source_id]) >= min_visits]
    log(f'{len(tasks)} visits of {len({t.source_id for t in tasks})} stars')
    rows = []
    with Pool(max(1, processes)) as pool:
        for r in pool.imap_unordered(fit_visit, tasks):
            rows.append(r)
            log(f"{r['key']} {r.get('error', 'ok')}")
    with open(out_path, 'w') as fh:
        json.dump(dict(optics=O.version(), x_loss=O.models()['NRS1'].x_loss, disperser=disperser, rows=rows), fh,
                  indent=1)
    return rows


def repeat_pairs(rows, along='y', snr_min=REPEAT_SNR, sat_max=REPEAT_SAT, exclude=()):
    """Per band: each visit of a star against the same star's most central visit along
    ``along`` ('y': SRCYPOS, the slit direction; 'x': SRCXPOS, along the dispersion) on the
    same detector (bands over two detectors cover different wavelengths) and in the same program
    (a source id names one star only within its program's catalogue), among the visits where the
    band has S/N >= ``snr_min`` and at most ``sat_max`` saturated. Returns
    [[dict(lnr, pos, ref_pos, sid, lam, band)]] per band."""
    pos = 'srcy' if along == 'y' else 'srcx'
    by = {}
    for r in rows:
        if 'error' not in r and r['sid'] not in set(exclude) and np.isfinite(r[pos]):
            by.setdefault((r.get('program'), r['sid'], r['det']), []).append(r)
    nb = max((len(r['bands']) for r in rows if 'bands' in r), default=0)
    out = [[] for _ in range(nb)]
    for vs in by.values():
        for bi in range(nb):
            ok = [v for v in vs if len(v['flux']) > bi and np.isfinite(v['flux'][bi][0]) and v['flux'][bi][0] > 0
                  and v['flux'][bi][0] / v['flux'][bi][1] >= snr_min and v['sat'][bi] <= sat_max]
            if len(ok) < 2:
                continue
            ref = min(ok, key=lambda v: abs(v[pos]))
            out[bi] += [dict(lnr=float(np.log(v['flux'][bi][0] / ref['flux'][bi][0])), pos=v[pos], ref_pos=ref[pos],
                             sid=v['sid'], lam=float(np.sqrt(v['bands'][bi][0] * v['bands'][bi][1])), band=bi)
                        for v in ok if v is not ref]
    return out


def repeat(visits_path, along='y', exclude=(), snr_min=REPEAT_SNR, log=print):
    """The repeat test on a ``visits`` file: is a star's flux the same wherever it sits? Per
    band, ln(flux / the star's most central visit) (``repeat_pairs``).

    along 'y' (stars in several configurations, check 1): the rms over every visit and over the
    edge visits (|SRCYPOS| >= REPEAT_EDGE), and the edge visits' median.
    along 'x' (a scan along the dispersion): the clipped rms, the medians in bins of SRCXPOS, the
    slope against x^2 - x_ref^2 (the model predicts none), and b of the along-dispersion
    correction refitted (``OC.fit_x_loss``) on the fluxes with the file's correction undone."""
    with open(visits_path) as fh:
        d = json.load(fh)
    rows = d['rows']
    bad = [r for r in rows if 'error' in r]
    log(f'{len(rows) - len(bad)} visits fitted, {len(bad)} failed (optics {d.get("optics")})')
    pairs = repeat_pairs(rows, along=along, snr_min=snr_min, exclude=exclude)
    bands = next(r['bands'] for r in rows if 'bands' in r)
    out = []
    for bi, ps in enumerate(pairs):
        a, b = bands[bi]
        lnr = np.array([p['lnr'] for p in ps])
        if lnr.size < 5:
            log(f'{a}-{b} um: too few pairs ({lnr.size})')
            out.append(None)
            continue
        x = np.array([p['pos'] for p in ps])
        res = dict(band=(a, b), n=int(lnr.size))
        if along == 'y':
            edge = np.abs(x) >= REPEAT_EDGE
            res.update(rms=float(np.sqrt(np.mean(lnr ** 2))),
                       rms_edge=float(np.sqrt(np.mean(lnr[edge] ** 2))) if edge.any() else np.nan,
                       median_edge=float(np.median(lnr[edge])) if edge.any() else np.nan, n_edge=int(edge.sum()))
            log(f'{a}-{b} um: {lnr.size} pairs, rms {100 * res["rms"]:.1f} %; edge visits ({res["n_edge"]}) rms '
                f'{100 * res["rms_edge"]:.1f} %, median {100 * res["median_edge"]:+.1f} %')
        else:
            med = np.median(lnr)
            mad = 1.4826 * np.median(np.abs(lnr - med))
            keep = np.abs(lnr - med) < 4 * max(mad, 1e-3)
            g = x ** 2 - np.array([p['ref_pos'] for p in ps]) ** 2
            slope = float(np.sum(lnr[keep] * g[keep]) / np.sum(g[keep] ** 2))
            se = float(np.std(lnr[keep] - slope * g[keep]) / np.sqrt(np.sum(g[keep] ** 2)))
            binned = [(0.5 * (lo + hi), float(np.median(lnr[keep & (x >= lo) & (x < hi)]))
                       if np.sum(keep & (x >= lo) & (x < hi)) >= 3 else np.nan) for lo, hi in zip(X_BINS[:-1], X_BINS[1:])]
            res.update(rms=float(np.std(lnr[keep])), slope=slope, slope_err=se, binned=binned)
            log(f'{a}-{b} um: {lnr.size} pairs ({keep.sum()} kept), clipped rms {100 * res["rms"]:.1f} %, slope against '
                f'x^2 - x_ref^2 {slope:+.2f} +- {se:.2f}; median % by x: '
                + ', '.join(f'{c:+.2f} {100 * m:+.1f}' for c, m in binned))
        out.append(res)
    if along == 'x':
        xl = d.get('x_loss') or {}
        b0, lam0 = float(xl.get('b', 0.0)), float(xl.get('lam0', OC.X_LAM0))
        # the fit divided the flux by the correction c = exp(-b0 (lam - lam0)+ x^2); times c again is jwst's alone
        raw = [dict(p, xv=p['pos'], xr=p['ref_pos'],
                    lnr=p['lnr'] - b0 * max(p['lam'] - lam0, 0.0) * (p['pos'] ** 2 - p['ref_pos'] ** 2))
               for ps in pairs for p in ps]
        if any(max(p['lam'] - lam0, 0.0) > 0 for p in raw):
            b = OC.fit_x_loss(raw, lam0=lam0)
            log(f'along-dispersion correction refitted on these pairs: b = {b:.3f} (the file used {b0:.3f})')
            out.append(dict(b=b, b_used=b0))
    return out


# ---------------------------------------------------------------------------
# The bar shadow
# ---------------------------------------------------------------------------
def barshadow_misfit(grating, optics=None, n_lam=10, y_max=0.95):
    """The model's uniform light against the empirical bar-shadow table of ``grating``
    (``barshadow_calibration``; the light of every source position across the open shutters,
    equal weights, normalised at the centre, against the table for one open shutter and for a
    slitlet of three): the rms over |y| < ``y_max`` pitch of both, at ``n_lam`` of the table's
    wavelengths. The kernel only moves light between source positions, so for uniform light it
    cancels: this tests the diffraction numbers (``raw_image_table``), the same for both
    detectors. ``optics``: an ``Optics`` (default the shipped NRS1 one). Returns (lams, rms)."""
    from campfire_pipeline.config import get_empirical_barshadow_path
    from campfire_pipeline.nirspec import barshadow_calibration as B
    from campfire_pipeline.nirspec import optics as O
    tab = B.load_table(get_empirical_barshadow_path(grating))
    g = tab['grid']
    y_t = g.y0 + g.dy * np.arange(g.n_y)                    # pitches
    lam_t = (g.w0 + g.dw * np.arange(g.n_w)) * 1e6
    good = [j for j in range(g.n_w) if np.isfinite(tab['data1x1'][:, j]).all() and np.isfinite(tab['data1x3'][:, j]).all()]
    cols = good[::max(1, len(good) // n_lam)]
    sel = np.abs(y_t) < y_max
    yy = y_t[sel] * O.PITCH_AS / O.SLIT                     # slit units
    opt = optics or O.optics_for('NRS1')
    uniform = {mask: np.asarray(opt.raw_image_table(mask), float).sum(axis=1) for mask in ((0,), (-1, 0, 1))}
    rms = []
    for j in cols:
        li = np.interp(np.log(lam_t[j]), np.log(O.LAM), np.arange(O.LAM.size))
        i0, t = int(np.floor(li)), li - np.floor(li)
        res = []
        for data, mask in ((tab['data1x1'], (0,)), (tab['data1x3'], (-1, 0, 1))):
            U = uniform[mask]
            prof = (1 - t) * U[i0] + t * U[min(i0 + 1, O.LAM.size - 1)]
            prof = prof / np.interp(0.0, O.Y, prof)
            res.append(np.interp(yy, O.Y, prof) - data[sel, j])
        rms.append(float(np.sqrt(np.mean(np.concatenate(res) ** 2))))
    return lam_t[cols].tolist(), rms


def barshadow(gratings=('prism', 'g395m'), optics_path=None, log=print):
    """``barshadow_misfit`` for each grating's empirical table, printed."""
    from campfire_pipeline.nirspec import optics as O
    opt = O.load_optics(optics_path)['NRS1'] if optics_path else None
    out = {}
    for gr in gratings:
        out[gr] = lams, rms = barshadow_misfit(gr, optics=opt)
        log(f'{gr}: median rms {np.median(rms):.4f}; at ' + ', '.join(f'{L:.2f} um {v:.4f}' for L, v in zip(lams, rms)))
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('check', choices=('score', 'compare', 'visits', 'repeat', 'barshadow'))
    ap.add_argument('--stack', help='score, compare: a stack run (optics_calibration stack)')
    ap.add_argument('--against', help='compare: the second stack run')
    ap.add_argument('--groups', default='', help='score, compare: datasets apart, name=key prefix,...')
    ap.add_argument('--root', help='visits: CAMPFIRE root of the reduced calibration observations')
    ap.add_argument('--obs', help='visits: comma-separated observations')
    ap.add_argument('--disperser', default='prism_clear', help='visits')
    ap.add_argument('--by-group', action='store_true', help='visits: one visit per background group (a scan)')
    ap.add_argument('--out', help='visits: the JSON written')
    ap.add_argument('--visits', help='repeat: a visits JSON')
    ap.add_argument('--along', choices=('y', 'x'), default='y', help='repeat: the position the visits differ in')
    ap.add_argument('--exclude', default='', help='repeat: comma-separated source ids left out (not the same object)')
    ap.add_argument('--gratings', default='prism,g395m', help='barshadow: the empirical tables compared')
    ap.add_argument('--optics', default=None, help='visits, barshadow: a model file in place of the shipped one')
    ap.add_argument('--processes', type=int, default=1)
    a = ap.parse_args(argv)
    groups = parse_groups(a.groups)
    if a.check == 'score':
        OC.step_score(a.stack)
        if groups:
            score_groups(a.stack, groups)
    elif a.check == 'compare':
        compare(a.stack, a.against, groups or None)
    elif a.check == 'visits':
        visits(a.root, a.obs.split(','), a.out, disperser=a.disperser, by_group=a.by_group, optics_path=a.optics,
               processes=a.processes)
    elif a.check == 'repeat':
        repeat(a.visits, along=a.along, exclude=[int(s) for s in a.exclude.split(',') if s])
    else:
        barshadow(tuple(a.gratings.split(',')), optics_path=a.optics)


if __name__ == '__main__':
    main()
