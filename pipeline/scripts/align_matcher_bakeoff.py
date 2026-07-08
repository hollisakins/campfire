#!/usr/bin/env python
"""Coarse-matcher bake-off for the NIRCam ``align`` v2 redesign.

Compares candidate COARSE matchers for the pooled-per-module alignment on
synthetic tangent-plane catalogs, sweeping the two failure regimes we care
about:

  * a moderate *translation* error (WCS off by up to tens of arcsec), and
  * a *roll* error (WCS position angle off by up to ~1 degree),

crossed with source count and contamination. Everything runs in a local
tangent plane in **arcsec** (the frame the tweakwcs matchers operate in), so
no gwcs/jwst/CRDS is needed and the comparison isolates the matcher.

Configs compared (all end with the same rigid rshift fit + optional iterate,
so only the *initial correspondence* step differs):

  A  2dhist            XYXYMatch(use2dhist), single rshift fit (no iterate)
  B  2dhist+iterate    XYXYMatch(use2dhist) then match->fit->rematch to converge
  C  2dhist+rotscan    brute-force roll scan around XYXYMatch (jhat's approach)
  D  triangles         stsci.stimage xyxymatch(algorithm='triangles') + iterate

A solve "succeeds" when the recovered im->ref transform reproduces the TRUE
correspondences (not just the matched subset) to a median residual below
``SUCCESS_ARCSEC`` and rests on >= MIN_TRUE_MATCHES real pairs.
"""

import argparse
import logging
import time

import numpy as np

# tweakwcs logs a warning to stdout on every weak 2d-hist peak; in a sweep that
# floods the output. Silence it (weak-peak == a failed match, which we score).
logging.getLogger('tweakwcs').setLevel(logging.CRITICAL)
from astropy.table import Table
from stsci.stimage import xyxymatch
from tweakwcs.matchutils import XYXYMatch

# One NIRCam module footprint (arcsec): SW 2x2 ~ 130", LW single detector ~ 130".
FOOTPRINT = 130.0
CENTROID_NOISE = 0.031          # 1 SW pixel, arcsec
SUCCESS_ARCSEC = 0.10           # recovered transform good to ~3 SW pixels
MIN_TRUE_MATCHES = 4

# Fixed matcher knobs (the sweep varies the *scene*, not these).
SEARCHRAD = 70.0                # arcsec; must exceed the max injected offset
TOLERANCE = 2.0                 # arcsec; coarse pair-accept tolerance
SEPARATION = 1.0                # arcsec; min in-catalog separation
NMATCH_TRI = 50                 # triangle-algorithm object cap
ROTSCAN_DEG = np.linspace(-1.2, 1.2, 25)   # brute-force roll grid for config C


def _rot(theta_deg):
    t = np.radians(theta_deg)
    return np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])


def make_scene(n, shift, roll_deg, contam, missing, seed):
    """Return (ref[N,2], im[M,2], im_truth[M], ) in the arcsec tangent plane.

    ``im`` is the reference pushed through a known (roll, shift) with centroid
    noise; a ``missing`` fraction of refs are absent from the image and a
    ``contam`` fraction of the image are spurious (no ref counterpart, truth
    index -1). Rows are shuffled so index order carries no information.
    """
    rng = np.random.default_rng(seed)
    half = FOOTPRINT / 2.0
    ref = rng.uniform(-half, half, (n, 2))

    keep = rng.random(n) >= missing
    ref_kept_idx = np.flatnonzero(keep)
    im_true = (_rot(roll_deg) @ ref[keep].T).T + np.asarray(shift, float)
    im_true = im_true + rng.normal(0.0, CENTROID_NOISE, im_true.shape)

    n_real = len(im_true)
    n_contam = int(round(contam * n_real / max(1e-9, 1.0 - contam)))
    # spurious sources spread over the (shifted) image region
    lo = np.array([-half, -half]) + np.asarray(shift, float)
    spurious = rng.uniform(lo - 10.0, lo + FOOTPRINT + 10.0, (n_contam, 2))

    im = np.vstack([im_true, spurious]) if n_contam else im_true
    truth = np.concatenate([ref_kept_idx, -np.ones(n_contam, dtype=int)])
    order = rng.permutation(len(im))
    return ref, im[order], truth[order].astype(int)


def rigid_fit(im_xy, ref_xy):
    """Least-squares rotation+translation T with ref ~= R @ im + t (Kabsch)."""
    if len(im_xy) < 2:
        return None
    ic, rc = im_xy.mean(0), ref_xy.mean(0)
    H = (im_xy - ic).T @ (ref_xy - rc)
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, d]) @ U.T
    return R, rc - R @ ic


def _apply(T, xy):
    R, t = T
    return (R @ xy.T).T + t


