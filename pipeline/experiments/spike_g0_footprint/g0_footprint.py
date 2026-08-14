#!/usr/bin/env python
"""G0 footprint validation harness — model isophotes vs real spikes (design
doc `docs/design-nircam-spike-masking.md` §6.1, gate G0).

The gate question: does the scaled spike-model isophote actually **envelope**
real in-flight spikes to within the mask tolerance (`grow`), across magnitude
and filter? The in-hand models are pre-flight (WebbPSF 1.0.0, requirements
OPD — §7 OQ6), so this is the load-bearing check before M3 is built.

Method, per bright Gaia star on a level-2 cal frame:

1. Resample the model onto the frame grid around the star (nearest-neighbor
   for the mask grade — any linear kernel would re-introduce under-masking,
   see `experiments/spike_model_grade/`), with the anchor→pivot radial
   rescale ``λ_pivot / λ_anchor``.
2. Find the rigid rotation (+ optional parity flip) between model and data by
   correlating azimuthal arm profiles — no dependence on the roll_ref chain,
   and the recovered ``dtheta`` per detector is itself a G0 deliverable (it
   validates the orientation assumptions M3 will wire properly).
3. Fit the model amplitude on unsaturated arm/wing pixels (median data/model
   ratio) — G0 tests footprint *shape*, not Gaia→NIRCam flux prediction,
   which stays an M3 concern.
4. At each threshold ``L = f × σ_bkg``: compare per-arm radial extents
   (wedge-median profiles, so unrelated sources drop out) and count
   pixel-level misses — data spike pixels outside the grow-dilated model
   mask. Envelope holds iff ``r_model + tol ≥ r_data`` per arm and misses
   are consistent with noise.

Outputs: one overlay+profiles PNG per star and a per-frame JSON with arm
extents, verdicts, ``dtheta``/flip/amplitude, all under ``--out``.

Usage (on a machine with an existing reduction):

    conda run -n campfire python g0_footprint.py \
        --frames '$CAMPFIRE_ROOT/products/<field>/.../image2/*_cal.fits' \
        --out ./g0_out

Model files come from the published set (`spike_models/webbpsf100_v1`) via
the shared ref_cache engine (needs `campfire-pipeline` importable and
`$CAMPFIRE_ROOT` set), or point ``--model-dir`` at a directory that already
holds the ``SPIKE_MODEL_*.fits`` files. Stars come from a Gaia DR3 cone
query (astroquery), or pass ``--stars ra,dec[,gmag]`` explicitly.
"""

import argparse
import glob
import json
import math
import sys
from pathlib import Path

import numpy as np

# NIRCam filter pivot wavelengths [µm] (JDox). Narrow/medium pupil-wheel
# filters included — the harness resolves PUPIL before FILTER, per JWST
# header convention.
PIVOT_UM = {
    'F070W': 0.704, 'F090W': 0.901, 'F115W': 1.154, 'F140M': 1.404,
    'F150W': 1.501, 'F162M': 1.626, 'F164N': 1.644, 'F150W2': 1.671,
    'F182M': 1.845, 'F187N': 1.874, 'F200W': 1.990, 'F210M': 2.093,
    'F212N': 2.120, 'F250M': 2.503, 'F277W': 2.786, 'F300M': 2.996,
    'F322W2': 3.247, 'F323N': 3.237, 'F335M': 3.365, 'F356W': 3.563,
    'F360M': 3.621, 'F405N': 4.055, 'F410M': 4.092, 'F430M': 4.280,
    'F444W': 4.421, 'F460M': 4.630, 'F466N': 4.654, 'F470N': 4.707,
    'F480M': 4.834,
}

# DQ bits that disqualify a pixel from fits/profiles (DO_NOT_USE | SATURATED).
BAD_DQ = 0b11


# ---------------------------------------------------------------------------
# models

def load_model(path):
    """Read one published SPIKE_MODEL FITS into a plain dict."""
    from astropy.io import fits
    with fits.open(path) as hdul:
        hdr = hdul[0].header
        return dict(
            img=np.asarray(hdul[0].data, dtype=np.float32),
            pixscl=float(hdr['PIXELSCL']),
            ctr=float(hdr['CFSPKCTR']),
            wave=float(hdr['CFSPKWAV']),
            grade=str(hdr['CFSPKGRD']),
            channel=str(hdr['CFSPKCHN']),
        )


