"""
bkg: unified per-exposure background step (replaces striping + sky + variance).

Per-exposure step. Runs **after** ``image2`` and ``edge``, on flat-fielded,
flux-calibrated cal-stage data. One iterative chain, one shared source mask:

    for _ in range(n_iterations):
        mask    = SubtractBackground.mask_from_arrays(residual)  # mask only
        detrend = FIT-ONLY coarse-box 2-D structure model (zero-median),
                  fit directly on the residual — the coarse box is the
                  protection against absorbing detector striping
        ped     = pedestal on (residual - detrend)   # owns the DC (skymatch)
        vcol    = per-column (vertical) 1/f, on the conditioned residual
        h       = amp-row 1/f: GP ρ≈5 then ρ≈20, on the conditioned residual
        b2d     = optional APPLIED smooth 2-D background (subtract_2d)
        residual -= ped + vcol + h + b2d             # detrend NOT subtracted
    rescale VAR_RNOISE on (residual - detrend), final mask

Two 2-D fits with opposite jobs (design realization 2026-07-17; the split
mirrors both the retired striping step's fit-only ``skyfit`` detrend and
R. Endsley's ``subtract_2d_before_1f``):

* The **conditioning detrend** exists to make the 1/f fit well-posed: sky
  gradients and diffuse (scattered-light) structure are per-amp-asymmetric,
  so the per-amp pedestal / GP DC means absorb them differently per amp and
  imprint seams at columns 512/1024/1536. The detrend is fit-only — never
  subtracted — so flux conservation cannot constrain it (a fit that removes
  nothing cannot harm it). Uses a COARSE box (256 px SW -> 128 px LW), no
  mask growth, no rejection: the coarse mesh conditions the smooth
  component — the actual seam driver — while being structurally unable to
  absorb the banding / amp-DC detail the per-amp terms are meant to fit.
  Fine boxes measured slightly WORSE on real frames (rj0911 detrend-box A/B
  2026-07-17). Always on by default; with it, the pedestal's per-amp scope
  is safe even under strong gradients.
* The **applied fit** (``subtract_2d``, opt-in per field) is the sky-match /
  ICL-removal subtraction: a *smooth* model (64 px SW -> 32 px LW — finer
  than the detrend, but deliberately gentle — plus grown source mask and
  map-outlier reject) whose parameters are set by flux conservation — zero
  median aperture loss in the synthetic sweep — not by flatness, which the
  detrend now owns.

The source mask is built by ``SubtractBackground`` (mask only), at a
mosaic-like depth, with LW pixel-scaling. The GP
(`gp_striping.gp_amprow_offsets`) is unchanged and simply called twice.

Writes the ``SRCMASK`` extension (consumed by ``diag_striping``), stamps
``CFP_BKG``, sets ``meta.background.*``, and rescales ``VAR_RNOISE``.

See ``docs/design-nircam-unified-background.md``.
"""

import os
from datetime import datetime

import numpy as np
from astropy.io import fits
from scipy.ndimage import distance_transform_edt

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


