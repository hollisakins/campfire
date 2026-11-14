"""Core arithmetic of the prototype diffraction-spike mask (no data, no network)."""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nircam import spike_mask as sm
from campfire_pipeline.nircam.field import _prime_work_copy


def _toy_model(n=401, pixscl=0.1):
    """Two crossed arms (along x and y) falling as r^ARM_SLOPE."""
    ctr = (n - 1) / 2.0
    yy, xx = np.indices((n, n))
    dx, dy = (xx - ctr) * pixscl, (yy - ctr) * pixscl
    r = np.maximum(np.hypot(dx, dy), pixscl)
    arm = (np.abs(dx) < 0.15) | (np.abs(dy) < 0.15)
    img = np.where(arm, 1e-3 * r ** sm.ARM_SLOPE, 0.0)
    return sm.SpikeModel(img, pixscl, ctr, 0.031 ** 2, 2.0)


def test_position_angles_cluster_modulo_180():
    labels = sm.cluster_position_angles([10.0, 11.0, 191.0, 120.0, 359.5, 179.0], 3.0)
    assert labels[0] == labels[1] == labels[2]        # 191 == 11 mod 180
    assert labels[3] != labels[0]
    assert labels[4] == labels[5]                     # 179.5 and 179
    # 179 and 10 are 11 deg apart mod 180: different groups
    assert labels[5] != labels[0]
    # wrap-around: 179.5 and 0.5 are one angle
    lab = sm.cluster_position_angles([0.5, 179.5, 90.0], 3.0)
    assert lab[0] == lab[1] != lab[2]


def test_band_mag_redder_star_is_brighter_in_the_near_ir():
    blue = sm.band_ab_mag(16.0, 0.6, 1.99)
    red = sm.band_ab_mag(16.0, 2.0, 1.99)
    assert red < blue - 1.0
    # no color: treated as red rather than dropped
    assert np.isfinite(sm.band_ab_mag(16.0, np.nan, 1.99))
    # Rayleigh-Jeans beyond Ks: fainter in AB toward the red
    assert sm.band_ab_mag(16.0, 1.0, 4.4) > sm.band_ab_mag(16.0, 1.0, 2.8)


def test_model_sample_and_reach_continue_past_the_raster():
    m = _toy_model()
    on_arm = m.sample(np.array([0.0, 0.0]), np.array([5.0, 60.0]))
    # the table is an envelope (maximum over neighbouring samples): a little high
    assert on_arm[0] == pytest.approx(1e-3 * 5.0 ** sm.ARM_SLOPE, rel=0.1)
    # 60" is beyond the 20" raster: power-law continuation of the arm
    assert on_arm[1] == pytest.approx(1e-3 * 60.0 ** sm.ARM_SLOPE, rel=0.15)
    off_arm = m.sample(np.array([30.0, 3.0]), np.array([30.0, 3.0]))
    assert (off_arm == 0).all()
    level = 1e-3 * 50.0 ** sm.ARM_SLOPE
    assert m.reach(level) == pytest.approx(50.0, rel=0.1)
    assert m.reach(1e-3 * 5.0 ** sm.ARM_SLOPE) == pytest.approx(5.0, abs=0.3)


def test_predicted_spike_follows_the_roll_angle():
    m = _toy_model()
    # a ring of sky positions 10" from a star at the origin of a small patch
    ra0, dec0 = 150.0, 2.0
    pa = np.radians(np.arange(0, 360, 1.0))
    ra = ra0 + 10.0 / 3600.0 * np.sin(pa) / np.cos(np.radians(dec0))
    dec = dec0 + 10.0 / 3600.0 * np.cos(pa)
    for roll in (0.0, 30.0):
        sb, used = sm.predict_spike_sb(
            ra, dec, np.array([ra0]), np.array([dec0]), np.array([1.0]), m, 1.0,
            roll, ra0, dec0, level=1e-9, max_radius=100.0)
        assert used == [0]
        hit = np.degrees(pa[sb > 0])
        expect = roll % 90.0
        assert np.all(np.minimum((hit - expect) % 90.0, (expect - hit) % 90.0) < 2.0)
        assert 4 <= len(hit) <= 16


def test_work_copy_fuses_spike_mask_only_when_asked(tmp_path):
    def write(path, spkfuse):
        spike = np.zeros((4, 4), np.uint8)
        spike[1, 2] = 1
        ext = fits.ImageHDU(spike, name='CFSPIKE')
        ext.header['SPKFUSE'] = spkfuse
        fits.HDUList([fits.PrimaryHDU(),
                      fits.ImageHDU(np.zeros((4, 4), np.float32), name='SCI'),
                      fits.ImageHDU(np.zeros((4, 4), np.uint32), name='DQ'),
                      ext]).writeto(path, overwrite=True)

    p = str(tmp_path / 'e.fits')
    write(p, True)
    _prime_work_copy(p)                        # step not enabled this run
    assert fits.getdata(p, 'DQ').sum() == 0
    _prime_work_copy(p, fuse_spike=True)
    assert fits.getdata(p, 'DQ')[1, 2] == 1 and fits.getdata(p, 'DQ').sum() == 1
    assert fits.getheader(p, 0)['SPKFUSED'] is True     # recorded for freshness
    write(p, False)                            # report-only extension
    _prime_work_copy(p, fuse_spike=True)
    assert fits.getdata(p, 'DQ').sum() == 0
