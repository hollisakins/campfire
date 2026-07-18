"""Correction-error metrics for the bkg2d synthetic validation.

Everything derives from the correction-error map

    E = correction - sky_truth        (correction = sci_in - sci_out)

i.e. what the step removed *beyond* the true background. E compares two
smooth models, so no photometric noise from the sources enters the per-source
numbers (the residual scatter in E is the Background2D mesh's response to
noise — see the README note on the faint-source scatter floor).

Aperture-to-aperture, always: a source's truth is the sum of *its own
rendered stamp* inside the measurement aperture (never the analytic total,
never the stamp total), and its loss is the sum of E inside the *same
aperture*. Flux outside the aperture is not loss; neighbors' oversubtraction
that lands inside the aperture is (as in real photometry).

The global DC of E is removed via a robust floor over quiet sky before any
aperture sum: the per-amp pedestal legitimately absorbs the mean of the
undetected-source floor (skymatch semantics), and that is not per-source
oversubtraction. Each source also gets a local-annulus variant (what an
annulus photometrist would experience) — reported separately because a bowl
partially cancels there, which is exactly the confound the EGS A/B exposed.
"""

import time

import numpy as np

# r_e below this (in SW-equivalent px) counts as "compact" in summaries
COMPACT_RE_SW = 8.0
AP_R_FACTOR = 2.5            # aperture radius = max(3 px, 2.5 r_e)
ANN_FACTORS = (4.0, 6.0)     # local annulus, in r_e (min width enforced)
MIN_SRC_FLUX_SIGMA = 30.0    # per-source rows only above this (see README)


def _aperture_masks(shape, x, y, r_ap, r_in, r_out):
    """Boolean aperture + annulus masks on a local window; returns
    (window slices, ap_mask, ann_mask)."""
    H, W = shape
    r = int(np.ceil(r_out)) + 1
    y0, y1 = max(0, int(y) - r), min(H, int(y) + r + 1)
    x0, x1 = max(0, int(x) - r), min(W, int(x) + r + 1)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    rr = np.hypot(yy - y, xx - x)
    return (slice(y0, y1), slice(x0, x1)), rr <= r_ap, (rr > r_in) & (rr <= r_out)


def quiet_sky_mask(scene):
    """Pixels safe for the E floor / sky-fidelity stats: on-detector, with
    negligible galaxy and ICL truth flux."""
    t = 0.1 * scene.sigma
    return (scene.galaxies < t) & (scene.icl < t) & scene.valid


