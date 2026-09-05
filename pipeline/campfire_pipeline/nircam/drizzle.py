"""
drizzle: campfire-native drizzle primitive replacing ``Image3Pipeline`` for
NIRCam stage-3 resample (issue #138).

Structural win over ``stcal.resample.resample.Resample``: the **variance
trick**. A single persistent accumulator ``outvar`` is filled by drizzling
``var_total · wht`` weighted by ``wht``; the final ERR is
``sqrt(outvar / outvarw)``. Replaces stcal's three transient per-component
variance drizzles plus Python-level full-tile masked accumulator updates
(~21 s/input × 200 inputs ≈ 70 min/tile of bookkeeping at COSMOS-Web
scale).

Non-finite or negative input variances are masked *per component* before the
sum (``_sanitize_variance``) — without this, one input pixel with ``inf``/
``nan`` variance poisons every output pixel its kernel touches (``inf`` is
sticky in cdriz's running weighted mean), which is what stcal's per-component
``isfinite`` masking quietly prevents. A bad component drops only its own term
(so a component that is bad across many inputs degrades gracefully instead of
punching a NaN hole); a pixel is dropped entirely only when no component is
valid. The surviving weight is accumulated in ``outvarw`` and used to normalize
``outvar``, so excluded pixels bias neither the numerator nor the denominator.
The SCI/WHT pass is unaffected.

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

Output metadata is populated the same way jwst's resample does, so the
campfire i2d carries the same header keywords as an ``Image3Pipeline``
product (BUNIT, PHOTMJSR/PHOTUJA2, instrument/program/target identity,
exposure times, an HDRTAB provenance table, etc.). Each input model is
fed to ``jwst.model_blender.blender.ModelBlender`` during the drizzle
loop (reusing the open we already do per input), and ``_apply_output_metadata``
finalizes the blend into the output model. The two values that must *not*
be inherited from the inputs are recomputed for the output grid:
``PIXAR_SR``/``PIXAR_A2`` (pixel area scales as ``pixel_scale**2``;
copying the native value would bias MJy/sr → Jy/pixel by the square of the
scale ratio) and the WCS keywords (overwritten from the output gwcs via
``ResampleImage.update_fits_wcsinfo``). ``BUNIT`` and ``PHOTMJSR``/
``PHOTUJA2`` are surface-brightness (per-sr) quantities and are
scale-invariant, so they ride along unchanged.
"""

import os
from copy import deepcopy
from datetime import datetime, timezone

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
    with ImageModel(crf_files[0], memmap=False) as ref:
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
    # ``with_bounding_box=False`` on the inverse is essential: with the default
    # (True), ``invert`` returns NaN for every input pixel whose footprint maps
    # outside the output WCS bounding box (e.g. an exposure that only partially
    # overlaps the tile). cdriz (``drizzle`` 2.x) then raises "No or too few
    # valid pixels in the pixel map" when the *finite* remainder is degenerate,
    # aborting the whole tile. A finite, geometrically-continuous pixmap instead
    # lets cdriz drop off-frame pixels via its normal output-bounds clipping
    # while keeping correct kernel geometry at the tile edge. This is how
    # jwst/stcal treat the pixmap — a pure coordinate map, with all masking
    # (DQ, low weight) carried by the weight array, not by NaNs in the pixmap.
    # Replacing the NaNs with a sentinel does *not* work: cdriz derives each
    # pixel's drizzle footprint from neighbouring pixmap entries, so a sentinel
    # poisons the geometry and zeroes the whole exposure's contribution.
    ra, dec = input_gwcs(ix, iy)
    out_x, out_y = output_wcs.invert(ra, dec, with_bounding_box=False)
    pixmap = np.empty((in_ny, in_nx, 2), dtype=np.float64)
    pixmap[..., 0] = out_x
    pixmap[..., 1] = out_y
    return pixmap


