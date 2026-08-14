"""Tests for the campfire-native NIRCam drizzle (`campfire_pipeline.nircam.drizzle`).

Regression coverage for the variance-pass sanitization. Before the fix, the
variance components were summed before drizzling, so one ``inf``/``nan``/
negative component propagated into ``var_total * weight``; cdriz spread it
across every output pixel the kernel touched, and ``inf`` is sticky in the
running weighted mean — so one bad input pixel blew up the ERR for all
co-located inputs (inf-dominated ERR maps).

``_sanitize_variance`` masks each component independently with
``(var >= 0) & isfinite(var)`` (matching stcal's per-component masking), so a
bad component drops only its own term. A pixel is discarded entirely only when
*no* component is valid. This degrades gracefully when a component is bad across
many inputs (a flat-field column, a reference region), where masking the summed
variance would instead punch a NaN hole in the ERR map.

The integration tests drive the **real** ``drizzle.resample.Drizzle`` (the same
primitive ``drizzle_tile`` uses) through an identity pixmap, so they reproduce
the bug and validate the fix deterministically without depending on WCS
construction.
"""

import os

import numpy as np
import pytest
from astropy.io import fits

import campfire_pipeline.nircam.drizzle as drizzle_mod
from campfire_pipeline.nircam.drizzle import (
    _apply_output_metadata, _sanitize_variance, _output_bbox_in_tile,
    compress_context_extension,
)

Drizzle = pytest.importorskip("drizzle.resample").Drizzle
ImageModel = pytest.importorskip("stdatamodels.jwst.datamodels").ImageModel
ModelBlender = pytest.importorskip("jwst.model_blender.blender").ModelBlender


# ---------------------------------------------------------------------------
# unit tests: the per-component masking helper
# ---------------------------------------------------------------------------

def test_sanitize_variance_sums_clean_components():
    vr = np.full((1, 3), 1e-3, np.float32)
    vp = np.full((1, 3), 2e-3, np.float32)
    vf = np.full((1, 3), 1e-4, np.float32)
    weight = np.full((1, 3), 7.0, np.float32)
    var_total, var_weight = _sanitize_variance(vr, vp, vf, weight)
    np.testing.assert_allclose(var_total, 3.1e-3, rtol=1e-6)
    np.testing.assert_array_equal(var_weight, weight)


def test_sanitize_variance_drops_only_the_bad_component():
    # col 0 clean; col 1 bad poisson; col 2 negative flat
    vr = np.array([[1e-3, 1e-3, 1e-3]], np.float32)
    vp = np.array([[2e-3, np.inf, 2e-3]], np.float32)
    vf = np.array([[1e-4, 1e-4, -5.0]], np.float32)
    weight = np.full((1, 3), 10.0, np.float32)
    var_total, var_weight = _sanitize_variance(vr, vp, vf, weight)

    np.testing.assert_allclose(var_total[0, 0], 3.1e-3, rtol=1e-6)   # all three
    np.testing.assert_allclose(var_total[0, 1], 1.1e-3, rtol=1e-6)   # poisson dropped
    np.testing.assert_allclose(var_total[0, 2], 3.0e-3, rtol=1e-6)   # flat dropped
    assert np.isfinite(var_total).all()
    # every pixel keeps >= 1 valid component, so the SCI weight is untouched
    np.testing.assert_array_equal(var_weight, weight)


def test_sanitize_variance_drops_pixel_with_no_valid_component():
    vr = np.array([[1e-3, np.inf]], np.float32)
    vp = np.array([[2e-3, np.nan]], np.float32)
    vf = np.array([[1e-4, -1.0]], np.float32)
    weight = np.full((1, 2), 9.0, np.float32)
    var_total, var_weight = _sanitize_variance(vr, vp, vf, weight)
    assert var_weight[0, 0] == 9.0      # has valid components
    assert var_weight[0, 1] == 0.0      # fully dead -> dropped
    assert np.isfinite(var_total).all()
    assert weight[0, 1] == 9.0          # input weight not mutated


# ---------------------------------------------------------------------------
# pixmap overlap detection + the cdriz "too few valid pixels" crash guard
# ---------------------------------------------------------------------------

