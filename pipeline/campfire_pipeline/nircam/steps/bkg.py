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
        b2d     = optional APPLIED smooth 2-D background (subtract_2d),
                  fit here when bkg2d.fit_order = "first" (recommended for
                  bright/extended fields) or after the 1/f terms when "last"
        vcol    = per-column (vertical) 1/f, on the conditioned residual
                  (minus b2d when it was fit first)
        h       = amp-row 1/f: GP ρ≈5 then ρ≈20, on the same residual
        residual -= ped + vcol + h + b2d             # detrend NOT subtracted
    rescale VAR_RNOISE on (residual - detrend), final mask

With ``fit_order = "last"`` (the legacy order) the amp-row terms are fit
before the applied 2-D model ever sees the frame, so unmasked halo/wing flux
around bright galaxies leaks into the clipped amp-row medians and the GP —
smooth structure slower than ρ is exactly what it follows — and is broadcast
across each amp's full width: oversubtracted amp-blocky patches with hard
edges at the amp boundaries (cols 512/1024/1536) and at the source's
top/bottom rows. The 2-D fit then runs on the *post-h* residual, so it can
never reclaim that flux, in this iteration or any later one (corrections
accumulate one-way; the loop's fixed point — zero clipped amp-row medians —
is satisfied by the artifact). ``fit_order = "first"`` fits the smooth model
on ``resid - ped`` with the halo intact and conditions the 1/f measurement on
its output, so smooth flux is claimed by the model that can represent it.

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
    b2d_order = str(b2d_cfg.get('fit_order', 'last'))
    _ORDERS = ('first', 'last')
    if b2d_order not in _ORDERS:
        raise ValueError(
            f"[nircam.bkg.bkg2d].fit_order={b2d_order!r} not in {_ORDERS}")
    det_cfg = step_config.get('detrend', {})
    detrend_on = bool(det_cfg.get('enabled', True))

    estimator = strp_cfg.get('estimator', 'gp')
    _VALID = ('gp', 'median', 'none')
    if estimator not in _VALID:
        raise ValueError(
            f"[nircam.bkg.striping].estimator={estimator!r} not in {_VALID}")
    maxiters = int(strp_cfg.get('maxiters', 3))
    remask = strp_cfg.get('remask_each_iter', True)
    # Angular px (channel-scaled below, like the mask/bkg2d length params).
    strp_extra_dilate = float(strp_cfg.get('extra_dilate', 0.0))
    # Only grow source-tier footprints at least this big (angular px^2,
    # channel-scaled x f^2). 0 = grow everything. Growing ALL tiers starves
    # the amp-row anchors frame-wide (hundreds of faint sources each donate
    # a grown disk) and the GP then follows sampling noise — measured on the
    # amprow_halo harness (strp_d150 arm): injected row/column striping and
    # a remask runaway. The artifact driver is the few LARGE footprints, so
    # grow only those.
    strp_min_area = float(strp_cfg.get('extra_dilate_min_area', 0.0))
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
    # on low-NGROUPS ramps), not real transients. Such a bit is dropped from the
    # fit mask per-exposure. 1.0 disables the guard.
    #
    # There are TWO failure modes, and the quieter one binds first:
    #   * loud — the Background2D detrend runs out of unmasked boxes and the
    #     step hard-fails. Needs a near-total blanket.
    #   * quiet — the per-amp-row 1/f fit is starved long before that. It does
    #     not fail; it fits noise and SUBTRACTS it, injecting row/column
    #     structure. A2744 f444w jw03073008001 (JUMP_DET 79-81%, so nowhere near
    #     total) left 11.4% of pixels usable, a median of 58 unmasked px per
    #     amp-row vs 279 on a healthy frame, and injected ~1e-3 MJy/sr.
    # The old 0.85 default was sized for the loud mode only. Keep this literal
    # in sync with [nircam.bkg.mask] in config_default.toml, where the full
    # measurement is recorded — every config path merges over that file today,
    # so this fallback is unreachable, but it must not drift.
    aggressive_dq_max_frac = float(
        mask_cfg.pop('mask_aggressive_dq_max_frac', 0.50))

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
        if subtract_2d and b2d_order == 'first' and sb.bg_reject:
            # Measured on the synthetic amp-spanning-halo scene (see
            # test_nircam_bkg.test_b2d_fit_order_first_starves_amprow_of_halo):
            # the map-outlier reject flags the very halo bump the first-order
            # fit exists to model, masks it, and refits it away — cancelling
            # the reorder. Warn rather than override: reject still guards
            # against compact-source leakage, so the trade is the field
            # config's to make.
            log(f"  {rootname}: bkg2d fit_order='first' with reject=true — "
                f"the map-outlier reject can re-reject extended halo bumps "
                f"and cancel the first-order benefit; consider "
                f"[nircam.bkg.bkg2d].reject=false for bright-halo fields")

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

            # Optional grown fit mask for the 1/f terms ONLY
            # ([nircam.bkg.striping].extra_dilate): the amp-row medians are
            # the chain's most halo-contaminable statistic (508-px samples,
            # one-signed bias), and the mask tiers structurally cannot reach
            # a bright galaxy's halo (the ring-median pre-filter removes
            # structure broader than its radius before detection). Growing
            # the source tiers pushes the row/column anchors out to true
            # sky; the GP bridges the widened gap by design (that is what
            # rho_long is for), reverting to the per-amp DC where nothing
            # anchors — i.e. leaving the halo alone. Detrend, pedestal and
            # the applied 2-D fit keep their own masks.
            if strp_extra_dilate > 0:
                src_only_1f = (srcbits >> 1) != 0
                if strp_min_area > 0:
                    # grow only the large footprints (the artifact drivers);
                    # see the config-parse comment for why growing all tiers
                    # backfires
                    from scipy.ndimage import label as ndi_label
                    lab, nlab = ndi_label(src_only_1f)
                    if nlab:
                        areas = np.bincount(lab.ravel())
                        keep = areas >= strp_min_area * f2 * f2
                        keep[0] = False
                        src_only_1f = keep[lab]
                grown_1f = (distance_transform_edt(~src_only_1f)
                            <= strp_extra_dilate * f2)
                fitmask_1f = fitmask | grown_1f
            else:
                fitmask_1f = fitmask

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

            # (4) APPLIED smooth background (subtract_2d) — the sky-match /
            #     ICL subtraction, with the source tiers grown by
            #     extra_dilate (bit 0 — off-detector/DQ/NaN — is not grown,
            #     so detector edges don't cost a dilated border band).
            #     fit_order='first' fits it HERE, on resid - ped with halo /
            #     wing flux still in the frame, so the smooth model gets
            #     first claim on smooth structure and the 1/f terms below
            #     measure a b2d-subtracted residual (the amp-blocky halo
            #     oversubtraction fix — see module docstring). The ~20-row
            #     banding still present under this fit averages out in the
            #     32/64 px clipped mesh boxes, and the next iteration refits
            #     on the striping-corrected residual anyway.
            #     fit_order='last' is the legacy order: fit on the
            #     1/f-corrected residual, after the amp-row terms.
            b2d = 0.0
            if subtract_2d:
                src_only = (srcbits >> 1) != 0
                if b2d_extra_dilate > 0:
                    grown = (distance_transform_edt(~src_only)
                             <= b2d_extra_dilate)
                else:
                    grown = src_only
                b2d_mask = srcmask | grown | dq_fit
                if b2d_order == 'first':
                    b2d = sb.estimate_background(
                        resid - ped, b2d_mask).background.astype(resid.dtype)

            # (5) VERTICAL + HORIZONTAL 1/f, on the conditioned residual
            #     (additionally minus the applied 2-D model when fit first —
            #     b2d is 0.0 in the legacy order at this point)
            meas = cond - ped - b2d
            if estimator == 'none':
                vcol = np.zeros_like(resid)
                h = np.zeros_like(resid)
            elif estimator == 'gp':
                from campfire_pipeline.nircam.gp_striping import gp_amprow_offsets
                vcol = oneoverf.column_pattern(meas, fitmask_1f, maxiters)
                base = meas - vcol
                # GP kernel amplitude is measured on the PRE-detrend frame
                # (applied terms only): the prior must reflect how far the
                # offsets vary across wide masked gaps, and the post-detrend
                # residual underestimates that, over-regularizing the gap
                # interpolation (gp_amprow_offsets docstring; rj0911 f444w
                # calibration). With the detrend off the frames coincide, so
                # skip the extra statistics pass.
                amp5 = (base + det_struct) if detrend_on else None
                gp_kw = dict(
                    kernel_sigma_factor=gp_cfg.get('kernel_sigma_factor', 1.0),
                    q=gp_cfg.get('q', 1.0 / np.sqrt(2.0)),
                    sigma_clip_sigma=gp_cfg.get('sigma_clip', 2.0),
                    maxiters=maxiters,
                    weak_frac=gp_cfg.get('weak_frac', 0.5),
                    min_row_pixels=int(gp_cfg.get('min_row_pixels', 0)),
                    # pedestal is the chain's only per-amp DC carrier: the
                    # GP passes return zero-DC offsets (see gp_striping)
                    zero_dc=True,
                )
                h5, _, ks5 = gp_amprow_offsets(base, fitmask_1f,
                                               rho=rho_short,
                                               amplitude_data=amp5, **gp_kw)
                amp20 = (amp5 - h5) if amp5 is not None else None
                h20, _, ks20 = gp_amprow_offsets(base - h5, fitmask_1f,
                                                 rho=rho_long,
                                                 amplitude_data=amp20, **gp_kw)
                h = h5 + h20
                ks_last = f'{ks5:.3e}/{ks20:.3e}'
            else:  # 'median' — legacy reference arm (h+v together)
                h, vcol, ampc, _ = oneoverf.fit_residual_striping(
                    meas, fitmask_1f, maxiters, estimator='median')
                ampc_last = ','.join(ampc)

            # (6) legacy fit_order='last': applied fit on the 1/f-corrected
            #     residual. Whatever the amp-row terms absorbed above is
            #     invisible to this fit — the reason 'first' exists.
            if subtract_2d and b2d_order == 'last':
                b2d = sb.estimate_background(
                    resid - ped - vcol - h, b2d_mask
                ).background.astype(resid.dtype)

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

        # (7) VARIANCE rescale on the final mask. Measure the sky variance on
        #     the *conditioned* corrected residual (resid - det_struct): when
        #     the applied fit is off (or misses structure), retained sky
        #     structure would otherwise inflate the noise estimate.
        factor = oneoverf.variance_rescale(resid - det_struct,
                                           model.var_rnoise, srcmask,
                                           block_size)

        # (8) Write. SCI = original - accumulated correction.
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
        if strp_extra_dilate > 0:
            cfp_value += f', strp_dilate={strp_extra_dilate * f2:.0f}'
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
                f'bkg2d_reject={sb.bg_reject}, '
                f'bkg2d_order={b2d_order}'
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