def _output_bbox_in_tile(pixmap, out_shape, pad=4):
    """Return ``(sly, slx)`` bbox of input footprint in output frame, or None.

    Considers only input pixels that map *inside* the output frame (the pixmap
    is now finite and continuous everywhere — see ``_input_to_output_pixmap`` —
    so an in-frame test, not an ``isfinite`` test, is what identifies the
    overlapping footprint). Pads by ``pad`` pixels on each side to cover the
    kernel halo, then clips to the tile bounds. Returns ``None`` if the input
    does not overlap the output tile.
    """
    out_ny, out_nx = out_shape
    out_x = pixmap[..., 0]
    out_y = pixmap[..., 1]
    inside = (
        np.isfinite(out_x) & np.isfinite(out_y)
        & (out_x >= 0) & (out_x <= out_nx - 1)
        & (out_y >= 0) & (out_y <= out_ny - 1)
    )
    if not inside.any():
        return None
    out_x = out_x[inside]
    out_y = out_y[inside]

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


def _apply_output_metadata(model, *, blender, pixel_scale, pixfrac, kernel,
                           weight_type, exptime, n_pointings=None,
                           pixel_scale_ratio=None):
    """Populate the output model's metadata to match a jwst resample i2d.

    Two-part contract, mirroring ``jwst.resample.resample.ResampleImage``:

    1. **Inherited** — ``blender.finalize_model`` writes the blended input
       metadata (instrument/program/target identity, exposure timing, the
       surface-brightness photometry keywords ``PHOTMJSR``/``PHOTUJA2``, the
       per-input HDRTAB provenance table, …) into ``model``. ``blender`` is
       built in ``drizzle_tile`` with ``meta.photometry.pixelarea_*`` and
       ``meta.wcs`` on its ignore list, so those are *not* inherited.

    2. **Recomputed for the output grid** — the values that would be wrong if
       copied from the native inputs:

       - ``PIXAR_A2`` / ``PIXAR_SR``: pixel area scales as ``pixel_scale**2``.
         Copying the native ~0.06" value onto a 0.03" mosaic would bias every
         MJy/sr → Jy/pixel conversion by the square of the scale ratio (~4×).
         ``BUNIT`` and ``PHOTMJSR``/``PHOTUJA2`` are per-steradian and
         scale-invariant, so they are left as inherited.
       - drizzle provenance (``pixfrac``/``kernel``/``weight_type``), the
         resample-product exposure time, and ``cal_step.resample`` reflect this
         resampling run, not any single input.

    The WCS keywords are handled by the caller (``update_fits_wcsinfo`` on the
    output gwcs), which runs after this and overwrites any inherited wcsinfo.
    """
    if blender is not None:
        blender.finalize_model(model)

    arcsec_to_rad = np.pi / (180.0 * 3600.0)
    pixar_a2 = float(pixel_scale) ** 2
    model.meta.photometry.pixelarea_arcsecsq = pixar_a2
    model.meta.photometry.pixelarea_steradians = pixar_a2 * arcsec_to_rad ** 2

    # photom always emits MJy/sr; set on SCI (bunit_data) and ERR (bunit_err)
    # so the split-extension files inherit units.
    model.meta.bunit_data = 'MJy/sr'
    model.meta.bunit_err = 'MJy/sr'

    model.meta.resample.pixfrac = float(pixfrac)
    model.meta.resample.kernel = str(kernel)
    model.meta.resample.weight_type = str(weight_type)
    if n_pointings is not None:
        model.meta.resample.pointings = int(n_pointings)  # -> NDRIZ
    if pixel_scale_ratio is not None:
        model.meta.resample.pixel_scale_ratio = float(pixel_scale_ratio)  # -> PXSCLRT
    model.meta.exposure.exposure_time = float(exptime)
    model.meta.resample.product_exposure_time = float(exptime)
    model.meta.cal_step.resample = 'COMPLETE'


