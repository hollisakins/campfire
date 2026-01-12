"""
CAMPFIRE Python API Client

Python interface for querying and downloading NIRSpec spectroscopic data
from the CAMPFIRE archive (COSMOS Archive of MultiPle-Field Internal Reductions & Extractions).
"""

from .client import Campfire
from .exceptions import (
    CampfireError,
    AuthenticationError,
    NotFoundError,
    DownloadError,
    ValidationError,
    APIError,
)

# Lazy import for plotting to avoid requiring plotly for basic usage
def __getattr__(name):
    if name in ("plot_spectrum", "plot_redshift_fit", "plot_spectrum_simple",
                "EMISSION_LINES", "convert_flux_units", "get_emission_lines"):
        from . import plotting
        return getattr(plotting, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__version__ = "0.1.0"
__all__ = [
    "Campfire",
    "CampfireError",
    "AuthenticationError",
    "NotFoundError",
    "DownloadError",
    "ValidationError",
    "APIError",
    # Plotting (lazy loaded)
    "plot_spectrum",
    "plot_redshift_fit",
    "plot_spectrum_simple",
    "EMISSION_LINES",
    "convert_flux_units",
    "get_emission_lines",
]
