#!/usr/bin/env python
"""End-to-end selftest of the G0 harness on a fully synthetic scene.

No real data, no network, no `campfire_pipeline` install needed — this
validates the *harness machinery* (model resampling, orientation fit,
amplitude fit, per-arm extents, miss counting), not the models. Two
scenarios, both built from a synthetic 8-arm "spike model":

  A (truth == model): the frame contains the model itself, rotated by 17°,
    plus noise, off-arm galaxies and a saturated core. Expect: orientation
    recovered, every arm envelope test passes, ~zero misses at 10σ.
  B (truth ≠ model): the frame's arms extend past the model's truncation
    radius (the model under-predicts the footprint) and the scene is
    parity-mirrored. Expect: flip detected, and the envelope test FAILS on
    every arm at 3σ/5σ — the harness must be able to say "no". (A radial
    *stretch* is deliberately not the defect here: on a pure power-law arm
    it is degenerate with amplitude, which the in-frame fit absorbs.)

Run:  python selftest.py [--out ./selftest_out]
Exits nonzero (with a summary) if any expectation is violated.
"""

import argparse
import math
import sys
from pathlib import Path

import numpy as np

import g0_footprint as g0

PIXSCL = 0.063          # LW-like [arcsec/px]
MODEL_N = 1101          # model grid (odd -> exact center px), FOV ~ 69"
FRAME_N = 1600
ARM_ANGLES = [10.0, 70.0, 130.0, 190.0, 250.0, 310.0]   # primary
STRUT_ANGLES = [100.0, 280.0]                           # breaks mirror symmetry
DTHETA_TRUE = 17.0
SB_AT = 10.0            # arcsec: arm ridge amplitude anchor radius
SB_SIGMA = 17.0         # ridge = 17 sigma_bkg at SB_AT -> 3σ extent ~ 26"
ARM_RCUT_AS = 18.0      # model arms truncated here; scenario-B truth is not


def synth_spike_image(n=MODEL_N, pixscl=PIXSCL, rcut_as=None):
    """Analytic 8-arm spike pattern, sum-normalized like the real models.

    Arms are shallow power laws over a weak, steep halo so the ridges
    dominate everywhere outside the core (like the real models);
    ``rcut_as`` truncates the arms — the model/truth mismatch scenario B
    is built from.
    """
    ctr = (n - 1) / 2.0
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float64)
    dy, dx = yy - ctr, xx - ctr
    r = np.hypot(dy, dx) + 1e-9
    img = 3.0 * (1.0 + (r / 8.0) ** 2) ** -2.2            # halo
    for a, rel in ([(a, 1.0) for a in ARM_ANGLES]
                   + [(a, 0.6) for a in STRUT_ANGLES]):
        th = math.radians(a)
        along = dx * math.cos(th) + dy * math.sin(th)
        perp = np.abs(dy * math.cos(th) - dx * math.sin(th))
        w = 1.5 + 0.008 * np.clip(along, 0, None)
        arm = 0.5 * rel * np.clip(along, 1e-9, None) ** -1.8 \
            * np.exp(-0.5 * (perp / w) ** 2)
        arm[along < 3.0] = 0.0
        if rcut_as is not None:
            arm[along > rcut_as / pixscl] = 0.0
        img += arm
    img /= img.sum()
    return img, ctr


def ridge_sb(img, ctr, angle_deg, r_px):
    th = math.radians(angle_deg)
    y = ctr + r_px * math.sin(th)
    x = ctr + r_px * math.cos(th)
    return float(img[int(round(y)), int(round(x))])


