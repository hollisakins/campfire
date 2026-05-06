"""
drizzle: campfire-native drizzle primitive replacing ``Image3Pipeline`` for
NIRCam stage-3 resample (issue #138).

Structural win over ``stcal.resample.resample.Resample``: the **variance
trick**. A single persistent accumulator ``outvar`` is filled by drizzling
``var_total · wht`` weighted by ``wht``; the final ERR is
``sqrt(outvar / outwht)``. Replaces stcal's three transient per-component
variance drizzles plus Python-level full-tile masked accumulator updates
(~21 s/input × 200 inputs ≈ 70 min/tile of bookkeeping at COSMOS-Web
scale).

The trick is the canonical kernel-weighted variance estimator
``V = (Σᵢ kᵢ wᵢ² varᵢ_total) / (Σᵢ kᵢ wᵢ)²``. This is *not* identical
to what stcal computes — stcal sums per-variance-component contributions
``wsum_xx / (wt² · pixel_scale_ratio²)`` after drizzling each
``sqrt(varᵢ)`` separately. On the rj0911 f277w validation tile the two
agree on SCI/WHT/coverage bit-exactly modulo float32 accumulation order,
but campfire's ERR is systematically ~5% larger than stcal's, with the
discrepancy concentrated at low-coverage edges (~13%) and uniform at
~3% in well-covered regions. The bias does not correlate with
var_poisson/var_rnoise (Spearman ~0); it's a geometry/kernel artifact,
not a noise-model artifact. The trick is what grizli uses; both estimators
are defensible and the choice is a science call documented in the
Phase 1 CHANGELOG entry.

(The "output-bbox slicing" optimization mentioned in the issue was meant
to avoid stcal's per-input full-tile Python bookkeeping. Since this
implementation never has that bookkeeping — it hands full-tile
accumulators directly to ``drizzle.resample.Drizzle`` and lets cdriz's
internal pixmap-bounded writes do the cost containment — the slicing is
unnecessary here. The early-exit in ``_output_bbox_in_tile`` is still
useful for skipping inputs that don't overlap the tile.)

The output WCS is built via ``stcal.alignment.util.wcs_from_sregions``
using the campfire-supplied (crpix, crval, shape, rotation, pixel_scale)
parameters. The output i2d is written through
``stdatamodels.jwst.datamodels.ImageModel`` to preserve the
SCI/ERR/WHT/CON HDU layout that ``bkgsub`` and ``_split_extensions``
consume. Per-component variance arrays
(VAR_RNOISE/VAR_POISSON/VAR_FLAT) are intentionally not written: nothing
in pipeline/, python/, or web/ reads them from i2d files.
"""

import os
from copy import deepcopy
from datetime import datetime

import numpy as np
from astropy.io import fits

from campfire_pipeline.common.io import log


def _build_output_wcs(crf_files, crpix, crval, shape, rotation, pixel_scale):
    """Build the output gwcs using stcal's TAN convention.

    ``crpix`` / ``crval`` / ``shape`` / ``rotation`` / ``pixel_scale`` are
    the campfire tile parameters from ``Field.get_tile_wcs``. The first
    CRF supplies a reference gwcs and ``wcsinfo`` so stcal can construct
    the output frame.

    All inputs' ``S_REGION`` polygons are passed so the gwcs's
    ``bounding_box`` covers the full union footprint — important for the
    inverse transform (``world → output_pix``) to return finite values
    for inputs whose footprint extends beyond the first CRF's bbox. The
    output ``shape``, ``crpix``, and ``crval`` are explicit overrides so
    the geometry is fully determined by the campfire tile parameters and
    does not depend on input ordering.

    Parameters
    ----------
    crf_files : list of str
    crpix, crval, shape, rotation, pixel_scale : tile WCS overrides
        (see ``Field.get_tile_wcs``).

    Returns
    -------
    `gwcs.wcs.WCS`
    """
    from stcal.alignment.util import wcs_from_sregions
    from stdatamodels.jwst.datamodels import ImageModel

    sregions = []
    with ImageModel(crf_files[0]) as ref:
        ref_wcs = deepcopy(ref.meta.wcs)
        ref_wcsinfo = ref.meta.wcsinfo.instance
        sregions.append(ref.meta.wcsinfo.s_region)
    for crf in crf_files[1:]:
        sregions.append(fits.getheader(crf, extname='SCI')['S_REGION'])

    nx, ny = shape
    return wcs_from_sregions(
        sregions,
        ref_wcs=ref_wcs,
        ref_wcsinfo=ref_wcsinfo,
        pscale=pixel_scale / 3600.0,
        rotation=rotation,
        shape=(ny, nx),
        crpix=tuple(crpix),
        crval=tuple(crval),
    )