def read_manifest(manifest_path):
    """Full spike-model manifest as a list of entry dicts."""
    import toml
    data = toml.load(manifest_path)
    return data['model']


def default_manifest_path():
    try:
        import campfire_pipeline
        return (Path(campfire_pipeline.__file__).parent / 'data'
                / 'spike_model_manifest.toml')
    except ImportError:
        # running from a repo checkout without an installed pipeline
        return (Path(__file__).resolve().parents[2] / 'campfire_pipeline'
                / 'data' / 'spike_model_manifest.toml')


def pick_anchor(entries, channel, pivot_um):
    """Nearest anchor wavelength in this channel; returns {grade: name}."""
    waves = sorted({e['wavelength_um'] for e in entries
                    if e['channel'] == channel})
    if not waves:
        raise SystemExit(f'no {channel} models in manifest')
    anchor = min(waves, key=lambda w: abs(w - pivot_um))
    return anchor, {e['grade']: e['name'] for e in entries
                    if e['channel'] == channel
                    and e['wavelength_um'] == anchor}


def fetch_models(names, model_dir=None):
    """Resolve (fetching if allowed) model files; returns {name: path}."""
    if model_dir:
        out = {}
        for n in names:
            p = Path(model_dir) / n
            if not p.exists():
                raise SystemExit(f'--model-dir given but {p} not found')
            out[n] = str(p)
        return out
    from campfire_pipeline.common import ref_cache
    manifest = default_manifest_path()
    engine = ref_cache.RefCache(
        'spike_models', lambda: ref_cache.load_manifest(manifest, 'model'),
        log_prefix='spike', log_noun='spike model')
    engine.ensure(names)
    return {n: engine.resolve(n) for n in names}


# ---------------------------------------------------------------------------
# geometry

def resample_model(model, shape, star_yx, pixscl_data, lam_scale,
                   dtheta_deg=0.0, flip=False):
    """Model raster on the data grid: star at ``star_yx``, radial ×lam_scale,
    rotated by dtheta (deg, CCW in pixel coords), optionally y-mirrored.

    Nearest-neighbor for the mask grade (order 0 — preserves the block-max
    superset semantics), bilinear for photometric.
    """
    from scipy.ndimage import map_coordinates
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]].astype(np.float64)
    dy, dx = yy - star_yx[0], xx - star_yx[1]
    # Convention (matched by fit_orientation and measure_star): a model
    # feature at PA ``a`` lands in the data at ``a + dtheta`` (or
    # ``-a + dtheta`` with the parity flip): rotate data coords by -dtheta,
    # then mirror.
    th = math.radians(dtheta_deg)
    c, s = math.cos(th), math.sin(th)
    ry = c * dy - s * dx
    rx = c * dx + s * dy
    if flip:
        ry = -ry
    k = pixscl_data / (model['pixscl'] * lam_scale)
    order = 0 if model['grade'] == 'mask' else 1
    return map_coordinates(model['img'], [model['ctr'] + ry * k,
                                          model['ctr'] + rx * k],
                           order=order, cval=0.0, prefilter=False)


def polar_grid(shape, center_yx):
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]].astype(np.float64)
    dy, dx = yy - center_yx[0], xx - center_yx[1]
    return np.hypot(dy, dx), np.degrees(np.arctan2(dy, dx)) % 360.0


def cell_means(img, r_as, theta, r_lo, r_hi, n_rbins, good=None, nbins=360):
    """Mean SB on an (n_rbins × nbins) polar grid over an annulus.

    Narrow spike arms are a few px wide, so any statistic over a bin much
    wider than the arm dilutes the ridge into the background — a 1° × one-
    radial-bin cell stays comparable to the arm width, so the cell mean
    tracks the ridge. Downstream reductions pick the robust direction:
    median over radius (azimuthal profiles, kills point sources) or max
    over azimuth inside a wedge (ridge profiles, tracks the arm).
    """
    sel = (r_as >= r_lo) & (r_as < r_hi) & np.isfinite(img)
    if good is not None:
        sel &= good
    rb = np.clip(((r_as[sel] - r_lo) / (r_hi - r_lo)
                  * n_rbins).astype(int), 0, n_rbins - 1)
    tb = (theta[sel] * nbins / 360.0).astype(int) % nbins
    sums = np.zeros((n_rbins, nbins))
    cnts = np.zeros((n_rbins, nbins))
    np.add.at(sums, (rb, tb), img[sel])
    np.add.at(cnts, (rb, tb), 1)
    with np.errstate(invalid='ignore'):
        return sums / np.where(cnts >= 3, cnts, np.nan)


