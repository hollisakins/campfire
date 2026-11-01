"""
Master background, step 1: the count-rate sky and the stuck-shutter calls, from the rate
files alone, before stage 2a.

Per exposure group (one root: obs / visit / config), over every rate file:

1. **Geometry.** One ``assign_wcs`` per rate file on its original metafile gives every
   commanded slitlet's footprint (``coverage.rate_geometry``, cached beside the coverage
   counter, which comes from the same pass).
2. **Rate-unit sky.** The empty shutters of every slitlet (no catalogued source at this
   dither point, ``source_pad_px`` rows beyond a source shutter left out, the shutter core
   ``|f| <= core_frac``, pixels under one footprint only, not a known stuck shutter) are
   divided by the bar shadow there (the sky at the open-shutter centre,
   ``master_bkg.core_to_centre``) and binned in count rate on the disperser's grid
   continued to its wavelength cutoffs;
   ``master_bkg.select_clean`` rejects lines, excess and low throughput with the
   ``rate_*`` thresholds (no flat field in count rate: shutters scatter by ~7 % grey);
   the clean ones give the sky (``MBKG1DR``): the same detector's pool over the group
   unless this exposure's shutters differ from the other exposures' (``master_bkg.pool_check``;
   ``MBKGPSRC``, ``MBKGPDEC`` ...; the exposure's own sky kept as ``rate_exposure``), with
   the other detector and the nearest bin as fallbacks (``master_bkg.rate_sky_for``), and
   the quadrant scales in count rate (``MBKGQSR``).
3. **Stuck shutters.** ``shutter_fit.fit_shutters`` on every rate file with that sky, and
   ``call_shutters`` over the root; the stuck calls leave the clean set and the sky is
   combined again; the calls are merged into the observation's stuck list as ``auto``
   entries (``stuck_list.write_auto``) before stage 2a builds its canonicals.

Writes ``<rate base>_mbkgr.fits`` beside each rate file: ``MBKG1DR``, ``MBKGQSR``,
``MBKGSHR`` (every shutter offered, with its selection), ``SHUTAMP`` (the fit) and
``CALLS`` (the root's calls, the same in every file of the root). Step 2
(``master_bkg.run_master_bkg``, end of stage 2a) reads ``MBKG1DR`` and ``MBKGQSR`` from
it. Design: docs/design-nirspec-master-background.md (status block) and
docs/design-nirspec-pipeline-v2.md.
"""

import hashlib
import json
import os
import warnings
from datetime import datetime, timezone

import numpy as np
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.nirspec import master_bkg as M
from campfire_pipeline.nirspec import stuck_list as S
from campfire_pipeline.nirspec.coverage import (cache_path, cutoffs_for, default_cache_dir,
                                                 rate_geometry)

STEP1_VERSION = '1.2'          # 1.1: equal shutter weights, sky at the open-shutter centre; 1.2: pool unless different
N_GRID_SLITLETS = 6          # slitlets per detector whose central rows set the wavelength grid


def product_path(obs, rate_base):
    return os.path.join(obs.workspace_dir, f'{rate_base}_mbkgr.fits')


def _workspace_product(workspace, rate_base):
    return os.path.join(workspace, f'{rate_base}_mbkgr.fits')


# ---------------------------------------------------------------------------
# Readers (step 2, the nods plots)
# ---------------------------------------------------------------------------

def read_rate_sky(path):
    """``(MBKG1DR table, {quadrant: scale})`` of a step-1 product."""
    with fits.open(path, memmap=False) as h:
        sky = Table(h['MBKG1DR'].data)
        q = {int(r['quadrant']): float(r['scale']) for r in h['MBKGQSR'].data} if 'MBKGQSR' in h else {}
    return sky, q


def read_calls(workspace, root):
    """The root's stuck-shutter calls, ``{(q, row, col): {stuck, a, n, note}}``, from
    the first of its step-1 products; ``{}`` when there is none."""
    import glob
    for path in sorted(glob.glob(os.path.join(workspace, f'{root}_*_mbkgr.fits'))):
        try:
            with fits.open(path, memmap=False) as h:
                if 'CALLS' not in h:
                    continue
                return {(int(r['q']), int(r['row']), int(r['col'])):
                        dict(stuck=bool(r['stuck']), a=float(r['a']), n=int(r['n']), note=str(r['note']).strip())
                        for r in h['CALLS'].data}
        except OSError:
            continue
    return {}


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

