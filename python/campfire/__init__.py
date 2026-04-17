"""
CAMPFIRE Python API Client

Python interface for querying and downloading NIRSpec spectroscopic data
from the CAMPFIRE archive (COSMOS Archive of MultiPle-Field Internal Reductions & Extractions).
"""

from .client import Campfire
from .models import (
    SpectrumData,
    Spectrum,
    SpectrumCollection,
    Photometry,
    Band,
    Object,
)
from .exceptions import (
    CampfireError,
    AuthenticationError,
    NotFoundError,
    DownloadError,
    ValidationError,
    APIError,
)
from .flags import (
    RedshiftQuality,
    DQFlags,
    FlagQuery,
    list_flags,
    decode_flags,
    encode_flags,
)

# Lazy imports for optional dependencies (plotly for spectra, matplotlib for imaging)
def __getattr__(name):
    if name in ("plot_spectrum", "plot_redshift_fit", "plot_spectrum_simple",
                "EMISSION_LINES", "convert_flux_units", "get_emission_lines"):
        from . import plotting
        return getattr(plotting, name)
    if name in ("plot_cutout",):
        from . import imaging
        return getattr(imaging, name)
    if name in ("calibrate_to_photometry", "calibrate_and_stack",
                "stack_spectra", "synthetic_photometry",
                "CalibrationResult", "StackResult"):
        from . import calibration
        return getattr(calibration, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__version__ = "0.4.0"
__all__ = [
    "Campfire",
    "SpectrumData",
    "Spectrum",
    "SpectrumCollection",
    "Photometry",
    "Band",
    "Object",
    # Exceptions
    "CampfireError",
    "AuthenticationError",
    "NotFoundError",
    "DownloadError",
    "ValidationError",
    "APIError",
    # Flags
    "RedshiftQuality",
    "DQFlags",
    "FlagQuery",
    "list_flags",
    "decode_flags",
    "encode_flags",
    # Plotting (lazy loaded)
    "plot_spectrum",
    "plot_redshift_fit",
    "plot_spectrum_simple",
    "EMISSION_LINES",
    "convert_flux_units",
    "get_emission_lines",
    # Imaging (lazy loaded)
    "plot_cutout",
    # Calibration / stacking (lazy loaded)
    "calibrate_to_photometry",
    "calibrate_and_stack",
    "stack_spectra",
    "synthetic_photometry",
    "CalibrationResult",
    "StackResult",
]
