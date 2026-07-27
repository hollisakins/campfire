"""Tests for the unified ``bkg`` numerics (oneoverf + bkgsub mask-only path).

The step's I/O wrapper (``steps.bkg.bkg_step``) needs a JWST datamodel and is
exercised end-to-end in the pipeline; here we test the pure numerics it calls
and the skymatch invariant of its chain.
"""
import os
import tempfile

import numpy as np
import pytest

from astropy.io import fits

from campfire_pipeline.nircam import oneoverf
from campfire_pipeline.nircam.bkgsub import SubtractBackground
from campfire_pipeline.nircam.constants import NIR_AMPS

# amp geometry is 2048-column-specific; rows are free
COLS = 2048


def _amp_cols(amp):
    _, _, c0, c1 = NIR_AMPS[amp]['data']
    return c0, c1


def test_peramp_pedestal_recovers_dc():
    rng = np.random.default_rng(0)
    data = rng.normal(0.0, 1.0, (256, COLS))
    dcs = {'A': 5.0, 'B': -3.0, 'C': 2.0, 'D': -1.0}
    for amp, dc in dcs.items():
        c0, c1 = _amp_cols(amp)
        data[:, c0:c1] += dc
    mask = np.zeros_like(data, dtype=bool)
    ped, per_amp = oneoverf.peramp_pedestal(data, mask)
    for amp, dc in dcs.items():
        assert per_amp[amp] == pytest.approx(dc, abs=0.05)
    # residual per-amp median ~ 0 after subtracting the pedestal
    resid = data - ped
    for amp in 'ABCD':
        c0, c1 = _amp_cols(amp)
        assert abs(np.median(resid[:, c0:c1])) < 0.05


def test_peramp_pedestal_ignores_masked_sources():
    rng = np.random.default_rng(1)
    data = rng.normal(0.0, 1.0, (256, COLS))
    # a bright source in amp B that would bias an unmasked median
    c0, c1 = _amp_cols('B')
    data[100:150, c0 + 10:c0 + 60] += 500.0
    mask = np.zeros_like(data, dtype=bool)
    mask[100:150, c0 + 10:c0 + 60] = True
    _, per_amp = oneoverf.peramp_pedestal(data, mask)
    assert abs(per_amp['B']) < 0.1  # source masked -> DC ~ 0


def test_gp_zero_dc_returns_zero_median_offsets():
    """With zero_dc the GP horizontal term carries NO per-amp DC — the
    pedestal is the chain's only per-amp DC estimator (amp-seam fix)."""
    pytest.importorskip('celerite2')
    from campfire_pipeline.nircam.gp_striping import gp_amprow_offsets

    rng = np.random.default_rng(11)
    H = 512
    data = rng.normal(0.0, 1.0, (H, COLS))
    for amp, dc in zip('ABCD', (4.0, -2.0, 1.5, -3.0)):   # amp DC steps
        c0, c1 = _amp_cols(amp)
        data[:, c0:c1] += dc
    mask = np.zeros_like(data, dtype=bool)

    h_dc, _, _ = gp_amprow_offsets(data, mask, rho=5.0, maxiters=3)
    h_z, _, _ = gp_amprow_offsets(data, mask, rho=5.0, maxiters=3,
                                  zero_dc=True)
    for amp in 'ABCD':
        c0, c1 = _amp_cols(amp)
        assert abs(np.median(h_z[4:-4, c0:c1])) < 0.05      # no DC carried
        assert abs(np.median(h_dc[4:-4, c0:c1])) > 1.0      # legacy carries it
    # zero_dc removes ONLY the DC: the row-varying parts are identical
    assert np.allclose(h_dc - np.median(h_dc[4:-4], axis=0, keepdims=True),
                       h_z - np.median(h_z[4:-4], axis=0, keepdims=True),
                       atol=1e-9)