def az_ridge_profile(img, r_as, theta, r_lo, r_hi, good=None, n_rbins=24):
    """Per-degree azimuthal profile: median over radial cells of cell means
    (point sources hit 1–2 cells; arms elevate the whole column)."""
    with np.errstate(invalid='ignore'):
        return np.nanmedian(
            cell_means(img, r_as, theta, r_lo, r_hi, n_rbins, good), axis=0)


def find_arm_angles(model, r_lo_as, r_hi_as, max_arms=8):
    """Arm position angles [deg] from the model raster itself."""
    r, theta = polar_grid(model['img'].shape, (model['ctr'], model['ctr']))
    r_as = r * model['pixscl']
    prof = az_ridge_profile(model['img'], r_as, theta, r_lo_as, r_hi_as)
    base = np.nanpercentile(prof, 50)
    # smooth circularly, then greedy peak-pick with 15 deg exclusion
    k = np.array([1, 2, 3, 2, 1], dtype=float)
    sm = np.convolve(np.concatenate([prof[-2:], prof, prof[:2]]),
                     k / k.sum(), mode='same')[2:-2]
    order = np.argsort(np.nan_to_num(sm, nan=-np.inf))[::-1]
    peaks = []
    for i in order:
        if sm[i] <= base or len(peaks) >= max_arms:
            break
        if all(min(abs(i - p), 360 - abs(i - p)) >= 15 for p in peaks):
            peaks.append(int(i))
    return sorted(float(p) for p in peaks)


def fit_orientation(arm_angles, data_az):
    """Rigid dtheta (+ parity flip) maximizing summed data SB at arm PAs.

    ``data_az`` is a 360-bin azimuthal median profile of the data annulus.
    Returns (dtheta_deg, flip, score).
    """
    filled = np.nan_to_num(data_az, nan=np.nanmedian(data_az))
    k = np.array([1, 2, 3, 2, 1], dtype=float)
    filled = np.convolve(np.concatenate([filled[-2:], filled, filled[:2]]),
                         k / k.sum(), mode='same')[2:-2]
    best = (0.0, False, -np.inf)
    for flip in (False, True):
        angles = [(-a) % 360 for a in arm_angles] if flip else arm_angles
        for dth in np.arange(0.0, 360.0, 0.5):
            score = sum(filled[int((a + dth) % 360)] for a in angles)
            if score > best[2]:
                best = (float(dth), flip, float(score))
    return best


# ---------------------------------------------------------------------------
# measurement

def robust_sigma(vals):
    vals = vals[np.isfinite(vals)]
    med = np.median(vals)
    return float(1.4826 * np.median(np.abs(vals - med))), float(med)


def fit_amplitude(data, model_grid, good, r, r_min_px):
    """Median data/model over bright-but-unsaturated model pixels."""
    pos = model_grid[model_grid > 0]
    if pos.size < 100:
        return np.nan, 0
    lo, hi = np.percentile(pos, [99.0, 99.99])
    sel = (model_grid >= lo) & (model_grid <= hi) & good & (r > r_min_px)
    sel &= np.isfinite(data)
    n = int(sel.sum())
    if n < 50:
        return np.nan, n
    return float(np.median(data[sel] / model_grid[sel])), n


def ridge_profile(cells, arm_deg, half_deg):
    """Arm ridge SB profile from a polar cell grid (for plots/JSON): per
    radial bin, the max over the 1° azimuth columns inside the wedge."""
    cols = [int(round(arm_deg + d)) % 360
            for d in range(-int(round(half_deg)), int(round(half_deg)) + 1)]
    with np.errstate(invalid='ignore'):
        return np.nanmax(cells[:, cols], axis=1)


