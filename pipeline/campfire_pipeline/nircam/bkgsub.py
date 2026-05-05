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
"""

import os
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np
from astropy import stats as astrostats
from astropy.convolution import Gaussian2DKernel, Ring2DKernel, convolve_fft
from astropy.io import fits
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
from photutils.utils import circular_footprint
from scipy.ndimage import median_filter

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

    # -- Tiered source masking -------------------------------------------------
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
        """Read SCI and ERR (or RMS) extensions; detect DQ if present."""
        with fits.open(filepath) as hdu:
            sci = hdu["SCI"].data
            try:
                err = hdu["ERR"].data
            except KeyError:
                # RMS map for HST
                err = hdu["RMS"].data

            self.has_dq = False
            for h in hdu:
                if "EXTNAME" in h.header and h.header["EXTNAME"] == "DQ":
                    self.has_dq = True
                    self.dq = h.data
                    log(f"{os.path.basename(filepath)} has a DQ array")

        return sci, err

    # ------------------------------------------------------------------
    # Masking helpers
    # ------------------------------------------------------------------

    def replace_masked(
        self, sci: np.ndarray, mask: np.ndarray
    ) -> np.ndarray:
        """Fill masked pixels with a robust mean so convolution is clean."""
        sci_nan = np.choose(mask, (sci, np.nan))
        robust_mean = astrostats.biweight_location(sci_nan, c=6.0, ignore_nan=True)
        return np.choose(mask, (sci, robust_mean))

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
        self, sci: np.ndarray, mask: np.ndarray
    ) -> np.ndarray:
        """Ring-median filter with a sigma-clipped ceiling to preserve galaxy wings."""
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
        # RMS after subtracting the smooth background
        background_rms = astrostats.biweight_scale((sci - bkg.background)[~mask])
        # Floating ceiling: pixels above this are masked before ring-median
        ceiling = self.ring_clip_max_sigma * background_rms + bkg.background
        ceiling_mask = sci > ceiling

        log(
            f"Ring median filtering with radius = {self.ring_radius_in}, "
            f"width = {self.ring_width}"
        )
        sci_filled = self.replace_masked(sci, mask | ceiling_mask)
        ring = Ring2DKernel(self.ring_radius_in, self.ring_width)
        filtered = median_filter(sci_filled, footprint=ring.array)
        return sci - filtered

    # ------------------------------------------------------------------
    # Tiered source detection
    # ------------------------------------------------------------------

    def tier_mask(
        self,
        img: np.ndarray,
        mask: np.ndarray,
        tiernum: int = 0,
    ) -> Optional[np.ndarray]:
        """Detect sources at a single tier and return a boolean mask."""
        background_rms = astrostats.biweight_scale(img[~mask])
        background_level = astrostats.biweight_location(img[~mask])
        replaced_img = np.choose(mask, (img, background_level))

        convolved = convolve_fft(
            replaced_img,
            Gaussian2DKernel(self.tier_kernel_size[tiernum]),
            allow_huge=True,
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

        if self.tier_dilate_size[tiernum] == 0:
            tier_result = seg_detect.make_source_mask()
        else:
            footprint = circular_footprint(radius=self.tier_dilate_size[tiernum])
            tier_result = seg_detect.make_source_mask(footprint=footprint)

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
    ) -> np.ndarray:
        """Iteratively mask sources through all configured tiers."""
        first_mask = bitmask != 0
        log(f"Ring-filtered background median: {np.median(img[~first_mask])}")
        for tiernum in range(len(self.tier_nsigma)):
            mask = self.tier_mask(img, first_mask, tiernum=tiernum)
            if mask is None:
                continue
            bitmask = np.bitwise_or(
                bitmask, np.left_shift(mask, tiernum + starting_bit)
            )
        return bitmask

    # ------------------------------------------------------------------
    # Background estimation
    # ------------------------------------------------------------------

    def estimate_background(
        self, img: np.ndarray, mask: np.ndarray
    ) -> Background2D:
        """Compute a smooth 2-D background on unmasked pixels."""
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

        bitmask = np.zeros(sci.shape, np.uint32)

        off_detector_mask = self.off_detector(sci, err)
        if self.has_dq:
            self.mask_by_dq()
            mask = off_detector_mask | self.dqmask
        else:
            mask = off_detector_mask
        mask = np.logical_or(mask, np.isnan(sci))
        bitmask = np.bitwise_or(bitmask, np.left_shift(mask, 0))

        filtered = self.clipped_ring_median_filter(sci, mask)
        bitmask = self.mask_sources(filtered, bitmask, starting_bit=1)
        mask_final = bitmask != 0

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