def _central_rows(geo, n_sample=N_GRID_SLITLETS):
    """Wavelength along the source-shutter centre row of the first ``n_sample`` slitlets
    (one pixel per column, the one nearest ``ys = 0``): what the grid follows."""
    rows = []
    for b in geo.blocks(kind='msa', with_segments=False):
        near = np.abs(b['ys']) < 0.25
        if near.sum() < 20:
            continue
        col = b['pix'][near] % geo.shape[1]
        lam, ays = b['lam'][near], np.abs(b['ys'][near])
        order = np.lexsort((ays, col))
        first = np.r_[True, np.diff(col[order]) != 0]
        rows.append(lam[order][first])
        if len(rows) >= n_sample:
            break
    return rows


def _worker_geometry(args):
    rate, cache_dir, cutoffs, wr = args
    try:
        geo = rate_geometry(rate, cache_dir, cutoffs, wr)
    except Exception as e:                                   # noqa: BLE001 - reported, the file is skipped
        return rate, None, f'{type(e).__name__}: {e}'
    return rate, _central_rows(geo), None


def _px_per_pitch(b, ncols):
    """Detector rows per shutter pitch of one slitlet, from ``ys`` along its columns."""
    col = b['pix'] % ncols
    row = b['pix'] // ncols
    c = np.bincount(col).argmax()
    m = col == c
    if m.sum() < 5:
        return 5.3
    slope = np.polyfit(row[m], b['ys'][m], 1)[0]
    return 1.0 / abs(slope) if slope else 5.3


def _shutter_rows(rate):
    from campfire_pipeline.nirspec.shutter_fit import _shutter_info
    info, _ = _shutter_info(rate)
    return info


def _worker_samples(args):
    """Every empty shutter's count-rate spectra of one rate file, with its physical
    shutter: ``(rate, [(judged, sky)], {(slitlet_id, k): (q, row, col)}, error)``: the
    spectrum of the core less the source pad (judged by the selection) and of the whole
    core (combined into the sky once judged clean), as the calibrated pass does."""
    rate, cache_dir, cutoffs, wr, edges, stuck, opt, bs_path = args
    from campfire_pipeline.nirspec import barshadow_calibration as B
    try:
        geo = rate_geometry(rate, cache_dir, cutoffs, wr)
        table = B.load_table(bs_path)
        info = _shutter_rows(rate)
        counter = cache_path(rate, cache_dir, cutoffs, wr)
        frame, rvar, rate_ok, _ = M._rate_frame(rate, counter)
    except Exception as e:                                   # noqa: BLE001
        return rate, [], {}, f'{type(e).__name__}: {e}'
    flat, fvar, ok = frame.ravel(), rvar.ravel(), rate_ok.ravel()
    base = M.rate_base_of(rate)
    specs, keys = [], {}
    for b in geo.blocks(kind='msa', with_segments=True):
        if not b['name'].isdigit():
            continue
        sid_slit = int(b['name'])
        mr = info[info['slitlet_id'] == sid_slit]
        col_of_j, src_j, row = {}, set(), None
        if len(mr):
            c = np.asarray(mr['shutter_column'], int); c0 = int(c.min())
            if len(b['state']) == int(c.max()) - c0 + 1:
                col_of_j = {jj: c0 + jj for jj in range(len(b['state']))}
            for r in mr:
                # every shutter of a slitlet carries its source's id; the ones holding a
                # source at this dither point are background = 'N'
                if str(r['background']).strip() == 'N':
                    src_j.add(int(r['shutter_column']) - c0)
            row = int(mr['shutter_row'][0])
        core = b['is_open'] & (np.abs(b['f']) <= opt['core_frac']) & ok[b['pix']]
        # each core pixel divided by the bar shadow there: the sky at the open-shutter centre,
        # the level the table is normalised to and the painting multiplies by it
        bs = np.full(core.shape, np.nan)
        if core.any():
            bs[core] = M.core_to_centre(table, b['seg'][core], b['yseg'][core], b['lam'][core])
        core &= np.isfinite(bs)
        judged = core.copy()            # the pixels the selection judges: the core less a source pad
        if src_j:
            pad = float(opt['source_pad_px']) / _px_per_pitch(b, geo.shape[1])
            near = np.zeros(core.shape, bool)
            for jj in src_j:
                lo, hi = jj - b['kx'] - 0.5, jj - b['kx'] + 0.5          # the source shutter in ys
                near |= (b['ys'] > lo - pad) & (b['ys'] < hi + pad)
            judged &= ~near
            core &= ~np.isin(b['j'], list(src_j))                       # the source shutters themselves
        source_id = int(b['source_id']) if int(b['source_id']) > 0 else -sid_slit
        for jj in np.unique(b['j'][core]):
            jj = int(jj)
            key = (int(b['quadrant']), row, col_of_j[jj]) if (row is not None and jj in col_of_j) else None
            if key is not None and key in stuck:
                continue
            # as the calibrated pass: a shutter is offered (and judged) on its padded pixels,
            # and a clean one gives the sky over its whole core
            sel_j = judged & (b['j'] == jj)
            if sel_j.sum() < opt['min_pixels']:
                continue
            spec = {}
            for tag, sel in (('judged', sel_j), ('sky', core & (b['j'] == jj))):
                pix = b['pix'][sel]
                sb, err, n = M.bin_spectrum(b['lam'][sel], flat[pix] / bs[sel], fvar[pix] / bs[sel] ** 2, edges)
                spec[tag] = M.ShutterSpectrum(rate_base=base, source_id=source_id, slitlet_id=sid_slit,
                                              quadrant=int(b['quadrant']), k=jj - b['kx'], n_pix=int(sel.sum()),
                                              sb=sb, err=err, n=n)
            if np.isfinite(spec['judged'].sb).sum() < opt['min_bins']:
                continue
            specs.append((spec['judged'], spec['sky']))
            keys[(sid_slit, jj - b['kx'])] = key
    return rate, specs, keys, None