def test_output_bbox_in_tile_uses_in_frame_pixels():
    """``_output_bbox_in_tile`` must key off pixels that map *inside* the
    output frame, not merely finite ones — the pixmap is finite everywhere now.
    """
    out_shape = (50, 50)
    pm = np.empty((10, 10, 2), np.float64)
    iy, ix = np.indices((10, 10), dtype=np.float64)
    # map the whole input far off-frame except a 3x3 patch landing at (5..7, 5..7)
    pm[..., 0] = ix - 1000.0
    pm[..., 1] = iy - 1000.0
    pm[:3, :3, 0] = ix[:3, :3] + 5.0
    pm[:3, :3, 1] = iy[:3, :3] + 5.0

    sly, slx = _output_bbox_in_tile(pm, out_shape, pad=1)
    # the in-frame patch spans output [5,7]x[5,7]; pad=1 grows + clips to tile
    assert slx.start <= 5 and slx.stop >= 8
    assert sly.start <= 5 and sly.stop >= 8
    assert slx.stop <= 50 and sly.stop <= 50

    # a footprint entirely off-frame -> no overlap
    pm_off = np.empty((10, 10, 2), np.float64)
    pm_off[..., 0] = ix - 1000.0
    pm_off[..., 1] = iy - 1000.0
    assert _output_bbox_in_tile(pm_off, out_shape) is None


def test_drizzle_does_not_crash_on_grazing_finite_pixmap():
    """A finite, geometrically-continuous pixmap whose footprint barely grazes
    the output frame must drizzle (the in-frame pixels) without raising.

    This is the corner-graze case (exposure 19): with the default
    ``with_bounding_box=True`` invert, off-frame pixels became NaN and cdriz
    raised "No or too few valid pixels in the pixel map" on the degenerate
    finite remainder. With the finite-everywhere pixmap, cdriz clips the
    off-frame pixels itself and keeps correct edge geometry — no crash, and the
    in-frame contribution (here ~0) is handled gracefully.
    """
    ny = nx = 64
    out_shape = (16, 16)
    iy, ix = np.indices((ny, nx), dtype=np.float64)
    # continuous identity-shifted map; only a corner pokes into the 16x16 frame
    pm = np.empty((ny, nx, 2), np.float64)
    pm[..., 0] = ix - 62.0
    pm[..., 1] = iy - 62.0

    out = np.zeros(out_shape, np.float32)
    wht = np.zeros(out_shape, np.float32)
    d = Drizzle(out_img=out, out_wht=wht, kernel="square",
                fillval="INDEF", disable_ctx=True)
    # must not raise ValueError("No or too few valid pixels ...")
    d.add_image(data=np.ones((ny, nx), np.float32),
                weight_map=np.ones((ny, nx), np.float32),
                exptime=1.0, pixmap=pm, pixfrac=1.0, in_units="cps")
    assert np.isfinite(wht).all()


def test_drizzle_partial_overlap_keeps_full_interior_weight():
    """A finite, continuous pixmap that half-overlaps the output frame must
    deposit full weight across the in-frame bulk (not just the boundary).

    Guards against the rejected sentinel approach, which poisoned cdriz's
    per-pixel geometry (it derives each footprint from neighbouring pixmap
    entries) and zeroed the entire exposure's contribution. A continuous map —
    what ``invert(with_bounding_box=False)`` actually produces — has no such
    discontinuity at the frame edge.
    """
    ny = nx = 64
    iy, ix = np.indices((ny, nx), dtype=np.float64)
    pm = np.empty((ny, nx, 2), np.float64)
    # continuous shift: input cols 0..19 map off-frame (output x < 0),
    # cols 20..63 map in-frame to output x 0..43.
    pm[..., 0] = ix - 20.0
    pm[..., 1] = iy

    out = np.zeros((ny, nx), np.float32)
    wht = np.zeros((ny, nx), np.float32)
    d = Drizzle(out_img=out, out_wht=wht, kernel="square",
                fillval="INDEF", disable_ctx=True)
    d.add_image(data=np.ones((ny, nx), np.float32),
                weight_map=np.ones((ny, nx), np.float32),
                exptime=1.0, pixmap=pm, pixfrac=1.0, in_units="cps")
    # interior of the in-frame region gets full unit weight
    assert wht[:, 5:40].min() > 0.99
    # output columns no input maps to stay empty (no smear from the edge)
    assert wht[:, 45:].max() == 0.0


# ---------------------------------------------------------------------------
# integration tests: real Drizzle, identity pixmap, the variance trick
# ---------------------------------------------------------------------------

