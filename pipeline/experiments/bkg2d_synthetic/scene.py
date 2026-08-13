"""Synthetic NIRCam scene generator for the bkg2d (subtract_2d) validation.

Every scene is built as separate truth planes that sum to the frame:

    image = sky + galaxies + icl + noise

``galaxies`` and ``icl`` are independent planes, so "did we lose galaxy flux"
and "did we remove ICL" are independent integrals of the same correction-error
map (see metrics.py) — no BCG/ICL decomposition ever has to be fit. The split
is *by construction*: every compact/moderate Sersic component goes to the
galaxy plane (flux we must conserve); the extended envelopes (cluster ICL +
the BCG outer envelope) go to the ICL plane (flux we accept losing).

Morphologies are Sersic profiles (astropy Sersic2D — no new dependency):
log-normal sizes (most compact), power-law fluxes with a size-flux
correlation, a 70/30 disk/spheroid n mixture with the brightest sources
biased to high n (their wings are the hard case), Gaussian PSF (FWHM ~2 px in
native pixels for both channels). Each source's truth flux is the *rendered,
PSF-convolved, clipped stamp sum* — recovery is measured against what was
actually put in the pixels, aperture-to-aperture, never against an analytic
total. Sub-threshold sources are included on purpose: undetected faint
galaxies are the irreducible leaked-flux floor in real data too.

Units are sigma-relative (sky = 1.0, sigma_pix = 0.05 by default, matching
the pipeline test conventions) — the step cares about contrast ratios, not
MJy/sr. Channel scaling: 'lw' halves all angular sizes in px (native 63 vs
31 mas); the PSF FWHM stays ~2 px in native pixels for both.

Diffraction spikes are deliberately out of scope (separable failure mode).
"""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from astropy.modeling.functional_models import Sersic2D
from scipy.ndimage import gaussian_filter, gaussian_filter1d

PSF_FWHM_PX = 2.0            # native-px FWHM, both channels (F200W/F444W-ish)
REF_BORDER = 4               # reference-pixel border, off-detector like real
                             # cal frames (NIR_AMPS covers rows/cols 4..2044)
_CHANNEL_SCALE = {'sw': 1.0, 'lw': 0.5}


@dataclass
class Source:
    """One rendered component. kind='galaxy' components carry conservation
    requirements; kind='icl' components are accepted losses."""
    x: float                 # canvas coords of the profile center
    y: float
    flux: float              # sum of the placed (clipped) stamp — the truth
    r_e: float               # px, post channel scaling
    n: float
    q: float
    pa: float
    kind: str                # 'galaxy' | 'icl'
    peak: float = 0.0
    slices: Optional[tuple] = None       # canvas slices of the placed stamp
    stamp: Optional[np.ndarray] = None   # the placed (clipped) stamp


@dataclass
class Scene:
    shape: tuple
    channel: str
    sigma: float
    seed: int
    preset: str
    sky: np.ndarray = field(repr=False, default=None)
    galaxies: np.ndarray = field(repr=False, default=None)
    icl: np.ndarray = field(repr=False, default=None)
    noise: np.ndarray = field(repr=False, default=None)
    # detector systematics the step MUST remove (per-amp DC + 1/f banding +
    # column stripes); zeros unless inject_1f. Counted as true background in
    # the metrics, like sky.
    stripes: np.ndarray = field(repr=False, default=None)
    sources: list = field(default_factory=list)

    @property
    def scale(self):
        return _CHANNEL_SCALE[self.channel]

    @property
    def valid(self):
        """On-detector pixels (False on the reference-pixel border)."""
        v = np.zeros(self.shape, bool)
        v[REF_BORDER:-REF_BORDER, REF_BORDER:-REF_BORDER] = True
        return v

    @property
    def image(self):
        img = (self.sky + self.galaxies + self.icl + self.stripes
               + self.noise).astype(np.float32)
        img[~self.valid] = 0.0
        return img

    @property
    def err(self):
        e = np.full(self.shape, self.sigma, np.float32)
        e[~self.valid] = np.nan
        return e