def test_frame_pedestal_single_dc_across_amps():
    """frame_pedestal (the subtract_2d scope) recovers one global DC and is
    constant across amp boundaries — it must NOT stairstep a gradient."""
    rng = np.random.default_rng(9)
    data = 5.0 + rng.normal(0.0, 1.0, (256, COLS))
    data += 0.5 * np.linspace(-1, 1, COLS)[None, :]     # smooth gradient
    mask = np.zeros_like(data, dtype=bool)
    ped, per_amp = oneoverf.frame_pedestal(data, mask)
    assert len(set(per_amp.values())) == 1              # one DC, all amps
    assert per_amp['A'] == pytest.approx(5.0, abs=0.05)
    sci = ped[:, 4:2044]                                # science columns
    assert np.all(sci == sci[0, 0])                     # no amp steps


def test_column_pattern_shape_and_finite():
    rng = np.random.default_rng(2)
    data = rng.normal(0.0, 1.0, (128, COLS))
    mask = np.zeros_like(data, dtype=bool)
    v = oneoverf.column_pattern(data, mask, maxiters=3)
    assert v.shape == data.shape
    assert np.isfinite(v).all()
    # constant along rows (it is a per-column pattern broadcast over rows)
    assert np.allclose(v[0], v[-1])


def test_variance_rescale_returns_positive_factor():
    rng = np.random.default_rng(3)
    sci = rng.normal(0.0, 2.0, (256, COLS))          # sky variance ~ 4
    var_rnoise = np.full_like(sci, 1.0)              # under-estimated
    mask = np.zeros_like(sci, dtype=bool)
    factor = oneoverf.variance_rescale(sci, var_rnoise, mask, block_size=7)
    assert factor > 0
    assert factor == pytest.approx(4.0, rel=0.3)     # recovers ~sky/rnoise


def test_mask_from_arrays_matches_compute():
    rng = np.random.default_rng(4)
    sci = (1.0 + rng.normal(0, 0.05, (256, 256))).astype(np.float32)
    sci[120:130, 120:140] += 2.0
    err = np.full((256, 256), 0.05, np.float32)
    dq = np.zeros((256, 256), np.int32)
    dq[0, 0] = 1
    cfg = dict(ring_radius_in=40, ring_width=3, ring_downsample=1,
               tier_kernel_size=[15, 5, 2], tier_npixels=[10, 5, 3],
               tier_nsigma=[3, 3, 3], tier_dilate_size=[0, 0, 2])
    tmp = tempfile.NamedTemporaryFile(suffix='.fits', delete=False).name
    try:
        fits.HDUList([fits.PrimaryHDU(),
                      fits.ImageHDU(sci, name='SCI'),
                      fits.ImageHDU(err, name='ERR'),
                      fits.ImageHDU(dq, name='DQ')]).writeto(tmp, overwrite=True)
        _, mask_compute, bit_compute = SubtractBackground(**cfg).compute(tmp)
        mask_direct, bit_direct = SubtractBackground(**cfg).mask_from_arrays(
            sci, err, dq)
    finally:
        os.unlink(tmp)
    assert np.array_equal(mask_compute, mask_direct)
    assert np.array_equal(bit_compute, bit_direct)