def _input_to_output_pixmap(input_gwcs, output_wcs, in_shape):
    """Compute the (in_ny, in_nx, 2) pixmap from input pixels to output pixels.

    Uses the input gwcs forward to world coordinates, then the output gwcs
    inverse to output pixels. Pixmap convention matches drizzle's:
    ``pixmap[i, j, 0]`` is the output X coordinate of input pixel ``(j, i)``,
    ``pixmap[i, j, 1]`` is the output Y coordinate.
    """
    in_ny, in_nx = in_shape
    iy, ix = np.indices((in_ny, in_nx), dtype=np.float64)
    ra, dec = input_gwcs(ix, iy)
    out_x, out_y = output_wcs.invert(ra, dec)
    pixmap = np.empty((in_ny, in_nx, 2), dtype=np.float64)
    pixmap[..., 0] = out_x
    pixmap[..., 1] = out_y
    return pixmap


def _output_bbox_in_tile(pixmap, out_shape, pad=4):
    """Return ``(sly, slx)`` bbox of input footprint in output frame, or None.

    Pads by ``pad`` pixels on each side to cover the kernel halo, then clips
    to the tile bounds. Returns ``None`` if the input does not overlap the
    output tile.
    """
    out_ny, out_nx = out_shape
    valid = np.isfinite(pixmap).all(axis=-1)
    if not valid.any():
        return None
    out_x = pixmap[..., 0][valid]
    out_y = pixmap[..., 1][valid]

    x_min = int(np.floor(out_x.min())) - pad
    x_max = int(np.ceil(out_x.max())) + pad + 1
    y_min = int(np.floor(out_y.min())) - pad
    y_max = int(np.ceil(out_y.max())) + pad + 1

    x_min = max(0, x_min)
    x_max = min(out_nx, x_max)
    y_min = max(0, y_min)
    y_max = min(out_ny, y_max)

    if x_min >= x_max or y_min >= y_max:
        return None
    return slice(y_min, y_max), slice(x_min, x_max)


def _write_i2d_fits(output_path, sci, err, wht, ctx, output_wcs,
                    cmpfrver, exptime):
    """Write i2d FITS with the schema bkgsub and _split_extensions consume.

    Uses ``stdatamodels.jwst.datamodels.ImageModel`` so the HDU layout
    (SCI/ERR/CON/WHT) and primary header conventions match stcal's output.
    """
    from stdatamodels.jwst.datamodels import ImageModel

    model = ImageModel(sci.shape)
    model.data = sci.astype(np.float32, copy=False)
    model.err = err.astype(np.float32, copy=False)
    model.wht = wht.astype(np.float32, copy=False)
    if ctx.ndim == 3 and ctx.shape[0] == 1:
        model.con = ctx[0].astype(np.int32, copy=False)
    else:
        model.con = ctx.astype(np.int32, copy=False)
    model.meta.wcs = output_wcs
    model.meta.exposure.exposure_time = float(exptime)
    model.meta.resample.product_exposure_time = float(exptime)

    model.save(output_path)

    # Stamp campfire provenance + tighten WCS encoding in the SCI header
    # (ImageModel.save writes the gwcs via asdf-in-fits; downstream
    # campfire code reads CRPIX/CRVAL/CD via astropy.wcs from the SCI
    # header, so make sure those keys are present and correct).
    with fits.open(output_path, mode='update') as hdul:
        hdul[0].header['CMPFRTIM'] = (
            str(datetime.now()),
            'Date/time of CAMPFIRE reduction',
        )
        hdul[0].header['CMPFRVER'] = (
            cmpfrver,
            'CAMPFIRE git commit (or pinned version)',
        )