def _sample_powerlaw(rng, size, lo, hi, alpha):
    """Inverse-CDF draw from dN/dS ~ S^-alpha on [lo, hi]."""
    u = rng.random(size)
    a1 = 1.0 - alpha
    return (lo ** a1 + u * (hi ** a1 - lo ** a1)) ** (1.0 / a1)


def render_sersic_stamp(rng, flux, r_e, n, q, pa, psf_fwhm=PSF_FWHM_PX,
                        trunc=8.0, max_half=600):
    """Render one PSF-convolved Sersic stamp normalized to ``flux``.

    Returns (stamp, dx, dy) where (dx, dy) is the sub-pixel center jitter
    relative to the stamp's central pixel. The n>~2 cusp is refined on an
    8x-oversampled central square (pixel-mean, so surface brightness is
    conserved); normalization happens after PSF convolution so the stamp sum
    *is* the truth flux.
    """
    psf_sigma = psf_fwhm / 2.355
    half = int(np.ceil(max(trunc * r_e, 6 * psf_sigma)))
    half = min(half, max_half)
    side = 2 * half + 1
    dx, dy = rng.uniform(-0.5, 0.5, 2)
    x0, y0 = half + dx, half + dy

    mod = Sersic2D(amplitude=1.0, r_eff=r_e, n=n, x_0=x0, y_0=y0,
                   ellip=1.0 - q, theta=pa)
    yy, xx = np.mgrid[0:side, 0:side]
    stamp = mod(xx, yy)

    # refine the cusp: 8x subgrid on the central square, block-averaged
    ch = min(16, half)                     # central half-size to refine
    os = 8
    lo_y, lo_x = half - ch, half - ch
    n_fine = (2 * ch + 1) * os
    fy = lo_y - 0.5 + (np.arange(n_fine) + 0.5) / os
    fx = lo_x - 0.5 + (np.arange(n_fine) + 0.5) / os
    fine = mod(fx[None, :], fy[:, None])
    fine = fine.reshape(2 * ch + 1, os, 2 * ch + 1, os).mean(axis=(1, 3))
    stamp[lo_y:lo_y + 2 * ch + 1, lo_x:lo_x + 2 * ch + 1] = fine

    stamp = gaussian_filter(stamp, psf_sigma, mode='constant')
    stamp *= flux / stamp.sum()
    return stamp.astype(np.float64), dx, dy


def _place(canvas, stamp, iy, ix):
    """Add ``stamp`` centered at integer canvas position; return the placed
    (clipped) stamp and its canvas slices."""
    H, W = canvas.shape
    half = stamp.shape[0] // 2
    y0, y1 = iy - half, iy + half + 1
    x0, x1 = ix - half, ix + half + 1
    sy0, sx0 = max(0, -y0), max(0, -x0)
    y0, x0 = max(0, y0), max(0, x0)
    y1, x1 = min(H, y1), min(W, x1)
    sub = stamp[sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)]
    canvas[y0:y1, x0:x1] += sub
    return sub, (slice(y0, y1), slice(x0, x1))


def _draw_population(rng, n_gal, scale, sigma):
    """Field-galaxy population: (flux, r_e, n, q, pa) arrays.

    Fluxes in image units (sigma-scaled), power law alpha=1.8 over
    [10, 1e5] sigma. Sizes log-normal with a size-flux correlation
    (median 4 px SW at 100 sigma, 0.3 dex per flux dex, 0.25 dex scatter) —
    most sources compact, the extended tail rare and bright.
    """
    flux_sig = _sample_powerlaw(rng, n_gal, 10.0, 1e5, alpha=1.8)
    r_e = 4.0 * (flux_sig / 100.0) ** 0.3 * 10 ** rng.normal(0, 0.25, n_gal)
    r_e = np.clip(r_e, 1.2, 60.0) * scale
    n_idx = np.where(rng.random(n_gal) < 0.7,
                     rng.uniform(0.8, 1.5, n_gal),
                     rng.uniform(2.5, 4.5, n_gal))
    n_idx = np.where(flux_sig > 1e4, rng.uniform(3.0, 4.5, n_gal), n_idx)
    q = rng.uniform(0.3, 1.0, n_gal)
    pa = rng.uniform(0, np.pi, n_gal)
    return flux_sig * sigma, r_e, n_idx, q, pa


