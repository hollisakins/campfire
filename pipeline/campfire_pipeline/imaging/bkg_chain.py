"""The per-exposure background chain, shared by every imaging instrument.

One iterative loop on one detector image, with one source mask::

    for _ in range(n_iterations):
        mask    = SubtractBackground.mask_from_arrays(residual)   # mask only
        detrend = fit-only coarse 2-D structure model (zero median)
        ped     = pedestal on (residual - detrend)   # the level, per amplifier
        b2d     = applied smooth 2-D background, when ``subtract_2d``
        vcol, h = readout pattern terms, when the instrument has them
        residual -= ped + vcol + h + b2d             # detrend is not subtracted

The NIRCam ``bkg`` step (``nircam/steps/bkg.py``, where the reasons for each
term and their order are written down) and the HST ``bkg`` step
(``hst/steps/bkg.py``) both call :func:`run_chain`. What differs between
instruments is handed in:

* ``pedestal_fn``: where the amplifiers are (four column strips on a NIRCam
  detector, two halves of an ACS/WFC chip, one region on WFC3/IR);
* ``striping_fn``: the readout pattern estimator. NIRCam fits its 1/f noise
  per amplifier row; the HST detectors have no such term and pass ``None``;
* the masks and the two fitters, built from the instrument's config with its
  pixel scale.

Arrays are in whatever linear unit the caller works in; every threshold in the
chain is relative to the measured noise.
"""

import numpy as np
from scipy.ndimage import distance_transform_edt


def run_chain(sci, err, dq, dq_fit, *, sb, sb_det, n_iter, remask,
              pedestal_fn, striping_fn, subtract_2d, b2d_order,
              b2d_extra_dilate, strp_extra_dilate=0.0, strp_min_area=0.0,
              f2=1.0, components_out=None):
    """Run the chain on one detector image.

    ``sci`` is not modified. ``dq`` is what the source mask is seeded from
    (``sb.dq_flags_to_mask`` names its bits), ``dq_fit`` is True where a pixel
    must not enter a fit. ``sb`` builds the source mask and fits the applied
    2-D background; ``sb_det`` fits the conditioning detrend, or is ``None``
    to switch it off. ``pedestal_fn(image, mask)`` returns the pedestal image;
    ``striping_fn(meas, fitmask, det_struct)`` returns ``(vcol, h)`` or is
    ``None``. ``b2d_extra_dilate`` is in pixels of this image;
    ``strp_extra_dilate`` and ``strp_min_area`` are angular (multiplied by
    ``f2`` and ``f2**2``).

    Returns a dict: ``resid`` (the image minus every applied term),
    ``correction`` (their sum), ``srcmask`` and ``srcbits`` of the last
    iteration, ``det_struct`` of the last iteration (0.0 when the detrend is
    off).
    """
    detrend_on = sb_det is not None
    resid = sci.astype(np.float64, copy=True)
    correction = np.zeros_like(resid)
    srcmask = None
    srcbits = None
    det_struct = 0.0
    if components_out is not None:
        for k in ('ped', 'vcol', 'h', 'b2d'):
            components_out[k] = np.zeros_like(resid)

    for it in range(n_iter):
        # (1) SOURCE MASK — rebuilt on the running residual (sharpens as the
        #     frame flattens). mask_from_arrays = mask only, no 2-D bg.
        if srcmask is None or remask:
            srcmask, srcbits = sb.mask_from_arrays(resid, err, dq)
        fitmask = srcmask | dq_fit

        # Optional grown fit mask for the readout pattern terms ONLY
        # (NIRCam's [nircam.bkg.striping].extra_dilate; see that step).
        if strp_extra_dilate > 0:
            src_only_1f = (srcbits >> 1) != 0
            if strp_min_area > 0:
                # grow only the large footprints (the artifact drivers)
                from scipy.ndimage import label as ndi_label
                lab, nlab = ndi_label(src_only_1f)
                if nlab:
                    areas = np.bincount(lab.ravel())
                    keep = areas >= strp_min_area * f2 * f2
                    keep[0] = False
                    src_only_1f = keep[lab]
            if src_only_1f.any():
                grown_1f = (distance_transform_edt(~src_only_1f)
                            <= strp_extra_dilate * f2)
                fitmask_1f = fitmask | grown_1f
            else:
                # Nothing selected to grow — either no source tiers at all
                # or extra_dilate_min_area filtered every component out.
                # `distance_transform_edt` measures the distance to the
                # nearest ZERO, so on an all-True input it has no seed and
                # returns distances measured from OUTSIDE the array: a 256²
                # frame yields min 1.0 / max 361, and `<= extra_dilate`
                # then masks a border-and-corner band (314 px at 20 px
                # dilation) that no source put there. Those are exactly the
                # edge amp-row anchors, so it would degrade the 1/f fit on
                # the emptiest frames — the ones with the least to spare.
                fitmask_1f = fitmask
        else:
            fitmask_1f = fitmask

        # (2) CONDITIONING DETREND — fit-only, zero-median structure model.
        #     Subtracted from the *measurement copies* below so the pedestal
        #     and the readout terms see a structure-free residual; never
        #     enters the correction. Fit DIRECTLY on the residual (see the
        #     NIRCam step for why no readout pattern is removed first).
        if detrend_on:
            det = sb_det.estimate_background(
                resid, fitmask).background.astype(resid.dtype)
            good = ~fitmask & np.isfinite(resid)
            det_struct = det - (float(np.median(det[good]))
                                if good.any() else 0.0)
        else:
            det_struct = 0.0
        cond = resid - det_struct

        # (3) PEDESTAL — owns the level, measured on the conditioned residual
        #     so a pedestal per amplifier stays safe under a gradient
        ped = pedestal_fn(cond, fitmask)

        # (4) APPLIED smooth background (subtract_2d), with the source tiers
        #     grown by extra_dilate (bit 0 — off-detector/DQ/NaN — is not
        #     grown, so detector edges don't cost a dilated border band).
        #     fit_order='first' fits it HERE, on resid - ped; 'last' after
        #     the readout terms.
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

        # (5) READOUT PATTERN terms, on the conditioned residual (additionally
        #     minus the applied 2-D model when fit first — b2d is 0.0 in the
        #     'last' order at this point)
        meas = cond - ped - b2d
        if striping_fn is None:
            vcol = 0.0
            h = 0.0
        else:
            vcol, h = striping_fn(meas, fitmask_1f, det_struct)

        # (6) fit_order='last': applied fit on the pattern-corrected residual
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

    return {'resid': resid, 'correction': correction, 'srcmask': srcmask,
            'srcbits': srcbits, 'det_struct': det_struct}


