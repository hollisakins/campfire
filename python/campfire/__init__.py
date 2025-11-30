"""
CAMPFIRE Python API Client

Python interface for querying and downloading NIRSpec spectroscopic data
from the CAMPFIRE archive (COSMOS Archive of MultiPle-Field Internal Reductions & Extractions).
"""

from .client import Campfire
from .exceptions import CampfireError, AuthenticationError, NotFoundError

__version__ = "0.1.0"
__all__ = ["Campfire", "CampfireError", "AuthenticationError", "NotFoundError"]
