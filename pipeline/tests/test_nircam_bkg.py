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


def test_b2d_fit_order_first_starves_amprow_of_halo():
    """The amp-blocky halo-oversubtraction mechanism and its fix.

    A bright galaxy's halo is structurally invisible to the source mask (the
    ring-median pre-filter removes structure broader than its radius before
    tier detection), so unmasked halo flux enters the clipped amp-row medians
    and the GP — smooth structure slower than rho is exactly what it follows —
    and gets broadcast across the host amp's full width: oversubtracted
    amp-blocks with hard edges at the amp boundaries. With the legacy
    fit_order='last', the applied 2-D fit runs on the post-h residual, so it
    can never reclaim that flux. fit_order='first' fits the smooth model with
    the halo intact and conditions the 1/f measurement on it.

    Replicates the step's per-iteration numerics (this suite's convention —
    the I/O wrapper needs a datamodel), one iteration, no detrend (a coarse
    fit-only detrend does not change the mechanism). Three pinned facts:

    1. 'last' leaks the halo's row-collapse into h (artifact reproduced);
    2. 'first' + reject=False cuts that leak by ~half (measured 0.54x; the
       rest is the b2d model deficit inside the extra_dilate holes — with a
       perfect b2d model the leak measures ~0);
    3. 'first' + reject=True does NOT help: the map-outlier reject flags the
       halo bump in the map as leaked source flux and refits it away. This
       interaction is why the step warns on that combination and the config
       says to pair fit_order='first' with reject=false.
    """
    pytest.importorskip('celerite2')
    from scipy.ndimage import distance_transform_edt
    from campfire_pipeline.nircam.gp_striping import gp_amprow_offsets

    rng = np.random.default_rng(12)
    H = 2048
    rows = np.arange(H)
    sci = rng.normal(0.0, 1.0, (H, COLS))
    band = {'A': 1.0, 'B': 0.7, 'C': 1.3, 'D': 0.5}   # amp-dependent banding
    for amp in 'ABCD':
        c0, c1 = _amp_cols(amp)
        sci[:, c0:c1] += band[amp] * np.sin(2 * np.pi * rows[:, None] / 100.0)
    # bright halo hosted by amp C (center 176 px from the B/C boundary), far
    # broader than the masked core the tiers will catch
    halo = _gaussian_blob((H, COLS), 1024, 1200, 20.0, 120.0)
    sci = sci + halo
    err = np.full((H, COLS), 1.0, np.float32)
    dq = np.zeros((H, COLS), np.int32)

    mask_cfg = dict(ring_radius_in=80, ring_width=4, ring_downsample=4,
                    tier_kernel_size=[25, 15, 5, 2],
                    tier_npixels=[15, 10, 3, 1],
                    tier_nsigma=[1.5, 1.5, 1.5, 1.5],
                    tier_dilate_size=[33, 25, 21, 19])

    def chain(order, reject):
        sb = SubtractBackground(bg_box_size=64, bg_filter_size=5,
                                bg_reject=reject, bg_reject_dilate=40.0,
                                **mask_cfg)
        srcmask, srcbits = sb.mask_from_arrays(sci, err, dq)
        src_only = (srcbits >> 1) != 0
        grown = distance_transform_edt(~src_only) <= 20
        b2d_mask = srcmask | grown
        ped, _ = oneoverf.peramp_pedestal(sci, srcmask)
        b2d = 0.0
        if order == 'first':
            b2d = sb.estimate_background(sci - ped, b2d_mask).background
        meas = sci - ped - b2d
        vcol = oneoverf.column_pattern(meas, srcmask, 3)
        base = meas - vcol
        h5, _, _ = gp_amprow_offsets(base, srcmask, rho=5.0, maxiters=3,
                                     zero_dc=True)
        h20, _, _ = gp_amprow_offsets(base - h5, srcmask, rho=20.0,
                                      maxiters=3, zero_dc=True)
        h = h5 + h20
        if order == 'last':
            b2d = sb.estimate_background(sci - ped - vcol - h,
                                         b2d_mask).background
        return h, sci - (ped + vcol + h + b2d), srcmask

    def leak(h, amp='C'):
        # halo flux in the amp-row ledger: mean row-offset difference between
        # halo rows and far rows (both windows span whole banding periods, so
        # the sinusoid cancels)
        c0, c1 = _amp_cols(amp)
        prof = np.median(h[:, c0:c1], axis=1)
        return prof[924:1124].mean() - prof[200:400].mean()

    h_last, out_last, bg_last = chain('last', reject=True)     # shipped legacy
    h_first, out_first, bg_first = chain('first', reject=False)
    h_first_rej, _, _ = chain('first', reject=True)

    leak_last = leak(h_last)
    # (1) the artifact is reproduced by the legacy order (measured ~1.4)
    assert leak_last > 1.0
    # (2) first-order fit starves the amp-row term of halo flux (~0.54x)
    assert leak(h_first) < 0.65 * leak_last
    # (3) reject=True cancels the reorder (measured ~1.05x of legacy) — if a
    #     future scale-aware reject fixes this, loosen/flip this assertion
    #     and drop the warning in bkg_step
    assert leak(h_first_rej) > 0.8 * leak_last
    # the fix must not cost banding removal: far from the halo, the amp-row
    # profile of the corrected frame is flat in both orders
    bg = ~bg_first
    for amp in 'ABCD':
        c0, c1 = _amp_cols(amp)
        strip, m = out_first[:, c0:c1], bg[:, c0:c1]
        rowmed = np.array([np.median(strip[i][m[i]]) if m[i].any() else np.nan
                           for i in range(200, 400)])
        assert np.nanstd(rowmed) < 0.3 * band[amp]


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
    # tier 0 (100 sigma / 30k px / 600 px pre-tier) removed with bg_guard:
    # both stages now run the same 4-tier compact-source cascade
    assert {len(mosaic[k]) for k in keys} == {4}          # consumed in lockstep
    assert mosaic['tier_nsigma'][0] == 1.5
    assert mosaic['bg_guard'] is True                     # negativity guard on

    per_exp = get_nircam_step_config('bkg', cfg, field).get('mask') or {}
    assert {len(per_exp[k]) for k in keys} == {4}
    # the guard is mosaic-scoped: the per-exposure mask section must not
    # carry it (SubtractBackground default is False)
    assert 'bg_guard' not in per_exp


