"""
Per-step modules for the NIRCam pipeline.

Each module owns one logical step (detector1, persistence, wisp, striping,
image2, edge, sky, variance, jhat, apply_masks, bad_pixel, skymatch, outlier,
resample). Steps operate on the canonical per-exposure FITS file at
``field.get_exposure_path(rootname, filter)`` and stamp a ``CFP_<step>``
keyword via ``common.io.atomic_save`` once they finish.

The legacy ``stage1.py`` / ``stage2.py`` / ``stage3.py`` modules and the
``cfpipe nircam stage{1,2,3}`` CLI continue to work against the old
``stage1_dir`` / ``stage2_dir`` / ``stage3_dir`` layout until the cleanup
commit at the end of the restructure.
"""