def bkg_step(exposure_file, field, step_config, overwrite=False, status=None,
             components_out=None):
    """Run the unified background step on a single canonical exposure.

    ``components_out``: optional dict — when passed, filled with the
    accumulated per-component correction arrays (``before``, ``srcmask``,
    ``ped``, ``vcol``, ``h``, ``b2d``, ``det_struct`` [last iteration,
    fit-only], ``after``, ``fitmask``) for diagnostics harnesses. No effect
    on processing.
    """
    rootname = os.path.basename(exposure_file).removesuffix('.fits')

    if cfp.should_skip(exposure_file, 'CFP_BKG', rootname,
                       'bkg', status, overwrite):
        return

    # Guard against double-correcting legacy canonical files. Products reduced
    # before this unification carry the retired CFP_1F/CFP_SKY/CFP_VAR stamps
    # (their SCI is already striping/sky/variance-corrected) but no CFP_BKG, so
    # the skip check above would let bkg run and compound the pedestal/1f +
    # rescale VAR_RNOISE again. Those keys are no longer in the keyset, so read
    # the raw header. Fail loud — rebuild from uncal rather than corrupt.
    with fits.open(exposure_file, memmap=False) as _hdul:
        _legacy = [k for k in ('CFP_1F', 'CFP_SKY', 'CFP_VAR')
                   if k in _hdul[0].header]
    if _legacy:
        raise RuntimeError(
            f"{rootname}: reduced with the retired {'/'.join(_legacy)} chain; "
            f"the unified bkg step would double-subtract. Rebuild from uncal "
            f"(or `cfpipe nircam reset --from image2`) before running bkg.")

    log(f"Running bkg on {rootname}")

    from jwst.datamodels import ImageModel, dqflags
    from stdatamodels import util as stutil

    n_iter = int(step_config.get('n_iterations', 3))
    do_plot = step_config.get('plot', True)

    mask_cfg = dict(step_config.get('mask', {}))
    ped_cfg = step_config.get('pedestal', {})
    strp_cfg = step_config.get('striping', {})
    var_cfg = step_config.get('variance', {})
    subtract_2d = bool(step_config.get('subtract_2d', False))
    b2d_cfg = step_config.get('bkg2d', {})
    det_cfg = step_config.get('detrend', {})
    detrend_on = bool(det_cfg.get('enabled', True))

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

    # Pedestal scope. With the conditioning detrend on, the pedestal measures
    # on a structure-free residual, so the per-amp scope is safe under any
    # gradient (the sawtooth pathology needs structure to stairstep). 'auto'
    # therefore resolves to per_amp — except in the detrend-off escape hatch
    # with subtract_2d on, where the frame scope avoids the sawtooth.
    ped_scope = ped_cfg.get('scope', 'auto')
    _SCOPES = ('auto', 'per_amp', 'frame')
    if ped_scope not in _SCOPES:
        raise ValueError(
            f"[nircam.bkg.pedestal].scope={ped_scope!r} not in {_SCOPES}")
    if ped_scope == 'auto':
        ped_scope = ('frame' if (subtract_2d and not detrend_on)
                     else 'per_amp')
    pedestal_fn = (oneoverf.frame_pedestal if ped_scope == 'frame'
                   else oneoverf.peramp_pedestal)

    pix_factors = mask_cfg.pop('pixel_scale_factor', {'sw': 1.0, 'lw': 0.5})
    aggressive_dq = mask_cfg.pop('mask_aggressive_dq', True)
    # Pathology guard: an aggressive-DQ class that blankets more than this
    # fraction of the frame is a broken calibration step (e.g. JUMP_DET runaway
    # on low-NGROUPS ramps), not real transients — folding it into the fit mask
    # starves the Background2D detrend of unmasked boxes and hard-fails the step.
    # Such a bit is dropped from the fit mask per-exposure. 1.0 disables the guard.
    # Default 0.85 targets only the near-total blankets that actually starve the
    # fit; borderline low-NGROUPS visits (~50% salt-and-pepper) fit fine either
    # way and are left untouched (consistent across modules).
    aggressive_dq_max_frac = float(
        mask_cfg.pop('mask_aggressive_dq_max_frac', 0.85))

    with ImageModel(exposure_file, memmap=False) as model:
        sci0 = model.data.copy()
        err = model.err
        dq = model.dq

        channel_meta = (getattr(model.meta.instrument, 'channel', None)
                        or 'short').lower()
        channel = 'lw' if channel_meta.startswith('l') else 'sw'
        mcfg = _scale_mask_config(mask_cfg, channel, pix_factors)
        f2 = float(pix_factors.get(channel, 1.0))
        if subtract_2d:
            # Applied-fit lengths are angular scales in px like the mask
            # params: scale per channel, then route the fit + reject config
            # through the same SubtractBackground instance the mask comes
            # from.
            b2d_box = max(1, int(round(b2d_cfg.get('box_size', 64) * f2)))
            b2d_extra_dilate = float(b2d_cfg.get('extra_dilate', 20)) * f2
            mcfg.update(
                bg_box_size=b2d_box,
                bg_filter_size=b2d_cfg.get('filter_size', 5),
                bg_sigma=b2d_cfg.get('sigma', 3.0),
                bg_exclude_percentile=b2d_cfg.get('exclude_percentile', 90),
                bg_reject=b2d_cfg.get('reject', True),
                bg_reject_sigma_hi=b2d_cfg.get('reject_sigma_hi', 4.0),
                bg_reject_sigma_lo=b2d_cfg.get('reject_sigma_lo', 3.0),
                bg_reject_percentile=b2d_cfg.get('reject_percentile', 60.0),
                bg_reject_dilate=float(b2d_cfg.get('reject_dilate', 40)) * f2,
            )
        sb = SubtractBackground.from_config(mcfg)

        # Conditioning detrend fitter: coarse box, undilated mask, no reject —
        # fit-only, so flux conservation cannot constrain it (see docstring).
        # Fallback mirrors config_default.toml (256 px SW -> 128 px LW); a fine
        # box here would let the detrend absorb the banding/amp-DC detail the
        # per-amp terms are supposed to fit.
        det_box = max(1, int(round(det_cfg.get('box_size', 256) * f2)))
        sb_det = SubtractBackground(
            bg_box_size=det_box,
            bg_filter_size=det_cfg.get('filter_size', 3),
            bg_sigma=det_cfg.get('sigma', 3.0),
            bg_exclude_percentile=det_cfg.get('exclude_percentile', 90),
            bg_reject=False,
        )

        # DQ bits that mask the *fit* (not detection). DO_NOT_USE always;
        # transient classes too when aggressive (they carry residual signal
        # that would bias the per-amp-row estimate; the GP tolerates the extra
        # masking via inflated per-row sigma).
        dq_bits = dqflags.pixel['DO_NOT_USE']
        if aggressive_dq:
            npix = dq.size
            for bit in ('JUMP_DET', 'SATURATED', 'PERSISTENCE'):
                bit_frac = float((np.bitwise_and(dq, dqflags.pixel[bit]) != 0
                                  ).sum()) / npix
                if bit_frac > aggressive_dq_max_frac:
                    log(f"  {rootname}: {bit} flags {100*bit_frac:.1f}% of the "
                        f"frame (> {100*aggressive_dq_max_frac:.0f}% guard) — "
                        f"spurious over-flagging, excluding it from the bkg fit "
                        f"mask (underlying pixels kept as good sky)")
                    continue
                dq_bits |= dqflags.pixel[bit]
        dq_fit = np.bitwise_and(dq, dq_bits) != 0

        resid = sci0.astype(np.float64, copy=True)
        correction = np.zeros_like(resid)
        srcmask = None
        srcbits = None
        ks_last = ''
        ampc_last = ''
        if components_out is not None:
            for k in ('ped', 'vcol', 'h', 'b2d'):
                components_out[k] = np.zeros_like(resid)

        for it in range(n_iter):
            # (1) SOURCE MASK — rebuilt on the running residual (sharpens as the
            #     frame flattens). mask_from_arrays = mask only, no 2-D bg.
            if srcmask is None or remask:
                srcmask, srcbits = sb.mask_from_arrays(resid, err, dq)
            fitmask = srcmask | dq_fit

            # (2) CONDITIONING DETREND — fit-only, zero-median structure
            #     model. Subtracted from the *measurement copies* below so
            #     the pedestal / 1/f terms see a structure-free residual;
            #     never enters the correction.
            #
            #     Fit DIRECTLY on the residual (Endsley's design: his
            #     conditioning sep.Background is likewise fit with no 1/f
            #     pre-removal) — the COARSE box is the theft protection, as
            #     a 128-native-px mesh cannot follow ~20-row banding. Do NOT
            #     pre-clean 1/f with a provisional row-median pass first:
            #     that pass absorbs the row-collapse of large diffuse
            #     structure (scattered light) before the detrend can see it,
            #     blinding the conditioning and pushing the structure into
            #     the per-amp h term as per-amp ramps — the within-amp
            #     top/bottom gradient pathology (diagnosed on rj0911 F444W
            #     NRCB, 2026-07-17, via the component harness).
            if detrend_on:
                det = sb_det.estimate_background(
                    resid, fitmask).background.astype(resid.dtype)
                good = ~fitmask & np.isfinite(resid)
                det_struct = det - (float(np.median(det[good]))
                                    if good.any() else 0.0)
            else:
                det_struct = 0.0
            cond = resid - det_struct

            # (3) PEDESTAL — owns the DC (skymatch, §4.5), measured on the
            #     conditioned residual so per-amp stays gradient-safe
            ped, _ = pedestal_fn(cond, fitmask,
                                 sigma=ped_sigma, maxiters=maxiters)

            # (4) VERTICAL + HORIZONTAL 1/f, on the conditioned residual
            if estimator == 'none':
                vcol = np.zeros_like(resid)
                h = np.zeros_like(resid)
            elif estimator == 'gp':
                from campfire_pipeline.nircam.gp_striping import gp_amprow_offsets
                vcol = oneoverf.column_pattern(cond - ped, fitmask, maxiters)
                base = cond - ped - vcol
                gp_kw = dict(
                    kernel_sigma_factor=gp_cfg.get('kernel_sigma_factor', 1.0),
                    q=gp_cfg.get('q', 1.0 / np.sqrt(2.0)),
                    sigma_clip_sigma=gp_cfg.get('sigma_clip', 2.0),
                    maxiters=maxiters,
                    weak_frac=gp_cfg.get('weak_frac', 0.5),
                    # pedestal is the chain's only per-amp DC carrier: the
                    # GP passes return zero-DC offsets (see gp_striping)
                    zero_dc=True,
                )
                h5, _, ks5 = gp_amprow_offsets(base, fitmask, rho=rho_short, **gp_kw)
                h20, _, ks20 = gp_amprow_offsets(base - h5, fitmask,
                                                 rho=rho_long, **gp_kw)
                h = h5 + h20
                ks_last = f'{ks5:.3e}/{ks20:.3e}'
            else:  # 'median' — legacy reference arm (h+v together)
                h, vcol, ampc, _ = oneoverf.fit_residual_striping(
                    cond - ped, fitmask, maxiters, estimator='median')
                ampc_last = ','.join(ampc)

            # (5) APPLIED smooth background (subtract_2d) — the sky-match /
            #     ICL subtraction, fit on the 1/f-corrected residual with the
            #     source tiers grown by extra_dilate (bit 0 — off-detector/
            #     DQ/NaN — is not grown, so detector edges don't cost a
            #     dilated border band).
            if subtract_2d:
                src_only = (srcbits >> 1) != 0
                if b2d_extra_dilate > 0:
                    grown = (distance_transform_edt(~src_only)
                             <= b2d_extra_dilate)
                else:
                    grown = src_only
                b2d = sb.estimate_background(
                    resid - ped - vcol - h, srcmask | grown | dq_fit
                ).background.astype(resid.dtype)
            else:
                b2d = 0.0

            step = ped + vcol + h + b2d
            resid -= step
            correction += step
            if components_out is not None:
                components_out['ped'] += ped
                components_out['vcol'] += vcol
                components_out['h'] += h
                components_out['b2d'] += b2d
                components_out['det_struct'] = (
                    det_struct if detrend_on else np.zeros_like(resid))

        # (6) VARIANCE rescale on the final mask. Measure the sky variance on
        #     the *conditioned* corrected residual (resid - det_struct): when
        #     the applied fit is off (or misses structure), retained sky
        #     structure would otherwise inflate the noise estimate.
        factor = oneoverf.variance_rescale(resid - det_struct,
                                           model.var_rnoise, srcmask,
                                           block_size)

        # (7) Write. SCI = original - accumulated correction.
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
            f'pedestal={bkg_level:.5e}, ped_scope={ped_scope}, '
            f'var_factor={factor:.3f}, '
            f'detrend={"box%d" % det_box if detrend_on else "off"}, '
            f'subtract_2d={subtract_2d}'
        )
        if estimator == 'gp':
            cfp_value += (f', rho_short={rho_short}, rho_long={rho_long}, '
                          f'kernel_sigma[last]={ks_last}')
        elif estimator == 'median':
            cfp_value += f', fallbacks[last]={ampc_last}'
        if subtract_2d:
            # The per-exposure skip is CFP_BKG-presence-based (no config
            # hash): record the 2-D fit knobs so a config flip on already-
            # processed exposures is at least auditable.
            cfp_value += (
                f', bkg2d_box={b2d_box}, '
                f'bkg2d_dilate={b2d_extra_dilate:.0f}, '
                f'bkg2d_reject={sb.bg_reject}'
            )

        sci_after = model.data.copy() if do_plot else None

        if components_out is not None:
            components_out['before'] = sci0.copy()
            components_out['after'] = model.data.copy()
            components_out['srcmask'] = srcmask.copy()
            components_out['fitmask'] = (srcmask | dq_fit)

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
