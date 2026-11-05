"""Calibration stars seen at several positions in their shutters, fitted with the current pipeline: the data behind
the path-loss figure (``fig_pathloss.py --stars``).

Every visit (star x detector x background group) of the given observations is fitted with the profile fit as stage
3 runs it (``profile_fit.load_source`` with the stage's options: the observation's bar offset and the path-loss
settings; ``profile_fit.fit``). Written per visit: the fitted center and width, SRCXPOS, the saturated share of the
trace, and the flux and its error in each band. A star's measured light at a position, relative to its most
central visit, is then the model's transmission ratio between the two positions times the ratio of the fitted
fluxes (what the model predicts, times how far the data moved it).

The observation's bar offset is measured first as stage 3 measures it (``bar_offset.ensure``, which writes
``bar_offset.json`` into the workspace, or keeps the one there).

    python scripts/docs/nirspec/fit_star_positions.py --root <root> --obs <obs> [<obs> ...] --out-json <json>
        [--processes N] [--bands 0.8:1.2 4.5:5.3]
"""
import common as C

p = C.parser(__doc__.splitlines()[0], obs=False)
p.add_argument('--obs', nargs='+', required=True)
p.add_argument('--out-json', required=True)
p.add_argument('--processes', type=int, default=4)
p.add_argument('--disperser', default='prism_clear')
p.add_argument('--bands', nargs='+', default=['0.8:1.2', '2.7:3.3', '4.2:4.8', '4.5:5.3'], help='µm ranges, lo:hi')
args = p.parse_args()
root = C.setup_root(args.root)

import json                                                           # noqa: E402
import os                                                             # noqa: E402
import warnings                                                       # noqa: E402
from multiprocessing import Pool                                      # noqa: E402

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402

BANDS = [tuple(float(v) for v in b.split(':')) for b in args.bands]


def band_fluxes(R):
    lam, f, e = np.asarray(R.lam), np.asarray(R.flux, float), np.asarray(R.flux_err, float)
    ok = np.isfinite(f) & np.isfinite(e) & (e > 0)
    out = []
    for a, b in BANDS:
        m = ok & (lam >= a) & (lam < b)
        if m.sum() < 5:
            out.append([None, None])
            continue
        w = 1.0 / e[m] ** 2
        out.append([float(np.sum(w * f[m]) / np.sum(w)), float(1.0 / np.sqrt(np.sum(w)))])
    return out


def tasks(obs_name):
    """One task per (source, detector, background group) of a reduced observation."""
    from campfire_pipeline.nirspec import bar_offset as BO
    from campfire_pipeline.nirspec import stage3 as S3
    from campfire_pipeline.nirspec.observation import Observation
    obs = Observation.load(obs_name, observations_file=os.path.join(root, 'config', 'observations.toml'))
    obs.workspace_dir = C.workspace(root, obs_name)
    obs.reference_dir = os.path.join(root, 'reference', 'nirspec', obs_name)
    every = Observation.group_files(obs.discover_files(ext='canonical', source_ids='all'))
    every = every[every['filter_grating'] == args.disperser]
    sci = S3._science_files(every)
    bars = BO.ensure(obs.workspace_dir, processes=args.processes, log=print)   # as stage 3 does
    out = []
    for sid in np.unique(sci['source_id']):
        for det in np.unique(sci['detector'][sci['source_id'] == sid]):
            for bg in np.unique(sci['bkg_group'][(sci['source_id'] == sid) & (sci['detector'] == det)]):
                tf = sci[(sci['source_id'] == sid) & (sci['detector'] == det) & (sci['bkg_group'] == bg)]
                t = S3._profile_fit_task(obs, every, tf, '', sid)
                out.append(dict(obs=obs_name, sid=int(sid), det=str(det).upper(), bg=str(bg), groups=t['groups'],
                                overrides=t['overrides'], science=[str(n) for n in t['science']],
                                paths=[str(q) for q in tf['path']], bars=bars))
    return out


def one(t):
    from campfire_pipeline.nirspec import optics_calibration as OC
    from campfire_pipeline.nirspec import profile_fit as PF
    warnings.filterwarnings('ignore')
    head = dict(obs=t['obs'], sid=t['sid'], det=t['det'], bg=t['bg'])
    try:
        hs = [fits.getheader(q, 'SCI') for q in t['paths']]
        exps = PF.load_source(t['groups'], bkg_overrides=t['overrides'], science=set(t['science']),
                              **PF.load_options({'bar_offsets': t['bars']}))
        R = PF.fit(exps)
        flags = OC.saturated_flags(exps)
        return dict(head, srcx=float(np.median([h.get('SRCXPOS', np.nan) for h in hs])),
                    srcy=float(np.median([h.get('SRCYPOS', np.nan) for h in hs])),
                    centre=float(R.params['centre']), width=float(R.params['width']),
                    bar=float(np.median([e.bar_offset for e in exps])),
                    sat=[OC.saturated_fraction(exps, flags, R.params['centre'], b) for b in BANDS],
                    bands=band_fluxes(R))
    except Exception as err:                                           # noqa: BLE001 - reported, the others go on
        return dict(head, error=f'{type(err).__name__}: {err}')


def main():
    ts = [t for o in args.obs for t in tasks(o)]
    print(f'{len(ts)} visits in {len(args.obs)} observations', flush=True)
    with Pool(args.processes) as pool:
        rows = list(pool.imap_unordered(one, ts, chunksize=2))
    print(f'{sum("error" in r for r in rows)} failed', flush=True)
    # each star's reference: its visit nearest the shutter center (per observation, source and detector)
    best = {}
    for r in rows:
        if 'error' in r:
            continue
        k = (r['obs'], r['sid'], r['det'])
        if k not in best or abs(r['centre']) < abs(best[k]['centre']):
            best[k] = r
    for r in rows:
        b = best.get((r['obs'], r['sid'], r['det']))
        r['ref_bg'] = b['bg'] if b else None
    with open(args.out_json, 'w') as fh:
        json.dump(dict(bands=BANDS, rows=rows), fh, indent=1)
    print(args.out_json)


if __name__ == '__main__':
    main()
