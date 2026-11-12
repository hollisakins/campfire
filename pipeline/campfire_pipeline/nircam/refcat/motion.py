"""Moved to :mod:`campfire_pipeline.imaging.refcat.motion` (shared with HST).

This name is an alias of that module — the very same module object — so
existing imports, attribute access and monkeypatching behave as before.
"""
import sys

from campfire_pipeline.imaging.refcat import motion as _moved

sys.modules[__name__] = _moved
