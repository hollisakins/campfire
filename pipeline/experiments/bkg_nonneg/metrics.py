"""Negativity-validation metrics for background subtraction.

Everything here is judged on *statistical* negativity: individual negative
pixels at the noise level are correct; the failure mode is a region whose
mean is significantly below zero on scales larger than the noise
correlation length. Drizzled mosaics have correlated noise, so all
significance here is empirical (measured from the map itself), never a
naive per-pixel sigma scaling.

Independent of the pipeline's own source mask by design: the compact-source
exclusion mask is rebuilt here with a simple positive-only detector, so
metrics sample the dilated/interpolated rings where oversubtraction lives.
"""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from astropy import stats as astrostats
from photutils.segmentation import detect_sources
from scipy.ndimage import distance_transform_edt, gaussian_filter


# ----------------------------------------------------------------------
# Source exclusion mask (metrics-internal, independent of pipeline mask)
# ----------------------------------------------------------------------

def compact_source_mask(
    sci: np.ndarray,
    off: np.ndarray,
    nsigma: float = 2.0,
    smooth_sigma: float = 2.0,
    npixels: int = 8,
    dilate: float = 5.0,
) -> np.ndarray:
    """Positive-only compact-source segments with light dilation.

    Deliberately *not* the pipeline's tiered mask: no heavy dilation, so the
    wing/ring regions the pipeline interpolates across remain available to
    the aperture metrics. Detection is on a lightly smoothed image against a
    robust global RMS.
    """
    sm = gaussian_filter(np.where(off, 0.0, sci), smooth_sigma, mode="reflect")
    rms = astrostats.biweight_scale(sm[~off])
    lvl = astrostats.biweight_location(sm[~off])
    seg = detect_sources(sm, threshold=lvl + nsigma * rms, npixels=npixels,
                         mask=off)
    if seg is None:
        return np.zeros(sci.shape, bool)
    m = seg.make_source_mask()
    if dilate > 0:
        m = distance_transform_edt(~m) <= dilate
    return m


# ----------------------------------------------------------------------
# Empty-aperture statistics
# ----------------------------------------------------------------------

@dataclass
class ApertureStats:
    diameter_px: float
    n: int
    fluxes: np.ndarray            # aperture sums
    dists: np.ndarray             # center distance from reference point (px)
    median: float = 0.0
    sigma: float = 0.0            # robust width of the flux distribution
    mean_signif: float = 0.0      # median / (sigma / sqrt(n)) -- crude
    frac_below_m3sig: float = 0.0
    frac_above_p3sig: float = 0.0

    def finalize(self):
        self.n = len(self.fluxes)
        if self.n == 0:
            return self
        self.median = float(np.median(self.fluxes))
        self.sigma = float(astrostats.biweight_scale(self.fluxes))
        if self.sigma > 0 and self.n > 1:
            self.mean_signif = float(
                self.median / (self.sigma / np.sqrt(self.n))
            )
            self.frac_below_m3sig = float(
                np.mean(self.fluxes < -3 * self.sigma))
            self.frac_above_p3sig = float(
                np.mean(self.fluxes > 3 * self.sigma))
        return self


def empty_aperture_stats(
    resid: np.ndarray,
    exclude: np.ndarray,
    diameters_px=(10, 20, 33, 50),
    n_target: int = 2000,
    rng: Optional[np.random.Generator] = None,
    ref_center=None,
) -> dict:
    """Random-aperture flux distributions on the residual image.

    Apertures are fully contained in ~exclude (checked via the Euclidean
    distance transform, so placement is O(1) per try). Overlap between
    apertures is allowed -- we want dense sampling near the bright galaxy,
    and the summary statistics are robust.
    """
    if rng is None:
        rng = np.random.default_rng(42)
    ny, nx = resid.shape
    if ref_center is None:
        ref_center = (ny / 2, nx / 2)
    edt = distance_transform_edt(~exclude)
    out = {}
    yy, xx = np.mgrid[0:ny, 0:nx]
    for d in diameters_px:
        r = d / 2.0
        valid = edt > r + 1
        # keep away from the image edge
        valid[: int(r) + 1, :] = False
        valid[-(int(r) + 1):, :] = False
        valid[:, : int(r) + 1] = False
        valid[:, -(int(r) + 1):] = False
        vy, vx = np.nonzero(valid)
        if len(vy) == 0:
            out[d] = ApertureStats(d, 0, np.array([]), np.array([])).finalize()
            continue
        pick = rng.integers(0, len(vy), size=min(n_target, len(vy) * 4))
        cy, cx = vy[pick], vx[pick]
        fluxes = np.empty(len(cy))
        for i, (y0, x0) in enumerate(zip(cy, cx)):
            y1, y2 = int(y0 - r), int(np.ceil(y0 + r)) + 1
            x1, x2 = int(x0 - r), int(np.ceil(x0 + r)) + 1
            sub = resid[y1:y2, x1:x2]
            dy = yy[y1:y2, x1:x2] - y0
            dx = xx[y1:y2, x1:x2] - x0
            circ = (dy * dy + dx * dx) <= r * r
            fluxes[i] = np.nansum(sub[circ])
        dists = np.hypot(cy - ref_center[0], cx - ref_center[1])
        out[d] = ApertureStats(d, len(fluxes), fluxes, dists).finalize()
    return out