def test_wht_aware_mask_variable_depth_recovers_shallow_sky():
    """Depth-blind (flux-space) masking pins the global RMS to the deep
    coverage and mass-flags shallow-region noise as sources; the 2-D fit then
    has no data there and extrapolates the deep sky, leaving the shallow
    zone's own sky level in place. The WHT-aware (noise-equalized) detection
    keeps the masking depth-fair so the fit measures — and removes — it."""
    rng = np.random.default_rng(9)
    ny, nx = 600, 600
    split = int(nx * 0.85)              # deep-dominated: 15% shallow strip
    s_deep, s_shal, offset = 1.0, 5.0, 3.0
    err = np.full((ny, nx), s_deep, np.float32)
    err[:, split:] = s_shal
    sci = rng.normal(0.0, err).astype(np.float32)
    sci[:, split:] += offset            # the shallow visit's own sky level
    wht = (1.0 / err ** 2).astype(np.float32)

    cfg = dict(ring_radius_in=80, ring_width=4, ring_downsample=4,
               tier_kernel_size=[25, 15, 5, 2], tier_npixels=[15, 10, 3, 1],
               tier_nsigma=[1.5, 1.5, 1.5, 1.5],
               tier_dilate_size=[33, 25, 21, 19],
               bg_box_size=10, bg_filter_size=5)

    shal = np.s_[:, split + 40:]        # interiors, away from the depth edge
    deep = np.s_[:, :split - 40]

    legacy_mask, _ = SubtractBackground(**cfg).mask_from_arrays(sci, err)
    aware = SubtractBackground(**cfg)
    aware_mask, _ = aware.mask_from_arrays(sci, err, wht=wht)

    # flux-space thresholds blanket the shallow strip; noise-equalized don't
    assert legacy_mask[shal].mean() > 0.9
    assert aware_mask[shal].mean() < 0.3
    assert aware_mask[deep].mean() < 0.3

    # with data surviving in the shallow zone, the fit recovers its sky
    bmap = aware.estimate_background(sci, aware_mask).background
    assert bmap[shal].mean() == pytest.approx(offset, abs=0.3)
    assert abs(bmap[deep].mean()) < 0.1


def test_wht_aware_mask_uniform_depth_matches_legacy():
    """With a uniform weight map the noise-equalized detection reduces to the
    historical flux-space detection (stats and thresholds scale together), so
    uniform-depth tiles are unaffected by the wht_aware default."""
    rng = np.random.default_rng(10)
    sci = (1.0 + rng.normal(0, 0.05, (256, 256))).astype(np.float32)
    sci[120:130, 120:140] += 2.0
    err = np.full((256, 256), 0.05, np.float32)
    wht = np.full((256, 256), 400.0, np.float32)   # 1 / err**2
    cfg = dict(ring_radius_in=40, ring_width=3, ring_downsample=1,
               tier_kernel_size=[15, 5, 2], tier_npixels=[10, 5, 3],
               tier_nsigma=[3, 3, 3], tier_dilate_size=[0, 0, 2])
    m_legacy, _ = SubtractBackground(**cfg).mask_from_arrays(sci, err)
    m_aware, _ = SubtractBackground(**cfg).mask_from_arrays(sci, err, wht=wht)
    m_off, _ = SubtractBackground(wht_aware=False, **cfg).mask_from_arrays(
        sci, err, wht=wht)
    # not bit-guaranteed (the ring fill differs at float margins), but any
    # divergence beyond a stray boundary pixel is a regression
    assert (m_aware != m_legacy).mean() < 1e-3
    assert np.array_equal(m_off, m_legacy)         # escape hatch is exact


def _gradient_sky(shape, amplitude):
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    return amplitude * (xx + yy) / (shape[0] + shape[1])


def _gaussian_blob(shape, y0, x0, amp, sigma):
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    return amp * np.exp(-((yy - y0) ** 2 + (xx - x0) ** 2) / (2 * sigma ** 2))


def test_bg_reject_refits_leaked_source():
    """Extended flux NOT in the source mask imprints on the background map
    (broad enough to survive the mesh median filter); bg_reject flags the map
    outlier region and the refit flattens it."""
    rng = np.random.default_rng(6)
    shape = (256, 256)
    truth = 1.0 + _gradient_sky(shape, 0.1)
    sci = truth + rng.normal(0, 0.05, shape)
    sci += _gaussian_blob(shape, 128, 128, 1.0, 20.0)   # leaked diffuse source
    mask = np.zeros(shape, dtype=bool)

    kw = dict(bg_box_size=16, bg_filter_size=3,
              bg_reject_sigma_hi=4.0, bg_reject_sigma_lo=3.0,
              bg_reject_percentile=60.0, bg_reject_dilate=10.0)
    plain = SubtractBackground(bg_reject=False, **kw)
    guard = SubtractBackground(bg_reject=True, **kw)

    bmap_plain = plain.estimate_background(sci, mask).background
    bmap_guard = guard.estimate_background(sci, mask).background

    near = _gaussian_blob(shape, 128, 128, 1.0, 20.0) > 0.05
    err_plain = np.abs(bmap_plain - truth)[near].max()
    err_guard = np.abs(bmap_guard - truth)[near].max()
    assert err_plain > 0.3          # unguarded fit absorbs the diffuse flux
    assert err_guard < 0.5 * err_plain
    assert err_guard < 0.15         # guarded map ~ true sky under the source