def count_extent(rb, vals, level, rbins, min_px):
    """Outermost radius with 2 consecutive radial bins each holding at
    least ``min_px`` pixels above ``level``.

    The verdict metric works on *pixels*, not azimuthal-cell statistics:
    cell means dilute a narrow ridge by its fill factor (level-dependent,
    and different between data and the resampled model), which biases the
    envelope test. "≥ min_px pixels above L" is exactly the mask semantics
    and is noise-robust (expected false count per bin ≪ min_px at 3σ).
    """
    counts = np.bincount(rb[vals > level], minlength=len(rbins) - 1)
    above = counts >= min_px
    ext = 0.0
    for b in range(1, len(above)):
        if above[b] and above[b - 1]:
            ext = rbins[b + 1]
    return float(ext)


def measure_star(data, good, star_yx, models, pixscl, lam_scale, args):
    """All G0 measurements for one star. Returns a JSON-ready dict + rasters
    for plotting (or None if the star is unusable)."""
    from scipy.ndimage import binary_dilation

    shape = data.shape
    r, theta = polar_grid(shape, star_yx)
    r_min_px = args.r_min / pixscl

    # background sigma from pixels the model calls empty (measured after
    # alignment below; first estimate from the outer cutout corners)
    sigma, bkg = robust_sigma(data[good & (r > 0.75 * r.max())])
    if not np.isfinite(sigma) or sigma <= 0:
        return None

    phot, mask = models['photometric'], models['mask']
    arm_angles = find_arm_angles(phot, args.arm_lo, args.arm_hi)
    if len(arm_angles) < 4:
        return None

    # orientation: data azimuthal profile over the same annulus (in arcsec)
    annulus = az_ridge_profile(data - bkg, r * pixscl, theta,
                               args.arm_lo * lam_scale,
                               args.arm_hi * lam_scale, good=good)
    dtheta, flip, _ = fit_orientation(arm_angles, annulus)

    m_phot = resample_model(phot, shape, star_yx, pixscl, lam_scale,
                            dtheta, flip)
    amp, n_fit = fit_amplitude(data - bkg, m_phot, good, r, r_min_px)
    if not np.isfinite(amp) or amp <= 0:
        return None
    m_mask = resample_model(mask, shape, star_yx, pixscl, lam_scale,
                            dtheta, flip)

    # refine sigma on model-empty pixels
    empty = good & (m_phot * amp < 0.5 * sigma) & (r > r_min_px)
    if empty.sum() > 500:
        sigma, bkg2 = robust_sigma(data[empty])
        bkg += bkg2

    r_model_edge = (phot['img'].shape[0] / 2.0) * phot['pixscl'] * lam_scale
    rbin_as = max(2 * pixscl, mask['pixscl'] * lam_scale)
    rbins = np.arange(args.r_min, min(r_model_edge, r.max() * pixscl),
                      rbin_as)
    if len(rbins) < 8:
        return None
    tol_as = args.grow * pixscl + rbin_as

    cells_d = cell_means(data - bkg, r * pixscl, theta,
                         rbins[0], rbins[-1], len(rbins) - 1, good)
    cells_m = cell_means(amp * m_mask, r * pixscl, theta,
                         rbins[0], rbins[-1], len(rbins) - 1)

    # per-arm wedge pixel sets, radially binned, for the count extents
    r_as = r * pixscl
    in_annulus = (r_as >= rbins[0]) & (r_as < rbins[-1])
    rb_all = np.clip(((r_as - rbins[0]) / (rbins[1] - rbins[0])).astype(int),
                     0, len(rbins) - 2)
    arm_px = {}
    arm_info = []
    for a in arm_angles:
        a_data = ((-a if flip else a) + dtheta) % 360
        dth = np.abs(((theta - a_data) + 180) % 360 - 180)
        sel_d = (dth <= args.wedge) & in_annulus & good & np.isfinite(data)
        sel_m = (dth <= args.wedge) & in_annulus
        arm_px[a] = (rb_all[sel_d], (data - bkg)[sel_d],
                     rb_all[sel_m], (amp * m_mask)[sel_m], a_data)
        arm_info.append(dict(
            theta_model=a, theta_data=a_data,
            profile_data=[None if not np.isfinite(v) else round(float(v), 6)
                          for v in ridge_profile(cells_d, a_data, args.wedge)],
            profile_model=[None if not np.isfinite(v) else round(float(v), 6)
                           for v in ridge_profile(cells_m, a_data,
                                                  args.wedge)],
        ))

    levels = {}
    for f in args.levels:
        L = f * sigma
        # data pixels cross L wherever the underlying SB is within ~1 sigma
        # below it, so the model side of the comparison is evaluated at
        # L - sigma: without this slack a *perfect* model fails the envelope
        # test from noise skew alone (measured on the synthetic selftest)
        L_m = max(L - sigma, 0.5 * sigma)
        arms = []
        for a in arm_angles:
            rb_d, val_d, rb_m, val_m, a_data = arm_px[a]
            rd = count_extent(rb_d, val_d, L, rbins, 3)
            rm = count_extent(rb_m, val_m, L_m, rbins, 1)
            arms.append(dict(
                theta_model=a, theta_data=a_data,
                r_data=rd, r_model=rm,
                censored_frame=bool(rd >= rbins[-1] - rbin_as),
                censored_model=bool(rm >= r_model_edge - 2 * rbin_as),
                ok=bool(rm + tol_as >= rd),
            ))
        M = binary_dilation(amp * m_mask > L_m, iterations=max(1, args.grow))
        dth_all = np.full(theta.shape, 360.0)
        for x_ in arms:
            np.minimum(dth_all, np.abs(((theta - x_['theta_data']) + 180)
                                       % 360 - 180), out=dth_all)
        in_wedges = (dth_all <= args.wedge) & (r > r_min_px)
        spike_px = good & in_wedges & ((data - bkg) > L)
        miss = spike_px & ~M
        levels[f'{f:g}'] = dict(
            threshold=float(L), arms=arms,
            n_spike_px=int(spike_px.sum()), n_miss_px=int(miss.sum()),
            ok=bool(all(x['ok'] for x in arms)),
        )

    result = dict(
        x=float(star_yx[1]), y=float(star_yx[0]),
        sigma_bkg=float(sigma), bkg=float(bkg), amplitude=float(amp),
        n_fit_px=int(n_fit), dtheta_deg=float(dtheta), flip=bool(flip),
        arm_angles_model=arm_angles, rbins_arcsec=[float(v) for v in rbins],
        arms=arm_info, levels=levels,
    )
    rasters = dict(m_mask=m_mask, m_phot=m_phot)
    return result, rasters


