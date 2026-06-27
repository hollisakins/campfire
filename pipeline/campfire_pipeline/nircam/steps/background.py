"""
background: per-exposure 2D background subtraction.

Per-exposure step. Runs **after ``image2`` and before ``artifact``/``striping``**
(order: ... image2 -> background -> artifact -> striping -> ...) so the smooth
background — sky-fixed (ICL, zodi gradients) *and* detector-fixed (smooth
persistence / scattered light) — is removed before the 1/f stripe offsets are
measured. A non-uniform background biases the per-amp-row clipped medians; the
striping step's own fit-only detrend mitigates but doesn't eliminate it, so
removing it for real upstream is strictly better.

Why per-exposure (not a sky-frame / drizzle background): only a model built in
the exposure's *own* frame sees both the sky-fixed ICL and the detector-fixed
smooth component as background. A sky-frame median/drizzle would capture only
the sky-fixed part and leave detector-fixed persistence behind.

Reuses the nircamx ``SubtractBackground`` (``bkgsub.py``) — the same tiered-
mask + ``clipped_ring_median`` + ``Background2D`` algorithm the mosaic uses,
explicitly tuned to preserve galaxy outskirts — so the per-exposure and mosaic
backgrounds are consistent. ``SubtractBackground.compute`` does all the work in
memory and returns ``sci - background``; this step writes that back to the
canonical SCI and stamps ``CFP_BKG``.

Opt-in: disabled unless ``[nircam.background].enabled = true`` (a per-exposure
2D subtraction changes flux for every field, so the default reproduces current
behaviour). The shallow-vs-deep mask trade-off (single exposure is shallower
than the mosaic, so faint sources can leak into the model) is the known cost;
the box size and the ``clipped_ring_median`` ceiling bound it, and the A/B's
photometry-conservation metric sets the box.
"""

import os
from datetime import datetime

import numpy as np

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp


def background_step(exposure_file, field, step_config, overwrite=False,
                    status=None):
    """Subtract a per-exposure 2D background from a single canonical exposure.

    Parameters
    ----------
    exposure_file : str
        Canonical ``<rootname>.fits`` path (post-image2, cal-stage).
    field : Field
    step_config : dict
        ``[nircam.background]`` block. Keys matching ``SubtractBackground``
        dataclass fields (``bg_box_size``, ``bg_filter_size``, ``ring_*``,
        ``tier_*`` ...) are forwarded to it; ``enabled`` / ``plot`` are read here.
    overwrite : bool
    status : StepStatus, optional
        Pre-scanned CFP_* status cache.
    """
    plot = step_config.get('plot', True)
    rootname = os.path.basename(exposure_file).removesuffix('.fits')

    if cfp.should_skip(exposure_file, 'CFP_BKG', rootname,
                       'background', status, overwrite):
        return

    log(f"Running background subtraction on {rootname}")

    from jwst.datamodels import ImageModel, dqflags
    from campfire_pipeline.nircam.bkgsub import SubtractBackground

    # Build from the config section (unknown keys ignored) and run the
    # tiered-mask + Background2D estimate in memory.
    bg = SubtractBackground.from_config(step_config)
    bkgsub_sci, _mask_final, _bitmask = bg.compute(exposure_file)

    model = ImageModel(exposure_file, memmap=False)
    sci_before = model.data.copy()
    bg_model = sci_before - bkgsub_sci

    # Mirror striping's output hygiene: keep blank pixels blank, flag any NaN
    # the subtraction produced as DO_NOT_USE (the bg model is finite, so NaNs
    # come from NaN SCI input).
    outsci = bkgsub_sci.astype(model.data.dtype, copy=True)
    outsci[sci_before == 0] = 0
    wnan = np.isnan(outsci)
    outsci[wnan] = 0
    model.dq[wnan] = np.bitwise_or(model.dq[wnan], dqflags.pixel['DO_NOT_USE'])
    model.data = outsci

    from stdatamodels import util as stutil
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    model.history.append(stutil.create_history_entry(
        f'Subtracted per-exposure 2D background '
        f'(SubtractBackground, box={bg.bg_box_size}); {now}'))

    cfp_value = (f'SubtractBackground box={bg.bg_box_size}, '
                 f'filter={bg.bg_filter_size}, ring_radius={bg.ring_radius_in}, '
                 f'exclude_pct={bg.bg_exclude_percentile}')
    atomic_save(model, exposure_file,
                header_updates=cfp.format(CFP_BKG=cfp_value))
    sci_after = model.data.copy()
    model.close()
    log(f"Background subtracted: {rootname}")

    if plot:
        from campfire_pipeline.nircam.steps._plots import plot_two
        bkg_pdf = os.path.join(os.path.dirname(exposure_file),
                               f'{rootname}_background.pdf')
        plot_two(sci_after, bg_model,
                 title1='Background subtracted', title2='2D background model',
                 save_file=bkg_pdf)
        log(f"Saved {os.path.basename(bkg_pdf)}")