def variance_rescale(sci, var_rnoise, mask, block_size):
    """Return the factor that rescales ``VAR_RNOISE`` to the measured sky variance.

    Block-reduces SCI and VAR_RNOISE, measures the sky variance
    (``biweight_midvariance``) and the mean VAR_RNOISE (``biweight_location``)
    over unmasked bins, and returns ``skyvar / masked_mean_var_rnoise``. The
    caller multiplies ``VAR_RNOISE`` by this factor. Identical math to the
    retired ``variance_step``.
    """
    from astropy.nddata import block_reduce
    from astropy.stats import biweight_location, biweight_midvariance

    block_mask = block_reduce(mask, block_size)
    block_mask_bool = block_mask != 0

    block_sci = block_reduce(sci, block_size)
    unmasked_bins = block_sci[block_mask_bool == 0]
    variance = biweight_midvariance(unmasked_bins)
    skyvar = variance / block_size ** 2

    block_var_rnoise = block_reduce(var_rnoise, block_size)
    unmasked_bins = block_var_rnoise[block_mask_bool == 0]
    masked_mean_var_rnoise = (
        biweight_location(unmasked_bins) / block_size ** 2
    )

    return float(skyvar / masked_mean_var_rnoise)


# Mask parameters are angular scales in pixels, tuned at 30 mas; rescale by
# f = 30 mas / (the detector's pixel) to hold the angular scale fixed. Linear
# lengths x f, area counts x f**2, the rest unchanged.
_MASK_LINEAR = ('ring_radius_in', 'ring_width', 'ring_clip_box_size')
_MASK_LINEAR_LIST = ('tier_kernel_size', 'tier_dilate_size')
_MASK_AREA_LIST = ('tier_npixels',)


def scale_mask_config(mask_cfg, f):
    """A copy of ``mask_cfg`` with its length parameters multiplied by ``f``."""
    f = float(f)
    out = {k: v for k, v in mask_cfg.items()}
    if f == 1.0:
        return out
    for k in _MASK_LINEAR:
        if k in out:
            out[k] = type(out[k])(out[k] * f)
            # a length written as a whole number of pixels stays at least one
            # (a ring 4 px wide at 30 mas is under one 128 mas pixel)
            if isinstance(out[k], int):
                out[k] = max(1, out[k])
    for k in _MASK_LINEAR_LIST:
        if k in out:
            out[k] = [max(1, int(round(v * f))) for v in out[k]]
    for k in _MASK_AREA_LIST:
        if k in out:
            out[k] = [max(1, int(round(v * f * f))) for v in out[k]]
    return out


def region_pedestal(data, mask, regions, sigma=3.0, maxiters=3):
    """One level per region: the sigma-clipped median of its unmasked pixels.

    ``regions`` is a list of ``(row slice, column slice)``, the amplifiers of
    the detector. The rule is NIRCam's ``oneoverf.peramp_pedestal``, for any
    amplifier layout. Returns ``(pedestal image, [level per region])``;
    pixels outside every region stay 0.
    """
    import warnings

    from astropy.stats import sigma_clipped_stats

    ped = np.zeros(data.shape)
    levels = []
    for rows, cols in regions:
        part = data[rows, cols]
        vals = part[~mask[rows, cols] & np.isfinite(part)]
        if vals.size:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', category=RuntimeWarning)
                _, dc, _ = sigma_clipped_stats(vals, sigma=sigma,
                                               maxiters=maxiters)
            dc = float(dc) if np.isfinite(dc) else 0.0
        else:
            dc = 0.0
        ped[rows, cols] = dc
        levels.append(dc)
    return ped, levels