def test_bg_reject_harmless_on_clean_sky():
    """With nothing leaking through the mask the reject pass is (near-)inert:
    the trimmed-percentile sigma may flag a few noise boxes, but the refit
    must not move the map. Degenerate (zero-variance) maps skip the refit."""
    rng = np.random.default_rng(7)
    shape = (256, 256)
    sci = 1.0 + _gradient_sky(shape, 0.1) + rng.normal(0, 0.05, shape)
    mask = np.zeros(shape, dtype=bool)
    kw = dict(bg_box_size=16, bg_filter_size=3)
    bmap_plain = SubtractBackground(bg_reject=False, **kw).estimate_background(
        sci, mask).background
    guard = SubtractBackground(bg_reject=True, **kw)
    bmap_guard = guard.estimate_background(sci, mask).background
    assert np.abs(bmap_guard - bmap_plain).max() < 0.02
    # degenerate: constant sky -> zero-variance map -> no refit
    const = np.full(shape, 1.0)
    assert guard.reject_background_outliers(
        const, mask, guard._fit_background2d(const, mask)) is None


def test_bkg2d_grown_mask_recovers_gradient_without_bowl():
    """The subtract_2d numerics: mask a bright source, grow the source tiers
    (not bit 0) as the step does, fit the 2-D background — the gradient is
    recovered and no negative bowl is carved around the source."""
    from scipy.ndimage import distance_transform_edt

    rng = np.random.default_rng(8)
    shape = (256, 256)
    truth = 1.0 + _gradient_sky(shape, 0.5)
    sci = (truth + rng.normal(0, 0.05, shape)).astype(np.float32)
    sci += _gaussian_blob(shape, 128, 128, 50.0, 4.0).astype(np.float32)
    err = np.full(shape, 0.05, np.float32)
    dq = np.zeros(shape, np.int32)

    sb = SubtractBackground(
        ring_radius_in=40, ring_width=3,
        tier_kernel_size=[15, 5, 2], tier_npixels=[10, 5, 3],
        tier_nsigma=[1.5, 1.5, 1.5], tier_dilate_size=[10, 5, 2],
        bg_box_size=16, bg_filter_size=3, bg_reject=True,
        bg_reject_dilate=10.0)
    srcmask, srcbits = sb.mask_from_arrays(sci, err, dq)

    # step logic: grow source tiers only (bit 0 untouched)
    src_only = (srcbits >> 1) != 0
    grown = distance_transform_edt(~src_only) <= 20
    bmap = sb.estimate_background(sci, srcmask | grown).background

    bg = ~(grown | srcmask)
    assert np.abs(bmap - truth)[bg].mean() < 0.03   # gradient recovered
    # no bowl: annulus around the source stays at the true sky level
    rr = np.hypot(*(np.mgrid[0:256, 0:256] - 128))
    annulus = (rr > 30) & (rr < 45)
    assert np.mean((sci - bmap)[annulus]) == pytest.approx(0.0, abs=0.05)


