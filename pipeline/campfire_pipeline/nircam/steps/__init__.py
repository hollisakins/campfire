"""
Per-step modules for the NIRCam pipeline.

Each module owns one logical step (detector1, persistence, wisp, striping,
image2, edge, sky, variance, jhat, apply_masks, bad_pixel, outlier,
resample). Steps operate on the canonical per-exposure FITS file at
``field.get_exposure_path(rootname, filter)`` and stamp a ``CFP_<step>``
keyword via ``common.io.atomic_save`` once they finish.
"""