def _nn_rematch(im_xy, ref_xy, T, tol):
    """One-to-one nearest-neighbour rematch of T(im) onto ref within tol."""
    pred = _apply(T, im_xy)
    # brute-force NN (catalogs are small)
    d = np.linalg.norm(pred[:, None, :] - ref_xy[None, :, :], axis=2)
    im_to_ref = d.argmin(1)
    ref_to_im = d.argmin(0)
    ii = np.arange(len(im_xy))
    mutual = ref_to_im[im_to_ref] == ii
    within = d[ii, im_to_ref] <= tol
    keep = mutual & within
    return ii[keep], im_to_ref[keep]


def _iterate(im_xy, ref_xy, ii, ri, tol, niter):
    """Refine an initial (im_idx, ref_idx) correspondence by match->fit->rematch."""
    T = rigid_fit(im_xy[ii], ref_xy[ri])
    if T is None:
        return None
    for _ in range(niter):
        ii2, ri2 = _nn_rematch(im_xy, ref_xy, T, tol)
        if len(ii2) < 2:
            break
        T2 = rigid_fit(im_xy[ii2], ref_xy[ri2])
        if T2 is None:
            break
        T = T2
    return T


def _xyxy_2dhist(ref_xy, im_xy):
    rt = Table({'TPx': ref_xy[:, 0], 'TPy': ref_xy[:, 1]})
    it = Table({'TPx': im_xy[:, 0], 'TPy': im_xy[:, 1]})
    m = XYXYMatch(use2dhist=True, searchrad=SEARCHRAD,
                  tolerance=TOLERANCE, separation=SEPARATION)
    ri, ii = m(rt, it, tp_pscale=1.0)
    return np.asarray(ii, int), np.asarray(ri, int)


# The stsci.stimage 'triangles' C routine segfaults on too-few / degenerate
# point sets (needs enough sources to build & vote on triangles). Guard the
# native call — below this it is not a viable coarse matcher anyway.
MIN_TRI_POINTS = 7


def _triangles(ref_xy, im_xy):
    if len(ref_xy) < MIN_TRI_POINTS or len(im_xy) < MIN_TRI_POINTS:
        return np.array([], int), np.array([], int)
    m = xyxymatch(im_xy.astype(np.float32), ref_xy.astype(np.float32),
                  algorithm='triangles', tolerance=TOLERANCE,
                  separation=SEPARATION, nmatch=NMATCH_TRI)
    return np.asarray(m['input_idx'], int), np.asarray(m['ref_idx'], int)


def solve(config, ref_xy, im_xy):
    """Return the recovered im->ref transform T, or None on failure."""
    try:
        if config in ('2dhist', '2dhist+iter'):
            ii, ri = _xyxy_2dhist(ref_xy, im_xy)
            niter = 0 if config == '2dhist' else 3
            return _iterate(im_xy, ref_xy, ii, ri, TOLERANCE, niter)
        if config == '2dhist+rotscan':
            best = None
            for th in ROTSCAN_DEG:
                imr = (_rot(th) @ im_xy.T).T
                ii, ri = _xyxy_2dhist(ref_xy, imr)
                if best is None or len(ii) > best[0]:
                    best = (len(ii), ii, ri)
            if best is None or best[0] < 2:
                return None
            return _iterate(im_xy, ref_xy, best[1], best[2], TOLERANCE, 3)
        if config == 'triangles':
            ii, ri = _triangles(ref_xy, im_xy)
            return _iterate(im_xy, ref_xy, ii, ri, TOLERANCE, 3)
    except Exception:
        return None
    return None


def evaluate(T, ref_xy, im_xy, truth):
    """Outcome for transform T against the true correspondences.

    Returns one of:
      'correct' — recovered to < SUCCESS_ARCSEC on the true pairs (fine fit
                  will refine from here),
      'wrong'   — a transform was produced but it is geometrically wrong
                  (the dangerous silent-failure class), or
      'none'    — the matcher produced no usable transform (fails safe ->
                  NOT_ALIGNED, loud).
    Also returns the median residual (nan when 'none').
    """
    if T is None:
        return 'none', np.nan
    real = truth >= 0
    if real.sum() < MIN_TRUE_MATCHES:
        return 'none', np.nan
    pred = _apply(T, im_xy[real])
    resid = np.linalg.norm(pred - ref_xy[truth[real]], axis=1)
    med = float(np.median(resid))
    n_ok = int((resid <= SUCCESS_ARCSEC).sum())
    if med <= SUCCESS_ARCSEC and n_ok >= MIN_TRUE_MATCHES:
        return 'correct', med
    return 'wrong', med


CONFIGS = ['2dhist', '2dhist+iter', '2dhist+rotscan', 'triangles']