def test_skymatch_invariant_and_banding_removal():
    """The full chain zeroes the masked background (skymatch) and removes the
    per-amp DC + amp-dependent banding, using the unchanged two-scale GP."""
    pytest.importorskip('celerite2')
    from campfire_pipeline.nircam.gp_striping import gp_amprow_offsets

    rng = np.random.default_rng(5)
    H = 512
    rows = np.arange(H)
    sci = rng.normal(0.0, 1.0, (H, COLS))
    dcs = {'A': 6.0, 'B': -4.0, 'C': 3.0, 'D': -2.0}
    band = {'A': 1.5, 'B': 1.0, 'C': 2.0, 'D': 0.5}   # amp-DEPENDENT banding
    for amp in 'ABCD':
        c0, c1 = _amp_cols(amp)
        b = band[amp] * np.sin(2 * np.pi * rows / 100.0) + 0.3 * rng.normal(0, 1, H)
        sci[:, c0:c1] += dcs[amp] + b[:, None]
    sci = sci.astype(np.float32)
    err = np.full((H, COLS), 1.0, np.float32)
    dq = np.zeros((H, COLS), np.int32)

    sb = SubtractBackground(ring_radius_in=80, ring_width=4, ring_downsample=4,
                            tier_kernel_size=[25, 15, 5, 2],
                            tier_npixels=[15, 10, 3, 1],
                            tier_nsigma=[1.5, 1.5, 1.5, 1.5],
                            tier_dilate_size=[33, 25, 21, 19])

    resid = sci.astype(np.float64).copy()
    correction = np.zeros_like(resid)
    srcmask, _ = sb.mask_from_arrays(resid, err, dq)
    ped, _ = oneoverf.peramp_pedestal(resid, srcmask)
    vcol = oneoverf.column_pattern(resid - ped, srcmask, 3)
    base = resid - ped - vcol
    h5, _, _ = gp_amprow_offsets(base, srcmask, rho=5.0, maxiters=3)
    h20, _, _ = gp_amprow_offsets(base - h5, srcmask, rho=20.0, maxiters=3)
    correction = ped + vcol + h5 + h20
    out = sci - correction
    bg = ~srcmask

    # skymatch invariant: masked-background median ~ 0
    assert abs(np.median(out[bg])) < 0.05
    # per-amp DC removed
    for amp in 'ABCD':
        c0, c1 = _amp_cols(amp)
        m = bg[:, c0:c1]
        assert abs(np.median(out[:, c0:c1][m])) < 0.1
    # amp-dependent banding knocked down (row-median std drops sharply)
    def band_std(img):
        s = {}
        for amp in 'ABCD':
            c0, c1 = _amp_cols(amp)
            strip, m = img[:, c0:c1], bg[:, c0:c1]
            rowmed = np.array([np.median(strip[i][m[i]]) if m[i].any() else np.nan
                               for i in range(H)])
            s[amp] = np.nanstd(rowmed)
        return s
    before, after = band_std(sci), band_std(out)
    for amp in 'ABCD':
        assert after[amp] < 0.3 * before[amp]


def _extended_galaxy_scene(rng, n=1600, re=150.0, amp=200.0):
    """Mostly-sky frame with one very bright, very extended galaxy at centre.

    Three properties are load-bearing and each was found the hard way:
    a de Vaucouleurs (r^1/4) profile, because a Gaussian's wing is too steep to
    bias the mesh at all; NO truncation, because a hard cut puts clean sky just
    outside the mask and removes the bias entirely; and a frame the galaxy
    occupies only a small part of, because otherwise the galaxy inflates the RMS
    the 100-sigma tier-0 threshold is measured against and tier 0 never fires.
    """
    y, x = np.mgrid[:n, :n]
    r = np.hypot(x - n / 2, y - n / 2)
    prof = amp * np.exp(-7.669 * ((np.maximum(r, 1e-3) / re) ** 0.25 - 1.0))
    sci = (rng.normal(0.0, 1.0, (n, n)) + prof).astype(np.float32)
    return sci, np.ones((n, n), np.float32), r


