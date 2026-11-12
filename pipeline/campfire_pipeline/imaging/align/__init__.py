"""Instrument-neutral parts of the astrometric ``align`` method.

``detect`` (source detection on one chip), ``footprint`` (clip a reference
catalog to an exposure's footprint) and ``histmatch`` (the offset-histogram
catalog matcher). The solver and the I/O layer are still NIRCam-specific and
live in ``campfire_pipeline.nircam.align``.

Nothing is imported here on purpose: the align modules pull in ``tweakwcs`` and
``sep``, and a caller should pay for only the one it uses.
"""