def test_extra_dilate_no_sources_does_not_mask_the_border():
    """`striping.extra_dilate` must be a no-op when nothing is selected to grow.

    `distance_transform_edt` measures the distance to the nearest ZERO, so an
    all-True input (no selected source pixels — either no tiers at all, or
    `extra_dilate_min_area` filtered every component out) has no seed and the
    transform falls back to distances measured from OUTSIDE the array. A naive
    `edt(~src) <= r` then masks a border-and-corner band that no source put
    there, deleting exactly the edge amp-row anchors.

    Asserts the failure mode is real BEFORE asserting the guard removes it, so
    the test cannot pass vacuously if the EDT behaviour ever changes.
    """
    from scipy.ndimage import distance_transform_edt

    src_only_1f = np.zeros((256, 256), dtype=bool)      # nothing selected
    radius = 20.0

    # 1. the failure mode exists: unguarded, this masks a spurious band
    unguarded = distance_transform_edt(~src_only_1f) <= radius
    assert unguarded.any(), (
        'EDT no longer produces a spurious band on an all-True input; '
        'the guard under test may be obsolete — re-check bkg.py')
    assert not unguarded[128, 128], 'spurious mask should hug the border'

    # 2. the guard removes it, and leaves the fit mask untouched
    fitmask = np.zeros((256, 256), dtype=bool)
    if src_only_1f.any():
        fitmask_1f = fitmask | (distance_transform_edt(~src_only_1f) <= radius)
    else:
        fitmask_1f = fitmask
    assert not fitmask_1f.any()

    # 3. and it still grows normally when something IS selected
    src_only_1f[128, 128] = True
    grown = distance_transform_edt(~src_only_1f) <= radius
    assert grown[128, 128] and grown[128, 128 + int(radius)]
    assert not grown[0, 0]
