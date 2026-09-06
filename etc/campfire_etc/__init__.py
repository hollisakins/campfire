"""campfire-etc: empirical NIRSpec/MSA noise model, exposure-time calculator and
spectrum simulator built from the CAMPFIRE archive.

The calculator core (`model`, `sed`, `simulate`) depends on numpy only. The MCP
server (`server`) needs the `mcp` extra; rebuilding the noise model from a local
archive (`build`) needs the `build` extra.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from .model import (
    Disperser,
    Exposure,
    NoiseModel,
    continuum,
    line,
    load_model,
    make_exposure,
    resolve_placement,
    source_flux,
    time_for_snr,
)
from .sed import SED, parse_sed
from .simulate import simulate

try:
    __version__ = _pkg_version("campfire-etc")
except PackageNotFoundError:  # editable checkout without metadata
    __version__ = "0.0.0"

__all__ = [
    "Disperser",
    "Exposure",
    "NoiseModel",
    "SED",
    "continuum",
    "line",
    "load_model",
    "make_exposure",
    "parse_sed",
    "resolve_placement",
    "simulate",
    "source_flux",
    "time_for_snr",
    "__version__",
]