def _tier_cfg(tier0=False, nsigma0=100.0, npixels0=30000):
    """Mosaic tiers, using the SHIPPED tier-0 values. *nsigma0* / *npixels0* are
    exposed only so a test can toggle ONE gate and show it is load-bearing."""
    base = dict(ring_radius_in=80, ring_width=4, ring_downsample=4,
                bg_box_size=10, bg_filter_size=5)
    if tier0:
        return dict(base, tier_kernel_size=[25, 25, 15, 5, 2],
                    tier_npixels=[npixels0, 15, 10, 3, 1],
                    tier_nsigma=[nsigma0, 1.5, 1.5, 1.5, 1.5],
                    tier_dilate_size=[600, 33, 25, 21, 19])
    return dict(base, tier_kernel_size=[25, 15, 5, 2],
                tier_npixels=[15, 10, 3, 1], tier_nsigma=[1.5, 1.5, 1.5, 1.5],
                tier_dilate_size=[33, 25, 21, 19])


def test_tier0_removes_extended_source_oversubtraction_bowl():
    """A galaxy far larger than bg_box_size is over-subtracted even when fully
    masked at pixel level: Background2D keeps mesh cells up to
    bg_exclude_percentile masked at the galaxy edge, samples its outskirts
    there, and the zoom interpolator extrapolates that gradient inward. Tier 0
    pushes the mesh boundary out to true sky."""
    rng = np.random.default_rng(4)
    sci, err, radius = _extended_galaxy_scene(rng)
    assert int((sci > 100.0).sum()) > 30000, 'scene must trip the tier-0 floor'

    without = SubtractBackground(**_tier_cfg(tier0=False))
    m0, _ = without.mask_from_arrays(sci, err)
    bowl = float(without.estimate_background(sci, m0).background[radius < 50].mean())

    with_t0 = SubtractBackground(**_tier_cfg(tier0=True))
    m1, _ = with_t0.mask_from_arrays(sci, err)
    fixed = float(with_t0.estimate_background(sci, m1).background[radius < 50].mean())

    # Guard: without a real bowl the comparison below passes vacuously.
    assert bowl > 3.0, f'scene did not reproduce the bowl (got {bowl:.2f} sigma)'
    assert m1.mean() > m0.mean()             # tier 0 actually fired
    assert abs(fixed) < 0.35 * bowl          # observed ~0.18x


def test_tier0_is_a_noop_on_an_ordinary_field():
    """End-to-end safety property: on a field of ordinary compact sources tier 0
    must not fire at all, so their wings keep the normal aggressive flattening.

    A realistic-scene check, NOT a test of either gate individually. `nsigma` is
    what rejects here — after the tier-0 kernel (gaussian_filter, sigma=25) these
    blobs peak at a*s^2/(s^2+25^2) = 2.18 and 2.87 against a 100-sigma threshold,
    so detect_sources returns None before npixels is ever consulted (setting
    npixels to 1 gives a byte-identical mask). The two gates are covered
    individually by the two tests below.
    """
    rng = np.random.default_rng(5)
    n = 400
    y, x = np.mgrid[:n, :n]
    sci = rng.normal(0.0, 1.0, (n, n)).astype(np.float32)
    for cx, cy, a, s in ((100, 120, 40.0, 6.0), (280, 300, 25.0, 9.0)):
        sci += a * np.exp(-(((x - cx) ** 2 + (y - cy) ** 2) / (2 * s ** 2)))
    err = np.ones((n, n), np.float32)

    m0, _ = SubtractBackground(**_tier_cfg(tier0=False)).mask_from_arrays(sci, err)
    m1, _ = SubtractBackground(**_tier_cfg(tier0=True)).mask_from_arrays(sci, err)
    assert np.array_equal(m0, m1)