def _identity_pixmap(ny, nx):
    iy, ix = np.indices((ny, nx), dtype=np.float64)
    pm = np.empty((ny, nx, 2), np.float64)
    pm[..., 0] = ix
    pm[..., 1] = iy
    return pm


def _drizzle_variance_trick(inputs, shape, *, sanitize):
    """Mirror drizzle_tile's SCI + variance passes on an identity pixmap.

    ``inputs`` is a list of ``(var_rnoise, var_poisson, var_flat, weight)``.
    With ``sanitize=False`` this is the pre-fix behaviour (raw sum, shared SCI
    weight, normalize by the SCI weight); with ``sanitize=True`` it uses the
    production ``_sanitize_variance`` and normalizes by ``outvarw``.
    """
    ny, nx = shape
    pm = _identity_pixmap(ny, nx)
    outsci = np.zeros(shape, np.float32); outwht = np.zeros(shape, np.float32)
    outvar = np.zeros(shape, np.float32); outvarw = np.zeros(shape, np.float32)
    sci = Drizzle(out_img=outsci, out_wht=outwht, kernel="square",
                  fillval="INDEF", disable_ctx=True)
    var = Drizzle(out_img=outvar, out_wht=outvarw, kernel="square",
                  fillval="INDEF", disable_ctx=True)
    addkw = dict(exptime=1000.0, pixmap=pm, pixfrac=1.0, in_units="cps",
                 xmin=0, xmax=nx - 1, ymin=0, ymax=ny - 1)
    with np.errstate(all="ignore"):
        for vr, vp, vf, weight in inputs:
            sci.add_image(data=np.zeros(shape, np.float32), weight_map=weight,
                          **addkw)
            if sanitize:
                var_total, var_weight = _sanitize_variance(vr, vp, vf, weight)
            else:
                var_total = (vr + vp + vf).astype(np.float32)   # raw sum
                var_weight = weight
            var.add_image(data=(var_total * weight).astype(np.float32),
                          weight_map=var_weight, **addkw)
        denom = outvarw if sanitize else outwht
        err = np.sqrt(np.where(denom > 0, outvar / denom, np.nan)).astype(np.float32)
    return err, outwht


def _const(shape, value):
    return np.full(shape, value, np.float32)


@pytest.mark.parametrize("bad_value", [np.inf, np.nan, -1.0])
def test_variance_trick_sanitized_vs_raw(bad_value):
    """One bad variance component at a GOOD pixel in one input: the raw trick
    produces a non-finite ERR there; the sanitized trick stays finite."""
    shape = (5, 5)
    weight = _const(shape, 1000.0)
    clean = (_const(shape, 1e-3), _const(shape, 2e-3), _const(shape, 1e-4),
             weight)
    vp_bad = _const(shape, 2e-3); vp_bad[2, 2] = bad_value
    bad = (_const(shape, 1e-3), vp_bad, _const(shape, 1e-4), weight)
    inputs = [clean, bad, clean]

    raw_err, _ = _drizzle_variance_trick(inputs, shape, sanitize=False)
    san_err, san_wht = _drizzle_variance_trick(inputs, shape, sanitize=True)

    covered = san_wht > 0
    assert covered[2, 2]
    assert not np.isfinite(raw_err[2, 2])         # pre-fix: poisoned
    assert np.isfinite(san_err[covered]).all()    # fixed: all covered finite
    assert san_err[2, 2] > 0
    # away from the bad pixel the two agree (masking is local)
    off = covered.copy(); off[2, 2] = False
    np.testing.assert_allclose(san_err[off], raw_err[off], rtol=1e-5)


def test_per_component_masking_survives_component_bad_across_all_inputs():
    """A component that is bad in EVERY input at a pixel (e.g. a flat-field
    column) must still yield a finite ERR from the surviving components — this
    is the case where masking the summed variance would punch a NaN hole."""
    shape = (4, 4)
    weight = _const(shape, 500.0)
    inputs = []
    for _ in range(3):
        vf = _const(shape, 1e-4); vf[1, 1] = np.inf      # flat bad in all inputs
        inputs.append((_const(shape, 1e-3), _const(shape, 2e-3), vf, weight))

    err, wht = _drizzle_variance_trick(inputs, shape, sanitize=True)
    assert wht[1, 1] > 0
    assert np.isfinite(err[1, 1])                         # NOT a NaN hole
    # ERR reflects rnoise+poisson only (flat dropped), averaged over 3 equal-
    # weight inputs: V = var_per_input / N for N equal measurements.
    np.testing.assert_allclose(err[1, 1], np.sqrt((1e-3 + 2e-3) / 3), rtol=1e-3)
    assert not np.isinf(err).any()


