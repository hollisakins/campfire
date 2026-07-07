"""
bkg: unified per-exposure background step (replaces striping + sky + variance).

Per-exposure step. Runs **after** ``image2`` and ``edge``, on flat-fielded,
flux-calibrated cal-stage data. One iterative chain, one shared source mask:

    for _ in range(n_iterations):
        mask   = SubtractBackground.mask_from_arrays(residual)   # mask only
        ped    = per-amp pedestal (owns the per-exposure DC — skymatch)
        vcol   = per-column (vertical) 1/f
        h      = amp-row 1/f: GP ρ≈5 (fine) then GP ρ≈20 (banding)
        residual -= ped + vcol + h
    rescale VAR_RNOISE on the final mask

The source mask is built by ``SubtractBackground`` (mask only — **no** 2-D
background subtraction; the astrophysical sky is left for the mosaic), at a
mosaic-like depth, with LW pixel-scaling. The per-amp pedestal carries the DC so
the amp-row GPs are fit on a ~zero-per-amp-mean residual (design §4.5). The GP
(`gp_striping.gp_amprow_offsets`) is unchanged and simply called twice.

Writes the ``SRCMASK`` extension (consumed by ``diag_striping``), stamps
``CFP_BKG``, sets ``meta.background.*``, and rescales ``VAR_RNOISE``.

See ``docs/design-nircam-unified-background.md``.
"""

import os
from datetime import datetime

import numpy as np
from astropy.io import fits

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam.bkgsub import SubtractBackground
from campfire_pipeline.nircam import oneoverf


# Mask parameters are angular scales in pixels, tuned at 30 mas; rescale per
# channel (SW native ≈31 mas → 1.0; LW native ≈63 mas → 0.5) to hold the
# angular scale fixed. Linear lengths × f, area counts × f², rest unchanged.
_MASK_LINEAR = ('ring_radius_in', 'ring_width', 'ring_clip_box_size')
_MASK_LINEAR_LIST = ('tier_kernel_size', 'tier_dilate_size')
_MASK_AREA_LIST = ('tier_npixels',)


def _scale_mask_config(mask_cfg, channel, factors):
    """Return a copy of ``mask_cfg`` with length params scaled for ``channel``."""
    f = float(factors.get(channel, 1.0))
    out = {k: v for k, v in mask_cfg.items()}
    if f == 1.0:
        return out
    for k in _MASK_LINEAR:
        if k in out:
            out[k] = type(out[k])(out[k] * f)
    for k in _MASK_LINEAR_LIST:
        if k in out:
            out[k] = [max(1, int(round(v * f))) for v in out[k]]
    for k in _MASK_AREA_LIST:
        if k in out:
            out[k] = [max(1, int(round(v * f * f))) for v in out[k]]
    return out