def _worker_fit(args):
    rate, cache_dir, cutoffs, wr, sky, qscales, bs_path = args
    from campfire_pipeline.nirspec import barshadow_calibration as B
    from campfire_pipeline.nirspec.shutter_fit import fit_shutters
    try:
        geo = rate_geometry(rate, cache_dir, cutoffs, wr)
        t = fit_shutters(rate, geo, sky, qscales, B.load_table(bs_path))
    except Exception as e:                                   # noqa: BLE001
        return rate, None, f'{type(e).__name__}: {e}'
    return rate, t, None


# ---------------------------------------------------------------------------
# The sky of a group
# ---------------------------------------------------------------------------

def _rate_options(opt):
    o = dict(opt)
    o['excess_min'] = opt['rate_excess_min']
    o['low_min'] = opt['rate_low_min']
    return o


def _select(pairs, opt):
    """Judge one rate file's shutters on their padded pixels (count-rate thresholds) and
    return their whole-core spectra carrying the verdict: what the sky combines."""
    if not pairs:
        return []
    judged = [p[0] for p in pairs]
    M.select_clean(judged, M.median_seed(judged), _rate_options(opt))
    out = []
    for j, sky in pairs:
        sky.clean, sky.reason = j.clean, j.reason
        sky.scale, sky.scale_err, sky.chi2_red, sky.line_run = j.scale, j.scale_err, j.chi2_red, j.line_run
        out.append(sky)
    return out


def _skies(clean_of, centres, opt):
    """Per rate file of a group: ``dict(rs, qs, own, src, check, members)`` from the clean
    shutters ``clean_of[base]``. ``rs`` is the sky painted: the pool of the same detector's
    exposures that are consistent with each other (``M.pool_members``) for a member, else
    the exposure's own; ``own`` is the exposure's own sky either way; ``qs`` the quadrant
    scales against ``rs``."""
    members, checks = set(), {}
    for det in sorted({b.rsplit('_', 2)[2] for b in clean_of}):
        of_det = {b: sp for b, sp in clean_of.items() if b.endswith(det)}
        if opt['prefer_pool']:
            m, c = M.pool_members(of_det, centres[next(iter(of_det))], opt)
            members |= m; checks.update(c)
    out = {}
    for base in clean_of:
        det = base.rsplit('_', 2)[2]
        own = M.rate_sky_for(base, clean_of, opt['clip_sigma'])
        check = checks.get(base, M.pool_check([], None, centres[base], opt))
        mem = sorted(b for b in members if b.endswith(det))
        if own is None:
            out[base] = dict(rs=None, qs={}, own=None, src='none', check=check, members=mem)
            continue
        src = 'pool' if (base in members and len(mem) > 1) else 'exposure'
        if src == 'pool':
            # the same detector's pool over the members; the other detector stays a fallback
            pool_of = {b: sp for b, sp in clean_of.items() if b in members or not b.endswith(det)}
            rs = M.rate_sky_for(base, pool_of, opt['clip_sigma'], pool_first=True)
        else:
            rs = own
        qs = M.quadrant_scales(clean_of[base], (rs[0], rs[1]), opt) if clean_of[base] else {}
        out[base] = dict(rs=rs, qs=qs, own=own, src=src, check=check, members=mem)
    return out