def _corr_noise(rng, n, scale):
    """Unit-variance correlated 1-D noise (Gaussian-smoothed white noise)."""
    x = gaussian_filter1d(rng.normal(0, 1, n), scale)
    s = x.std()
    return x / s if s > 0 else x


def _inject_stripes(rng, shape, sigma, amp_dc_sigma, banding_sigma,
                    colstripe_sigma):
    """Detector-systematics truth plane: per-amp DC offsets, per-amp two-scale
    amp-row banding (slow ~8-row + fast ~2-row, the rho~20/rho~5 GP targets),
    and full-width column stripes. Amplitudes are in sigma_pix units.
    """
    H, W = shape
    stripes = np.zeros(shape, np.float64)
    for c0 in range(0, W, 512):
        c1 = min(c0 + 512, W)
        dc = rng.normal(0, amp_dc_sigma * sigma)
        band = (0.8 * _corr_noise(rng, H, 8.0)
                + 0.6 * _corr_noise(rng, H, 2.0))
        band *= banding_sigma * sigma / band.std()
        stripes[:, c0:c1] += dc + band[:, None]
    vcol = _corr_noise(rng, W, 1.5) * colstripe_sigma * sigma
    stripes += vcol[None, :]
    return stripes


def _render_fullframe_sersic(shape, x0, y0, r_e, n, q, pa, peak):
    """Full-frame Sersic evaluation normalized to ``peak`` at its maximum
    (used for the smooth ICL envelopes; no oversampling needed at r_e >> px).
    """
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    mod = Sersic2D(amplitude=1.0, r_eff=r_e, n=n, x_0=x0, y_0=y0,
                   ellip=1.0 - q, theta=pa)
    plane = mod(xx, yy)
    return plane * (peak / plane.max())