def bkg_step(exposure_file, field, step_config, overwrite=False, status=None):
    """Run the unified background step on a single canonical exposure."""
    rootname = os.path.basename(exposure_file).removesuffix('.fits')

    if cfp.should_skip(exposure_file, 'CFP_BKG', rootname,
                       'bkg', status, overwrite):
        return

    log(f"Running bkg on {rootname}")

    from jwst.datamodels import ImageModel, dqflags
    from stdatamodels import util as stutil

    n_iter = int(step_config.get('n_iterations', 3))
    do_plot = step_config.get('plot', True)

    mask_cfg = dict(step_config.get('mask', {}))
    ped_cfg = step_config.get('pedestal', {})
    strp_cfg = step_config.get('striping', {})
    var_cfg = step_config.get('variance', {})

    estimator = strp_cfg.get('estimator', 'gp')
    _VALID = ('gp', 'median', 'none')
    if estimator not in _VALID:
        raise ValueError(
            f"[nircam.bkg.striping].estimator={estimator!r} not in {_VALID}")
    maxiters = int(strp_cfg.get('maxiters', 3))
    remask = strp_cfg.get('remask_each_iter', True)
    gp_cfg = strp_cfg.get('gp', {})
    rho_short = gp_cfg.get('rho_short', 5.0)
    rho_long = gp_cfg.get('rho_long', 20.0)
    ped_sigma = ped_cfg.get('sigma', 3.0)
    block_size = var_cfg.get('block_size', 7)

    pix_factors = mask_cfg.pop('pixel_scale_factor', {'sw': 1.0, 'lw': 0.5})
    aggressive_dq = mask_cfg.pop('mask_aggressive_dq', True)

    with ImageModel(exposure_file, memmap=False) as model:
        sci0 = model.data.copy()
        err = model.err
        dq = model.dq

        channel_meta = (getattr(model.meta.instrument, 'channel', None)
                        or 'short').lower()
        channel = 'lw' if channel_meta.startswith('l') else 'sw'
        mcfg = _scale_mask_config(mask_cfg, channel, pix_factors)
        sb = SubtractBackground.from_config(mcfg)

        # DQ bits that mask the *fit* (not detection). DO_NOT_USE always;
        # transient classes too when aggressive (they carry residual signal
        # that would bias the per-amp-row estimate; the GP tolerates the extra
        # masking via inflated per-row sigma).
        dq_bits = dqflags.pixel['DO_NOT_USE']
        if aggressive_dq:
            for bit in ('JUMP_DET', 'SATURATED', 'PERSISTENCE'):
                dq_bits |= dqflags.pixel[bit]
        dq_fit = np.bitwise_and(dq, dq_bits) != 0

        resid = sci0.astype(np.float64, copy=True)
        correction = np.zeros_like(resid)
        srcmask = None
        ks_last = ''
        ampc_last = ''

        for it in range(n_iter):
            # (1) SOURCE MASK — rebuilt on the running residual (sharpens as the
            #     frame flattens). mask_from_arrays = mask only, no 2-D bg.
            if srcmask is None or remask:
                srcmask, _ = sb.mask_from_arrays(resid, err, dq)
            fitmask = srcmask | dq_fit

            # (2) PER-AMP PEDESTAL — owns the DC (skymatch, §4.5)
            ped, _ = oneoverf.peramp_pedestal(resid, fitmask,
                                              sigma=ped_sigma, maxiters=maxiters)

            # (3) VERTICAL + (4) HORIZONTAL 1/f
            if estimator == 'none':
                vcol = np.zeros_like(resid)
                h = np.zeros_like(resid)
            elif estimator == 'gp':
                from campfire_pipeline.nircam.gp_striping import gp_amprow_offsets
                vcol = oneoverf.column_pattern(resid - ped, fitmask, maxiters)
                base = resid - ped - vcol
                gp_kw = dict(
                    kernel_sigma_factor=gp_cfg.get('kernel_sigma_factor', 1.0),
                    q=gp_cfg.get('q', 1.0 / np.sqrt(2.0)),
                    sigma_clip_sigma=gp_cfg.get('sigma_clip', 2.0),
                    maxiters=maxiters,
                    weak_frac=gp_cfg.get('weak_frac', 0.5),
                )
                h5, _, ks5 = gp_amprow_offsets(base, fitmask, rho=rho_short, **gp_kw)
                h20, _, ks20 = gp_amprow_offsets(base - h5, fitmask,
                                                 rho=rho_long, **gp_kw)
                h = h5 + h20
                ks_last = f'{ks5:.3e}/{ks20:.3e}'
            else:  # 'median' — legacy reference arm (h+v together)
                h, vcol, ampc, _ = oneoverf.fit_residual_striping(
                    resid - ped, fitmask, maxiters, estimator='median')
                ampc_last = ','.join(ampc)

            step = ped + vcol + h
            resid -= step
            correction += step

        # (5) VARIANCE rescale on the final mask
        factor = oneoverf.variance_rescale(sci0, model.var_rnoise, srcmask,
                                           block_size)

        # (6) Write. SCI = original - accumulated correction.
        outsci = sci0 - correction
        outsci[sci0 == 0] = 0
        wnan = np.isnan(outsci)
        outsci[wnan] = 0
        model.dq[wnan] = np.bitwise_or(model.dq[wnan],
                                       dqflags.pixel['DO_NOT_USE'])
        model.data = outsci.astype(sci0.dtype)

        # Per-exposure DC removed (skymatch record) = frame mean of the total
        # correction over background pixels.
        good = (~srcmask) & np.isfinite(correction) & (sci0 != 0)
        bkg_level = float(np.mean(correction[good])) if good.any() else 0.0
        model.meta.background.level = bkg_level
        model.meta.background.subtracted = True
        model.meta.background.method = 'local'

        model.var_rnoise = factor * model.var_rnoise
        for name in ('var_rnoise', 'var_poisson', 'var_flat'):
            arr = getattr(model, name)
            arr[arr == 0] = np.inf
            setattr(model, name, arr)

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        model.history.append(stutil.create_history_entry(
            f'Unified bkg (pedestal + 1/f + variance) {now}'))

        cfp_value = (
            f'estimator={estimator}, n_iter={n_iter}, channel={channel}, '
            f'pedestal={bkg_level:.5e}, var_factor={factor:.3f}'
        )
        if estimator == 'gp':
            cfp_value += (f', rho_short={rho_short}, rho_long={rho_long}, '
                          f'kernel_sigma[last]={ks_last}')
        elif estimator == 'median':
            cfp_value += f', fallbacks[last]={ampc_last}'

        sci_after = model.data.copy() if do_plot else None

        srcmask_hdu = fits.ImageHDU(srcmask.astype('uint8'), name='SRCMASK')
        atomic_save(
            model, exposure_file,
            header_updates=cfp.format(CFP_BKG=cfp_value),
            extra_hdus=[srcmask_hdu],
        )
        log(f"bkg done (pedestal={bkg_level:.5e}, var_factor={factor:.2f}): "
            f"{rootname}")

    if do_plot:
        try:
            from campfire_pipeline.nircam.steps._plots import plot_two
            pdf = os.path.join(os.path.dirname(exposure_file),
                               f'{rootname}_bkg.pdf')
            plot_two(sci0, sci_after, title1=f'{rootname}: before',
                     title2='after bkg', save_file=pdf, scaling=1)
            log(f"Saved {os.path.basename(pdf)}")
        except Exception as e:  # plotting must never fail the step
            log(f"bkg plot failed for {rootname}: {e}")