def radial_aperture_profile(ap: ApertureStats, bins_px) -> dict:
    """Median aperture flux and robust scatter vs distance from reference."""
    med, sig, n, cen = [], [], [], []
    for lo, hi in zip(bins_px[:-1], bins_px[1:]):
        sel = (ap.dists >= lo) & (ap.dists < hi)
        cen.append(0.5 * (lo + hi))
        n.append(int(sel.sum()))
        if sel.sum() > 5:
            med.append(float(np.median(ap.fluxes[sel])))
            sig.append(float(astrostats.biweight_scale(ap.fluxes[sel])))
        else:
            med.append(np.nan)
            sig.append(np.nan)
    return {"r_px": np.array(cen), "median": np.array(med),
            "sigma": np.array(sig), "n": np.array(n)}


# ----------------------------------------------------------------------
# Negative-structure detection
# ----------------------------------------------------------------------

@dataclass
class NegStructure:
    n_neg: int
    n_pos: int                    # positive control on source-masked image
    area_neg: int
    area_pos: int
    min_signif: float             # most negative smoothed significance
    signif_map: np.ndarray = field(repr=False)
    neg_segmap: Optional[np.ndarray] = field(default=None, repr=False)


def smooth_masked(
    resid: np.ndarray, exclude: np.ndarray, sigma: float
) -> np.ndarray:
    """NaN-tolerant gaussian smoothing (normalized convolution); excluded
    pixels contribute nothing, regions with <5% local coverage go NaN."""
    filled = np.where(exclude, np.nan, resid)
    w = np.isfinite(filled).astype(float)
    f0 = np.where(np.isfinite(filled), filled, 0.0)
    num = gaussian_filter(f0, sigma, mode="reflect")
    den = gaussian_filter(w, sigma, mode="reflect")
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0.05, num / den, np.nan)


def negative_structure(
    resid: np.ndarray,
    exclude: np.ndarray,
    smooth_sigma: float = 5.0,
    nsigma: float = 2.0,
    npixels: int = 200,
    err: Optional[np.ndarray] = None,
) -> NegStructure:
    """Detect coherent negative regions on a smoothed significance map.

    The map noise sigma is measured empirically on the smoothed residual in
    source-free pixels (correlated-noise safe). The positive-control run on
    the same (source-excluded) map calibrates the false-positive rate: for a
    clean subtraction, negative counts should match the positive control.

    With ``err`` the significance is depth-aware: computed on the
    noise-equalized residual ``resid / err`` (unit variance everywhere), so
    variable-depth strips neither hide nor fake negative structure.
    """
    field = resid if err is None else np.where(err > 0, resid / err, np.nan)
    sm = smooth_masked(field, exclude, smooth_sigma)
    ok = np.isfinite(sm) & ~exclude
    rms = astrostats.biweight_scale(sm[ok])
    lvl = 0.0  # significance is against literal zero, not the map median
    signif = (sm - lvl) / rms

    detmask = ~ok
    seg_neg = detect_sources(-signif, threshold=nsigma, npixels=npixels,
                             mask=detmask)
    seg_pos = detect_sources(signif, threshold=nsigma, npixels=npixels,
                             mask=detmask)
    n_neg = 0 if seg_neg is None else seg_neg.nlabels
    n_pos = 0 if seg_pos is None else seg_pos.nlabels
    a_neg = 0 if seg_neg is None else int(seg_neg.make_source_mask().sum())
    a_pos = 0 if seg_pos is None else int(seg_pos.make_source_mask().sum())
    return NegStructure(
        n_neg=n_neg, n_pos=n_pos, area_neg=a_neg, area_pos=a_pos,
        min_signif=float(np.nanmin(np.where(ok, signif, np.nan))),
        signif_map=signif,
        neg_segmap=None if seg_neg is None else seg_neg.data,
    )


# ----------------------------------------------------------------------
# Mesh diagnostics
# ----------------------------------------------------------------------

def mesh_mask_fraction(mask: np.ndarray, box: int) -> np.ndarray:
    """Fraction of masked pixels per (box x box) mesh cell."""
    ny, nx = mask.shape
    my, mx = int(np.ceil(ny / box)), int(np.ceil(nx / box))
    pad = np.pad(mask.astype(float),
                 ((0, my * box - ny), (0, mx * box - nx)),
                 constant_values=1.0)
    return pad.reshape(my, box, mx, box).mean(axis=(1, 3))


def summarize(name: str, apstats: dict, neg: NegStructure) -> dict:
    row = {"arm": name,
           "neg_n": neg.n_neg, "neg_area": neg.area_neg,
           "pos_n": neg.n_pos, "pos_area": neg.area_pos,
           "min_signif": round(neg.min_signif, 2)}
    for d, ap in apstats.items():
        row[f"apmed_d{d}"] = ap.median
        row[f"apsig_d{d}"] = ap.sigma
        row[f"apfneg_d{d}"] = ap.frac_below_m3sig
        row[f"apfpos_d{d}"] = ap.frac_above_p3sig
    return row