def make_scene(preset='cluster', shape=(2048, 2048), channel='sw', seed=0,
               sigma=0.05, sky_level=1.0, grad_amp=0.2, n_gal=500,
               icl_peak_sigma=0.6, margin=24, inject_1f=False,
               amp_dc_sigma=0.5, banding_sigma=0.3, colstripe_sigma=0.15,
               n_bright=5, bright_flux_range=(2e5, 2e6),
               bright_halo_peak_sigma=2.0,
               sky_patch_amp=0.0, sky_patch_scale=400.0):
    """Build a Scene.

    preset='blank': uniform field population, no ICL plane.
    preset='cluster': adds radially-concentrated bright members, a two-
      component BCG (inner n=4 -> galaxy plane; outer envelope -> ICL plane)
      and a large ICL envelope (peak ``icl_peak_sigma`` * sigma per px,
      i.e. ~3% of sky at the defaults — mu ~ 26-28 mag/arcsec^2 territory).
    preset='brightfield': the amp-row-artifact scene (the "cluster outskirts"
      look of e.g. jw03073 A2744 frames): ``n_bright`` very bright extended
      ellipticals scattered across the frame — the first few deliberately
      near amp boundaries (cols 512/1024/1536), since a source *spanning*
      amps is the artifact's trigger — each with a compact n~3-4 body
      (galaxy plane) plus a broad n~1 halo envelope rendered full-frame into
      the ICL plane (peak ``bright_halo_peak_sigma`` * sigma; accepted loss
      under subtract_2d, exactly the flux whose *row-wise* misattribution is
      the artifact under study).
    grad_amp: linear sky gradient amplitude across the frame, in sky units.
    sky_patch_amp: adds a smooth "complex" background component — a Gaussian
      random field (white noise smoothed at ``sky_patch_scale`` px, unit-std
      normalized) times this amplitude in sky units. This is the structure
      subtract_2d exists to remove; 0 disables (legacy scenes unchanged).
    inject_1f: add detector systematics (per-amp DC offsets + amp-row
      banding + column stripes; amplitudes in sigma_pix units) as a separate
      ``stripes`` truth plane — required to exercise the GP / per-amp-DC
      interplay, which is invisible on stripe-free scenes.
    """
    if channel not in _CHANNEL_SCALE:
        raise ValueError(f"channel must be 'sw' or 'lw', got {channel!r}")
    rng = np.random.default_rng(seed)
    H, W = shape
    scale = _CHANNEL_SCALE[channel]

    scene = Scene(shape=shape, channel=channel, sigma=sigma, seed=seed,
                  preset=preset)

    # --- sky: level + linear gradient along a random direction -------------
    yy, xx = np.mgrid[0:H, 0:W]
    ang = rng.uniform(0, 2 * np.pi)
    ramp = (np.cos(ang) * xx + np.sin(ang) * yy) / max(H, W)
    scene.sky = (sky_level + grad_amp * (ramp - ramp.mean())).astype(
        np.float64)
    if sky_patch_amp > 0:
        # smooth "complex" component: unit-std Gaussian random field at the
        # patch scale (pad-free: smooth a larger grid and crop the center so
        # the field statistics don't taper at the frame edge)
        pad = int(2 * sky_patch_scale)
        grf = gaussian_filter(rng.normal(0, 1, (H + 2 * pad, W + 2 * pad)),
                              sky_patch_scale, mode='wrap')
        grf = grf[pad:pad + H, pad:pad + W]
        grf = (grf - grf.mean()) / grf.std()
        scene.sky += sky_patch_amp * grf

    scene.galaxies = np.zeros(shape, np.float64)
    scene.icl = np.zeros(shape, np.float64)

    # --- field population ---------------------------------------------------
    flux, r_e, n_idx, q, pa = _draw_population(rng, n_gal, scale, sigma)
    px = rng.integers(margin, W - margin, n_gal)
    py = rng.integers(margin, H - margin, n_gal)

    comps = [(flux[i], r_e[i], n_idx[i], q[i], pa[i],
              int(px[i]), int(py[i]), 'galaxy') for i in range(n_gal)]

    cy, cx = H // 2, W // 2
    if preset == 'cluster':
        # bright members, ~r^-1 projected concentration around the center;
        # radial extent capped to the frame so small (--quick) frames don't
        # squash the whole cluster into one crowded band
        n_mem = 20
        r_hi = min(600.0 * scale, min(H, W) / 3.0)
        r_mem = _sample_powerlaw(rng, n_mem, 30.0, r_hi, alpha=2.0)
        th = rng.uniform(0, 2 * np.pi, n_mem)
        mx = np.clip(cx + r_mem * np.cos(th), margin, W - margin).astype(int)
        my = np.clip(cy + r_mem * np.sin(th), margin, H - margin).astype(int)
        mflux = _sample_powerlaw(rng, n_mem, 1e3, 1e5, alpha=1.5) * sigma
        mre = rng.uniform(10, 40, n_mem) * scale
        mn = rng.uniform(2.5, 4.5, n_mem)
        mq = rng.uniform(0.5, 1.0, n_mem)
        mpa = rng.uniform(0, np.pi, n_mem)
        comps += [(mflux[i], mre[i], mn[i], mq[i], mpa[i],
                   mx[i], my[i], 'galaxy') for i in range(n_mem)]
        # BCG inner component — galaxy plane (flux we must conserve)
        comps.append((2e5 * sigma, 25.0 * scale, 4.0, 0.8,
                      rng.uniform(0, np.pi), cx, cy, 'galaxy'))

    bright = []
    if preset == 'brightfield':
        # a few very bright extended ellipticals scattered over the frame,
        # the first few pinned near amp boundaries so they span amps
        bounds = [512, 1024, 1536]
        bx, by = [], []
        for i in range(n_bright):
            if i < len(bounds):
                x = int(np.clip(bounds[i] + rng.integers(-140, 140),
                                margin, W - margin))
            else:
                x = int(rng.integers(margin, W - margin))
            bx.append(x)
            by.append(int(rng.integers(H // 8, H - H // 8)))
        lo, hi = bright_flux_range
        bflux = _sample_powerlaw(rng, n_bright, lo, hi, alpha=1.5) * sigma
        bre = rng.uniform(15, 45, n_bright) * scale
        bn = rng.uniform(2.8, 4.5, n_bright)
        bq = rng.uniform(0.55, 0.9, n_bright)
        bpa = rng.uniform(0, np.pi, n_bright)
        comps += [(bflux[i], bre[i], bn[i], bq[i], bpa[i],
                   bx[i], by[i], 'galaxy') for i in range(n_bright)]
        bright = [(bx[i], by[i], bre[i], bq[i], bpa[i], bflux[i])
                  for i in range(n_bright)]

    for f, re_i, ni, qi, pai, ix, iy, kind in comps:
        stamp, dx, dy = render_sersic_stamp(rng, f, re_i, ni, qi, pai)
        plane = scene.galaxies if kind == 'galaxy' else scene.icl
        sub, slc = _place(plane, stamp, iy, ix)
        scene.sources.append(Source(
            x=ix + dx, y=iy + dy, flux=float(sub.sum()), r_e=re_i, n=ni,
            q=qi, pa=pai, kind=kind, peak=float(sub.max()),
            slices=slc, stamp=sub.astype(np.float32)))

    if preset == 'cluster':
        # ICL plane: large envelope + BCG outer envelope (accepted losses).
        # Rendered full-frame; recorded as sources with stamp=None (their
        # truth is the plane integral, not an aperture).
        for re_i, ni, qi, pai, peak in [
                (300.0 * scale, 1.5, 0.7, rng.uniform(0, np.pi),
                 icl_peak_sigma * sigma),
                (120.0 * scale, 1.0, 0.8, rng.uniform(0, np.pi),
                 2.0 * icl_peak_sigma * sigma)]:
            plane = _render_fullframe_sersic(shape, cx, cy, re_i, ni, qi,
                                             pai, peak)
            scene.icl += plane
            scene.sources.append(Source(
                x=cx, y=cy, flux=float(plane.sum()), r_e=re_i, n=ni, q=qi,
                pa=pai, kind='icl', peak=float(peak)))

    if preset == 'brightfield':
        # halo envelopes of the bright galaxies -> ICL plane (accepted loss
        # under subtract_2d; the artifact under study is their row-wise
        # misattribution, not their removal). Peak scales weakly with the
        # body flux so the brightest galaxies carry the biggest halos.
        fmax = max(f for *_, f in bright)
        for hx, hy, re_i, qi, pai, f in bright:
            peak = bright_halo_peak_sigma * sigma * (f / fmax) ** 0.5
            plane = _render_fullframe_sersic(shape, hx, hy, 4.0 * re_i, 1.0,
                                             qi, pai, peak)
            scene.icl += plane
            scene.sources.append(Source(
                x=hx, y=hy, flux=float(plane.sum()), r_e=4.0 * re_i, n=1.0,
                q=qi, pa=pai, kind='icl', peak=float(peak)))

    if inject_1f:
        scene.stripes = _inject_stripes(rng, shape, sigma, amp_dc_sigma,
                                        banding_sigma, colstripe_sigma)
    else:
        scene.stripes = np.zeros(shape, np.float64)

    scene.noise = rng.normal(0, sigma, shape)
    return scene