def drizzle_tile(
    crf_files,
    output_path,
    *,
    crpix,
    crval,
    shape,
    rotation,
    pixel_scale,
    pixfrac=1.0,
    kernel='square',
    weight_type='ivm',
    good_bits='~DO_NOT_USE',
    reduction_version='unknown',
):
    """Drizzle ``crf_files`` into a single i2d at ``output_path``.

    Persistent accumulators across all inputs:
    - ``outsci`` / ``outwht`` / ``outctx`` — SCI pass (Drizzle sees data,
      writes weighted-mean SCI and weight sum, tracks input contributions
      in the context array).
    - ``outvar`` / ``outvarw`` — variance-trick pass (drizzle ``var·wht``
      weighted by ``wht``; ``outvarw`` exists only because Drizzle requires
      an out_wht buffer, its values are discarded and equal ``outwht`` mod
      float32 accumulation order).

    Each input drizzles into a sliced view of every accumulator sized to
    the input's bounding box in the output frame plus a 4-pixel kernel halo.

    Parameters mirror ``Field.get_tile_wcs`` outputs (``crpix``, ``crval``,
    ``shape``, ``rotation``) and ``[nircam.resample]`` config knobs
    (``pixfrac``, ``kernel``, ``weight_type``, ``good_bits``).
    """
    from drizzle.resample import Drizzle
    from jwst.datamodels.dqflags import pixel as pixel_flags
    from stcal.resample.utils import build_driz_weight, resample_range
    from stdatamodels.jwst.datamodels import ImageModel

    n_inputs = len(crf_files)
    nx, ny = shape
    out_shape = (ny, nx)

    log(f"  campfire drizzle: {n_inputs} inputs into {nx}x{ny} tile")

    output_wcs = _build_output_wcs(
        crf_files, crpix, crval, shape, rotation, pixel_scale,
    )

    # Persistent accumulators owned by Drizzle objects below. ctx is 3D
    # `(n_planes, ny, nx)` since >32 inputs requires bit-plane stacking.
    outsci = np.zeros(out_shape, dtype=np.float32)
    outwht = np.zeros(out_shape, dtype=np.float32)
    outvar = np.zeros(out_shape, dtype=np.float32)
    outvarw = np.zeros(out_shape, dtype=np.float32)
    n_planes = max(1, (n_inputs + 31) // 32)
    outctx = np.zeros((n_planes, ny, nx), dtype=np.int32)

    # SCI pass — keeps ctx tracking, accumulates exptime internally
    sci_drizzle = Drizzle(
        out_img=outsci, out_wht=outwht, out_ctx=outctx,
        kernel=kernel, fillval='INDEF',
        max_ctx_id=n_inputs,
    )
    # Variance trick — single accumulator, no ctx
    var_drizzle = Drizzle(
        out_img=outvar, out_wht=outvarw,
        kernel=kernel, fillval='INDEF',
        disable_ctx=True,
    )

    skipped = 0

    for crf_file in crf_files:
        with ImageModel(crf_file) as model:
            data = model.data
            input_gwcs = model.meta.wcs
            in_shape = data.shape
            exptime = float(model.meta.exposure.exposure_time)

            pixmap = _input_to_output_pixmap(input_gwcs, output_wcs, in_shape)
            if _output_bbox_in_tile(pixmap, out_shape) is None:
                skipped += 1
                continue

            # Per-input weight (uses input DQ + var_rnoise for ivm).
            # ImageModel auto-allocates var_* as zeros if the FITS lacks
            # them; build_driz_weight then falls back to uniform weights
            # via _get_inverse_variance.
            weight = build_driz_weight(
                {'data': data, 'dq': model.dq, 'var_rnoise': model.var_rnoise},
                weight_type=weight_type,
                good_bits=good_bits,
                flag_name_map=pixel_flags,
            ).astype(np.float32)

            var_total = (
                np.asarray(model.var_rnoise, dtype=np.float32)
                + np.asarray(model.var_poisson, dtype=np.float32)
                + np.asarray(model.var_flat, dtype=np.float32)
            )

            # Input-side bounds from gwcs bounding box (caps pixmap eval
            # to valid detector region, matches stcal)
            xmin, xmax, ymin, ymax = resample_range(
                in_shape, input_gwcs.bounding_box,
            )

            common = dict(
                exptime=exptime,
                pixmap=pixmap,
                weight_map=weight,
                pixfrac=pixfrac,
                in_units='cps',
                xmin=xmin, xmax=xmax, ymin=ymin, ymax=ymax,
            )

            sci_drizzle.add_image(
                data=data.astype(np.float32, copy=False),
                **common,
            )
            var_drizzle.add_image(
                data=(var_total * weight).astype(np.float32),
                **common,
            )

    if skipped:
        log(f"  {skipped} inputs did not overlap tile")

    total_exptime = float(sci_drizzle.total_exptime)

    # Final ERR. The variance trick gives outvar (per pixel) =
    # Σwᵢ²kᵢvarᵢ / Σwᵢkᵢ; dividing by outwht (= Σwᵢkᵢ) yields the canonical
    # weighted variance Σwᵢ²kᵢvarᵢ / (Σwᵢkᵢ)². Pixels with zero weight
    # become ERR = NaN (matches stcal's missing-data convention used by
    # bkgsub.off_detector via np.isnan(err)).
    with np.errstate(divide='ignore', invalid='ignore'):
        out_var_final = np.where(outwht > 0, outvar / outwht, np.nan)
        outerr = np.sqrt(out_var_final).astype(np.float32)

    _write_i2d_fits(
        output_path,
        sci=outsci, err=outerr, wht=outwht, ctx=outctx,
        output_wcs=output_wcs,
        cmpfrver=reduction_version,
        exptime=total_exptime,
    )

    log(f"  wrote {os.path.basename(output_path)} "
        f"({n_inputs - skipped} contributing inputs)")