def _sky_table(centres, rs, own=None):
    t = Table()
    t['wavelength'] = np.asarray(centres, np.float64); t['wavelength'].unit = 'um'
    t['rate'] = np.asarray(rs[0], np.float32); t['rate'].unit = 'count/s'
    t['rate_err'] = np.asarray(rs[1], np.float32); t['rate_err'].unit = 'count/s'
    t['n_shutters'] = np.asarray(rs[2], np.int32)
    t['source'] = np.asarray(rs[3], np.int16)
    if own is not None:                  # the exposure's own sky, whichever MBKGPSRC chose
        t['rate_exposure'] = np.asarray(own[0], np.float32); t['rate_exposure'].unit = 'count/s'
        t['rate_exposure_err'] = np.asarray(own[1], np.float32); t['rate_exposure_err'].unit = 'count/s'
    return t


# ---------------------------------------------------------------------------
# Skip key
# ---------------------------------------------------------------------------

def _decided_hash(resolved, root):
    """Hash of the root's manual / web entries: what step 1 takes as given."""
    ents = sorted((S.shutter_key(*k), e.origin, e.stuck) for k, e in resolved.get(root, {}).items() if e.origin != 'auto')
    return hashlib.sha1(json.dumps(ents).encode()).hexdigest()[:12]


def _inputs_cards(rate, bs_path, cutoffs, decided):
    h = fits.getheader(rate)
    return {
        'CFMB1VER': (STEP1_VERSION, 'master background step 1 version'),
        'MBKGRATE': (os.path.basename(rate), 'rate file this product belongs to'),
        'MBKGBKDT': (str(h.get('CFBKGDT')), 'rate CFBKGDT at derivation'),
        'MBKGMETA': (os.path.basename(h.get('OGMETFL') or h.get('MSAMETFL') or ''), 'MSA metafile'),
        'MBKGMTID': (int(h.get('MSAMETID', -1)), 'MSA metadata id'),
        'MBKGBSRF': (os.path.basename(bs_path), 'bar-shadow table of the templates'),
        'MBKGBSDT': (M.barshadow_stamp(bs_path), 'that table: derivation date / USEAFTER'),
        'MBKGCUT': (json.dumps(list(cutoffs)), 'footprint wavelength cutoffs [um]'),
        'MBKGDEC': (decided, 'hash of the manual/web stuck entries of the root'),
    }


def product_current(path, cards):
    if not os.path.exists(path):
        return False
    try:
        hdr = fits.getheader(path)
    except OSError:
        return False
    return all(str(hdr.get(k)) == str(v[0]) for k, v in cards.items())


# ---------------------------------------------------------------------------
# The step
# ---------------------------------------------------------------------------

def run_before_stage2a(obs, stage_config, n_processes=1, overwrite=False):
    """Step 1 where stage 2a needs it, then what changed: ``(source_ids, roots)`` whose
    canonicals (sources) and background-slitlet files (roots) hold a shutter whose stuck
    state the step changed, so stage 2a re-runs them."""
    _ = obs.stuck_closed_shutters                # creates the blank file when absent
    metafiles = S.root_metafiles(obs)
    before = S.load(obs.stuck_closed_shutters_file, metafiles)
    run_step1(obs, stage_config, n_processes=n_processes, overwrite=overwrite,
              write_calls=bool(stage_config.get('detect_stuck_shutters', True)))
    after = S.load(obs.stuck_closed_shutters_file, metafiles)
    sids, roots = set(), set()
    for root, mf in metafiles.items():
        changed = S.stuck_set(before, root) ^ S.stuck_set(after, root)
        if not changed:
            continue
        roots.add(root)
        for sid in mf.unique_source_ids:
            try:
                rows = mf.rows_for_source(int(sid))
            except RuntimeError:
                continue
            if any(k in changed for k in S.row_keys(rows)):
                sids.add(int(sid))
        log(f'master background step 1: {root}: {len(changed)} shutter(s) changed stuck state; '
            f'{len(sids)} source(s) to re-run in stage 2a')
    return sids, roots


