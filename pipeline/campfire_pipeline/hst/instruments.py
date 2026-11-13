"""Per-detector constants for the HST imaging detectors CAMPFIRE reduces.

One :class:`Detector` per supported detector. ``token`` is the name used in
product names, directories and config tables (``acswfc``, ``wfc3ir``), so that a
filter name shared between detectors (F606W on ACS/WFC and WFC3/UVIS; F164N on
WFC3/IR and NIRCam) never collides.

DQ policy. HST data-quality arrays are 16-bit masks whose meanings differ per
detector (ACS Data Handbook table 3.4; WFC3 Data Handbook table 2.7). A pixel
is usable when it has no bit set outside ``tolerated_bits``. The defaults are
deliberately strict:

* ACS/WFC tolerates only bit 64 (warm pixel, 0.06-0.14 e-/s, dark-subtracted).
  Hot (16), unstable (32), bad-column (128), saturated (256), sink (1024) and
  cosmic-ray (4096, 8192) pixels are dropped.
* WFC3/IR tolerates only bit 16 (stable hot pixel, dark-calibrated since the
  2019 bad-pixel tables). Bit 512 (bad flat value, which includes the "blobs")
  is dropped by default; ``tolerated_bits`` can add it per filter where the
  delta-flat blob correction applies.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Detector:
    token: str              # product-name token
    instrume: str           # INSTRUME keyword
    detector: str           # DETECTOR keyword
    sci_extvers: tuple      # EXTVER of each chip's SCI/ERR/DQ imset
    calibrated_suffix: str  # canonical calibrated file suffix
    tolerated_bits: int     # DQ bits that do not make a pixel bad
    native_scale: float     # nominal arcsec / pixel (for reporting only)
    apply_var_scale: bool   # scale the ERR-based background variance by the
                            # measured pixel scatter (CFVARSC of the sky step)


# The sky step measures, for every detector, the scatter of blank background
# against the variance derived from ERR and records the ratio as CFVARSC.
# Whether the adapter then scales its variance by that ratio is per detector:
#
# * ACS/WFC, yes. On CTE-corrected data ERR is low by tens of percent in
#   variance, and CCD pixels are independent, so the pixel scatter is the noise.
# * WFC3/IR, no. The pixel scatter is 0.63 to 0.80 of ERR**2 (eight exposures of
#   one visit, four filters), but neighboring pixels are correlated: the
#   variance of 2x2 block averages times 4 is 1.09 to 1.17 times the
#   single-pixel variance, and of 4x4 blocks times 16 is 1.27 to 1.50 times.
#   ERR is therefore about right for an aperture a few pixels across, and
#   scaling to the pixel scatter would understate aperture noise by about 15
#   percent (docs/hst/design.md section 8b, 2026-11-13).
ACS_WFC = Detector(
    token='acswfc', instrume='ACS', detector='WFC',
    sci_extvers=(1, 2), calibrated_suffix='flc',
    tolerated_bits=64, native_scale=0.05, apply_var_scale=True,
)

WFC3_IR = Detector(
    token='wfc3ir', instrume='WFC3', detector='IR',
    sci_extvers=(1,), calibrated_suffix='flt',
    tolerated_bits=16, native_scale=0.128, apply_var_scale=False,
)

DETECTORS = {d.token: d for d in (ACS_WFC, WFC3_IR)}


def detector_for(instrume, detector):
    """The :class:`Detector` for a file's ``INSTRUME`` / ``DETECTOR`` keywords.

    Raises ``ValueError`` for anything else (WFC3/UVIS, ACS/HRC, ...): those are
    not supported, and a file from one of them must not be silently reduced with
    another detector's settings.
    """
    for det in DETECTORS.values():
        if (det.instrume, det.detector) == (str(instrume).upper(),
                                            str(detector).upper()):
            return det
    raise ValueError(
        f"Unsupported HST detector {instrume}/{detector}; supported: "
        + ', '.join(f'{d.instrume}/{d.detector}' for d in DETECTORS.values()))


def filter_name(header):
    """Lower-case filter of an HST primary header.

    WFC3 has ``FILTER``. ACS has two wheels, ``FILTER1`` and ``FILTER2``, one of
    which holds a ``CLEAR`` element for ordinary imaging; an exposure with two
    real elements (a polarizer plus a filter) is reported as both, joined by
    ``+``, so it can never be mistaken for the plain filter.
    """
    if header.get('FILTER'):
        return str(header['FILTER']).strip().lower()
    elements = [str(header.get(k, '')).strip() for k in ('FILTER1', 'FILTER2')]
    real = [e.lower() for e in elements if e and not e.upper().startswith('CLEAR')]
    if not real:
        return 'clear'
    return '+'.join(real)
