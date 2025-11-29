"""
NIRSpec Data Reduction Pipeline

This package contains the core data reduction pipeline for processing JWST NIRSpec
observations through preprocessing, spectrum extraction, and redshift fitting phases.

Main modules:
- reduction: Preprocessing and spectrum extraction pipeline
- fitting: Redshift fitting using template SED matching
- plots: Visualization and quality assurance plotting
"""

from .reduction import ReductionEngine

__version__ = "0.2.0"

__all__ = [
    "ReductionEngine",
]