def run_step1(obs, stage_config, n_processes=1, overwrite=False, write_calls=True):
    """Step 1 for every MSA rate file of ``obs`` whose product is stale; returns the
    products written. With ``write_calls`` the stuck calls are merged into the
    observation's stuck list as ``auto`` entries (they are stored in ``CALLS`` either
    way)."""
    from multiprocessing import Pool
    from campfire_pipeline.nirspec.shutter_fit import call_shutters

    opt = M.options(stage_config)
    cache_dir = default_cache_dir()
    extended_refs = {}
    if stage_config.get('extend_g140m_g235m'):
        from campfire_pipeline.nirspec.extended_wavelength import build_extended_reference_files
        extended_refs = build_extended_reference_files(obs.rate_files) or {}

    stuck_path = obs.stuck_closed_shutters_file
    _ = obs.stuck_closed_shutters                # creates the blank file when absent
    by_root = {}
    for rate in sorted(obs.rate_files):
        h = fits.getheader(rate)
        if str(h.get('EXP_TYPE', '')).upper() != 'NRS_MSASPEC' or not (h.get('OGMETFL') or h.get('MSAMETFL')):
            continue
        base = M.rate_base_of(rate)
        key = (str(h['GRATING']).strip().upper(), str(h['FILTER']).strip().upper())
        ext = extended_refs.get((str(h.get('DETECTOR', '')).strip(), key[0], key[1]))
        by_root.setdefault(S.root_of(base), []).append(dict(rate=rate, base=base, key=key, cutoffs=cutoffs_for(*key),
                                                            wr=ext.get('wavelengthrange') if ext else None))
    if not by_root:
        return []
    metafiles = S.root_metafiles(obs, roots=set(by_root))
    resolved = S.load(stuck_path, metafiles)

    todo = {}
    for root, items in by_root.items():
        decided = _decided_hash(resolved, root)
        for it in items:
            _, bs_path = M._barshadow_reference(it['key'][0], it['rate'])
            it['bs_path'] = bs_path
            it['cards'] = _inputs_cards(it['rate'], bs_path, it['cutoffs'], decided)
        stale = overwrite or not all(product_current(product_path(obs, it['base']), it['cards']) for it in items)
        if stale:
            todo[root] = items
        else:
            log(f'master background step 1: {root}: current, skipped')
    if not todo:
        return []
    items_all = [it for items in todo.values() for it in items]
    log(f'master background step 1 for {obs.name}: {len(todo)} root(s), {len(items_all)} rate file(s)')
    pool_n = max(1, min(n_processes, len(items_all)))

    def _map(fn, args):
        if pool_n > 1:
            with Pool(pool_n) as pool:
                return list(pool.imap(fn, args))
        return [fn(a) for a in args]

    # 1. geometry (and the coverage counter), and the central rows for the grid
    rows_of, failed = {}, set()
    for rate, rows, err in _map(_worker_geometry, [(it['rate'], cache_dir, it['cutoffs'], it['wr']) for it in items_all]):
        if err:
            log(f'master background step 1: {M.rate_base_of(rate)}: no geometry ({err})'); failed.add(rate)
        else:
            rows_of[rate] = rows
    edges_of = {}
    for key in sorted({it['key'] for it in items_all}):
        rows = []
        for det in ('nrs1', 'nrs2'):
            first = [it for it in items_all if it['key'] == key and it['base'].endswith(det) and it['rate'] in rows_of]
            if first:
                rows += rows_of[first[0]['rate']]
        if not rows:
            continue
        grid = M.edges_from_rows(rows, opt['bin_pixels'])
        c = 0.5 * (grid[1:] + grid[:-1])
        blue, red = cutoffs_for(*key)
        edges_of[key] = M.rate_sky_edges(c, blue if blue is not None else c[0], red if red is not None else 1.5 * c[-1])

    # 2. the empty shutters in count rate
    sample_args = [(it['rate'], cache_dir, it['cutoffs'], it['wr'], edges_of[it['key']],
                    S.stuck_set({r: {k: e for k, e in resolved.get(r, {}).items() if e.origin != 'auto'}}, r), opt,
                    it['bs_path'])
                   for r, items in todo.items() for it in items if it['rate'] not in failed and it['key'] in edges_of]
    specs_of, keys_of = {}, {}
    for rate, specs, keys, err in _map(_worker_samples, sample_args):
        if err:
            log(f'master background step 1: {M.rate_base_of(rate)}: no samples ({err})'); failed.add(rate)
            continue
        specs_of[M.rate_base_of(rate)] = _select(specs, opt); keys_of[M.rate_base_of(rate)] = keys

    written = []
    for root, items in todo.items():
        items = [it for it in items if it['base'] in specs_of]
        if not items:
            continue
        centres = {it['base']: 0.5 * (edges_of[it['key']][1:] + edges_of[it['key']][:-1]) for it in items}
        clean_of = {it['base']: [s for s in specs_of[it['base']] if s.clean] for it in items}
        skies = _skies(clean_of, centres, opt)

        # 3. the fit, the calls, then the sky without the stuck shutters
        fit_args = [(it['rate'], cache_dir, it['cutoffs'], it['wr'], _sky_table(centres[it['base']], skies[it['base']]['rs']),
                     {q: v[0] for q, v in skies[it['base']]['qs'].items()}, it['bs_path'])
                    for it in items if skies[it['base']]['rs'] is not None]
        fits_of = {}
        for rate, t, err in _map(_worker_fit, fit_args):
            if err:
                log(f'master background step 1: {M.rate_base_of(rate)}: fit failed ({err})')
            else:
                fits_of[M.rate_base_of(rate)] = t
        calls = call_shutters(list(fits_of.values()), a_cut=opt['stuck_a_cut'], min_n=opt['stuck_min_n'],
                              guard=opt['stuck_guard'])
        dark = {k for k, c in calls.items() if c['stuck']} | S.stuck_set(resolved, root)
        for it in items:
            for s in specs_of[it['base']]:
                if keys_of[it['base']].get((s.slitlet_id, s.k)) in dark and s.clean:
                    s.clean, s.reason = False, 'stuck'
        clean_of = {it['base']: [s for s in specs_of[it['base']] if s.clean] for it in items}
        skies = _skies(clean_of, centres, opt)
        n_stuck = sum(c['stuck'] for c in calls.values())
        log(f'master background step 1: {root}: {len(fits_of)} rate file(s) fitted, {len(calls)} shutters called, '
            f'{n_stuck} stuck')

        # 4. the products, then the stuck list
        calls_t = Table(rows=[(k[0], k[1], k[2], c['stuck'], c['a'], c['n'], c['note']) for k, c in sorted(calls.items())],
                        names=('q', 'row', 'col', 'stuck', 'a', 'n', 'note'),
                        dtype=(np.int16, np.int16, np.int16, bool, np.float32, np.int16, 'U48')) if calls else None
        for it in items:
            sk = skies[it['base']]
            rs, qs = sk['rs'], sk['qs']
            if rs is None:
                log(f'master background step 1: {it["base"]}: no sky'); continue
            out = product_path(obs, it['base'])
            _write(out, it, centres[it['base']], rs, qs, specs_of[it['base']], keys_of[it['base']],
                   fits_of.get(it['base']), calls_t, opt, sky=sk)
            written.append(out)
        if calls and write_calls:
            S.write_auto(stuck_path, root, calls, metafiles, obs_name=obs.name)
    return written