def evaluate(correction, scene, srcmask=None):
    """Evaluate one cell. Returns (summary, per-source rows, bowls, maps).

    The galaxy-attributable error map separates accepted ICL removal from
    galaxy loss: removal within the per-pixel ICL truth band is fine, so

        E_gal = Ef - clip(Ef, 0, icl_truth)

    A fit that removes sky + any fraction of the local ICL scores E_gal = 0;
    only removal *beyond* sky+ICL (positive: galaxy flux absorbed) or below
    sky (negative: undersubtraction) counts. Per-source losses and bowls use
    E_gal; the raw-Ef variants are recorded for reference.

    bowls: {source index: (radii_px, profile)} — azimuthal median of E_gal
    around the brightest galaxies, the direct oversubtraction imprint.
    maps: {'Ef', 'E_gal'} for plotting.
    """
    t0 = time.time()
    # stripes (per-amp DC + 1/f, when injected) are true background exactly
    # like sky: the step is supposed to remove them, so they subtract out of E
    E = correction.astype(np.float64) - scene.sky - scene.stripes
    valid = scene.valid
    quiet = quiet_sky_mask(scene)
    floor = float(np.median(E[quiet]))
    Ef = np.where(valid, E - floor, 0.0)
    E_gal = Ef - np.clip(Ef, 0.0, scene.icl)

    scale = scene.scale
    rows = []
    galaxies = [s for s in scene.sources if s.kind == 'galaxy'
                and s.flux >= MIN_SRC_FLUX_SIGMA * scene.sigma]
    for i, src in enumerate(galaxies):
        r_ap = max(3.0, AP_R_FACTOR * src.r_e)
        r_in = max(r_ap + 2.0, ANN_FACTORS[0] * src.r_e)
        r_out = max(r_in + 4.0, ANN_FACTORS[1] * src.r_e)
        win, ap, ann = _aperture_masks(scene.shape, src.x, src.y,
                                       r_ap, r_in, r_out)
        ap &= valid[win]
        ann &= valid[win]

        # truth: THIS source's stamp inside the same aperture
        sy, sx = src.slices
        syy, sxx = np.mgrid[sy, sx]
        srr = np.hypot(syy - src.y, sxx - src.x)
        truth_ap = float(src.stamp[srr <= r_ap].sum())
        if truth_ap <= 0:
            continue

        loss = float(E_gal[win][ap].sum())
        loss_raw = float(Ef[win][ap].sum())
        ann_med = float(np.median(E_gal[win][ann])) if ann.any() else 0.0
        loss_local = float((E_gal[win][ap] - ann_med).sum())
        rows.append(dict(
            x=src.x, y=src.y, flux=src.flux, r_e=src.r_e, n=src.n,
            compact=bool(src.r_e < COMPACT_RE_SW * scale),
            truth_ap=truth_ap, loss=loss, frac=loss / truth_ap,
            loss_raw=loss_raw, frac_raw=loss_raw / truth_ap,
            loss_local=loss_local, frac_local=loss_local / truth_ap))

    # ICL absorption: fraction of the injected envelope removed, counting
    # only removal inside the truth band (bounded [0, 1]; -> 1 is success
    # for subtract_2d). Region = where the ICL plane is substantial.
    icl_frac = np.nan
    if scene.icl.max() > 0:
        region = (scene.icl > 0.1 * scene.icl.max()) & valid
        icl_truth = float(scene.icl[region].sum())
        if icl_truth > 0:
            removed = np.clip(Ef[region], 0.0, scene.icl[region])
            icl_frac = float(removed.sum()) / icl_truth

    # sky fidelity: residual structure over quiet sky, in sigma_pix units;
    # gradient removal check via the quiet-sky rms of the *input* gradient
    sky_rms = float(np.std(Ef[quiet]))
    grad_in = float(np.std(scene.sky[quiet] - scene.sky[quiet].mean()))
    stripes_in = float(np.std(scene.stripes[quiet]))

    # amp-seam amplitude: largest |median-E step| across the three amp
    # boundaries (the sawtooth-vs-mesh pathology's direct signature)
    seams = []
    for b in (512, 1024, 1536):
        if b >= scene.shape[1]:
            continue
        left = np.median(Ef[:, b - 9:b - 1][valid[:, b - 9:b - 1]])
        right = np.median(Ef[:, b + 1:b + 9][valid[:, b + 1:b + 9]])
        seams.append(abs(float(right) - float(left)))
    amp_seam = max(seams) if seams else 0.0

    # bowls around the brightest galaxies (galaxy-attributable error only)
    bowls = {}
    bright = sorted(range(len(galaxies)), key=lambda i: -galaxies[i].flux)[:5]
    for i in bright:
        src = galaxies[i]
        r_max = min(250.0, 12 * src.r_e + 60)
        win, _, _ = _aperture_masks(scene.shape, src.x, src.y, 0, 0, r_max)
        yy, xx = np.mgrid[win[0], win[1]]
        rr = np.hypot(yy - src.y, xx - src.x)
        ok = valid[win]
        edges = np.linspace(0, r_max, 26)
        prof = np.array([np.median(E_gal[win][(rr >= a) & (rr < b) & ok])
                         if ((rr >= a) & (rr < b) & ok).any() else np.nan
                         for a, b in zip(edges[:-1], edges[1:])])
        bowls[i] = (0.5 * (edges[:-1] + edges[1:]), prof)

    fr = np.array([r['frac'] for r in rows])
    cm = np.array([r['compact'] for r in rows])
    # bright subset: mesh-noise scatter is negligible against truth_ap here,
    # so any nonzero median/worst is systematic, not noise
    br = np.array([r['flux'] >= 300 * scene.sigma for r in rows])

    def _stats(v):
        if v.size == 0:
            return dict(n=0, med=np.nan, p95=np.nan, worst=np.nan)
        return dict(n=int(v.size), med=float(np.median(v)),
                    p95=float(np.percentile(np.abs(v), 95)),
                    worst=float(v[np.argmax(np.abs(v))]))

    summary = dict(
        floor=floor,
        sky_rms_sigma=sky_rms / scene.sigma,
        grad_in_sigma=grad_in / scene.sigma,
        stripes_in_sigma=stripes_in / scene.sigma,
        amp_seam_sigma=amp_seam / scene.sigma,
        icl_removed_frac=icl_frac,
        compact=_stats(fr[cm]),
        extended=_stats(fr[~cm]),
        bright=_stats(fr[br]),
        masked_frac=(float(srcmask.mean()) if srcmask is not None else np.nan),
        eval_s=time.time() - t0,
    )
    return summary, rows, bowls, {'Ef': Ef, 'E_gal': E_gal}