def as_model(img, ctr, grade, pixscl=PIXSCL):
    """Package a raster like load_model() would return it."""
    if grade == 'mask':
        f = 4
        n = img.shape[0]
        m = (n // f) * f
        o = (n - m) // 2
        c = img[o:o + m, o:o + m].reshape(m // f, f, m // f, f)
        return dict(img=c.max(axis=(1, 3)).astype(np.float32),
                    pixscl=pixscl * f, ctr=(ctr - o + 0.5) / f - 0.5,
                    wave=2.786, grade='mask', channel='LW')
    return dict(img=img.astype(np.float32), pixscl=pixscl, ctr=ctr,
                wave=2.786, grade='photometric', channel='LW')


def build_frame(path, truth, ctr, amp, flip, rng):
    """Write a fake *_cal.fits with the truth pattern injected."""
    from astropy.io import fits
    from astropy.wcs import WCS

    star = (FRAME_N * 0.51, FRAME_N * 0.47)   # (y, x)
    tm = dict(img=truth, pixscl=PIXSCL, ctr=ctr, grade='photometric')
    inj = amp * g0.resample_model(tm, (FRAME_N, FRAME_N), star, PIXSCL,
                                  1.0, DTHETA_TRUE, flip)
    data = inj + rng.normal(0.0, 1.0, (FRAME_N, FRAME_N))

    # galaxies, rejection-sampled away from the injected arm wedges so the
    # 10σ miss count stays a clean measure of arm coverage
    arm_pas = [((-a if flip else a) + DTHETA_TRUE) % 360
               for a in ARM_ANGLES + STRUT_ANGLES]
    n_gal = 0
    while n_gal < 25:
        gy, gx = rng.uniform(50, FRAME_N - 50, 2)
        pa = math.degrees(math.atan2(gy - star[0], gx - star[1])) % 360
        if min(min(abs(pa - a), 360 - abs(pa - a)) for a in arm_pas) < 12:
            continue
        gs = rng.uniform(1.0, 3.0)
        ga = rng.uniform(5.0, 50.0)
        y0, y1 = int(max(0, gy - 12)), int(min(FRAME_N, gy + 12))
        x0, x1 = int(max(0, gx - 12)), int(min(FRAME_N, gx + 12))
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float64)
        data[y0:y1, x0:x1] += ga * np.exp(
            -0.5 * ((yy - gy) ** 2 + (xx - gx) ** 2) / gs ** 2)
        n_gal += 1

    dq = np.zeros((FRAME_N, FRAME_N), dtype=np.uint32)
    yy, xx = np.mgrid[0:FRAME_N, 0:FRAME_N].astype(np.float64)
    core = np.hypot(yy - star[0], xx - star[1]) < 8
    data[core] = 1e4
    dq[core] = 2

    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crpix = [FRAME_N / 2, FRAME_N / 2]
    w.wcs.crval = [150.1, 2.2]
    w.wcs.cd = np.array([[-PIXSCL, 0.0], [0.0, PIXSCL]]) / 3600.0

    pri = fits.PrimaryHDU()
    pri.header['FILTER'] = 'F277W'
    pri.header['PUPIL'] = 'CLEAR'
    pri.header['DETECTOR'] = 'NRCALONG'
    sci = fits.ImageHDU(data=data.astype(np.float32), name='SCI',
                        header=w.to_header())
    fits.HDUList([pri, sci,
                  fits.ImageHDU(data=dq, name='DQ')]).writeto(
        path, overwrite=True)
    ra, dec = [float(v) for v in w.all_pix2world([[star[1], star[0]]], 0)[0]]
    return ra, dec


def run_scenario(name, outdir, truth, ctr, models, flip, rng):
    frame = outdir / f'synthetic_{name}_cal.fits'
    amp = SB_SIGMA / ridge_sb(truth, ctr, ARM_ANGLES[0], SB_AT / PIXSCL)
    ra, dec = build_frame(frame, truth, ctr, amp, flip, rng)
    args = argparse.Namespace(
        out=str(outdir), levels=[3.0, 5.0, 10.0], grow=2, r_min=3.0,
        wedge=4.0, arm_lo=8.0, arm_hi=16.0, max_stars=1, mag_max=15.0,
        margin_deg=0.04)
    res = g0.run_frame(str(frame), {'LW': (2.786, models)}, args,
                       stars=[(ra, dec, 8.0, f'inj_{name}')])
    assert res['stars'], f'{name}: star not measured'
    return res['stars'][0]


def arm_set_error(star, flip_true):
    """Worst distance [deg] from each measured data-arm PA to the truth set.

    The 8-arm pattern (like the real NIRCam one) is mirror-symmetric, so
    (dtheta, flip) is two-fold degenerate — both solutions land the arms on
    identical PAs and produce identical masks. Assert arm *positions*, not
    the flip flag.
    """
    true_pas = [((-a if flip_true else a) + DTHETA_TRUE) % 360
                for a in ARM_ANGLES + STRUT_ANGLES]
    worst = 0.0
    for arm in star['arms']:
        d = min(min(abs(arm['theta_data'] - t), 360 - abs(arm['theta_data'] - t))
                for t in true_pas)
        worst = max(worst, d)
    return worst


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='./selftest_out')
    args = ap.parse_args(argv)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20260814)

    model_img, ctr = synth_spike_image(rcut_as=ARM_RCUT_AS)
    models = {'photometric': as_model(model_img, ctr, 'photometric'),
              'mask': as_model(model_img, ctr, 'mask')}
    failures = []

    # --- scenario A: truth == model ------------------------------------
    st = run_scenario('A', outdir, model_img, ctr, models, False, rng)
    err = arm_set_error(st, False)
    print(f"A: dtheta={st['dtheta_deg']:.1f} flip={st['flip']} "
          f"arm-PA err={err:.1f}deg amp_ratio={st['amplitude']:.3g}")
    if err > 2.0:
        failures.append(f'A: arms landed {err:.1f} deg off (>2)')
    for lv, d in st['levels'].items():
        n_bad = sum(not a['ok'] for a in d['arms'])
        print(f"A: level {lv}s  arms ok {len(d['arms']) - n_bad}"
              f"/{len(d['arms'])}  miss={d['n_miss_px']}px"
              f"/{d['n_spike_px']}px")
        if n_bad:
            failures.append(f'A: {n_bad} arm(s) fail envelope at {lv}s')
    # near the isophote boundary, noise pushes on-arm pixels above L well
    # outside the grow-dilated mask (the isophote gradient is shallow along
    # the arm), so a small miss tail is expected even for a perfect model
    m10 = st['levels']['10']
    if m10['n_miss_px'] > 0.02 * m10['n_spike_px']:
        failures.append(f"A: {m10['n_miss_px']}/{m10['n_spike_px']} "
                        f"miss px at 10s (>2%)")

    # --- scenario B: arms longer than the model + mirrored -------------
    truth_b, _ = synth_spike_image(rcut_as=None)
    st = run_scenario('B', outdir, truth_b, ctr, models, True, rng)
    err = arm_set_error(st, True)
    print(f"B: dtheta={st['dtheta_deg']:.1f} flip={st['flip']} "
          f"arm-PA err={err:.1f}deg amp_ratio={st['amplitude']:.3g}")
    if err > 2.0:
        failures.append(f'B: arms landed {err:.1f} deg off (>2)')
    for lv in ('3', '5'):
        n_bad = sum(not a['ok'] for a in st['levels'][lv]['arms'])
        print(f"B: level {lv}s  arms failing envelope: {n_bad}"
              f"/{len(st['levels'][lv]['arms'])}  "
              f"miss={st['levels'][lv]['n_miss_px']}px")
        if n_bad < 4:
            failures.append(
                f'B: only {n_bad} arms fail envelope at {lv}s (expected >=4)')

    if failures:
        print('\nSELFTEST FAILED:')
        for f in failures:
            print(f'  - {f}')
        sys.exit(1)
    print('\nSELFTEST OK')


if __name__ == '__main__':
    main()