def _write_i2d_fits(output_path, sci, err, wht, ctx, output_wcs,
                    cmpfrver, exptime, *, pixel_scale, pixfrac, kernel,
                    weight_type, blender=None, n_pointings=None,
                    pixel_scale_ratio=None, compress_context=True):
    """Write i2d FITS with the schema bkgsub and _split_extensions consume.

    Uses ``stdatamodels.jwst.datamodels.ImageModel`` so the HDU layout
    (SCI/ERR/CON/WHT) and primary header conventions match stcal's output.

    Metadata is populated via ``_apply_output_metadata`` (the blended input
    headers plus the output-grid-specific photometry/provenance keywords), so
    the i2d carries the same header keywords as a jwst ``Image3Pipeline``
    product. ``blender`` is the ``ModelBlender`` accumulated over the inputs in
    ``drizzle_tile`` (``None`` to skip blending).

    Calls ``jwst.resample.resample.ResampleImage.update_fits_wcsinfo`` —
    the canonical helper jwst's own resample step uses — to populate
    ``model.meta.wcsinfo`` (CRPIX/CRVAL/CDELT/PC/CTYPE) directly from the
    gwcs's forward-transform parameters, then ``update_s_region_imaging`` to
    stamp the footprint. ``model.save`` serialises those into the SCI extension
    header in the same PC+CDELT form a standard jwst i2d carries, so downstream
    tools (DS9, astropy.wcs) read the WCS the same way they would from any
    pipeline output.
    """
    from jwst.resample.resample import ResampleImage
    from stdatamodels.jwst.datamodels import ImageModel

    model = ImageModel(sci.shape)
    model.data = sci.astype(np.float32, copy=False)
    model.err = err.astype(np.float32, copy=False)
    model.wht = wht.astype(np.float32, copy=False)
    # ctx is None when [nircam.resample].write_context = false. A 1x1x1
    # placeholder keeps the SCI/ERR/CON/WHT HDU layout the ImageModel schema
    # and downstream readers expect, without ever materialising the cube.
    if ctx is None:
        ctx_out = np.zeros((1, 1, 1), dtype=np.int32)
    else:
        ctx_out = (ctx[0] if (ctx.ndim == 3 and ctx.shape[0] == 1) else ctx)
        ctx_out = ctx_out.astype(np.int32, copy=False)

    # CON is written tile-compressed (see compress_context_extension). It is
    # by far the largest extension: one int32 plane per 32 inputs, each at FULL
    # tile size, so its cost is tile_area * n_inputs/32 — 17 planes / 80 GiB for
    # a 1.26 Gpix tile with 534 inputs, against 14 GiB for SCI+ERR+WHT combined.
    # Almost every bit is zero (a pixel is touched by a handful of inputs, not
    # hundreds), so it compresses ~50x losslessly.
    #
    # A 1x1x1 placeholder goes in first so `model.save` does not materialise the
    # uncompressed array on disk at all; without it the swap below would mean
    # writing ~95 GiB and immediately rewriting it.
    if compress_context:
        model.con = np.zeros((1, 1, 1), dtype=np.int32)
    else:
        model.con = ctx_out

    _apply_output_metadata(
        model, blender=blender, pixel_scale=pixel_scale,
        pixfrac=pixfrac, kernel=kernel, weight_type=weight_type,
        exptime=exptime, n_pointings=n_pointings,
        pixel_scale_ratio=pixel_scale_ratio,
    )

    model.meta.wcs = output_wcs
    ResampleImage.update_fits_wcsinfo(model)
    # update_fits_wcsinfo writes the projection keywords (CRPIX/CDELT/PC/CTYPE)
    # but not WCSAXES/CUNIT — in the jwst path those carry over from the input
    # wcsinfo. The output is always a 2D celestial TAN in degrees, so set them
    # explicitly so the SCI header (and the split-extension files) is complete.
    model.meta.wcsinfo.wcsaxes = 2
    model.meta.wcsinfo.cunit1 = 'deg'
    model.meta.wcsinfo.cunit2 = 'deg'
    try:
        from jwst.assign_wcs import util as assign_wcs_util
        assign_wcs_util.update_s_region_imaging(model)
    except Exception as exc:  # footprint stamping is best-effort
        log(f"  could not stamp S_REGION: {exc}")

    model.save(output_path)

    if compress_context and ctx is not None:
        compress_context_extension(output_path, ctx=ctx_out)

    with fits.open(output_path, mode='update') as hdul:
        hdul[0].header['CMPFRTIM'] = (
            datetime.now(timezone.utc).isoformat(),
            'UTC date/time of CAMPFIRE reduction (ISO 8601)',
        )
        hdul[0].header['CMPFRVER'] = (
            cmpfrver,
            'CAMPFIRE git commit (or pinned version)',
        )
        # CON is retained purely for external consumers, so a placeholder
        # must announce itself: without this card a 1x1x1 cube of zeros is
        # indistinguishable from a genuinely empty context except by shape,
        # and the placeholder is a plain ImageHDU where a full run writes a
        # CompImageHDU. Stamped only when the cube was skipped (like
        # CFEPOCH), so normal products keep byte-identical headers.
        if ctx is None:
            hdul[0].header['CFNOCTX'] = (
                True,
                'CAMPFIRE: CON is a placeholder (write_context=false)',
            )