def test_variance_trick_all_components_bad_gives_nan_not_inf():
    """A pixel where no input has any valid variance component has no noise
    estimate -> ERR is NaN (missing-data convention), never inf or 0."""
    shape = (4, 4)
    weight = _const(shape, 500.0)
    inputs = []
    for _ in range(2):
        vr = _const(shape, 1e-3); vr[1, 1] = np.inf
        vp = _const(shape, 2e-3); vp[1, 1] = np.inf
        vf = _const(shape, 1e-4); vf[1, 1] = np.inf
        inputs.append((vr, vp, vf, weight))

    err, wht = _drizzle_variance_trick(inputs, shape, sanitize=True)
    assert not np.isinf(err).any()
    assert np.isnan(err[1, 1])
    other = (wht > 0).copy(); other[1, 1] = False
    assert np.isfinite(err[other]).all()


# ---------------------------------------------------------------------------
# output metadata: blend inputs + recompute output-grid photometry
# ---------------------------------------------------------------------------

# native NIRCam LONG values, so we can assert pixel area is recomputed (not
# inherited) and the per-sr photometry is inherited unchanged.
_NATIVE_PIXAR_A2 = 0.003957788
_NATIVE_PIXAR_SR = 9.30255e-14
_PHOTMJSR = 0.402


def _fake_input_model(filt='F444W'):
    m = ImageModel((4, 4))
    m.meta.telescope = 'JWST'
    m.meta.instrument.name = 'NIRCAM'
    m.meta.instrument.filter = filt
    m.meta.instrument.detector = 'NRCALONG'
    m.meta.observation.program_number = '06882'
    m.meta.target.proposer_name = 'RXCJ0911+1746'
    m.meta.exposure.exposure_time = 418.734
    m.meta.bunit_data = 'MJy/sr'
    m.meta.photometry.conversion_megajanskys = _PHOTMJSR
    m.meta.photometry.pixelarea_arcsecsq = _NATIVE_PIXAR_A2
    m.meta.photometry.pixelarea_steradians = _NATIVE_PIXAR_SR
    return m


def _blender():
    return ModelBlender(blend_ignore_attrs=[
        'meta.photometry.pixelarea_steradians',
        'meta.photometry.pixelarea_arcsecsq',
        'meta.wcs',
    ])


def test_apply_output_metadata_recomputes_pixel_area_for_output_grid():
    """PIXAR_A2/PIXAR_SR must reflect the OUTPUT pixel scale, not the native
    input scale — copying the input would bias MJy/sr -> Jy/pixel by the scale
    ratio squared. PHOTMJSR (per-sr) is scale-invariant and rides along."""
    blender = _blender()
    for _ in range(2):
        blender.accumulate(_fake_input_model())

    out = ImageModel((4, 4))
    _apply_output_metadata(
        out, blender=blender, pixel_scale=0.03,
        pixfrac=1.0, kernel='square', weight_type='ivm', exptime=837.468,
    )

    # recomputed for the 0.03" output grid
    assert out.meta.photometry.pixelarea_arcsecsq == pytest.approx(0.03 ** 2)
    assert out.meta.photometry.pixelarea_steradians == pytest.approx(
        0.03 ** 2 * (np.pi / (180.0 * 3600.0)) ** 2)
    # explicitly NOT the inherited native value
    assert out.meta.photometry.pixelarea_arcsecsq != pytest.approx(
        _NATIVE_PIXAR_A2)
    # per-sr photometry inherited unchanged
    assert out.meta.photometry.conversion_megajanskys == pytest.approx(
        _PHOTMJSR)


