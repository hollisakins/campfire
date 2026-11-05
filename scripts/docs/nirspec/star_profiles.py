"""Calibration stars' light along the slit, for the bottom panel of the optics-steps figure (``fig_optics_steps.py
--stars``).

Stars are taken from the fits of ``fit_star_positions.py`` (its JSON): visits on one detector whose fitted center
sits between ``--pos-min`` and ``--pos-max`` shutter pitches from the center of the star's shutter (either side),
compact (fitted width under ``--width-max`` slit units), unsaturated and bright in the band. Each visit is loaded as
stage 3 loads it (bar offset included) and turned into single frames with their master background subtracted
(``mbkg_extraction.single_frame_exposures``), so no negative nod copies enter. In each exposure the star sits at the
fitted center plus that exposure's nod shift; only exposures where the star's shutter has both neighbors open (a
three-shutter slitlet, the star in the middle) are kept. Every usable pixel in the band is placed at its slit
position relative to the center of the star's shutter, mirrored when the star sits on the negative side and moved
by the difference between the star's position and ``--position`` (a few hundredths of an arcsec at most). Each
detector column is scaled by its own sum over the slitlet, so the result is the shape of the light along the slit;
the figure scales it to the model's total. Written: per band, the pixels' positions (arcsec) and values (share of
the column's light per arcsec), and the visits used.

    python scripts/docs/nirspec/star_profiles.py --root <root> --stars <star_positions.json> --out-json <json>
        [--detector NRS1] [--bands 0.8:1.2 4.5:5.3] [--position 0.3] [--processes 4]
"""
import common as C

p = C.parser(__doc__.splitlines()[0], obs=False)
p.add_argument('--stars', required=True, help="fit_star_positions.py's JSON")
p.add_argument('--out-json', required=True)
p.add_argument('--detector', default='NRS1')
p.add_argument('--bands', nargs='+', default=['0.8:1.2', '4.5:5.3'], help='µm ranges, lo:hi')
p.add_argument('--position', type=float, default=0.3, help='shutter pitches from the center (the figure\'s)')
p.add_argument('--pos-min', type=float, default=0.25)
p.add_argument('--pos-max', type=float, default=0.35)
p.add_argument('--width-max', type=float, default=0.06, help='fitted width, slit units (wider: not a star)')
p.add_argument('--snr-min', type=float, default=100.0, help='band S/N of the visit')
p.add_argument('--sat-max', type=float, default=0.0, help='saturated share of the trace in the band')
p.add_argument('--processes', type=int, default=4)
p.add_argument('--disperser', default='prism_clear')
args = p.parse_args()
root = C.setup_root(args.root)

import json                                                           # noqa: E402
import os                                                             # noqa: E402
import warnings                                                       # noqa: E402
from multiprocessing import Pool                                      # noqa: E402

import numpy as np                                                    # noqa: E402

BANDS = [tuple(float(v) for v in b.split(':')) for b in args.bands]
PITCH = 1.15                                                          # slit units per shutter pitch
SLIT_AS = 0.46                                                        # arcsec per slit unit


def selected(rows, stars_bands):
    """The visits to use, with the bands each qualifies in."""
    out = []
    for r in rows:
        if 'error' in r or r['det'] != args.detector or r['width'] > args.width_max:
            continue
        pos = abs(r['centre']) / PITCH
        if not args.pos_min <= pos <= args.pos_max:
            continue
        ok = []
        for b in BANDS:
            j = stars_bands.index(list(b)) if list(b) in stars_bands else None
            if j is None:
                continue
            f, e = r['bands'][j]
            sat = r['sat'][j]
            if f is None or e is None or e <= 0 or f / e < args.snr_min or (sat is not None and sat > args.sat_max):
                continue
            ok.append(list(b))
        if ok:
            out.append(dict(r, use_bands=ok))
    return out


def task(r):
    """What fit_star_positions.py loaded for this visit."""
    from campfire_pipeline.nirspec import bar_offset as BO
    from campfire_pipeline.nirspec import stage3 as S3
    from campfire_pipeline.nirspec.observation import Observation
    obs = Observation.load(r['obs'], observations_file=os.path.join(root, 'config', 'observations.toml'))
    obs.workspace_dir = C.workspace(root, r['obs'])
    obs.reference_dir = os.path.join(root, 'reference', 'nirspec', r['obs'])
    every = Observation.group_files(obs.discover_files(ext='canonical', source_ids='all'))
    every = every[every['filter_grating'] == args.disperser]
    sci = S3._science_files(every)
    tf = sci[(sci['source_id'] == r['sid']) & (np.char.upper(sci['detector'].astype(str)) == r['det'])
             & (sci['bkg_group'].astype(str) == str(r['bg']))]
    if len(tf) == 0:
        raise ValueError('no science files for this visit')
    t = S3._profile_fit_task(obs, every, tf, '', r['sid'])
    return t, BO.ensure(obs.workspace_dir, processes=1, log=lambda *a, **k: None)


