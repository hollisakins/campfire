"""Negativity-constrained background estimation prototypes.

Three mechanisms, composable with the stock SubtractBackground fit:

1. AsymmetricSubtractBackground -- SigmaClip(sigma_upper, sigma_lower) in the
   main Background2D fit (mask held fixed, so arms differ only in the fit).

2. onesided_ceiling() -- maskless coarse Background2D with a hard upper clip.
   Because true flux >= 0, this maskless map is a valid *upper bound* on any
   admissible background even where it tracks (sky + wing). Calibrated
   against the arm's own masked fit on quiet pixels (removes the one-sided
   clip bias empirically, per image), plus k-sigma slack so the ceiling sits
   slightly above true sky (a slack ceiling is costless -- it's a bound).

3. Enforcement:
   - clamp_map():   bkg_final = minimum(bkg, ceiling) at pixel level.
   - cap_mesh():    cap the low-res background *mesh* at the ceiling sampled
     on the mesh grid, then re-run the zoom interpolator -- smooth by
     construction, and it fixes violations *under* fully-masked regions
     where a mask-and-refit pass is a no-op (the interpolator would
     extrapolate identically).
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
from astropy import stats as astrostats
from astropy.nddata import block_reduce
from photutils.background import (
    Background2D,
    BiweightLocationBackground,
    BkgZoomInterpolator,
)

from campfire_pipeline.nircam.bkgsub import SubtractBackground

import metrics as _metrics


@dataclass
class AsymmetricSubtractBackground(SubtractBackground):
    """Main-fit sigma clip made asymmetric: hard on positive contamination,
    loose on the negative side so it anchors the estimate."""

    bg_sigma_upper: float = 2.0
    bg_sigma_lower: float = 10.0

    def _fit_background2d(self, img, mask):
        if self.bg_interpolator != "zoom":
            raise ValueError("prototype supports zoom only")
        return Background2D(
            img,
            box_size=self.bg_box_size,
            sigma_clip=astrostats.SigmaClip(
                sigma_lower=self.bg_sigma_lower,
                sigma_upper=self.bg_sigma_upper,
            ),
            filter_size=self.bg_filter_size,
            bkg_estimator=BiweightLocationBackground(),
            exclude_percentile=self.bg_exclude_percentile,
            mask=mask,
            interpolator=BkgZoomInterpolator(),
        )


# ----------------------------------------------------------------------
# One-sided ceiling map
# ----------------------------------------------------------------------

def wht_sigma_map(
    resid: np.ndarray,
    wht: np.ndarray,
    skymask: np.ndarray,
) -> np.ndarray:
    """Source-independent local sky-noise map from the drizzle weight.

    Background variance is alpha/WHT with alpha a global factor, so
    sigma(x) = s / sqrt(WHT) with s calibrated empirically as the robust
    scale of the noise-equalized residual over sky pixels (``skymask``
    True = usable sky). Unlike ERR this carries no source Poisson term, so
    trough significance is not suppressed (and the ceiling not loosened)
    under bright ICL/wings.
    """
    good = np.isfinite(wht) & (wht > 0)
    sqw = np.where(good, np.sqrt(np.where(good, wht, 0.0)), np.nan)
    sel = skymask & good & np.isfinite(resid)
    s = float(astrostats.biweight_scale((resid * sqw)[sel]))
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(good, s / sqw, np.nan)


def onesided_ceiling_map(
    sci: np.ndarray,
    off: np.ndarray,
    box: int = 64,
    filter_size: int = 3,
    sigma_upper: float = 1.0,
    sigma_lower: float = 10.0,
) -> np.ndarray:
    """Maskless (DQ/off-detector only) coarse background with a hard upper
    clip: tracks the lower envelope of the local flux distribution, which is
    an upper bound on the background up to a (calibratable) clip bias."""
    bkg = Background2D(
        sci,
        box_size=box,
        sigma_clip=astrostats.SigmaClip(
            sigma_lower=sigma_lower, sigma_upper=sigma_upper, maxiters=10
        ),
        filter_size=filter_size,
        bkg_estimator=BiweightLocationBackground(),
        exclude_percentile=95,
        mask=off,
        interpolator=BkgZoomInterpolator(),
    )
    return bkg.background


def calibrate_ceiling(
    onesided: np.ndarray,
    bkgmap: np.ndarray,
    fitmask: np.ndarray,
    k: float = 2.0,
):
    """Empirical per-image bias removal + slack.

    On quiet (unmasked) pixels both the one-sided map and the arm's masked
    symmetric fit estimate the same sky; their robust median difference is
    the one-sided clip bias, and the robust scatter sets the slack unit.
    Returns (ceiling, delta, sigma_diff).
    """
    diff = (onesided - bkgmap)[~fitmask]
    delta = float(astrostats.biweight_location(diff))
    sigma = float(astrostats.biweight_scale(diff))
    ceiling = onesided - delta + k * sigma
    return ceiling, delta, sigma


# ----------------------------------------------------------------------
# Enforcement
# ----------------------------------------------------------------------

def multiscale_ceiling(
    sci: np.ndarray,
    off: np.ndarray,
    bkgmap: np.ndarray,
    fitmask: np.ndarray,
    boxes=(32, 64, 128),
    k: float = 2.0,
    err: Optional[np.ndarray] = None,
):
    """Minimum over independently-calibrated one-sided ceilings at several
    box scales. Each scale is (statistically) an upper bound on the
    background; the min is a tighter bound. Finer scales tighten the bound
    in the sky gaps next to bright masked sources, where a coarse mesh is
    source-embedded and its ceiling useless.

    With ``err`` the whole construction is depth-aware: the one-sided map
    is fit on the noise-equalized image sci/err (so the hard upper clip cuts
    at the same significance at every depth), calibration happens in
    equalized units, and the bound is mapped back through the local err.
    True flux >= 0 makes sci/err an upper bound on bkg/err pixelwise, so
    the bound survives the transformation.
    """
    if err is not None:
        good = np.isfinite(err) & (err > 0)
        e_sci = np.where(good, sci / err, np.nan)
        e_bkg = np.where(good, bkgmap / err, np.nan)
        off_e = off | ~good
        ceils, cal = [], {}
        for box in boxes:
            om = onesided_ceiling_map(e_sci, off_e, box=box)
            c_e, d, s = calibrate_ceiling(om, e_bkg, fitmask | off_e, k=k)
            ceils.append(c_e)
            cal[box] = (d, s)
        ceil_e = np.minimum.reduce(ceils)
        ceiling = np.where(good, ceil_e * err, np.inf)
        return ceiling, cal
    ceils, cal = [], {}
    for box in boxes:
        om = onesided_ceiling_map(sci, off, box=box)
        c, d, s = calibrate_ceiling(om, bkgmap, fitmask, k=k)
        ceils.append(c)
        cal[box] = (d, s)
    return np.minimum.reduce(ceils), cal


def clamp_map(bkgmap: np.ndarray, ceiling: np.ndarray):
    """Pixel-level minimum. Returns (new map, violation mask)."""
    viol = bkgmap > ceiling
    return np.minimum(bkgmap, ceiling), viol


def trough_correction(
    sci: np.ndarray,
    bkgmap: np.ndarray,
    exclude: np.ndarray,
    smooth_sigma: float = 5.0,
    t: float = 2.0,
):
    """Residual-driven trough pass (handoff option C6).

    The residual sci - bkgmap is observable everywhere, including under the
    fit mask, so this catches oversubtraction the maskless ceiling misses
    (meshes that mix wing flux with the trough). Wherever the smoothed
    residual is coherently below -t*sigma_sm, lower the map by
    (sm + t*sigma_sm): continuous at the boundary (no seams), zero over
    >= -t*sigma_sm sky, so the positive-pedestal bias is bounded by the
    t-tail of the smoothed noise. `exclude` should mask compact positive
    sources only -- their segments must not dilute the smoothing.

    Returns (corr, frac_corrected); apply as bkgmap + corr (corr <= 0).
    """
    resid = sci - bkgmap
    sm = _metrics.smooth_masked(resid, exclude, smooth_sigma)
    ok = np.isfinite(sm) & ~exclude
    rms = float(astrostats.biweight_scale(sm[ok]))
    corr = np.minimum(sm + t * rms, 0.0)
    corr[~np.isfinite(corr)] = 0.0
    return corr, float((corr < 0).mean())


def gated_trough_correction(
    sci: np.ndarray,
    bkgmap: np.ndarray,
    exclude: np.ndarray,
    smooth_sigma: float = 5.0,
    t: float = 2.0,
    npix_per_sigma2: float = 8.0,
    err: Optional[np.ndarray] = None,
):
    """Detection-gated trough pass.

    Same correction field as trough_correction, but applied only inside
    *detected* coherent negative regions: connected areas below -t*sigma_sm
    of at least npix_per_sigma2 * smooth_sigma**2 pixels (area threshold
    scales with the smoothing correlation area, so the blank-field
    false-fire rate is scale-independent and small). Because the correction
    field is zero exactly at the -t*sigma_sm isophote, restricting it to
    the detected segments keeps it continuous -- no seams.

    On a blank field this is an exact no-op almost always (regression
    safety); on a real trough it applies the full aggressive correction.
    """
    from photutils.segmentation import detect_sources

    resid = sci - bkgmap
    if err is not None:
        # depth-aware: work on the noise-equalized residual (unit variance
        # everywhere), map the correction back through the local err.
        field = np.where(err > 0, resid / err, np.nan)
    else:
        field = resid
    sm = _metrics.smooth_masked(field, exclude, smooth_sigma)
    ok = np.isfinite(sm) & ~exclude
    rms = float(astrostats.biweight_scale(sm[ok]))
    signif = np.where(ok, sm / rms, 0.0)
    npixels = max(int(npix_per_sigma2 * smooth_sigma**2), 30)
    seg = detect_sources(-signif, threshold=t, npixels=npixels)
    if seg is None:
        return np.zeros_like(bkgmap), 0.0
    gate = seg.make_source_mask()
    corr = np.where(gate, np.minimum(sm + t * rms, 0.0), 0.0)
    corr[~np.isfinite(corr)] = 0.0
    if err is not None:
        corr = corr * np.where(np.isfinite(err), err, 0.0)
    return corr, float(gate.mean())


def iterated_gated_trough(
    sci: np.ndarray,
    bkgmap: np.ndarray,
    exclude: np.ndarray,
    sigmas=(5, 15, 45),
    t: float = 2.0,
    max_iter: int = 12,
    err: Optional[np.ndarray] = None,
    verbose: bool = True,
):
    """Run the gated trough pass to convergence at each scale (a single
    pass under-corrects deep troughs: re-smoothing the corrected image
    erodes the correction peak)."""
    m = bkgmap.copy()
    for s in sigmas:
        for it in range(max_iter):
            corr, frac = gated_trough_correction(
                sci, m, exclude, smooth_sigma=s, t=t, err=err)
            if frac == 0.0:
                break
            m = m + corr
            if verbose:
                print(f"    gated sigma={s} iter={it}: frac={frac:.4f} "
                      f"min={corr.min():.5g}")
    return m


def cap_mesh(bkg2d, ceiling: np.ndarray, shape) -> np.ndarray:
    """Cap the low-res mesh at the ceiling (block-averaged onto the mesh
    grid) and re-run the zoom interpolation. Smooth by construction."""
    mesh = np.asarray(bkg2d.background_mesh, dtype=float)
    box = np.atleast_1d(bkg2d.box_size)
    by, bx = (int(box[0]), int(box[-1]))
    my, mx = mesh.shape
    pady, padx = my * by - shape[0], mx * bx - shape[1]
    cpad = np.pad(ceiling, ((0, pady), (0, padx)), mode="edge")
    cmesh = block_reduce(cpad, (by, bx), func=np.mean)
    capped = np.minimum(mesh, cmesh)
    interp = BkgZoomInterpolator()
    newmap = interp(capped, box_size=(by, bx), shape=shape, dtype=float)
    n_capped = int((mesh > cmesh).sum())
    return newmap, n_capped, mesh.size
