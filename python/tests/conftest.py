"""Shared fixtures for CAMPFIRE API tests."""

import pytest
import json


@pytest.fixture
def sample_api_key():
    """Sample API key for testing."""
    return "sk_live_test1234567890abcdef1234567890ab"


@pytest.fixture
def sample_objects_response():
    """Sample response from /api/v1/objects endpoint."""
    return {
        "data": [
            {
                "object_id": "ember_uds_p4_123456",
                "program_id": 1,
                "program_name": "EMBER-UDS",
                "field": "UDS",
                "observation": "ember_uds_p4",
                "ra": 34.12345,
                "dec": -5.67890,
                "redshift": 2.5432,
                "redshift_auto": 2.5400,
                "redshift_inspected": 2.5432,
                "redshift_quality": 3,
                "spectral_features": 0,
                "object_flags": 0,
                "dq_flags": 0,
                "spectra": [
                    {
                        "id": 1,
                        "object_id": "ember_uds_p4_123456",
                        "grating": "PRISM",
                        "fits_path": "spectra/ember_uds_p4/ember_uds_p4_PRISM_CLEAR_123456_spec.fits",
                        "signal_to_noise": 15.5,
                    },
                    {
                        "id": 2,
                        "object_id": "ember_uds_p4_123456",
                        "grating": "G395M",
                        "fits_path": "spectra/ember_uds_p4/ember_uds_p4_G395M_F290LP_123456_spec.fits",
                        "signal_to_noise": 8.2,
                    },
                ],
            },
        ],
        "pagination": {
            "total": 1,
            "limit": 1000,
            "offset": 0,
        },
    }


@pytest.fixture
def sample_metadata_response():
    """Sample response from /api/v1/metadata endpoint."""
    return {
        "programs": [
            {"program_id": 1, "program_name": "EMBER-UDS", "pi_name": "Jane Doe", "is_public": True},
            {"program_id": 2, "program_name": "CAPERS", "pi_name": "John Smith", "is_public": False},
        ],
        "fields": ["COSMOS", "UDS", "EGS"],
        "gratings": ["PRISM", "G140M", "G235M", "G395M"],
        "observations": ["ember_uds_p4", "capers_cosmos_p1"],
    }


@pytest.fixture
def sample_spectrum_data():
    """Sample spectrum data for plotting tests."""
    import numpy as np

    n_wave = 100
    n_spatial = 15

    wave = np.linspace(0.6, 5.3, n_wave)
    fnu = np.random.normal(1.0, 0.1, n_wave)
    fnu_err = np.abs(np.random.normal(0.05, 0.01, n_wave))
    snr_2d = np.random.normal(5.0, 2.0, (n_spatial, n_wave))
    profile = np.exp(-0.5 * np.linspace(-3, 3, n_spatial) ** 2)
    profile_fit = profile * 0.9
    profile_pix = np.arange(n_spatial) - n_spatial // 2

    return {
        "wave": wave.tolist(),
        "fnu": fnu.tolist(),
        "fnu_err": fnu_err.tolist(),
        "snr_2d": snr_2d.tolist(),
        "n_spatial": n_spatial,
        "n_wave": n_wave,
        "profile": profile.tolist(),
        "profile_fit": profile_fit.tolist(),
        "profile_pix": profile_pix.tolist(),
    }


@pytest.fixture
def sample_redshift_fit_data():
    """Sample redshift fit data for plotting tests."""
    import numpy as np

    z_grid = np.linspace(0, 10, 500)
    chi2_grid = 100 + 50 * (z_grid - 2.5) ** 2  # Minimum at z=2.5
    chi2_grid = chi2_grid + np.random.normal(0, 5, len(z_grid))
    chi2_grid = np.maximum(chi2_grid, 10)  # Floor

    return {
        "redshift": 2.5,
        "chi2_min": 100.0,
        "confidence": 95.5,
        "z_grid": z_grid.tolist(),
        "chi2_grid": chi2_grid.tolist(),
        "model_wave": np.linspace(0.6, 5.3, 100).tolist(),
        "model_fnu": np.random.normal(1.0, 0.05, 100).tolist(),
    }
