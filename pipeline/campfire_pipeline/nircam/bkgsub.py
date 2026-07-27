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
    BkgZoomInterpolator,
)
from photutils.segmentation import detect_sources
from photutils.utils import ShepardIDWInterpolator as idw  # noqa: F401
from scipy.ndimage import (
    distance_transform_edt,
    gaussian_filter,
    median_filter,
    zoom,
)

from campfire_pipeline.common.io import log


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
    # Tier 0 is a large-extended-source pre-tier that fires ONLY on the handful
    # of extremely bright, large galaxies (and comparably bright stars) per tile.
    # A galaxy far larger than ``bg_box_size`` is fully masked at the pixel level
    # yet still over-subtracted: ``Background2D`` keeps mesh cells up to
    # ``bg_exclude_percentile`` masked at the galaxy edge, samples its outskirts
    # there, and the zoom interpolator extrapolates that gradient inward (a
    # galaxy-shaped +30..90e-3 MJy/sr bowl, 4-11 sigma of sky). Tier 0 masks the
    # bright core + a heavy dilation so the mesh boundary lands on true sky.
    # Selectivity: a HIGH ``tier_nsigma`` (100) plus a large ``tier_npixels``
    # (30k min connected area) means only objects with a >=30k-px core above
    # 100*sigma survive -- ~4 per COSMOS 30mas tile (vs ~120 at 5*sigma), so the
    # normal aggressive flattening of ordinary galaxy wings is UNCHANGED
    # everywhere else. The heavy ``tier_dilate_size`` (600) compensates for the
    # small high-sigma footprint and reaches the galaxy's sky radius. It is a
    # no-op on tiles with no such object. Tiers 1-4 are the compact-source cascade.
    tier_kernel_size: list = field(default_factory=lambda: [25, 25, 15, 5, 2])
    tier_npixels: list = field(default_factory=lambda: [30000, 15, 10, 3, 1])
    tier_nsigma: list = field(default_factory=lambda: [100.0, 1.5, 1.5, 1.5, 1.5])
    tier_dilate_size: list = field(default_factory=lambda: [600, 33, 25, 21, 19])

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
        bkg = Background2D(
            sci,
            box_size=self.ring_clip_box_size,
            sigma_clip=astrostats.SigmaClip(sigma=self.bg_sigma),
            filter_size=self.ring_clip_filter_size,
            bkg_estimator=BiweightLocationBackground(),
            exclude_percentile=90,
            mask=mask,
            interpolator=BkgZoomInterpolator(),
        )
        resid = sci - bkg.background
        if sqw is None:
            # RMS after subtracting the smooth background
            background_rms = astrostats.biweight_scale(resid[~mask])
            # Floating ceiling: pixels above this are masked before ring-median
            ceiling = self.ring_clip_max_sigma * background_rms + bkg.background
        else:
            # Depth-aware ceiling: one RMS in noise-equalized units, scaled to
            # local flux units per pixel (sqw == 0 pixels are already masked).
            nrms = astrostats.biweight_scale((resid * sqw)[~mask])
            with np.errstate(divide="ignore"):
                local_rms = np.where(sqw > 0, nrms / sqw, np.inf)
            ceiling = self.ring_clip_max_sigma * local_rms + bkg.background
        ceiling_mask = sci > ceiling

        f = max(int(self.ring_downsample), 1)
        log(
            f"Ring median filtering with radius = {self.ring_radius_in}, "
            f"width = {self.ring_width}, downsample = {f}"
        )
        if sqw is None:
            sci_filled = self.replace_masked(sci, mask | ceiling_mask)
        else:
            sci_filled = np.where(mask | ceiling_mask, bkg.background, sci)

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
            radius_small = max(int(round(self.ring_radius_in / f)), 1)
            width_small = max(int(round(self.ring_width / f)), 1)
            ring_small = Ring2DKernel(radius_small, width_small)
            filtered_small = median_filter(small, footprint=ring_small.array)
            filtered_padded = zoom(filtered_small, f, order=1, mode='nearest')
            filtered = filtered_padded[:h, :w]
        else:
            ring = Ring2DKernel(self.ring_radius_in, self.ring_width)
            filtered = median_filter(sci_filled, footprint=ring.array)

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
        convolved = gaussian_filter(
            replaced_img,
            sigma=self.tier_kernel_size[tiernum],
            mode='reflect',
        )

        seg_detect = detect_sources(
            convolved,
            threshold=self.tier_nsigma[tiernum] * background_rms,
            npixels=self.tier_npixels[tiernum],
            mask=mask,
        )
        if seg_detect is None:
            log(f"No sources detected for tier {tiernum}, moving to next tier...")
            return None

        base = seg_detect.make_source_mask()
        if self.tier_dilate_size[tiernum] == 0:
            tier_result = base
        else:
            # Euclidean disk dilation via distance transform: O(N) instead
            # of O(N * footprint_area) for binary_dilation with a large
            # circular footprint, and bit-identical for integer radii.
            radius = float(self.tier_dilate_size[tiernum])
            tier_result = distance_transform_edt(~base) <= radius

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
        background_rms = astrostats.biweight_scale(valid)
        background_level = astrostats.biweight_location(valid)
        log(f"Ring-filtered background median: {np.median(valid)}")

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
            interpolator = BkgZoomInterpolator()
        elif self.bg_interpolator == "IDW":
            interpolator = BkgIDWInterpolator()
        else:
            raise ValueError(f"Unknown bg_interpolator: {self.bg_interpolator!r}")

        return Background2D(
            img,
            box_size=self.bg_box_size,
            sigma_clip=astrostats.SigmaClip(sigma=self.bg_sigma),
            filter_size=self.bg_filter_size,
            bkg_estimator=BiweightLocationBackground(),
            exclude_percentile=self.bg_exclude_percentile,
            mask=mask,
            interpolator=interpolator,
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
        med = np.median(vals)
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
            outliers = (
                distance_transform_edt(~outliers) <= self.bg_reject_dilate
            )
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

        # open_file set self.has_dq / self.dq / self.wht; hand the DQ and WHT
        # arrays (or None) to the shared mask builder.
        mask_final, bitmask = self.mask_from_arrays(
            sci, err, self.dq if self.has_dq else None, wht=self.wht
        )

        bkg = self.estimate_background(sci, mask_final)
        bkgd_subtracted = sci - bkg.background

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