def test_apply_output_metadata_blends_identity_and_sets_provenance():
    blender = _blender()
    for _ in range(2):
        blender.accumulate(_fake_input_model())

    out = ImageModel((4, 4))
    _apply_output_metadata(
        out, blender=blender, pixel_scale=0.03,
        pixfrac=0.8, kernel='square', weight_type='ivm', exptime=837.468,
    )

    # inherited identity from the blended inputs
    assert out.meta.instrument.name == 'NIRCAM'
    assert out.meta.instrument.filter == 'F444W'
    assert out.meta.observation.program_number == '06882'
    # units on both SCI and ERR
    assert out.meta.bunit_data == 'MJy/sr'
    assert out.meta.bunit_err == 'MJy/sr'
    # resample provenance + step flag describe THIS run
    assert out.meta.resample.pixfrac == pytest.approx(0.8)
    assert out.meta.resample.weight_type == 'ivm'
    assert out.meta.exposure.exposure_time == pytest.approx(837.468)
    assert out.meta.cal_step.resample == 'COMPLETE'


def test_apply_output_metadata_without_blender_still_sets_photometry():
    """blendheaders=False (or an all-skipped tile) must still produce a
    well-formed product: units, pixel area, and the resample step flag."""
    out = ImageModel((4, 4))
    _apply_output_metadata(
        out, blender=None, pixel_scale=0.06,
        pixfrac=1.0, kernel='square', weight_type='ivm', exptime=100.0,
    )
    assert out.meta.photometry.pixelarea_arcsecsq == pytest.approx(0.06 ** 2)
    assert out.meta.bunit_data == 'MJy/sr'
    assert out.meta.bunit_err == 'MJy/sr'
    assert out.meta.cal_step.resample == 'COMPLETE'


# ---------------------------------------------------------------------------
# CON compression
#
# Two call paths share one rewrite: the campfire backend hands the array in
# (the file holds a 1x1x1 placeholder), the jwst backend hands nothing in and
# the full uncompressed CON is read back off the disk. Both must land on a
# bit-identical CON under the same extension name and order, with every other
# extension untouched.
# ---------------------------------------------------------------------------

_SCI = np.arange(12, dtype=np.float32).reshape(3, 4)
_ERR = (_SCI + 0.5).astype(np.float32)
_WHT = np.ones((3, 4), dtype=np.float32)


def _context_array():
    """A coherent int32 bitmask: blocky, mostly zero — like a real CON."""
    ctx = np.zeros((3, 3, 4), dtype=np.int32)
    ctx[0, 1:, 1:3] = 0b1011
    ctx[1, 2:, :2] = 0b110
    return ctx


def _write_i2d_like(path, con_data):
    """Minimal stand-in for the i2d layout, CON in its usual third slot."""
    primary = fits.PrimaryHDU()
    primary.header['CMPFRVER'] = ('test', 'CAMPFIRE git commit')
    con = fits.ImageHDU(con_data, name='CON')
    con.header['BUNIT'] = ('', 'provenance the datamodel put on CON')
    hdrtab = fits.BinTableHDU.from_columns(
        [fits.Column(name='FILENAME', format='8A',
                     array=np.array(['a.fits', 'b.fits']))],
        name='HDRTAB',
    )
    fits.HDUList([
        primary,
        fits.ImageHDU(_SCI, name='SCI'),
        fits.ImageHDU(_ERR, name='ERR'),
        con,
        fits.ImageHDU(_WHT, name='WHT'),
        hdrtab,
    ]).writeto(path, overwrite=True)


def _assert_compressed_i2d_intact(path, expected_ctx):
    with fits.open(path) as hdul:
        assert [h.name for h in hdul] == [
            'PRIMARY', 'SCI', 'ERR', 'CON', 'WHT', 'HDRTAB']
        assert isinstance(hdul['CON'], fits.CompImageHDU)
        con = hdul['CON'].data
        assert con.shape == expected_ctx.shape
        # FITS stores big-endian >i4 and CompImageHDU decompresses to native
        # int32 — same integers, so compare on kind/itemsize not exact dtype.
        assert con.dtype.kind == 'i' and con.dtype.itemsize == 4
        assert np.array_equal(con, expected_ctx)
        assert hdul['CON'].header.get('BUNIT') == ''
        np.testing.assert_array_equal(hdul['SCI'].data, _SCI)
        np.testing.assert_array_equal(hdul['ERR'].data, _ERR)
        np.testing.assert_array_equal(hdul['WHT'].data, _WHT)
        assert list(hdul['HDRTAB'].data['FILENAME']) == ['a.fits', 'b.fits']
        assert hdul[0].header['CMPFRVER'] == 'test'


