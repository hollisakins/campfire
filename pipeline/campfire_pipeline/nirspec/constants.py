"""
NIRSpec-specific constants: grating wavelength ranges.
"""

GRATING_LIMITS = {
    "prism": [0.54, 5.51, 0.01],
    "g140m": [0.55, 3.35, 0.00063],
    "g235m": [1.58, 5.3, 0.00106],
    "g395m": [2.68, 5.51, 0.00179],
    "g140h": [0.68, 1.9, 0.000238],
    "g235h": [1.66, 3.17, 0.000396],
    "g395h": [2.83, 5.24, 0.000666],
}

GRATINGS = [k.upper() for k in GRATING_LIMITS]


# ---------------------------------------------------------------------------
# Extended-wavelength reduction (G140M/F100LP, G235M/F170LP)
# ---------------------------------------------------------------------------
# Constants for the opt-in extended-wavelength feature
# ([nirspec.stage2].extend_g140m_g235m). See
# docs/extended-wavelength-integration.md and campfire_pipeline/nirspec/
# extended_wavelength.py.

# The only grating/filter combos the extension applies to. F070LP (hard filter
# cutoff) and the H gratings are intentionally excluded.
EXTENDED_GRATING_FILTERS = {("G140M", "F100LP"), ("G235M", "F170LP")}

# Red edge the extension reaches: the assign_wcs wavelengthrange and the stage1
# background mask are pushed out to this, in microns.
EXTENDED_MAX_WAVELENGTH_UM = 5.3

# Upper bound of the extended flat-field wavelength grid, in microns (a hair
# past EXTENDED_MAX_WAVELENGTH_UM so the 5.3 um sample is included).
EXTENDED_GRID_MAX_UM = 5.301

# Wavelength (microns) beyond which the measured flat ends and the extended flat
# holds its last real value constant (flat extrapolation).
FLAT_HOLD_WAVELENGTH_UM = {
    ("G140M", "F100LP"): 1.87,
    ("G235M", "F170LP"): 3.17,
}

# Extended assign_wcs wavelength ranges, in METERS, keyed by the CRDS
# wavelengthrange selector "<FILTER>_<GRATING>".
EXTENDED_WAVELENGTHRANGE_M = {
    "F100LP_G140M": [9.7e-7, 5.3e-6],
    "F170LP_G235M": [1.66e-6, 5.3e-6],
}

# CRDS context the committed extended photom (extended_jwst_nirspec_photom_0015.fits)
# was calibrated under. The extended flats are regenerated live under the *current*
# context; verify_extension_crds_compatibility() refuses to run if the current
# context resolves fflat/sflat/photom to versions newer than the baseline below.
EXTENDED_CALIBRATION_CONTEXT = "jwst_1464.pmap"

# Baseline CRDS reference basenames the calibration was derived against, keyed by
# (DETECTOR, GRATING, FILTER). photom is detector-agnostic and is fixed by the
# committed filename (v0015). fflat (detector-agnostic) and sflat (per-detector)
# basenames are resolved under EXTENDED_CALIBRATION_CONTEXT by
# scripts/capture_extended_calibration_refs.py — re-run it and update this dict if
# the calibration is re-derived against a newer context. A None field means "not yet
# pinned" — the guard warns rather than enforces until it is filled.
EXTENDED_CALIBRATION_REFERENCES = {
    ("NRS1", "G140M", "F100LP"): {"fflat": "jwst_nirspec_fflat_0168.fits",
                                  "sflat": "jwst_nirspec_sflat_0231.fits",
                                  "photom": "jwst_nirspec_photom_0015.fits"},
    ("NRS2", "G140M", "F100LP"): {"fflat": "jwst_nirspec_fflat_0168.fits",
                                  "sflat": "jwst_nirspec_sflat_0226.fits",
                                  "photom": "jwst_nirspec_photom_0015.fits"},
    ("NRS1", "G235M", "F170LP"): {"fflat": "jwst_nirspec_fflat_0166.fits",
                                  "sflat": "jwst_nirspec_sflat_0229.fits",
                                  "photom": "jwst_nirspec_photom_0015.fits"},
    ("NRS2", "G235M", "F170LP"): {"fflat": "jwst_nirspec_fflat_0166.fits",
                                  "sflat": "jwst_nirspec_sflat_0224.fits",
                                  "photom": "jwst_nirspec_photom_0015.fits"},
}