def compress_context_extension(output_path, ctx=None):
    """Rewrite ``output_path`` with its CON extension tile-compressed.

    Two callers, one rewrite:

    * the campfire backend passes ``ctx`` explicitly. ``model.save`` wrote a
      1x1x1 CON placeholder, so the file on disk is just SCI+ERR+WHT and the
      rewrite is cheap — the uncompressed array is never materialised on disk.
    * the jwst backend passes ``ctx=None``. ``Image3Pipeline`` has already
      written the full uncompressed CON, so the array is read back (lazily,
      through the memmap) and the file is rewritten compressed. That path pays
      one extra read+write of the i2d; the placeholder trick isn't available
      because the write is inside jwst's own step.

    GZIP_1 is lossless on the int32 bitmask (verified bit-identical on
    round-trip); the extension keeps the name ``CON`` and reads back
    transparently through ``astropy.io.fits`` — ``hdul['CON'].data`` returns the
    same int32 array as before.

    Non-astropy readers see a compressed-image BinTable rather than a plain
    ImageHDU. Nothing in this repository reads CON (the only references are the
    writes in this module), so that is a compatibility note rather than a
    breakage; ``[nircam.resample].compress_context = false`` restores the
    uncompressed extension.

    Returns True if the file was rewritten, False if there was nothing to do
    (no CON extension, or one that is already compressed).
    """
    tmp = f'{output_path}.ctx.tmp'
    # Same staging discipline as `common.io.atomic_save`: the i2d is the
    # largest file this pipeline writes, so a mid-write ENOSPC is a realistic
    # failure — and under `--processes N` one leaked temp per failed tile adds
    # up on the very disk that just filled. BaseException so a KeyboardInterrupt
    # cleans up too. The `.ctx.tmp` suffix (rather than atomic_save's
    # `.tmp<ext>`) keeps a partial file from ever matching an `*_i2d.fits` glob.
    try:
        with fits.open(output_path) as hdul:
            try:
                idx = hdul.index_of('CON')
            except KeyError:
                log("  no CON extension to compress")
                return False
            if isinstance(hdul[idx], fits.CompImageHDU):
                return False

            # Left lazy on purpose when it comes from the file: astropy
            # compresses tile-by-tile, so a memmapped source is paged in rather
            # than held resident, and the ~80 GiB CON never has to fit in RAM
            # at once.
            data = hdul[idx].data if ctx is None else ctx

            out = fits.HDUList()
            for i, hdu in enumerate(hdul):
                if i == idx:
                    comp = fits.CompImageHDU(
                        data=data, name='CON', compression_type='GZIP_1')
                    # Keep any provenance the datamodel put on the source CON,
                    # minus the structural keywords (they describe the *old*
                    # layout) and the checksums (stale the moment we
                    # recompress).
                    for card in hdu.header.cards:
                        k = card.keyword
                        if k and k not in comp.header and not k.startswith(
                                ('NAXIS', 'BITPIX', 'PCOUNT', 'GCOUNT',
                                 'XTENSION', 'EXTNAME', 'SIMPLE', 'ZIMAGE',
                                 'ZCMPTYPE', 'CHECKSUM', 'DATASUM')):
                            try:
                                comp.header[k] = (card.value, card.comment)
                            except Exception:
                                pass
                    out.append(comp)
                else:
                    # No .copy() — that would fault every lazily-memmapped
                    # extension (SCI/ERR/WHT/ASDF, ~14 GiB on a large tile)
                    # into RAM and then allocate a second copy of each. `hdul`
                    # stays open for the writeto below, so the unmodified HDUs
                    # stream straight from the source file.
                    out.append(hdu)
            out.writeto(tmp, overwrite=True)
        os.replace(tmp, output_path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return True


def _sanitize_variance(var_rnoise, var_poisson, var_flat, weight):
    """Sum the variance components for the variance pass, masking per component.

    Each component is validated independently with ``(c >= 0) & isfinite(c)``
    (the same condition stcal applies per component in
    ``stcal.resample.resample.resample_variance_arrays``) and contributes 0
    where it is non-finite or negative. A single bad component — e.g.
    ``var_poisson = inf`` at a pixel that is *not* flagged ``DO_NOT_USE`` —
    therefore drops only that term; the pixel keeps its surviving components
    instead of being discarded wholesale. This matters most when a bad
    component is correlated across inputs (a flat-field column, a reference
    region): masking the *summed* variance would zero every input there and
    punch a NaN hole into the ERR map, whereas per-component masking keeps the
    good terms (``var_rnoise`` is essentially always finite and positive, so
    the pixel stays finite).

    Returns ``(var_total, var_weight)``:

    - ``var_total`` is the sum of the validated components — finite and ``>= 0``
      everywhere, so the ``var·weight`` data array handed to cdriz never carries
      an ``inf``/``nan``.
    - ``var_weight`` is the SCI ``weight`` zeroed only at *fully dead* pixels,
      where no component is valid. Those are dropped from both the variance
      numerator and its normalizing weight (``outvarw``), so they become
      ERR = NaN rather than a spurious ERR = 0; everywhere else ``var_weight``
      equals ``weight``, leaving the SCI/coverage weight untouched.

    Without any masking, one input pixel with ``inf``/``nan`` variance poisons
    every output pixel its kernel touches (``inf`` is sticky in cdriz's running
    weighted mean), blowing up the final ERR for all co-located inputs.
    """
    var_total = np.zeros(weight.shape, dtype=np.float32)
    any_valid = np.zeros(weight.shape, dtype=bool)
    for component in (var_rnoise, var_poisson, var_flat):
        valid = np.isfinite(component) & (component >= 0)
        var_total += np.where(valid, component, np.float32(0.0))
        any_valid |= valid
    var_weight = np.where(any_valid, weight, np.float32(0.0)).astype(np.float32)
    return var_total, var_weight


def _prepare_drizzle_input(crf_file, output_wcs, out_shape, *,
                           weight_type, good_bits, blender=None):
    """Open one CRF and prepare the per-input arrays drizzle needs.

    Returns a dict with ``data``, ``err``, ``var_total``, ``weight``,
    ``pixmap``, ``exptime``, ``xmin``/``xmax``/``ymin``/``ymax``,
    ``in_shape``, ``input_gwcs`` — or ``None`` if the input footprint
    does not overlap the tile.

    If ``blender`` is given (the resample path), the input model is fed to it
    for header blending — only once we know the input overlaps the tile, so
    skipped inputs don't contribute metadata. This reuses the single open the
    array prep already does. ``drizzle_tile_singles`` (outlier) passes ``None``.

    Shared by ``drizzle_tile`` (accumulate mode for resample) and
    ``drizzle_tile_singles`` (per-input rasters for outlier).
    """
    from jwst.datamodels.dqflags import pixel as pixel_flags
    from stcal.resample.utils import build_driz_weight, resample_range
    from stdatamodels.jwst.datamodels import ImageModel

    with ImageModel(crf_file, memmap=False) as model:
        data = np.asarray(model.data, dtype=np.float32)
        err = np.asarray(model.err, dtype=np.float32)
        in_shape = data.shape
        input_gwcs = deepcopy(model.meta.wcs)
        exptime = float(model.meta.exposure.exposure_time)
        input_pixelarea_a2 = model.meta.photometry.pixelarea_arcsecsq

        pixmap = _input_to_output_pixmap(input_gwcs, output_wcs, in_shape)
        bbox = _output_bbox_in_tile(pixmap, out_shape)
        if bbox is None:
            return None
        sly, slx = bbox

        if blender is not None:
            blender.accumulate(model)

        weight = build_driz_weight(
            {'data': model.data, 'dq': model.dq,
             'var_rnoise': model.var_rnoise},
            weight_type=weight_type,
            good_bits=good_bits,
            flag_name_map=pixel_flags,
        ).astype(np.float32)

        # Sum the variance components, masking each independently (see
        # _sanitize_variance) so one bad component can't poison the ERR map.
        var_total, var_weight = _sanitize_variance(
            np.asarray(model.var_rnoise, dtype=np.float32),
            np.asarray(model.var_poisson, dtype=np.float32),
            np.asarray(model.var_flat, dtype=np.float32),
            weight,
        )

        xmin, xmax, ymin, ymax = resample_range(
            in_shape, input_gwcs.bounding_box,
        )

    return {
        'data': data, 'err': err,
        'var_total': var_total, 'weight': weight, 'var_weight': var_weight,
        'pixmap': pixmap, 'exptime': exptime,
        'xmin': xmin, 'xmax': xmax, 'ymin': ymin, 'ymax': ymax,
        'in_shape': in_shape, 'input_gwcs': input_gwcs,
        'input_pixelarea_a2': input_pixelarea_a2,
        'sly': sly, 'slx': slx,
        'bbox_shape': (sly.stop - sly.start, slx.stop - slx.start),
    }


def _add_image_kwargs(prep, pixfrac):
    """Common kwargs for `Drizzle.add_image` from a `_prepare_drizzle_input` dict."""
    return dict(
        exptime=prep['exptime'],
        pixmap=prep['pixmap'],
        weight_map=prep['weight'],
        pixfrac=pixfrac,
        in_units='cps',
        xmin=prep['xmin'], xmax=prep['xmax'],
        ymin=prep['ymin'], ymax=prep['ymax'],
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
    blendheaders=True,
    reduction_version='unknown',
    compress_context=True,
    write_context=True,
):
    """Drizzle ``crf_files`` into a single i2d at ``output_path``.

    Persistent accumulators across all inputs:
    - ``outsci`` / ``outwht`` / ``outctx`` — SCI pass (Drizzle sees data,
      writes weighted-mean SCI and weight sum, tracks input contributions
      in the context array).
    - ``outvar`` / ``outvarw`` — variance-trick pass (drizzle ``var·wht``
      weighted by the *masked* variance weight; ``outvarw`` is the running
      sum of that weight and is used to normalize ``outvar`` into the final
      variance. It equals ``outwht`` bit-for-bit except at pixels where some
      input's variance was non-finite/negative and thus excluded from the
      variance estimate — see ``_prepare_drizzle_input``).

    Parameters mirror ``Field.get_tile_wcs`` outputs (``crpix``, ``crval``,
    ``shape``, ``rotation``) and ``[nircam.resample]`` config knobs
    (``pixfrac``, ``kernel``, ``weight_type``, ``good_bits``,
    ``blendheaders``). With ``blendheaders`` (default), input header metadata
    is blended into the output i2d via ``jwst.model_blender`` so the product
    carries the same keywords as a jwst ``Image3Pipeline`` mosaic.
    """
    from drizzle.resample import Drizzle

    n_inputs = len(crf_files)
    nx, ny = shape
    out_shape = (ny, nx)

    log(f"  campfire drizzle: {n_inputs} inputs into {nx}x{ny} tile")

    output_wcs = _build_output_wcs(
        crf_files, crpix, crval, shape, rotation, pixel_scale,
    )

    blender = None
    if blendheaders:
        from jwst.model_blender.blender import ModelBlender
        # Pixel area and WCS are recomputed for the output grid in
        # _apply_output_metadata / update_fits_wcsinfo, so they must not be
        # inherited from the native-scale inputs.
        blender = ModelBlender(blend_ignore_attrs=[
            'meta.photometry.pixelarea_steradians',
            'meta.photometry.pixelarea_arcsecsq',
            'meta.wcs',
        ])

    outsci = np.zeros(out_shape, dtype=np.float32)
    outwht = np.zeros(out_shape, dtype=np.float32)
    outvar = np.zeros(out_shape, dtype=np.float32)
    outvarw = np.zeros(out_shape, dtype=np.float32)
    # The context cube is one int32 plane per 32 inputs at FULL tile size, so
    # it costs tile_area * 4 * ceil(n/32) — for a deep tile it dwarfs
    # SCI+ERR+WHT combined (measured: 660 GiB for a 1.15 Gpix tile with 3,471
    # inputs, vs ~104 GiB for everything else). Nothing in this pipeline reads
    # CON: it is not among the extensions `_split_extensions` writes out
    # (sci/err/wht/srcmask), and bkgsub does not touch it. `write_context =
    # false` therefore skips the allocation entirely and drizzles with
    # `disable_ctx=True` — the same path the variance drizzle below already
    # uses — turning otherwise unschedulable tiles into ordinary ones.
    if write_context:
        n_planes = max(1, (n_inputs + 31) // 32)
        outctx = np.zeros((n_planes, ny, nx), dtype=np.int32)
    else:
        outctx = None

    # fillval='NaN' leaves every zero-weight output pixel — no coverage OR masked
    # in all overlapping inputs — as NaN in SCI, which is why the mosaic no longer
    # needs a post-drizzle "set SCI=NaN where WHT=0" pass. The variance drizzle
    # keeps 'INDEF' (0): outvarw==0 pixels are turned into NaN by the
    # outvar/outvarw normalization below, so ERR is already NaN there.
    if write_context:
        sci_drizzle = Drizzle(
            out_img=outsci, out_wht=outwht, out_ctx=outctx,
            kernel=kernel, fillval='NaN',
            max_ctx_id=n_inputs,
        )
    else:
        sci_drizzle = Drizzle(
            out_img=outsci, out_wht=outwht,
            kernel=kernel, fillval='NaN',
            disable_ctx=True,
        )
    var_drizzle = Drizzle(
        out_img=outvar, out_wht=outvarw,
        kernel=kernel, fillval='INDEF',
        disable_ctx=True,
    )

    skipped = 0
    input_pixelarea_a2 = None
    for i, crf_file in enumerate(crf_files, start=1):
        basename = os.path.basename(crf_file)
        prep = _prepare_drizzle_input(
            crf_file, output_wcs, out_shape,
            weight_type=weight_type, good_bits=good_bits, blender=blender,
        )
        if prep is None:
            skipped += 1
            log(f"  [{i}/{n_inputs}] {basename}: no tile overlap, skipping")
            continue

        if input_pixelarea_a2 is None:
            input_pixelarea_a2 = prep['input_pixelarea_a2']

        common = _add_image_kwargs(prep, pixfrac)
        sci_drizzle.add_image(data=prep['data'], **common)
        # The variance pass uses the *masked* variance weight from
        # _sanitize_variance (zero only at fully-dead pixels, where no variance
        # component is valid) so those are excluded from both the numerator and
        # ``outvarw`` (the denominator). var_total is finite everywhere (bad
        # components contribute 0), so the ``var·weight`` data array is finite.
        # ``data`` keeps the unmasked SCI ``weight`` so the numerator carries
        # the wᵢ² factor; ``weight_map`` carries the masked weight.
        var_drizzle.add_image(
            data=(prep['var_total'] * prep['weight']).astype(np.float32),
            **dict(common, weight_map=prep['var_weight']),
        )
        log(f"  [{i}/{n_inputs}] drizzled {basename}")

    total_exptime = float(sci_drizzle.total_exptime)

    # Final ERR. The variance trick gives outvar (per pixel) =
    # Σwᵢ²kᵢvarᵢ / Σwᵢkᵢ; dividing by outvarw (= Σwᵢkᵢ over the inputs with a
    # valid variance estimate) yields the canonical weighted variance
    # Σwᵢ²kᵢvarᵢ / (Σwᵢkᵢ)². outvarw is used in place of outwht so that inputs
    # masked out of the variance estimate are excluded from its normalization
    # too; at pixels with no masking the two are bit-identical (same inputs,
    # weights, and pixmap in the same order). Pixels with zero variance weight
    # become ERR = NaN (matches stcal's missing-data convention used by
    # bkgsub.off_detector via np.isnan(err)) — including pixels that hold flux
    # but where no contributing input had any valid variance component.
    with np.errstate(divide='ignore', invalid='ignore'):
        out_var_final = np.where(outvarw > 0, outvar / outvarw, np.nan)
        outerr = np.sqrt(out_var_final).astype(np.float32)

    # Only blend if at least one input actually contributed; finalizing an
    # empty blender would have no input metadata to draw from.
    contributing = n_inputs - skipped

    # Pixel scale ratio (output/native input) -> PXSCLRT, for parity with jwst.
    pixel_scale_ratio = None
    if input_pixelarea_a2:
        pixel_scale_ratio = float(pixel_scale) / float(np.sqrt(input_pixelarea_a2))

    _write_i2d_fits(
        output_path,
        sci=outsci, err=outerr, wht=outwht, ctx=outctx,
        output_wcs=output_wcs,
        cmpfrver=reduction_version,
        exptime=total_exptime,
        pixel_scale=pixel_scale,
        pixfrac=pixfrac,
        kernel=kernel,
        weight_type=weight_type,
        blender=blender if contributing > 0 else None,
        n_pointings=contributing if contributing > 0 else None,
        compress_context=compress_context,
        pixel_scale_ratio=pixel_scale_ratio,
    )

    log(f"  wrote {os.path.basename(output_path)} "
        f"({n_inputs - skipped} contributing inputs)")


def drizzle_tile_singles(
    crf_files,
    output_wcs,
    out_shape,
    *,
    pixfrac=1.0,
    kernel='square',
    weight_type='ivm',
    good_bits='~DO_NOT_USE',
):
    """Yield per-input bbox-sliced ``(sci, wht, prep)`` rasters.

    Each input is drizzled into a fresh **bbox-sized** buffer (the input
    footprint in the output frame plus a small kernel-halo pad), not a
    full-tile buffer. The pixmap is shifted by ``(slx.start, sly.start)``
    so cdriz writes into bbox-local coordinates. ``prep['sly']`` and
    ``prep['slx']`` carry the slice into the full tile so the caller can
    paste each raster into a tile-shape scratch buffer when feeding
    ``MedianComputer`` (which requires full-shape input).

    Avoiding per-input full-tile allocation is the point: with N inputs at
    COSMOS-Web tile scale, the old full-tile path zero-init'd ~70 GB of
    Drizzle scratch per tile run; bbox-allocation drops that by roughly the
    ratio of input-footprint area to tile area (~5×).

    Only SCI is drizzled — matches ``jwst.outlier_detection``'s
    ``ResampleImage(enable_var=False, compute_err=None)`` setup.
    ``flag_resampled_model_crs`` falls back to the input model's own ERR
    for the SNR comparison when ``median_err`` is not supplied, which is
    the upstream default and what we want here.

    Used by ``outlier_detect_for_visit`` to feed a streaming median for
    cosmic-ray rejection. The caller builds ``output_wcs`` and
    ``out_shape`` appropriately (per-visit intermediate WCS for outlier,
    tile WCS for resample) and reuses them across this function and the
    downstream blot pass.

    Yields
    ------
    sci, wht : `numpy.ndarray` (bbox_shape, float32)
        Bbox-sliced rasters. Pixels with no input contribution are NaN
        (sci) or zero (wht).
    prep : dict
        Per-input metadata. Includes ``sly`` and ``slx`` slices into the
        full tile, plus ``bbox_shape``, ``input_gwcs``, ``exptime``, etc.

    Inputs that don't overlap the tile are skipped (no yield).
    """
    from drizzle.resample import Drizzle

    skipped = 0
    for crf_file in crf_files:
        prep = _prepare_drizzle_input(
            crf_file, output_wcs, out_shape,
            weight_type=weight_type, good_bits=good_bits,
        )
        if prep is None:
            skipped += 1
            continue

        sly, slx = prep['sly'], prep['slx']
        pixmap_local = prep['pixmap'].copy()
        pixmap_local[..., 0] -= slx.start
        pixmap_local[..., 1] -= sly.start

        common = _add_image_kwargs(prep, pixfrac)
        common['pixmap'] = pixmap_local

        sci_driz = Drizzle(
            out_shape=prep['bbox_shape'], kernel=kernel, fillval='NaN',
            disable_ctx=True,
        )
        sci_driz.add_image(data=prep['data'], **common)

        yield sci_driz.out_img, sci_driz.out_wht, prep

    if skipped:
        log(f"  {skipped} inputs did not overlap tile")