def test_tier0_npixels_is_load_bearing():
    """`npixels = 30000` must do real selectivity work. This source is BRIGHT
    enough to clear the 100-sigma threshold after smoothing (peak
    a*s^2/(s^2+25^2) = 400*900/1525 = 236) but COMPACT, so its footprint stays
    under the 30k floor and only npixels can reject it."""
    rng = np.random.default_rng(11)
    n, amp, sb = 600, 400.0, 30.0
    y, x = np.mgrid[:n, :n]
    sci = (rng.normal(0.0, 1.0, (n, n))
           + amp * np.exp(-(((x - n / 2) ** 2 + (y - n / 2) ** 2)
                            / (2 * sb ** 2)))).astype(np.float32)
    err = np.ones((n, n), np.float32)

    def mask(cfg):
        return SubtractBackground(**cfg).mask_from_arrays(sci, err)[0]

    baseline = mask(_tier_cfg(tier0=False))
    shipped = mask(_tier_cfg(tier0=True))                    # npixels = 30000
    lowered = mask(_tier_cfg(tier0=True, npixels0=500))      # only npixels changed

    assert np.array_equal(shipped, baseline)   # the 30k floor rejects it: no-op
    assert not np.array_equal(lowered, baseline)
    assert lowered.mean() > 0.9


def test_tier0_nsigma_is_load_bearing():
    """`nsigma = 100` must do real selectivity work, not ride along behind
    `npixels`. This scene is deliberately BROAD but FAINT — its smoothed
    footprint is far past the 30k-pixel floor, so `npixels` cannot reject it and
    only the 100-sigma threshold can. Dropping nsigma to 1.5 fires tier 0 and
    swallows the frame; at the shipped 100 the mask is untouched."""
    rng = np.random.default_rng(7)
    n, amp, s = 1000, 25.0, 150.0
    y, x = np.mgrid[:n, :n]
    sci = (rng.normal(0.0, 1.0, (n, n))
           + amp * np.exp(-(((x - n / 2) ** 2 + (y - n / 2) ** 2)
                            / (2 * s ** 2)))).astype(np.float32)
    err = np.ones((n, n), np.float32)

    def mask(cfg):
        return SubtractBackground(**cfg).mask_from_arrays(sci, err)[0]

    baseline = mask(_tier_cfg(tier0=False))
    shipped = mask(_tier_cfg(tier0=True))                 # nsigma = 100
    lowered = mask(_tier_cfg(tier0=True, nsigma0=1.5))    # only nsigma changed

    assert np.array_equal(shipped, baseline)   # 100 sigma rejects it: no-op
    assert not np.array_equal(lowered, baseline)
    assert lowered.mean() > 0.9                # 1.5 sigma would swallow the frame


def test_shipped_mosaic_tier_config_is_coherent(tmp_path):
    """Pin the tiers as SHIPPED. Both the config and the field file are given
    explicitly: `load_config()` would deep-merge $CAMPFIRE_ROOT/config/config.toml
    and `Field.load()` without `fields_file=` raises on a clean checkout, so the
    unqualified calls would test the developer's machine rather than the package
    (and would not run in CI at all)."""
    from pathlib import Path

    import campfire_pipeline
    from campfire_pipeline.config import get_nircam_step_config, load_config
    from campfire_pipeline.nircam.field import Field

    packaged = Path(campfire_pipeline.__file__).parent / 'data' / 'config_default.toml'
    cfg = load_config(config_path=str(packaged))
    ff = tmp_path / 'fields.toml'
    ff.write_text('[cosmos]\n'
                  'filters = ["f444w"]\n'
                  'files = ["jw01727*"]\n'
                  'tangent_point = [150.1, 2.1]\n')
    field = Field.load('cosmos', fields_file=str(ff))     # no step overrides
    assert not field.step_overrides

    keys = ('tier_kernel_size', 'tier_npixels', 'tier_nsigma',
            'tier_dilate_size')
    mosaic = get_nircam_step_config('resample', cfg, field)
    assert {len(mosaic[k]) for k in keys} == {5}          # consumed in lockstep
    assert mosaic['tier_nsigma'][0] == 100.0
    assert mosaic['tier_npixels'][0] == 30000
    assert mosaic['tier_dilate_size'][0] == 600

    # per-exposure deliberately keeps 4 tiers: 30k px / 600 px are sized for a
    # 30mas mosaic tile, not a 2048^2 frame
    per_exp = get_nircam_step_config('bkg', cfg, field).get('mask') or {}
    assert {len(per_exp[k]) for k in keys} == {4}


