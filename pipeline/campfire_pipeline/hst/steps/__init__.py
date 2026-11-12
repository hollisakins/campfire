"""Per-exposure and per-visit steps of the HST process phase.

Each step works on a canonical calibrated exposure (``<rootname>_flc.fits`` or
``<rootname>_flt.fits``) and records itself with a ``CFP_*`` keyword from the
``HST`` key set in ``campfire_pipeline.common.cfp``.
"""
