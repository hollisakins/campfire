"""
NIRSpec Data Reduction Pipeline — backwards compatibility shim.

The canonical package is now ``campfire_pipeline``.
This file re-exports ReductionEngine so that existing code like
``from pipeline import ReductionEngine`` continues to work.
"""

from campfire_pipeline.nirspec.engine import ReductionEngine

__version__ = "0.3.0"

__all__ = [
    "ReductionEngine",
]
