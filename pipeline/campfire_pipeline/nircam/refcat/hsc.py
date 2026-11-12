"""Moved to :mod:`campfire_pipeline.imaging.refcat.hsc` (shared with HST).

This name is an alias of that module — the very same module object — so
existing imports, attribute access and monkeypatching behave as before.
"""
import sys

from campfire_pipeline.imaging.refcat import hsc as _moved

sys.modules[__name__] = _moved