def one(r):
    from campfire_pipeline.nirspec import mbkg_extraction as MB
    from campfire_pipeline.nirspec import profile_fit as PF
    warnings.filterwarnings('ignore')
    try:
        t, bars = task(r)
        exps = PF.load_source(t['groups'], bkg_overrides=t['overrides'], science=set(t['science']),
                              **PF.load_options({'bar_offsets': bars}))
        sci = [e for e in exps if e.use_data]
        mexps = MB.single_frame_exposures(sci, MB.frames_for([e.path for e in sci]))
        c_plan = PF.planned_position(exps)[0]
        out = {tuple(b): ([], []) for b in r['use_bands']}
        n_used = 0
        for e in mexps:
            if not e.has_source:
                continue
            y_star = r['centre'] + (e.src_y - c_plan)
            k = int(np.round(y_star / PITCH))
            pos = y_star - k * PITCH                                  # in the star's shutter, slit units
            if tuple(sorted(int(v) - k for v in e.open_k)) != (-1, 0, 1):   # both neighbors open
                continue
            if not args.pos_min <= abs(pos) / PITCH <= args.pos_max:
                continue
            sign = 1.0 if pos > 0 else -1.0
            rel = sign * (np.asarray(e.y, float) - k * PITCH)         # mirrored so the star is on the + side
            rel += args.position * PITCH - abs(pos)                   # moved to the figure's position
            d, ok, wav = np.asarray(e.diff, float), np.asarray(e.ok, bool), np.asarray(e.wav, float)
            inside = ok & np.isfinite(d) & (np.abs(rel) < 1.6 * PITCH)
            used = False
            for b in r['use_bands']:
                m = inside & (wav >= b[0]) & (wav < b[1])
                for col in np.nonzero(m.any(axis=0))[0]:
                    rows_ = np.nonzero(m[:, col])[0]
                    if rows_.size < 8:
                        continue
                    tot = d[rows_, col].sum()
                    if not np.isfinite(tot) or tot <= 0:
                        continue
                    dy = np.median(np.abs(np.diff(rel[rows_, col]))) * SLIT_AS   # arcsec per row
                    out[tuple(b)][0].extend((rel[rows_, col] * SLIT_AS).tolist())
                    out[tuple(b)][1].extend((d[rows_, col] / tot / dy).tolist())
                    used = True
            n_used += used
        return dict(obs=r['obs'], sid=r['sid'], bg=r['bg'], centre=r['centre'], n_exp=n_used,
                    bands={f'{b[0]:g}:{b[1]:g}': v for b, v in out.items()})
    except Exception as err:                                           # noqa: BLE001 - reported, the others go on
        return dict(obs=r['obs'], sid=r['sid'], bg=r['bg'], error=f'{type(err).__name__}: {err}')


def main():
    with open(args.stars) as fh:
        S = json.load(fh)
    rows = selected(S['rows'], [list(b) for b in S['bands']])
    print(f'{len(rows)} visits selected', flush=True)
    with Pool(args.processes) as pool:
        res = list(pool.imap_unordered(one, rows, chunksize=1))
    bad = [r for r in res if 'error' in r]
    good = [r for r in res if 'error' not in r and r['n_exp'] > 0]
    print(f'{len(good)} visits used, {len(bad)} failed', flush=True)
    for r in bad[:5]:
        print('  ', r['obs'], r['sid'], r['bg'], r['error'], flush=True)
    bands = {}
    for b in BANDS:
        key = f'{b[0]:g}:{b[1]:g}'
        y = [v for r in good for v in r['bands'].get(key, ([], []))[0]]
        v = [v for r in good for v in r['bands'].get(key, ([], []))[1]]
        n = sum(1 for r in good if r['bands'].get(key, ([], []))[0])
        bands[key] = dict(y=y, v=v, n_visits=n)
        print(f'{key} um: {n} visits, {len(y)} pixels', flush=True)
    with open(args.out_json, 'w') as fh:
        json.dump(dict(detector=args.detector, position=args.position, bands=bands,
                       visits=[dict(obs=r['obs'], sid=r['sid'], bg=r['bg'], n_exp=r['n_exp']) for r in good]), fh)
    print(args.out_json)


if __name__ == '__main__':
    main()
