"""Tests for the refcat footprint clip (nircam/align/footprint.py).

The tangent-plane helpers are pure numpy; the clip itself uses shapely + a
CRDS-free mock gwcs (``_align_gwcs``) to build a detector footprint and check
that in-frame reference sources are kept and far decoys dropped.
"""

import numpy as np
import pytest
from astropy.table import Table, vstack

from _align_gwcs import HAVE_MOCK_WCS, make_mock_wcs
from campfire_pipeline.nircam.align import footprint as fp
from campfire_pipeline.nircam.align.footprint import clip_refcat_to_exposure


# --- tangent-plane helpers (numpy only) -------------------------------------

def test_gnomonic_center_and_axes():
    ra0, dec0 = 150.0, 2.3
    xi, eta = fp._gnomonic(np.array([ra0]), np.array([dec0]), ra0, dec0)
    assert abs(xi[0]) < 1e-6 and abs(eta[0]) < 1e-6
    # xi is +east (arcsec), eta is +north (arcsec)
    dra = 10.0 / 3600.0 / np.cos(np.radians(dec0))
    xi, eta = fp._gnomonic(np.array([ra0 + dra, ra0]),
                           np.array([dec0, dec0 + 30.0 / 3600.0]), ra0, dec0)
    assert abs(xi[0] - 10.0) < 0.01 and abs(eta[0]) < 0.01
    assert abs(xi[1]) < 0.01 and abs(eta[1] - 30.0) < 0.01


def test_gnomonic_far_hemisphere_is_nan():
    xi, eta = fp._gnomonic(np.array([270.0]), np.array([0.0]), 90.0, 0.0)
    assert not np.isfinite(xi[0]) and not np.isfinite(eta[0])


def test_tangent_center_is_wrap_safe():
    # points straddling the RA=0/360 seam -> center near 0/360, not ~180
    ra0, dec0 = fp._tangent_center(np.array([359.9, 0.1, 0.0]),
                                   np.array([1.0, 1.0, 1.1]))
    assert min(ra0, 360.0 - ra0) < 0.2
    assert abs(dec0 - 1.03) < 0.1


# --- fail-open --------------------------------------------------------------

def test_clip_fail_open_without_radec():
    refcat = Table({'x': [1.0, 2.0, 3.0], 'y': [1.0, 2.0, 3.0]})
    clip = clip_refcat_to_exposure(refcat, [object()], border_arcmin=0.5)
    assert clip.clipped is False and clip.n_kept == 3


def test_clip_fail_open_without_wcs():
    refcat = Table({'RA': [150.0, 150.1], 'DEC': [2.0, 2.1]})
    clip = clip_refcat_to_exposure(refcat, [], border_arcmin=0.5)
    assert clip.clipped is False and clip.n_kept == 2


# --- clip (shapely + mock gwcs) ---------------------------------------------

@pytest.mark.skipif(not HAVE_MOCK_WCS, reason="tweakwcs mock-gwcs helper unavailable")
def test_clip_keeps_in_footprint_drops_decoys():
    wcs = make_mock_wcs(v2ref=120.0, v3ref=500.0, roll=0.0, crpix=[512, 512],
                        cd=[[1e-5, 0], [0, 1e-5]], crval=[150.0, 2.0])
    # In-footprint sources: a pixel grid mapped to sky through the detector WCS.
    # The mock detector is 1024x2048, so stay inside that extent (x<1024).
    gx, gy = np.meshgrid(np.linspace(50, 1000, 12), np.linspace(50, 2000, 12))
    ra_in, dec_in = wcs(gx.ravel(), gy.ravel())
    ra_in = np.asarray(ra_in, float)
    dec_in = np.asarray(dec_in, float)
    # Decoys ~1 degree away — outside the footprint + border.
    decoy = Table({'RA': ra_in[:50] + 1.0, 'DEC': dec_in[:50] + 1.0})
    refcat = vstack([Table({'RA': ra_in, 'DEC': dec_in}), decoy])

    clip = clip_refcat_to_exposure(refcat, [wcs], border_arcmin=0.5)
    assert clip.clipped is True
    assert clip.n_total == len(refcat)
    assert clip.n_kept == len(ra_in)                 # every in-frame kept, decoys gone
    assert np.all(np.asarray(clip.table['RA']) < 150.5)


@pytest.mark.skipif(not HAVE_MOCK_WCS, reason="tweakwcs mock-gwcs helper unavailable")
def test_clip_starved_when_nothing_in_footprint():
    wcs = make_mock_wcs(v2ref=120.0, v3ref=500.0, roll=0.0, crpix=[512, 512],
                        cd=[[1e-5, 0], [0, 1e-5]], crval=[150.0, 2.0])
    far = Table({'RA': [150.0 + 2.0, 150.0 + 2.1], 'DEC': [2.0 + 2.0, 2.0 + 2.1]})
    clip = clip_refcat_to_exposure(far, [wcs], border_arcmin=0.5)
    assert clip.clipped is True and clip.n_kept == 0 and clip.starved is True