# ---------------------------------------------------------------------------
# frames + stars

def frame_meta(hdr_pri, hdr_sci):
    pupil = str(hdr_pri.get('PUPIL', '')).strip().upper()
    filt = str(hdr_pri.get('FILTER', '')).strip().upper()
    band = pupil if pupil.startswith('F') and pupil in PIVOT_UM else filt
    if band not in PIVOT_UM:
        raise ValueError(f'unknown filter {filt!r}/{pupil!r}')
    det = str(hdr_pri.get('DETECTOR', '')).strip().upper()
    channel = 'LW' if det.endswith('LONG') or det.endswith('5') else 'SW'
    return band, channel, det


def gaia_stars(wcs, shape, mag_max, margin_deg):
    from campfire_pipeline.nircam.refcat.query import query_gaia
    ny, nx = shape
    ra, dec = [float(v) for v in wcs.all_pix2world([[nx / 2, ny / 2]], 0)[0]]
    rad = margin_deg + math.hypot(nx, ny) / 2 * abs(
        wcs.proj_plane_pixel_scales()[0].value)
    tbl = query_gaia((ra, dec), rad, mag_band='G', mag_max=mag_max)
    return [(float(t['ra']), float(t['dec']), float(t['mag']),
             str(t['source_id'])) for t in tbl]


