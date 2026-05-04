"""
campfire-pipeline: JWST data reduction pipeline.

Instrument-specific modules:
- campfire_pipeline.nirspec: NIRSpec MSA spectroscopy
- campfire_pipeline.nircam: NIRCam imaging
- campfire_pipeline.common: Shared utilities (WCS, spectral math, I/O)
- campfire_pipeline.metadata: Product metadata and observation summaries

The package version is resolved lazily from setuptools-scm git tags
(`pipeline-vX.Y.Z`) at first access; see ``common.version.get_reduction_version``.
"""


def __getattr__(name):
    if name == "__version__":
        from campfire_pipeline.common.version import get_reduction_version
        return get_reduction_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