def test_compress_context_from_supplied_array(tmp_path):
    """campfire backend: the file carries a placeholder, ctx comes in memory."""
    path = str(tmp_path / 'placeholder_i2d.fits')
    ctx = _context_array()
    _write_i2d_like(path, np.zeros((1, 1, 1), dtype=np.int32))

    assert compress_context_extension(path, ctx=ctx) is True
    _assert_compressed_i2d_intact(path, ctx)


def test_compress_context_read_back_from_file(tmp_path):
    """jwst backend: CON is already on disk uncompressed and gets rewritten.

    This is the path that used to be a no-op — `compress_context` was only
    threaded into the campfire backend, so the shipped default
    (`implementation = "jwst"`) ignored it entirely.
    """
    path = str(tmp_path / 'uncompressed_i2d.fits')
    ctx = _context_array()
    _write_i2d_like(path, ctx)

    assert compress_context_extension(path) is True
    _assert_compressed_i2d_intact(path, ctx)


def test_compress_context_is_idempotent(tmp_path):
    """Re-running on an already-compressed i2d is a no-op, not a re-wrap."""
    path = str(tmp_path / 'twice_i2d.fits')
    ctx = _context_array()
    _write_i2d_like(path, ctx)

    assert compress_context_extension(path) is True
    assert compress_context_extension(path) is False
    _assert_compressed_i2d_intact(path, ctx)


def test_compress_context_without_con_extension(tmp_path):
    """A product with no CON is left alone rather than raising."""
    path = str(tmp_path / 'nocon.fits')
    fits.HDUList([
        fits.PrimaryHDU(), fits.ImageHDU(_SCI, name='SCI'),
    ]).writeto(path, overwrite=True)

    assert compress_context_extension(path) is False
    with fits.open(path) as hdul:
        assert [h.name for h in hdul] == ['PRIMARY', 'SCI']


def test_compress_context_leaves_no_temp_file(tmp_path):
    path = str(tmp_path / 'tmpcheck_i2d.fits')
    _write_i2d_like(path, _context_array())

    compress_context_extension(path)
    assert not os.path.exists(f'{path}.ctx.tmp')
    assert sorted(os.listdir(tmp_path)) == ['tmpcheck_i2d.fits']


def test_compress_context_cleans_up_temp_when_write_fails(tmp_path,
                                                          monkeypatch):
    """A failed compression must not strand a partial `.ctx.tmp`.

    ENOSPC partway through is realistic here — the i2d is the largest file the
    pipeline writes — and under ``--processes N`` a whole-field run would
    otherwise leak one partial temp per failed tile onto the disk that just
    filled. Same guarantee `common.io.atomic_save` gives.
    """
    path = str(tmp_path / 'failing_i2d.fits')
    ctx = _context_array()
    _write_i2d_like(path, ctx)
    original = fits.HDUList.writeto

    def _writeto_then_fail(self, fileobj, *args, **kwargs):
        # Land a partial file first, the way a truncated write would.
        with open(fileobj, 'wb') as fp:
            fp.write(b'SIMPLE  =                    T')
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(fits.HDUList, 'writeto', _writeto_then_fail)
    with pytest.raises(OSError):
        compress_context_extension(path, ctx=ctx)
    monkeypatch.setattr(fits.HDUList, 'writeto', original)

    assert not os.path.exists(f'{path}.ctx.tmp')
    # The original i2d is untouched — the swap never got as far as os.replace.
    assert sorted(os.listdir(tmp_path)) == ['failing_i2d.fits']
    with fits.open(path) as hdul:
        np.testing.assert_array_equal(hdul['CON'].data, ctx)
        np.testing.assert_array_equal(hdul['SCI'].data, _SCI)


def test_compress_context_cleans_up_temp_when_replace_fails(tmp_path,
                                                            monkeypatch):
    """Cleanup also covers a failure after the temp is fully written."""
    path = str(tmp_path / 'replacefail_i2d.fits')
    ctx = _context_array()
    _write_i2d_like(path, ctx)

    def _boom(src, dst):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(drizzle_mod.os, 'replace', _boom)
    with pytest.raises(OSError):
        compress_context_extension(path, ctx=ctx)

    assert not os.path.exists(f'{path}.ctx.tmp')
    assert sorted(os.listdir(tmp_path)) == ['replacefail_i2d.fits']
