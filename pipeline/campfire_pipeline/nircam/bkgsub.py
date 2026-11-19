"""
Iterative source removal and background subtraction for NIRCam imaging.

Ported from nircamx (H. C. Ferguson, STScI). Performs tiered source masking
with a clipped ring-median filter followed by photutils Background2D
estimation.

Original version history:
  1.1.0 -- output tier masks as a bitmask; fixed implicit mask count
  1.1.1 -- fixed background rms estimation ignoring mask in tier_mask
  1.2.1 -- appends BKGSUB extension instead of replacing SCI
  1.2.2 -- makes replace_sci an option
  1.3.0 -- optionally mask DQ bits
  1.3.1 -- only pass tier_mask the bad-pixel / off-detector mask
  1.4.0 -- added clipped_ring_median to reduce suppression in galaxy outskirts
  1.5.0 -- perf: ring-median on a block-reduced image, gaussian_filter in
           place of convolve_fft, EDT-based tier dilation, hoisted biweight
           stats out of the tier loop
  1.6.0 -- WHT-aware (variable-depth) source masking: when a weight map is
           available, tier detection and the ring-clip ceiling operate in
           noise-equalized units (SCI * sqrt(WHT)) so one global RMS
           threshold is valid at every depth
"""

import os
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np
from astropy import stats as astrostats
from astropy.convolution import Ring2DKernel
from astropy.io import fits
from astropy.nddata import block_reduce
from astropy.wcs import WCS
from jwst.datamodels import dqflags
from photutils.background import (
    Background2D,
    BiweightLocationBackground,
    BkgIDWInterpolator,
)
from photutils.utils import ShepardIDWInterpolator as idw  # noqa: F401
from scipy.ndimage import gaussian_filter, median_filter

from campfire_pipeline.common.io import log
from campfire_pipeline.nircam import _bkg_kernels as K