def _write(path, it, centres, rs, qs, specs, keys, fit, calls_t, opt, sky=None):
    srcs = np.bincount(np.asarray(rs[3], int), minlength=5)
    cards = dict(it['cards'])
    cards.update({
        'MBKGDATE': (datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'), 'derivation date (UTC)'),
        'MBKGRSRC': (f'own {srcs[1]} pool {srcs[2]} other {srcs[3]} held {srcs[4]}', 'MBKG1DR bins by source'),
        'MBKGNSH': (sum(s.clean for s in specs), 'clean shutters in this exposure x detector'),
        'MBKGNTOT': (len(specs), 'empty shutters offered'),
        'MBKGXSMN': (float(opt['rate_excess_min']), 'rate-unit excess threshold'),
        'MBKGLWMN': (float(opt['rate_low_min']), 'rate-unit low-throughput threshold'),
        'MBKGACUT': (float(opt['stuck_a_cut']), 'stuck call: median amplitude below'),
        'MBKGMINN': (int(opt['stuck_min_n']), 'stuck call: measurements needed'),
        'MBKGGARD': (float(opt['stuck_guard']), 'stuck call: detector agreement'),
    })
    if sky is not None:
        chk = sky['check']
        cards.update({
            'MBKGPSRC': (sky['src'], 'MBKG1DR: pool (consistent), exposure (differs)'),
            'MBKGPDEC': (chk['verdict'], 'vs the pool of the other members, worst band'),
            'MBKGPMEM': (' '.join(str(int(b.rsplit('_', 2)[1])) for b in sky['members']) or 'none',
                         'pool members (nods, this detector)'),
            'MBKGPDEV': (chk['dev'], 'that band: mean shutter / pool - 1'),
            'MBKGPSIG': (chk['nsig'], 'that band: significance [sigma]'),
            'MBKGPLO': (chk['lam_lo'], 'that band: from [um]'),
            'MBKGPHI': (chk['lam_hi'], 'that band: to [um]'),
            'MBKGPNB': (chk['n_bands'], 'bands tested'),
            'MBKGPTOL': (float(opt['pool_tol']), 'a band differs above this fraction'),
            'MBKGPNSG': (float(opt['pool_nsigma']), '... and above this many sigma'),
        })
    if fit is not None:
        for k in ('chi2_sky', 'chi2_all', 'const_sky', 'const_all'):
            v = fit.meta.get(k)
            if v is not None and np.isfinite(v):
                cards[f'FIT{k.upper().replace("_", "")[:5]}'] = (float(v), f'shutter fit {k}')
    hdr = fits.Header()
    for k, v in M._finite_cards(cards).items():
        hdr[k] = v
    hdr['MBKGSRC1'] = ('1 own shutters, 2 group pool same detector, 3 other detector scaled, 4 held', 'MBKG1DR source column')
    hdus = [fits.PrimaryHDU(header=hdr)]
    hdus.append(fits.BinTableHDU(_sky_table(centres, rs, own=sky['own'] if sky else None), name='MBKG1DR'))
    qt = Table(rows=[(q, v[0], v[1]) for q, v in sorted(qs.items())], names=('quadrant', 'scale', 'n_shutters'),
               dtype=(np.int16, np.float32, np.int32)) if qs else Table(names=('quadrant', 'scale', 'n_shutters'),
                                                                        dtype=(np.int16, np.float32, np.int32))
    hdus.append(fits.BinTableHDU(qt, name='MBKGQSR'))
    sh = Table()
    sh['slitlet_id'] = np.array([s.slitlet_id for s in specs], np.int32)
    sh['source_id'] = np.array([s.source_id for s in specs], np.int64)
    k3 = [keys.get((s.slitlet_id, s.k)) or (0, -1, -1) for s in specs]
    sh['q'] = np.array([k[0] for k in k3], np.int16)
    sh['row'] = np.array([k[1] for k in k3], np.int16)
    sh['col'] = np.array([k[2] for k in k3], np.int16)
    sh['k'] = np.array([s.k for s in specs], np.int16)
    sh['n_pix'] = np.array([s.n_pix for s in specs], np.int32)
    sh['scale'] = np.array([s.scale for s in specs], np.float32)
    sh['scale_err'] = np.array([s.scale_err for s in specs], np.float32)
    sh['chi2_red'] = np.array([s.chi2_red for s in specs], np.float32)
    sh['line_run'] = np.array([s.line_run for s in specs], np.int16)
    sh['clean'] = np.array([s.clean for s in specs], bool)
    sh['reason'] = np.array([s.reason for s in specs], 'U16')
    hdus.append(fits.BinTableHDU(sh, name='MBKGSHR'))
    if fit is not None:
        ft = Table(fit, copy=True); ft.meta.clear()
        hdus.append(fits.BinTableHDU(ft, name='SHUTAMP'))
    if calls_t is not None:
        hdus.append(fits.BinTableHDU(calls_t, name='CALLS'))
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        atomic_save(fits.HDUList(hdus), path)