def test_shipped_aggressive_dq_guard_catches_a_jump_det_runaway(tmp_path):
    """Pin the SHIPPED guard threshold against the pathology it exists for.

    A2744 f444w jw03073008001_03201 carries JUMP_DET on 79-81% of every LW
    detector. At the old 0.85 the guard missed it, the bit was folded into the
    background FIT mask, and the starved amp-row GP injected structure. The
    shipped value must therefore sit below the observed runaway fraction.
    """
    from pathlib import Path

    import campfire_pipeline
    from campfire_pipeline.config import get_nircam_step_config, load_config
    from campfire_pipeline.nircam.field import Field

    packaged = Path(campfire_pipeline.__file__).parent / 'data' / 'config_default.toml'
    cfg = load_config(config_path=str(packaged))
    ff = tmp_path / 'fields.toml'
    ff.write_text('[a2744]\n'
                  'filters = ["f444w"]\n'
                  'files = ["jw03073*"]\n'
                  'tangent_point = [3.5, -30.4]\n')
    field = Field.load('a2744', fields_file=str(ff))
    mask_cfg = get_nircam_step_config('bkg', cfg, field)['mask']

    assert mask_cfg['mask_aggressive_dq'] is True
    guard = mask_cfg['mask_aggressive_dq_max_frac']
    # 0.789 is the LOWEST JUMP_DET fraction measured across the six affected
    # detectors; the guard must fire below it, or the runaway slips through.
    assert guard < 0.789, (
        f'mask_aggressive_dq_max_frac={guard} does not catch a 78.9% JUMP_DET '
        f'blanket — the A2744 f444w runaway would starve the 1/f fit again')
    # ...but not so low that ordinary frames lose their DQ masking: the a2744
    # f444w population median JUMP_DET is 8.7%, p90 22.1%.
    assert guard > 0.25


def test_starved_amprow_mask_makes_the_gp_inject_structure():
    """The mechanism, in miniature: when a DQ blanket leaves only a handful of
    pixels per amp-row, the GP's row offsets are noise and subtracting them
    ADDS row structure. This is why the guard has to fire.

    Non-vacuous by construction: the same frame with a healthy mask must come
    out BETTER, so the assertion cannot pass by the GP simply doing nothing.
    """
    from campfire_pipeline.nircam.gp_striping import gp_amprow_offsets

    rng = np.random.default_rng(20260727)
    rows = 512
    # faint true 1/f (constant along each amp row) + white noise
    true_1f = rng.normal(0.0, 2.0e-4, size=(rows, 1))
    frame = true_1f + rng.normal(0.0, 8.0e-3, size=(rows, COLS))

    def row_sigma(a, m):
        v = np.where(m, np.nan, a)
        r = np.nanmedian(v, axis=1)
        return float(np.nanstd(r[np.isfinite(r)]))

    def run(mask):
        h, _, ks = gp_amprow_offsets(frame, mask, rho=5.0, zero_dc=True)
        return row_sigma(frame - h, mask), ks

    healthy = rng.random((rows, COLS)) < 0.35        # ~35% masked, like a real frame
    starved = rng.random((rows, COLS)) < 0.886       # ~11% usable, the pathology

    before_h = row_sigma(frame, healthy)
    after_h, ks_h = run(healthy)
    before_s = row_sigma(frame, starved)
    after_s, ks_s = run(starved)

    # healthy: the GP removes real 1/f
    assert after_h < before_h, (before_h, after_h)
    # starved: it cannot, and the fitted amplitude is inflated by the small n
    assert ks_s > ks_h, (ks_s, ks_h)
    assert after_s > after_h, (after_s, after_h)