@dataclass
class SubtractBackground:
    """Iterative source-masking and 2-D background subtraction.

    Workflow (executed by :meth:`call`):
      1. Open FITS file, build initial mask from off-detector / DQ pixels.
      2. Apply a clipped ring-median filter to remove large-scale structure.
      3. Iteratively detect and mask sources in *tiers* of decreasing kernel
         size (aggressive -> fine).
      4. Estimate a smooth 2-D background on the unmasked pixels.
      5. Write the background-subtracted image and source bitmask to a new
         FITS file.

    All parameters can be overridden at construction time or loaded from a
    ``stage_config`` dict via :meth:`from_config`.
    """

    # -- Clipped ring-median filtering -----------------------------------------
    ring_radius_in: float = 80
    ring_width: float = 4
    ring_clip_max_sigma: float = 5.0
    ring_clip_box_size: int = 100
    ring_clip_filter_size: int = 3
    # Downsample the ring-median pass by this integer factor in each axis.
    # The ring-median estimates a smooth large-scale background, so running
    # it on a block-reduced image (and zooming the result back) is
    # equivalent to within sampling noise. A factor of 4 gives ~250x
    # speedup at radius=80; default of 1 preserves the original behaviour.
    ring_downsample: int = 1

    # -- Tiered source masking -------------------------------------------------
    # Tiers are the compact-source detection cascade (aggressive -> fine).
    #
    # HISTORY: a "tier 0" giant-galaxy pre-tier (100 sigma / 30k px core,
    # 600 px dilation) used to precede these tiers on mosaics, masking the
    # handful of extremely bright large galaxies out to their sky radius so
    # the Background2D mesh boundary landed on true sky (guarding against the
    # galaxy-shaped oversubtraction bowl). It was REMOVED with the negativity
    # guard (``bg_guard``): the A2744 120" A/B showed the fit inside the
    # tier-0 hole is pure extrapolation — negative area ~3x WORSE than no
    # tier 0, and ~9x worse even after guard cleanup — while the tier never
    # fired on the envelope-dominated BCGs it was meant to protect (their
    # 25 px-smoothed cores never reach 100 sigma over 30k connected px). The
    # guard bounds the background with the observed flux floor instead, which
    # works under masks and needs no hole. See experiments/bkg_nonneg.
    tier_kernel_size: list = field(default_factory=lambda: [25, 15, 5, 2])
    tier_npixels: list = field(default_factory=lambda: [15, 10, 3, 1])
    tier_nsigma: list = field(default_factory=lambda: [1.5, 1.5, 1.5, 1.5])
    tier_dilate_size: list = field(default_factory=lambda: [33, 25, 21, 19])

    # -- Background estimation -------------------------------------------------
    bg_box_size: int = 10
    bg_filter_size: int = 5
    bg_exclude_percentile: int = 90
    bg_sigma: float = 3
    bg_interpolator: str = "zoom"

    # -- Background-map outlier rejection (opt-in refit guard) -----------------
    # After the first Background2D fit, regions where the background *map*
    # itself is an outlier (source flux leaking through the mask shows up as
    # source-shaped bumps in the map) are dilated into the mask and the map is
    # refit once. sigma is measured on the lowest bg_reject_percentile of map
    # values so the outliers being hunted don't inflate their own threshold.
    bg_reject: bool = False
    bg_reject_sigma_hi: float = 4.0
    bg_reject_sigma_lo: float = 3.0
    bg_reject_percentile: float = 60.0
    bg_reject_dilate: float = 40.0

    # -- Negativity guard (opt-in; the mosaic config enables it) ---------------
    # Constrains the final background map so the subtracted image carries no
    # statistically significant negative structure. Physical prior: true flux
    # >= 0, so every pixel — masked or not — asserts bkg(x) <= sci(x) + noise.
    # Two data-side corrections applied after the (bg_reject-refit) fit:
    #
    # 1. CEILING MESHCAP: maskless coarse Background2D fits with a hard
    #    one-sided upper clip track the lower envelope of the local flux — a
    #    valid upper bound on any admissible background, even where the map
    #    tracks (sky + wing). Fit on the noise-equalized image so the clip
    #    bias is depth-uniform; self-calibrated per image against the masked
    #    fit on quiet sky (removes the clip bias), plus guard_ceiling_k x the
    #    quiet-sky scatter of that difference as slack (a slack ceiling is
    #    costless — it is a bound, never an estimate). The minimum over
    #    guard_ceiling_boxes tightens the bound in sky gaps next to bright
    #    masked sources. Enforced by capping the low-res background MESH and
    #    re-interpolating (smooth by construction) — a mask-and-refit
    #    enforcement is a no-op for violations under fully-masked regions,
    #    where the interpolator would extrapolate identically.
    # 2. GATED TROUGH PASS: the residual is observable even under the fit
    #    mask. Where its smoothed, noise-equalized map is coherently below
    #    -guard_trough_t sigma over a connected region of at least
    #    guard_trough_npix * sigma_smooth^2 px (area threshold tracks the
    #    smoothing correlation area, so blank-field false-fire is scale-
    #    independent and small), the map is lowered so the residual comes
    #    back up to the -t sigma floor — the minimal correction that removes
    #    statistical significance, continuous at the region edge (no seams),
    #    and zero over compliant sky (blank fields are a near-no-op).
    #    Iterated to convergence per smoothing scale.
    #
    # Noise model: sigma(x) = s / sqrt(WHT), s calibrated as the robust scale
    # of the noise-equalized first-pass residual over unmasked sky. WHT is
    # source-independent (ERR's source-Poisson term would suppress trough
    # significance and loosen the ceiling exactly under bright wings/ICL).
    # Without a WHT map the noise is a single global scalar (uniform depth).
    # Validated on A2744 F444W 120" cutouts: negative structure 159k px ->
    # 1.3k px (cluster core) at unchanged empty-aperture medians. See
    # experiments/bkg_nonneg/README.md.
    bg_guard: bool = False
    guard_ceiling_boxes: list = field(default_factory=lambda: [32, 64, 128])
    guard_ceiling_k: float = 2.0
    guard_ceiling_sigma_upper: float = 1.0
    guard_trough_sigmas: list = field(default_factory=lambda: [5.0, 15.0])
    guard_trough_t: float = 2.0
    guard_trough_npix: float = 8.0
    guard_trough_max_iter: int = 12

    # -- WHT-aware (variable-depth) masking ------------------------------------
    # When a weight map is available (mosaic WHT extension, or the ``wht``
    # argument of :meth:`mask_from_arrays`), tier detection and the ring-clip
    # ceiling are evaluated on the noise-equalized image SCI * sqrt(WHT),
    # whose noise is spatially uniform under variable depth — so the single
    # global RMS threshold is valid everywhere instead of being pinned to the
    # deepest coverage. Equivalent to the historical flux-space thresholds for
    # uniform weights. Set False to force flux-space thresholds regardless.
    #
    # WHT is the drizzle's rnoise-based IVM, not inverse *total* variance:
    # background variance is alpha/WHT with alpha the total:rnoise ratio, so
    # equalization is exact up to the global factor (absorbed by the global
    # biweight RMS) whenever alpha is spatially uniform — the depth effect
    # proper cancels identically, and only ratio *variation* (mixed readout
    # patterns, epoch-varying sky) survives, at the tens-of-percent level the
    # tier thresholds tolerate. Deliberately NOT the total-ERR map: ERR
    # includes source Poisson, so SCI/ERR would deflate the detection
    # statistic over exactly the flux the mask must catch, while sqrt(WHT) is
    # source-independent.
    wht_aware: bool = True

    # -- Coverage crop -----------------------------------------------------------
    # Run on the bounding box of the image's coverage (any finite SCI / ERR or
    # positive WHT), padded and aligned so every mesh grid lands where it does
    # on the full image, and paste the result back. Outside the box there is
    # nothing to fit: every pixel there is off-detector (bit 0), and the
    # output there is the input NaN. An outer mosaic tile is 30-40% covered.
    # Not a pixel setting (the result is the full-image one), so it is not in
    # the manifest hash.
    crop_to_coverage: bool = True

    # -- Threads -------------------------------------------------------------------
    # Worker threads for the heavy kernels (gaussian / median filters, the
    # dilations, the robust statistics; see _bkg_kernels). Every kernel gives
    # the single-threaded result bit for bit, so this is not a pixel setting
    # either. The mosaic resample step passes the CLI's --processes.
    n_threads: int = 1

    # -- Output options --------------------------------------------------------
    plot_smooth: int = 0
    suffix: str = "bkgsub"
    replace_sci: bool = False
    dq_flags_to_mask: list = field(
        default_factory=lambda: ["DO_NOT_USE", "SATURATED"]
    )

    # -- Runtime state (set during call, not constructor args) -----------------
    has_dq: bool = field(default=False, init=False, repr=False)
    dq: Optional[np.ndarray] = field(default=None, init=False, repr=False)
    wht: Optional[np.ndarray] = field(default=None, init=False, repr=False)
    dqmask: Optional[np.ndarray] = field(default=None, init=False, repr=False)
    outfile: Optional[str] = field(default=None, init=False, repr=False)
    mask_final: Optional[np.ndarray] = field(default=None, init=False, repr=False)
    # (full shape, crop origin) while compute() works on a coverage crop
    _crop_frame: Optional[tuple] = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, stage_config: dict) -> "SubtractBackground":
        """Build an instance from a plain dict (e.g. a TOML section).

        Only keys that match dataclass fields are forwarded; unknown keys
        are silently ignored so callers can pass an entire stage config
        section without filtering.
        """
        valid_keys = {f.name for f in cls.__dataclass_fields__.values() if f.init}
        filtered = {k: v for k, v in stage_config.items() if k in valid_keys}
        return cls(**filtered)

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def open_file(self, filepath: str) -> Tuple[np.ndarray, np.ndarray]:
        """Read SCI and ERR (or RMS) extensions; detect DQ/WHT if present."""
        with fits.open(filepath) as hdu:
            sci = hdu["SCI"].data
            try:
                err = hdu["ERR"].data
            except KeyError:
                # RMS map for HST
                err = hdu["RMS"].data

            self.has_dq = False
            self.wht = None
            for h in hdu:
                if "EXTNAME" in h.header and h.header["EXTNAME"] == "DQ":
                    self.has_dq = True
                    self.dq = h.data
                    log(f"{os.path.basename(filepath)} has a DQ array")
            if "WHT" in hdu:
                self.wht = hdu["WHT"].data
                if self.wht_aware:
                    log(f"{os.path.basename(filepath)} has a WHT array; "
                        f"using depth-aware (noise-equalized) masking")

        return sci, err

    # ------------------------------------------------------------------
    # Masking helpers
    # ------------------------------------------------------------------

    def replace_masked(
        self, sci: np.ndarray, mask: np.ndarray
    ) -> np.ndarray:
        """Fill masked pixels with a robust mean so convolution is clean."""
        sci_nan = np.where(mask, np.nan, sci)
        robust_mean = astrostats.biweight_location(sci_nan, c=6.0, ignore_nan=True)
        return np.where(mask, robust_mean, sci)

    @staticmethod
    def off_detector(sci: np.ndarray, err: np.ndarray) -> np.ndarray:
        """Return boolean mask: True where pixel is off the detector."""
        return np.isnan(err)

    def mask_by_dq(self) -> None:
        """Set ``self.dqmask`` by OR-ing the requested DQ flag bits."""
        self.dqmask = np.zeros(self.dq.shape, bool)
        for flag_name in self.dq_flags_to_mask:
            flagbit = dqflags.pixel[flag_name]
            self.dqmask = self.dqmask | (np.bitwise_and(self.dq, flagbit) != 0)

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def ring_median_filter(
        self, sci: np.ndarray, mask: np.ndarray
    ) -> np.ndarray:
        """Simple ring-median filter (unused by default; see clipped variant)."""
        log(
            f"Ring median filtering with radius = {self.ring_radius_in}, "
            f"width = {self.ring_width}"
        )
        sci_filled = self.replace_masked(sci, mask)
        ring = Ring2DKernel(self.ring_radius_in, self.ring_width)
        filtered = median_filter(sci, footprint=ring.array)
        return sci - filtered

    def clipped_ring_median_filter(
        self, sci: np.ndarray, mask: np.ndarray,
        sqw: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Ring-median filter with a sigma-clipped ceiling to preserve galaxy wings.

        ``sqw`` (sqrt(WHT), built by :meth:`mask_from_arrays`) makes the
        ceiling depth-aware: the clip RMS is measured on the noise-equalized
        residual and mapped back to *local* flux units per pixel, and
        clipped/masked pixels are filled with the local smooth background
        (under variable depth the filled regions can sit at a genuinely
        different sky level than the global mean). ``sqw=None`` preserves the
        historical global-scalar behaviour.
        """
        # Smooth background at large scale
        bkg = K.FastBackground2D(
            sci,
            box_size=self.ring_clip_box_size,
            sigma_clip=astrostats.SigmaClip(sigma=self.bg_sigma),
            filter_size=self.ring_clip_filter_size,
            bkg_estimator=BiweightLocationBackground(),
            exclude_percentile=90,
            mask=mask,
            interpolator=K.ExactZoomInterpolator(),
            **K.background2d_threads(self.n_threads),
        )
        # Background2D.background re-runs the full-size interpolation on every
        # access (photutils 3.x), so evaluate it once.
        bkg_img = bkg.background
        resid = sci - bkg_img
        if sqw is None:
            # RMS after subtracting the smooth background
            background_rms = K.biweight_scale(resid[~mask], self.n_threads)
            # Floating ceiling: pixels above this are masked before ring-median
            ceiling = self.ring_clip_max_sigma * background_rms + bkg_img
        else:
            # Depth-aware ceiling: one RMS in noise-equalized units, scaled to
            # local flux units per pixel (sqw == 0 pixels are already masked).
            nrms = K.biweight_scale((resid * sqw)[~mask], self.n_threads)
            with np.errstate(divide="ignore"):
                local_rms = np.where(sqw > 0, nrms / sqw, np.inf)
            ceiling = self.ring_clip_max_sigma * local_rms + bkg_img
        ceiling_mask = sci > ceiling
        del resid, ceiling

        f = max(int(self.ring_downsample), 1)
        log(
            f"Ring median filtering with radius = {self.ring_radius_in}, "
            f"width = {self.ring_width}, downsample = {f}"
        )
        if sqw is None:
            sci_filled = self.replace_masked(sci, mask | ceiling_mask)
        else:
            sci_filled = np.where(mask | ceiling_mask, bkg_img, sci)

        if f > 1:
            # Block-reduce, ring-median at scaled radius/width, zoom back.
            # The ring-median is by construction a smooth large-scale
            # estimator, so this is equivalent to within sampling noise.
            h, w = sci_filled.shape
            pad_h = (-h) % f
            pad_w = (-w) % f
            if pad_h or pad_w:
                sci_padded = np.pad(
                    sci_filled, ((0, pad_h), (0, pad_w)), mode='edge'
                )
            else:
                sci_padded = sci_filled
            small = block_reduce(sci_padded, f, func=np.mean)
            del sci_padded
            if self._crop_frame is not None:
                # zoom's corner-aligned mapping (grid_mode=False) samples at
                # positions that depend on the image size, so a crop of the
                # image would sample differently. Run the (1/f^2-sized)
                # median and the zoom on the full image's block grid, with
                # the crop's blocks at their place (the crop origin is a
                # multiple of f) and edge values elsewhere (beyond the pad,
                # so never inside a ring of the crop), then cut the crop out.
                (H, W), (y0, x0) = self._crop_frame
                sy0, sx0 = y0 // f, x0 // f
                SH, SW = -(-H // f), -(-W // f)
                small = np.pad(small, (
                    (sy0, SH - sy0 - small.shape[0]),
                    (sx0, SW - sx0 - small.shape[1])), mode='edge')
            radius_small = max(int(round(self.ring_radius_in / f)), 1)
            width_small = max(int(round(self.ring_width / f)), 1)
            ring_small = Ring2DKernel(radius_small, width_small)
            filtered_small = K.median_filter(small, ring_small.array,
                                             self.n_threads)
            filtered_padded = K.zoom(filtered_small, f, order=1,
                                     mode='nearest', threads=self.n_threads)
            if self._crop_frame is not None:
                filtered = filtered_padded[y0:y0 + h, x0:x0 + w]
            else:
                filtered = filtered_padded[:h, :w]
        else:
            ring = Ring2DKernel(self.ring_radius_in, self.ring_width)
            filtered = median_filter(sci_filled, footprint=ring.array)
        del sci_filled, bkg_img

        return sci - filtered

    # ------------------------------------------------------------------
    # Tiered source detection
    # ------------------------------------------------------------------

    def tier_mask(
        self,
        replaced_img: np.ndarray,
        mask: np.ndarray,
        background_rms: float,
        tiernum: int = 0,
    ) -> Optional[np.ndarray]:
        """Detect sources at a single tier and return a boolean mask.

        ``replaced_img`` and ``background_rms`` are computed once by
        :meth:`mask_sources` (they don't change across tiers) and passed in
        to avoid redundant biweight passes over a multi-GB array.
        """
        convolved = K.gaussian(
            replaced_img,
            self.tier_kernel_size[tiernum],
            mode='reflect',
            threads=self.n_threads,
        )

        labels = K.detect(
            convolved,
            self.tier_nsigma[tiernum] * background_rms,
            self.tier_npixels[tiernum],
            mask=mask,
            threads=self.n_threads,
        )
        del convolved
        if labels is None:
            log(f"No sources detected for tier {tiernum}, moving to next tier...")
            return None

        base = labels != 0
        del labels
        if self.tier_dilate_size[tiernum] == 0:
            tier_result = base
        else:
            # Euclidean disk dilation via distance transform: O(N) instead
            # of O(N * footprint_area) for binary_dilation with a large
            # circular footprint, and bit-identical for integer radii.
            radius = float(self.tier_dilate_size[tiernum])
            tier_result = K.dilate(base, radius, self.n_threads)

        log(f"Tier #{tiernum}:")
        log(f"  kernel_size = {self.tier_kernel_size[tiernum]}")
        log(f"  tier_nsigma = {self.tier_nsigma[tiernum]}")
        log(f"  tier_npixels = {self.tier_npixels[tiernum]}")
        log(f"  tier_dilate_size = {self.tier_dilate_size[tiernum]}")
        return tier_result

    def mask_sources(
        self,
        img: np.ndarray,
        bitmask: np.ndarray,
        starting_bit: int = 1,
        sqw: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Iteratively mask sources through all configured tiers.

        With ``sqw`` (sqrt(WHT)) the tiers detect on the noise-equalized image
        ``img * sqw``, whose noise is spatially uniform under variable depth,
        so the single global ``background_rms`` threshold is valid everywhere
        (a flux-space threshold is pinned to the modal — usually deepest —
        coverage and mass-flags shallow-region noise as sources). For uniform
        weights the two are equivalent, since the biweight statistics scale
        with the image. ``sqw=None`` preserves flux-space detection.
        """
        det_img = img if sqw is None else img * sqw
        first_mask = bitmask != 0
        valid = det_img[~first_mask]
        background_rms = K.biweight_scale(valid, self.n_threads)
        background_level = K.biweight_location(valid, self.n_threads)
        log(f"Ring-filtered background median: "
            f"{K.median(valid, self.n_threads)}")

        replaced_img = np.where(first_mask, background_level, det_img)

        for tiernum in range(len(self.tier_nsigma)):
            mask = self.tier_mask(
                replaced_img, first_mask, background_rms, tiernum=tiernum
            )
            if mask is None:
                continue
            bitmask = np.bitwise_or(
                bitmask, np.left_shift(mask, tiernum + starting_bit)
            )
        return bitmask

    # ------------------------------------------------------------------
    # Background estimation
    # ------------------------------------------------------------------

    def _fit_background2d(
        self, img: np.ndarray, mask: np.ndarray
    ) -> Background2D:
        """Single Background2D fit on unmasked pixels (no rejection pass)."""
        if self.bg_interpolator == "zoom":
            interpolator = K.ExactZoomInterpolator()
        elif self.bg_interpolator == "IDW":
            interpolator = BkgIDWInterpolator()
        else:
            raise ValueError(f"Unknown bg_interpolator: {self.bg_interpolator!r}")

        return K.FastBackground2D(
            img,
            box_size=self.bg_box_size,
            sigma_clip=astrostats.SigmaClip(sigma=self.bg_sigma),
            filter_size=self.bg_filter_size,
            bkg_estimator=BiweightLocationBackground(),
            exclude_percentile=self.bg_exclude_percentile,
            mask=mask,
            interpolator=interpolator,
            **K.background2d_threads(self.n_threads),
        )

    def reject_background_outliers(
        self, img: np.ndarray, mask: np.ndarray, bkg: Background2D
    ) -> Optional[np.ndarray]:
        """Return a grown mask of background-map outlier regions, or None.

        Source flux leaking through the source mask imprints source-shaped
        bumps on the background map; a genuinely smooth sky does not. Flag map
        pixels beyond ``med + hi*sigma`` / ``med - lo*sigma`` — with sigma
        measured on the lowest ``bg_reject_percentile`` of map values so the
        bumps don't inflate their own threshold — and grow them by
        ``bg_reject_dilate`` px. Returns None when nothing is flagged (or the
        stats are degenerate) so the caller can skip the refit.
        """
        bmap = bkg.background
        vals = bmap[~mask & np.isfinite(bmap)]
        if vals.size == 0:
            return None
        med = K.median(vals, self.n_threads)
        cut = np.percentile(vals, self.bg_reject_percentile)
        std = np.std(vals[vals < cut])
        if not np.isfinite(std) or std <= 0:
            return None
        outliers = (bmap > med + self.bg_reject_sigma_hi * std) | (
            bmap < med - self.bg_reject_sigma_lo * std
        )
        if not outliers.any():
            return None
        if self.bg_reject_dilate > 0:
            outliers = K.dilate(outliers, self.bg_reject_dilate,
                                self.n_threads)
        return outliers

    def estimate_background(
        self, img: np.ndarray, mask: np.ndarray
    ) -> Background2D:
        """Compute a smooth 2-D background on unmasked pixels.

        With ``bg_reject`` enabled, regions where the first-pass background
        map is itself an outlier are folded into the mask and the map is
        refit once (see :meth:`reject_background_outliers`).
        """
        bkg = self._fit_background2d(img, mask)
        if not self.bg_reject:
            return bkg
        outliers = self.reject_background_outliers(img, mask, bkg)
        if outliers is None:
            return bkg
        n_new = int((outliers & ~mask).sum())
        log(f"bg_reject: refitting with {n_new} background-map outlier "
            f"pixels added to the mask")
        return self._fit_background2d(img, mask | outliers)

    # ------------------------------------------------------------------
    # Negativity guard
    # ------------------------------------------------------------------

    @staticmethod
    def _smooth_masked(
        field_img: np.ndarray, exclude: np.ndarray, sigma: float
    ) -> np.ndarray:
        """NaN-tolerant gaussian smoothing (normalized convolution).

        Excluded pixels contribute nothing; regions with <5% local coverage
        go NaN so starved areas never produce fake significance.
        """
        filled = np.where(exclude, np.nan, field_img)
        w = np.isfinite(filled).astype(np.float32)
        f0 = np.where(np.isfinite(filled), filled, 0.0).astype(np.float32)
        num = gaussian_filter(f0, sigma, mode="reflect")
        den = gaussian_filter(w, sigma, mode="reflect")
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(den > 0.05, num / den, np.nan)

    def _guard_sigma_map(
        self, resid: np.ndarray, skymask: np.ndarray
    ) -> np.ndarray:
        """Local sky-noise map for the guard.

        With a weight map (and ``wht_aware``): sigma(x) = s / sqrt(WHT),
        with s the robust scale of the noise-equalized residual over usable
        sky (``skymask``) — background variance is alpha/WHT with alpha
        global, and WHT carries no source-Poisson term. Otherwise a single
        global robust scale (uniform depth).
        """
        if self.wht is not None and self.wht_aware:
            good = np.isfinite(self.wht) & (self.wht > 0)
            with np.errstate(invalid="ignore"):
                sqw = np.where(good, np.sqrt(np.where(good, self.wht, 0.0)),
                               np.nan)
            sel = skymask & good & np.isfinite(resid)
            s = float(K.biweight_scale((resid * sqw)[sel], self.n_threads))
            with np.errstate(divide="ignore", invalid="ignore"):
                return np.where(good, s / sqw, np.nan)
        sel = skymask & np.isfinite(resid)
        s = float(K.biweight_scale(resid[sel], self.n_threads))
        return np.full(resid.shape, s, dtype=np.float32)

    def _onesided_ceiling_map(
        self, e_img: np.ndarray, off: np.ndarray, box: int
    ) -> np.ndarray:
        """Maskless (off-detector only) coarse background of the equalized
        image with a hard upper clip: tracks the lower envelope of the local
        flux distribution — an upper bound on the background up to a
        (calibrated) clip bias."""
        bkg = K.FastBackground2D(
            e_img,
            box_size=box,
            sigma_clip=astrostats.SigmaClip(
                sigma_lower=10.0,
                sigma_upper=self.guard_ceiling_sigma_upper,
                maxiters=10,
            ),
            filter_size=3,
            bkg_estimator=BiweightLocationBackground(),
            exclude_percentile=95,
            mask=off,
            interpolator=K.ExactZoomInterpolator(),
            **K.background2d_threads(self.n_threads),
        )
        return bkg.background

    def _multiscale_ceiling(
        self,
        sci: np.ndarray,
        off: np.ndarray,
        bkgmap: np.ndarray,
        fitmask: np.ndarray,
        sigma_map: np.ndarray,
    ) -> np.ndarray:
        """Minimum over independently calibrated one-sided ceilings at
        several box scales (min of upper bounds = tighter bound; finer
        scales catch the sky gaps next to bright masked sources, where a
        coarse mesh is source-embedded and its ceiling useless).

        Everything runs in noise-equalized units (sci/sigma bounds
        bkg/sigma pixelwise since true flux >= 0) so the one-sided clip
        bias is depth-uniform and one global calibration constant is valid;
        the bound maps back through the local sigma.
        """
        good = np.isfinite(sigma_map) & (sigma_map > 0)
        with np.errstate(invalid="ignore"):
            e_sci = np.where(good, sci / sigma_map, np.nan)
            e_bkg = np.where(good, bkgmap / sigma_map, np.nan)
        off_e = off | ~good
        quiet = ~(fitmask | off_e)
        ceils = []
        for box in self.guard_ceiling_boxes:
            om = self._onesided_ceiling_map(e_sci, off_e, int(box))
            diff = (om - e_bkg)[quiet]
            delta = float(K.biweight_location(diff, self.n_threads))
            scatter = float(K.biweight_scale(diff, self.n_threads))
            del diff
            ceils.append(om - delta + self.guard_ceiling_k * scatter)
            del om
            log(f"  guard ceiling box={box}: clip bias={delta:.3f} "
                f"quiet-sky scatter={scatter:.3f} (equalized units)")
        del e_sci, e_bkg, off_e, quiet
        ceil_e = np.minimum.reduce(ceils)
        del ceils
        return np.where(good, ceil_e * sigma_map, np.inf)

    def _cap_mesh(self, bkg2d: Background2D, ceiling: np.ndarray,
                  shape) -> np.ndarray:
        """Cap the low-res background mesh at the ceiling (block-averaged
        onto the mesh grid) and re-run the zoom interpolation — smooth by
        construction, and effective under fully-masked regions where a
        mask-and-refit pass cannot change the extrapolation."""
        mesh = np.asarray(bkg2d.background_mesh, dtype=float)
        box = np.atleast_1d(bkg2d.box_size)
        by, bx = int(box[0]), int(box[-1])
        my, mx = mesh.shape
        pady, padx = my * by - shape[0], mx * bx - shape[1]
        cpad = np.pad(np.where(np.isfinite(ceiling), ceiling, np.inf),
                      ((0, pady), (0, padx)), mode="edge")
        # mean over finite entries per cell; all-inf cells stay uncapped
        cgrid = cpad.reshape(my, by, mx, bx)
        finite = np.isfinite(cgrid)
        csum = np.where(finite, cgrid, 0.0).sum(axis=(1, 3))
        cnt = finite.sum(axis=(1, 3))
        with np.errstate(invalid="ignore"):
            cmesh = np.where(cnt > 0, csum / np.maximum(cnt, 1), np.inf)
        capped = np.minimum(mesh, cmesh)
        n_capped = int((mesh > cmesh).sum())
        log(f"  guard ceiling: capped {n_capped}/{mesh.size} mesh cells")
        # Re-run exactly the interpolation the original fit used:
        # Background2D.background is interpolator(background_mesh,
        # **_interp_kwargs) (photutils 2.3.0, background_2d.py), so calling
        # the fit's own interpolator with the fit's own kwargs reproduces
        # bkg2d.background bit-for-bit from the uncapped mesh, and the capped
        # map differs only through the capped cells. This also honors the
        # configured bg_interpolator: a hardcoded BkgZoomInterpolator here
        # would silently replace an IDW background with a zoom-interpolated
        # one even when no cell is capped (and _interp_kwargs carries the
        # mesh_yxcen/mesh_nan_mask the IDW call requires).
        interp_kwargs = getattr(bkg2d, "_interp_kwargs", None)
        if interp_kwargs is None:
            # photutils without _interp_kwargs: fall back to the zoom-style
            # call (edge_method is read unconditionally in 2.3.0).
            interp_kwargs = dict(
                box_size=(by, bx), shape=shape, dtype=float,
                edge_method=getattr(bkg2d, "edge_method", "pad"))
        return bkg2d.interpolator(capped, **interp_kwargs)

    def _residual_exclusion(
        self, resid_eq: np.ndarray, off: np.ndarray
    ) -> np.ndarray:
        """Positive-only compact-source mask on the equalized residual.

        Keeps source flux from diluting the trough-pass smoothing WITHOUT
        blinding it: exclusion follows positive detections in the residual,
        never SCI brightness, so negative lanes (e.g. under bright ICL)
        stay visible to the negativity detector.
        """
        field_img = np.where(np.isfinite(resid_eq), resid_eq, 0.0)
        sm = K.gaussian(np.where(off, 0.0, field_img).astype(np.float32),
                        2.0, mode="reflect", threads=self.n_threads)
        sky = sm[~off]
        rms = K.biweight_scale(sky, self.n_threads)
        lvl = K.biweight_location(sky, self.n_threads)
        del sky
        labels = K.detect(sm, lvl + 2.0 * rms, 8, mask=off,
                          threads=self.n_threads)
        if labels is None:
            return off.copy()
        m = K.dilate(labels != 0, 5.0, self.n_threads)
        return m | off

    # Side of the blocks the trough pass uses to find what an iteration's
    # correction touched (see _refresh_smoothed). Coarser blocks waste area on
    # sparse corrections, finer ones pay more halo per block row.
    _TROUGH_BLOCK = 512

    @staticmethod
    def _refresh_smoothed(sm, f0, den, valid, changed, sigma, block,
                          threads=1):
        """Recompute ``sm = num / den`` where ``f0`` changed, in place.

        ``num`` is ``gaussian_filter(f0, sigma, mode="reflect")``. A changed
        input pixel moves the output only within the kernel radius
        ``r = int(4 * sigma + 0.5)`` (scipy's default truncate), so the
        refresh covers the blocks within ``r`` of a changed pixel, merged into
        runs along each block row, and filters each run with an ``r`` halo
        around it. ``gaussian_filter`` is a sequence of 1-D correlations
        whose every output pixel is a fixed-order sum over its own ``r``
        neighbourhood, so a window that holds that neighbourhood (or meets
        the real image edge, where ``reflect`` is the same rule) yields the
        full-image result bit for bit. The runs write disjoint parts of
        ``sm``, so ``threads`` refresh them concurrently.
        """
        r = int(4.0 * float(sigma) + 0.5)
        ny, nx = changed.shape
        nby, nbx = -(-ny // block), -(-nx // block)
        pad = np.zeros((nby * block, nbx * block), dtype=bool)
        pad[:ny, :nx] = changed
        touched = pad.reshape(nby, block, nbx, block).any(axis=(1, 3))
        if not touched.any():
            return
        # every block within r px of a touched block
        g = -(-r // block)
        grown = np.zeros_like(touched)
        for dy in range(-g, g + 1):
            for dx in range(-g, g + 1):
                ys = slice(max(dy, 0), nby + min(dy, 0))
                yd = slice(max(-dy, 0), nby + min(-dy, 0))
                xs = slice(max(dx, 0), nbx + min(dx, 0))
                xd = slice(max(-dx, 0), nbx + min(-dx, 0))
                grown[yd, xd] |= touched[ys, xs]
        runs = []
        for by in range(nby):
            row = grown[by]
            bx = 0
            while bx < nbx:
                if not row[bx]:
                    bx += 1
                    continue
                start = bx
                while bx < nbx and row[bx]:
                    bx += 1
                runs.append((by * block, min((by + 1) * block, ny),
                             start * block, min(bx * block, nx)))

        def refresh(run):
            y0, y1, x0, x1 = run
            wy0, wy1 = max(y0 - r, 0), min(y1 + r, ny)
            wx0, wx1 = max(x0 - r, 0), min(x1 + r, nx)
            num = gaussian_filter(f0[wy0:wy1, wx0:wx1], sigma, mode="reflect")
            num = num[y0 - wy0:y1 - wy0, x0 - wx0:x1 - wx0]
            with np.errstate(invalid="ignore", divide="ignore"):
                sm[y0:y1, x0:x1] = np.where(
                    valid[y0:y1, x0:x1], num / den[y0:y1, x0:x1], np.nan)

        K._run(refresh, runs, threads)

    def _gated_trough_pass(
        self,
        sci: np.ndarray,
        bkgmap: np.ndarray,
        exclude: np.ndarray,
        sigma_map: np.ndarray,
    ) -> np.ndarray:
        """Iterated, detection-gated trough correction (see class docstring).

        Returns the corrected background map (only ever lowered, and only
        inside detected coherent negative regions).

        Equivalent to recomputing ``_smooth_masked((sci - m) / sigma)`` from
        scratch every iteration, bit for bit, but incremental: the smoothing
        weight is fixed (exclusion and finiteness never change, since a
        correction is always finite), and an iteration's correction touches
        only its gated pixels, so only those pixels of the smoothing input and
        their kernel neighbourhood of the smoothed map are recomputed.
        """
        m = bkgmap.copy()
        mv = m.reshape(-1)
        sci_v = sci.reshape(-1)
        with np.errstate(invalid="ignore"):
            inv_sigma = np.where(
                np.isfinite(sigma_map) & (sigma_map > 0),
                1.0 / sigma_map, np.nan)
        inv_v = inv_sigma.reshape(-1)
        sigma_fill = np.where(np.isfinite(sigma_map), sigma_map, 0.0)
        sigma_fill_v = sigma_fill.reshape(-1)
        excl_v = exclude.reshape(-1)
        sky = ~exclude

        # The smoothing input as _smooth_masked builds it: the equalized
        # residual, zero where excluded or non-finite, and its weight.
        with np.errstate(invalid="ignore"):
            resid_eq = (sci - m) * inv_sigma
        filled = np.where(exclude, np.nan, resid_eq)
        del resid_eq
        w_ok = np.isfinite(filled)
        f0 = np.where(w_ok, filled, 0.0).astype(np.float32)
        del filled
        f0_v = f0.reshape(-1)

        for s in self.guard_trough_sigmas:
            npixels = max(int(self.guard_trough_npix * s * s), 30)
            den = K.gaussian(w_ok.astype(np.float32), s, mode="reflect",
                             threads=self.n_threads)
            valid = den > 0.05
            num = K.gaussian(f0, s, mode="reflect", threads=self.n_threads)
            with np.errstate(invalid="ignore", divide="ignore"):
                sm = np.where(valid, num / den, np.nan)
            del num
            # sm is finite exactly where valid (den > 0.05, f0 finite)
            stat_sel = valid & sky
            for it in range(self.guard_trough_max_iter):
                # exclude keeps source flux out of the smoothing inputs and
                # the rms estimate ONLY. The gate and the correction use sm
                # wherever it is finite — including under excluded source
                # footprints, where the normalized convolution interpolates
                # the ambient trough from the surrounding sky — so corr stays
                # continuous across footprint edges. Zeroing signif on
                # excluded pixels instead would hard-zero corr over every
                # source inside a fired trough, leaving each one on a
                # pedestal at the original trough depth (a seam the class
                # docstring promises not to make).
                rms = float(K.biweight_scale(sm[stat_sel], self.n_threads))
                if not np.isfinite(rms) or rms <= 0:
                    break
                # detect_sources(-signif, guard_trough_t, npixels) with
                # signif = where(valid, sm / rms, 0), evaluated per band
                above = np.empty(sm.shape, dtype=bool)

                def _above(rows, rms=rms):
                    signif = np.where(valid[rows], sm[rows] / rms, 0.0)
                    above[rows] = -signif > self.guard_trough_t

                K.map_bands(_above, sm.shape, self.n_threads)
                labels = K.segments(above, npixels, self.n_threads)
                del above
                if labels is None:
                    break
                # A region's significance must rest on at least npixels of
                # real (non-excluded) sky: interpolated sm under excluded
                # islands may extend a genuine trough across a footprint,
                # but must never create or carry a firing on its own —
                # otherwise shallow negative patches bridged by source
                # footprints erode the blank-field near-no-op guarantee.
                sky_lab = np.where(sky, labels, 0)
                sky_area = K.bincount(sky_lab, int(labels.max()) + 1,
                                      self.n_threads)
                del sky_lab
                sky_area[0] = 0          # == bincount(labels[sky]) off label 0
                keep = sky_area >= npixels
                keep[0] = False
                if not keep.any():
                    break
                gate = keep[labels]
                del labels
                idx = np.flatnonzero(gate)
                # The correction is zero off the gate, so it is applied at the
                # gated pixels only (m + 0 == m).
                corr = np.minimum(sm.reshape(-1)[idx] + self.guard_trough_t * rms,
                                  0.0)
                corr[~np.isfinite(corr)] = 0.0
                corr = corr * sigma_fill_v[idx]
                mv[idx] += corr
                corr_min = float(corr.min())
                if idx.size < gate.size:
                    corr_min = min(corr_min, 0.0)
                log(f"  guard trough sigma={s} iter={it}: corrected "
                    f"{idx.size / gate.size:.4f} of area, "
                    f"min={corr_min:.3g}")
                # Refresh the smoothing input at the gated pixels, then the
                # smoothed map around them.
                with np.errstate(invalid="ignore"):
                    r_new = (sci_v[idx] - mv[idx]) * inv_v[idx]
                r_new[excl_v[idx]] = np.nan
                f0_v[idx] = np.where(np.isfinite(r_new), r_new, 0.0)
                self._refresh_smoothed(sm, f0, den, valid, gate, s,
                                       self._TROUGH_BLOCK, self.n_threads)
                del gate, idx, corr, r_new
            del den, valid, sm, stat_sel
        return m

    def apply_negativity_guard(
        self,
        sci: np.ndarray,
        bkg2d: Background2D,
        mask_final: np.ndarray,
        off: np.ndarray,
        bkgmap: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Apply ceiling meshcap + gated trough pass to a fitted background.

        Parameters: the SCI image, the fitted ``Background2D``, the full fit
        mask, and the off-detector/DQ/zero-weight seed mask (bit 0 of the
        bitmask); ``bkgmap`` is ``bkg2d.background`` when the caller already
        has it (photutils re-interpolates on every access). Returns the
        guarded background map.
        """
        if bkgmap is None:
            bkgmap = bkg2d.background
        sigma_map = self._guard_sigma_map(sci - bkgmap, ~mask_final & ~off)
        ceiling = self._multiscale_ceiling(
            sci, off, bkgmap, mask_final, sigma_map)
        capped = self._cap_mesh(bkg2d, ceiling, sci.shape)
        del ceiling
        with np.errstate(invalid="ignore"):
            resid_eq = np.where(
                np.isfinite(sigma_map) & (sigma_map > 0),
                (sci - capped) / sigma_map, np.nan)
        excl = self._residual_exclusion(resid_eq, off)
        del resid_eq
        return self._gated_trough_pass(sci, capped, excl, sigma_map)

    # ------------------------------------------------------------------
    # Evaluation / diagnostics
    # ------------------------------------------------------------------

    def evaluate_bias(
        self,
        bkgd: np.ndarray,
        err: np.ndarray,
        mask: np.ndarray,
    ) -> None:
        """Log the bias between background under masked vs unmasked pixels."""
        on_detector = ~np.isnan(err)

        mean_masked = bkgd[mask & on_detector].mean()
        std_masked = bkgd[mask & on_detector].std()
        stderr_masked = mean_masked / (np.sqrt(len(bkgd[mask])) * std_masked)

        mean_unmasked = bkgd[~mask & on_detector].mean()
        std_unmasked = bkgd[~mask & on_detector].std()
        stderr_unmasked = mean_unmasked / (
            np.sqrt(len(bkgd[~mask])) * std_unmasked
        )

        diff = mean_masked - mean_unmasked
        significance = diff / np.sqrt(stderr_masked**2 + stderr_unmasked**2)

        log(f"Mean under masked pixels   = {mean_masked:.4f} +- {stderr_masked:.4f}")
        log(
            f"Mean under unmasked pixels = "
            f"{mean_unmasked:.4f} +- {stderr_unmasked:.4f}"
        )
        log(f"Difference = {diff:.4f} at {significance:.2f} sigma significance")

    # ------------------------------------------------------------------
    # Main entry points
    # ------------------------------------------------------------------

    def mask_from_arrays(
        self,
        sci: np.ndarray,
        err: np.ndarray,
        dq: Optional[np.ndarray] = None,
        wht: Optional[np.ndarray] = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Build the source-rejection mask + bitmask from in-memory arrays.

        The mask-only core of :meth:`compute` — off-detector/DQ/NaN seed,
        clipped ring-median filter, then tiered source detection — with **no**
        ``estimate_background`` call and no file I/O. Used by the unified
        ``bkg`` step (which holds the datamodel arrays in memory and only needs
        the mask), and by :meth:`compute` so the two share one code path.

        ``wht`` (with ``wht_aware`` on, the default) switches detection and
        the ring-clip ceiling to noise-equalized units — ``sci * sqrt(wht)``
        has spatially uniform noise, so the global RMS thresholds stay valid
        across depth variations in a mosaic. Zero/invalid-weight pixels are
        folded into the seed mask. ``wht=None`` (e.g. the per-exposure ``bkg``
        step, where depth is uniform) keeps flux-space thresholds.

        Returns ``(mask_final, bitmask)`` with the same semantics as the 2nd/3rd
        elements of :meth:`compute`.
        """
        if dq is not None:
            self.has_dq = True
            self.dq = dq
        else:
            self.has_dq = False

        bitmask = np.zeros(sci.shape, np.uint32)

        off_detector_mask = self.off_detector(sci, err)
        if self.has_dq:
            self.mask_by_dq()
            mask = off_detector_mask | self.dqmask
        else:
            mask = off_detector_mask
        mask = np.logical_or(mask, np.isnan(sci))

        sqw = None
        if wht is not None and self.wht_aware:
            with np.errstate(invalid="ignore"):
                sqw = np.sqrt(
                    np.where(np.isfinite(wht) & (wht > 0), wht, 0.0)
                ).astype(np.float32)
            # Zero/invalid weight = no data: fold into the seed mask so the
            # noise-equalized image's zeros never enter the statistics.
            mask = np.logical_or(mask, sqw == 0)
        bitmask = np.bitwise_or(bitmask, np.left_shift(mask, 0))

        filtered = self.clipped_ring_median_filter(sci, mask, sqw=sqw)
        bitmask = self.mask_sources(filtered, bitmask, starting_bit=1, sqw=sqw)
        mask_final = bitmask != 0
        return mask_final, bitmask

    def coverage_crop(
        self,
        sci: np.ndarray,
        err: np.ndarray,
        wht: Optional[np.ndarray] = None,
    ) -> Optional[Tuple[slice, slice]]:
        """The region :meth:`compute` works on, or None for the whole image.

        The bounding box of every pixel with a finite SCI or ERR or a positive
        WHT, grown by ``pad`` and with its origin moved down to a multiple of
        every mesh box (and the ring-median block size), so each Background2D
        grid and the block-reduce land on the same pixels as on the full
        image. ``pad`` is 12 of the largest mesh boxes: the boxes beyond the
        coverage are all masked and IDW-filled from their nearest valid cells
        either way, and the cubic zoom couples mesh cells only through a
        prefilter decaying by 0.27 per cell (0.27**12 ~ 1e-7), far beyond
        every image-plane kernel (ring median, tier kernels and dilations,
        guard smoothing). None when the crop would keep 90% of the image or
        there is no coverage.
        """
        ny, nx = sci.shape
        rows = np.zeros(ny, bool)
        cols = np.zeros(nx, bool)
        step = 2048
        for y0 in range(0, ny, step):
            sl = slice(y0, min(y0 + step, ny))
            cov = np.isfinite(sci[sl]) | np.isfinite(err[sl])
            if wht is not None:
                with np.errstate(invalid="ignore"):
                    cov |= np.asarray(wht[sl]) > 0
            rows[sl] = cov.any(axis=1)
            cols |= cov.any(axis=0)
        if not rows.any():
            return None
        boxes = [self.ring_clip_box_size, self.bg_box_size,
                 max(int(self.ring_downsample), 1)]
        if self.bg_guard:
            boxes += [int(b) for b in self.guard_ceiling_boxes]
        align = int(np.lcm.reduce(np.array(boxes, dtype=np.int64)))
        pad = 12 * max(boxes)
        y = np.flatnonzero(rows)
        x = np.flatnonzero(cols)
        y0 = max(int(y[0]) - pad, 0) // align * align
        x0 = max(int(x[0]) - pad, 0) // align * align
        y1 = min(int(y[-1]) + 1 + pad, ny)
        x1 = min(int(x[-1]) + 1 + pad, nx)
        if (y1 - y0) * (x1 - x0) >= 0.9 * ny * nx:
            return None
        return slice(y0, y1), slice(x0, x1)

    def compute(
        self, filepath: str
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Run the source-rejection + background pipeline in memory only.

        Returns
        -------
        bkgd_subtracted : ndarray
            ``sci - background``, same dtype as the input SCI.
        mask_final : ndarray of bool
            True where pixels were excluded from the background fit (off
            detector, DQ-flagged, NaN, or rejected by any tier).
        bitmask : ndarray of uint32
            Per-bit source rejection map (bit 0 = off-detector / DQ / NaN,
            bits 1+ = each tier of source detection).

        Used by the variance-rescaling step in the canonical-exposure
        pipeline, which doesn't need the file on disk that ``call()`` writes.
        """
        log(f"Running background subtraction on {os.path.basename(filepath)}")
        sci, err = self.open_file(filepath)
        full_shape, full_dtype = sci.shape, sci.dtype

        crop = None
        if self.crop_to_coverage:
            crop = self.coverage_crop(sci, err, self.wht)
        if crop is not None:
            (ys, xs) = crop
            log(f"  cropping to coverage: [{ys.start}:{ys.stop}, "
                f"{xs.start}:{xs.stop}] = {ys.stop - ys.start}x"
                f"{xs.stop - xs.start} of {full_shape[0]}x{full_shape[1]} "
                f"({(ys.stop - ys.start) * (xs.stop - xs.start) / sci.size:.0%})")

            def _cut(a):
                return None if a is None else np.ascontiguousarray(a[crop])
            sci, err = _cut(sci), _cut(err)
            self.wht = _cut(self.wht)
            self._crop_frame = (full_shape, (ys.start, xs.start))
            if self.has_dq:
                self.dq = _cut(self.dq)

        # open_file set self.has_dq / self.dq / self.wht; hand the DQ and WHT
        # arrays (or None) to the shared mask builder.
        try:
            mask_final, bitmask = self.mask_from_arrays(
                sci, err, self.dq if self.has_dq else None, wht=self.wht
            )
        finally:
            self._crop_frame = None

        bkg = self.estimate_background(sci, mask_final)
        bkgmap = bkg.background
        if self.bg_guard:
            log("Applying negativity guard (ceiling meshcap + trough pass)")
            off = (bitmask & 1) != 0
            bkgmap = self.apply_negativity_guard(
                sci, bkg, mask_final, off, bkgmap=bkgmap)
            del off
        del bkg
        # Cast back to the input precision. The guard works in float64 —
        # _cap_mesh reads the mesh as `dtype=float` — so `sci - bkgmap` would
        # otherwise promote a float32 mosaic to float64 and double every SCI
        # array on disk (A2744 F444W: _sci 4.68 -> 9.37 GiB, _i2d 15.4 -> 20.1
        # GiB), silently, for no gain over float32 drizzle inputs. The
        # unguarded path already returns float32, and this method's docstring
        # promises "same dtype as the input SCI"; without this the guard breaks
        # that invariant. Keeping the arithmetic in float64 and casting only
        # the result costs 2.4e-07 relative rounding.
        bkgd_subtracted = (sci - bkgmap).astype(sci.dtype, copy=False)
        del bkgmap

        if crop is not None:
            # Outside the crop every pixel is off-detector (bit 0 only: no
            # tier dilation reaches past the pad) and the output is the
            # input's NaN.
            out = np.full(full_shape, np.nan, dtype=full_dtype)
            out[crop] = bkgd_subtracted
            bkgd_subtracted = out
            full_bits = np.ones(full_shape, dtype=bitmask.dtype)
            full_bits[crop] = bitmask
            bitmask = full_bits
            mask_final = bitmask != 0

        self.mask_final = mask_final
        return bkgd_subtracted, mask_final, bitmask

    def call(self, filepath: str) -> str:
        """Run the full background-subtraction pipeline and write the result.

        Parameters
        ----------
        filepath : str
            Full path to a FITS file containing at least SCI and ERR
            extensions.

        Returns
        -------
        str
            Path to the output background-subtracted FITS file (suffix from
            ``self.suffix``).
        """
        bkgd_subtracted, mask, bitmask = self.compute(filepath)

        outfile = filepath.replace(".fits", f"_{self.suffix}.fits")
        self.outfile = outfile

        with fits.open(filepath) as hdu:
            wcs = WCS(hdu["SCI"].header)

            if self.replace_sci:
                hdu["SCI"].data = bkgd_subtracted
            else:
                newhdu = fits.ImageHDU(
                    bkgd_subtracted, header=wcs.to_header(), name="BKGSUB"
                )
                hdu.append(newhdu)

            # Source-rejection bitmask
            newhdu = fits.ImageHDU(
                bitmask.astype("uint8"), header=wcs.to_header(), name="SRCMASK"
            )
            hdu.append(newhdu)

            log(f"Writing out {os.path.basename(outfile)}")
            hdu.writeto(outfile, overwrite=True)

        return outfile