def plot_star(png, data, res, rasters, star_yx, pixscl, args, title):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    sig, bkg, amp = res['sigma_bkg'], res['bkg'], res['amplitude']
    import itertools
    fig = plt.figure(figsize=(13, 6.5))
    ax = fig.add_subplot(1, 2, 1)
    ax.imshow(np.arcsinh((data - bkg) / sig), origin='lower',
              cmap='gray_r', vmin=-1, vmax=8, interpolation='nearest')
    for f, spec in zip(args.levels, itertools.cycle(('c', 'y', 'r'))):
        ax.contour(amp * rasters['m_mask'], levels=[f * sig],
                   colors=spec, linewidths=0.7)
    rmax_px = res['rbins_arcsec'][-1] / pixscl
    for arm in res['arms']:
        th = math.radians(arm['theta_data'])
        ax.plot([star_yx[1], star_yx[1] + rmax_px * math.cos(th)],
                [star_yx[0], star_yx[0] + rmax_px * math.sin(th)],
                color='tab:blue', lw=0.4, alpha=0.6)
    ax.set_title(f"{title}\namp={amp:.3g} dθ={res['dtheta_deg']:.1f}° "
                 f"flip={res['flip']}")
    ax.set_xlim(star_yx[1] - rmax_px, star_yx[1] + rmax_px)
    ax.set_ylim(star_yx[0] - rmax_px, star_yx[0] + rmax_px)

    arms0 = res['arms']
    rc = 0.5 * (np.array(res['rbins_arcsec'][:-1])
                + np.array(res['rbins_arcsec'][1:]))
    n = len(arms0)
    for i in range(n):
        axp = fig.add_subplot(math.ceil(n / 2), 4,
                              (i // 2) * 4 + 3 + (i % 2))
        pd = np.array([np.nan if v is None else v
                       for v in arms0[i]['profile_data']], dtype=float)
        pm = np.array([np.nan if v is None else v
                       for v in arms0[i]['profile_model']], dtype=float)
        pd[pd <= 0] = np.nan
        pm[pm <= 0] = np.nan
        axp.plot(rc, pd / sig, 'k-', lw=0.8, label='data')
        axp.plot(rc, pm / sig, 'r-', lw=0.8, label='amp×model')
        for f in args.levels:
            axp.axhline(f, color='0.7', lw=0.4)
        axp.set_yscale('log')
        axp.set_ylim(0.5, None)
        axp.set_title(f"arm {arms0[i]['theta_data']:.0f}°", fontsize=7)
        axp.tick_params(labelsize=6)
        if i == 0:
            axp.legend(fontsize=6)
    fig.tight_layout()
    fig.savefig(png, dpi=140)
    plt.close(fig)


def run_frame(frame_path, models, args, stars=None):
    """Process one cal frame; returns the per-frame result dict."""
    from astropy.io import fits
    from astropy.wcs import WCS

    with fits.open(frame_path) as hdul:
        hdr = hdul[0].header
        sci = hdul['SCI']
        data = np.asarray(sci.data, dtype=np.float64)
        wcs = WCS(sci.header, relax=True)
        try:
            dq = np.asarray(hdul['DQ'].data)
        except KeyError:
            dq = np.zeros(data.shape, dtype=np.uint32)
    band, channel, det = frame_meta(hdr, sci.header)
    pivot = PIVOT_UM[band]
    pixscl = float(abs(wcs.proj_plane_pixel_scales()[0].value) * 3600.0)
    good = np.isfinite(data) & ((dq & BAD_DQ) == 0)

    anchor, mods = models[channel]
    lam_scale = pivot / anchor

    if stars is None:
        stars = gaia_stars(wcs, data.shape, args.mag_max, args.margin_deg)
    out = dict(frame=str(frame_path), filter=band, channel=channel,
               detector=det, pixscale=pixscl, anchor_um=anchor,
               lam_scale=lam_scale, stars=[])
    ny, nx = data.shape
    pad = 0.25 * max(ny, nx)
    for ra, dec, gmag, sid in sorted(stars, key=lambda s: s[2]):
        x, y = [float(v) for v in wcs.all_world2pix([[ra, dec]], 0)[0]]
        if not (-pad <= x < nx + pad and -pad <= y < ny + pad):
            continue
        if len(out['stars']) >= args.max_stars:
            break
        got = measure_star(data, good, (y, x), mods, pixscl, lam_scale, args)
        if got is None:
            continue
        res, rasters = got
        res.update(source_id=sid, ra=ra, dec=dec, gmag=gmag)
        out['stars'].append(res)
        if args.out:
            png = (Path(args.out)
                   / f'{Path(frame_path).stem}_G{gmag:.1f}_{sid}.png')
            plot_star(png, data, res, rasters, (y, x), pixscl, args,
                      f'{Path(frame_path).name} {band} G={gmag:.1f}')
    return out


def summarize(frames):
    n_star = n_arm = n_ok = 0
    for fr in frames:
        for st in fr['stars']:
            n_star += 1
            for lv in st['levels'].values():
                for arm in lv['arms']:
                    n_arm += 1
                    n_ok += arm['ok']
    return dict(n_frames=len(frames), n_stars=n_star,
                n_arm_tests=n_arm, n_arm_pass=n_ok)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.split('\n')[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument('--frames', nargs='+', required=True,
                    help='glob(s) of level-2 *_cal.fits frames')
    ap.add_argument('--out', required=True, help='output directory')
    ap.add_argument('--model-dir',
                    help='directory already holding SPIKE_MODEL_*.fits '
                         '(skips the ref_cache fetch)')
    ap.add_argument('--manifest', help='override manifest TOML path')
    ap.add_argument('--stars', nargs='*', metavar='RA,DEC[,G]',
                    help='explicit stars instead of the Gaia query')
    ap.add_argument('--mag-max', type=float, default=15.0,
                    help='Gaia G faint limit')
    ap.add_argument('--margin-deg', type=float, default=0.04,
                    help='cone-search margin beyond the frame (off-frame '
                         'stars whose spikes intrude)')
    ap.add_argument('--levels', type=lambda s: [float(v) for v in
                                                s.split(',')],
                    default=[3.0, 5.0, 10.0],
                    help='isophote thresholds, in sigma_bkg multiples')
    ap.add_argument('--grow', type=int, default=2,
                    help='mask tolerance, native exposure px (design default)')
    ap.add_argument('--r-min', type=float, default=3.0,
                    help='[arcsec] inner radius: exclude core/halo')
    ap.add_argument('--wedge', type=float, default=4.0,
                    help='[deg] arm wedge half-angle')
    ap.add_argument('--arm-lo', type=float, default=15.0,
                    help='[arcsec] arm-angle/orientation annulus, inner')
    ap.add_argument('--arm-hi', type=float, default=60.0,
                    help='[arcsec] arm-angle/orientation annulus, outer')
    ap.add_argument('--max-stars', type=int, default=8,
                    help='brightest N qualifying stars per frame')
    args = ap.parse_args(argv)

    paths = sorted({p for g in args.frames for p in glob.glob(g)})
    if not paths:
        sys.exit('no frames matched')
    Path(args.out).mkdir(parents=True, exist_ok=True)

    manifest = Path(args.manifest) if args.manifest else default_manifest_path()
    entries = read_manifest(manifest)

    stars = None
    if args.stars:
        stars = []
        for spec in args.stars:
            parts = [float(v) for v in spec.split(',')]
            stars.append((parts[0], parts[1],
                          parts[2] if len(parts) > 2 else 99.0, 'manual'))

    results = []
    for p in paths:
        from astropy.io import fits
        with fits.open(p) as hdul:
            band, channel, _ = frame_meta(hdul[0].header,
                                          hdul['SCI'].header)
        anchor, names = pick_anchor(entries, channel, PIVOT_UM[band])
        pathmap = fetch_models(sorted(names.values()), args.model_dir)
        mods = {grade: load_model(pathmap[name])
                for grade, name in names.items()}
        print(f'{Path(p).name}: {band} {channel} -> anchor {anchor} µm')
        fr = run_frame(p, {channel: (anchor, mods)}, args, stars=stars)
        print(f"  {len(fr['stars'])} stars measured")
        results.append(fr)

    report = dict(args={k: v for k, v in vars(args).items()
                        if k not in ('frames',)},
                  summary=summarize(results), frames=results)
    out_json = Path(args.out) / 'g0_results.json'
    out_json.write_text(json.dumps(report, indent=1))
    s = report['summary']
    print(f"wrote {out_json}\n{s['n_stars']} stars, "
          f"{s['n_arm_pass']}/{s['n_arm_tests']} arm-level envelope tests pass")


if __name__ == '__main__':
    main()