SHIFTS = [0.0, 15.0, 30.0, 60.0]
ROLLS = [0.0, 0.1, 0.3, 0.5, 1.0]
COUNTS = [10, 20, 50, 150]
CONTAMS = [0.0, 0.3, 0.6]        # 0.0 is a degenerate (perfect-copy) edge case
MISSING = 0.2


def run_sweep(trials, seed0, configs):
    from collections import defaultdict
    # marg[axis][(cfg,val)] -> {'correct','wrong','none'} counts
    marg = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    grid = defaultdict(lambda: defaultdict(int))  # grid[(cfg,N,contam)] -> counts
    timing = defaultdict(float)

    seed = seed0
    ncell = 0
    for sh in SHIFTS:
        for roll in ROLLS:
            for n in COUNTS:
                for contam in CONTAMS:
                    ncell += 1
                    for _ in range(trials):
                        ref, im, truth = make_scene(
                            n, (sh, -0.4 * sh), roll, contam, MISSING, seed)
                        seed += 1
                        for cfg in configs:
                            t0 = time.perf_counter()
                            T = solve(cfg, ref, im)
                            timing[cfg] += time.perf_counter() - t0
                            out, _ = evaluate(T, ref, im, truth)
                            for axis, val in (('shift', sh), ('roll', roll),
                                              ('N', n), ('contam', contam),
                                              ('all', 'all')):
                                marg[axis][(cfg, val)][out] += 1
                            grid[(cfg, n, contam)][out] += 1
    return marg, grid, timing, ncell


def _rate(counts, key='correct'):
    tot = sum(counts.values())
    return 100.0 * counts.get(key, 0) / tot if tot else 0.0


def _fmt_table(marg, axis, values, title, configs):
    print(f"\n{title}  (% correct)")
    header = f"  {'config':<16}" + "".join(f"{str(v):>9}" for v in values)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for cfg in configs:
        row = f"  {cfg:<16}"
        for v in values:
            row += f"{_rate(marg[axis][(cfg, v)]):>8.0f} "
        print(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--trials', type=int, default=12)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--configs', default='2dhist,2dhist+iter,2dhist+rotscan',
                    help='comma-separated subset of ' + ','.join(CONFIGS))
    args = ap.parse_args()
    configs = [c for c in args.configs.split(',') if c]

    # sanity self-test: pure 20" shift, no roll/noise/contam must recover ~0
    ref, im, truth = make_scene(60, (20.0, -8.0), 0.0, 0.3, 0.0, 99)
    T = solve('2dhist', ref, im)
    out, med = evaluate(T, ref, im, truth)
    print(f"[self-test] pure-shift 2dhist recovery: {out} median_resid={med:.4f}\"")

    t0 = time.perf_counter()
    marg, grid, timing, ncell = run_sweep(args.trials, args.seed + 1, configs)
    dt = time.perf_counter() - t0
    print(f"\nSwept {ncell} cells x {args.trials} trials in {dt:.1f}s "
          f"(configs: {', '.join(configs)})")

    _fmt_table(marg, 'roll', ROLLS,
               "Correct vs ROLL error [deg] (marginalized)", configs)
    _fmt_table(marg, 'shift', SHIFTS,
               "Correct vs SHIFT error [arcsec]", configs)
    _fmt_table(marg, 'N', COUNTS, "Correct vs SOURCE COUNT", configs)
    _fmt_table(marg, 'contam', CONTAMS,
               "Correct vs CONTAMINATION (0.0 = degenerate perfect-copy)", configs)
    _fmt_table(marg, 'all', ['all'], "Overall", configs)

    # The key interaction: N x contamination, for the primary config.
    primary = '2dhist+iter' if '2dhist+iter' in configs else configs[0]
    print(f"\nN x CONTAM grid for '{primary}'  (% correct)")
    print(f"  {'N \\\\ contam':<12}" + "".join(f"{c:>9}" for c in CONTAMS))
    for n in COUNTS:
        row = f"  {n:<12}"
        for c in CONTAMS:
            row += f"{_rate(grid[(primary, n, c)]):>8.0f} "
        print(row)

    # Safety: of all trials, how many produced a WRONG (silent) transform?
    print("\nFail-safety — outcome breakdown over ALL trials:")
    print(f"  {'config':<16}{'correct':>9}{'none':>9}{'WRONG':>9}")
    for cfg in configs:
        allc = marg['all'][(cfg, 'all')]
        print(f"  {cfg:<16}{_rate(allc,'correct'):>8.0f} "
              f"{_rate(allc,'none'):>8.0f} {_rate(allc,'wrong'):>8.0f} ")

    print("\nRelative runtime (s, total across sweep):")
    for cfg in configs:
        print(f"  {cfg:<16}{timing[cfg]:>8.1f}")


if __name__ == '__main__':
    main()
