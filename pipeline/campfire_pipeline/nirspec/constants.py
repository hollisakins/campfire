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

# Spectroscopic filters (FILTER keyword); CLEAR pairs with the prism. The CLI's
# --filter option (issue #446) validates against this set.
FILTERS = ["clear", "f070lp", "f100lp", "f170lp", "f290lp"]


# ---------------------------------------------------------------------------
# Aperture geometry for slit/shutter sky overlays (shutters ECSV → web frontend)
# ---------------------------------------------------------------------------
# On-sky aperture dimensions in arcsec as (width, height), where width is the
# across-slit (dispersion) direction and height is the along-slit (spatial)
# direction. These are the single source of truth for the rectangles the web
# frontend draws over imaging cutouts; the pipeline writes them per row into the
# shutters ECSV so the frontend no longer hardcodes them.

# MSA micro-shutter open area. Matches the value the web overlay historically
# hardcoded (0.22 x 0.46), so MSA rendering is unchanged once the frontend reads
# dimensions from the data.
MSA_SHUTTER_SIZE_ARCSEC = (0.22, 0.46)

# NIRSpec fixed-slit apertures (SIAF nominal open sizes), one rectangle each.
FIXED_SLIT_SIZE_ARCSEC = {
    "S200A1": (0.20, 3.20),
    "S200A2": (0.20, 3.20),
    "S400A1": (0.40, 3.65),
    "S1600A1": (1.60, 1.60),
    "S200B1": (0.20, 3.20),
}
FIXED_SLIT_NAMES = frozenset(FIXED_SLIT_SIZE_ARCSEC)

# Pixels the stage-1 background mask grows each fixed slit's WCS footprint by.
# The slit's light reaches 2-3 px past the footprint in the cross-dispersion
# direction (measured on ZENITH PRISM exposures, 2026-11); spill along the
# dispersion axis is the wavelength range's job
# ([nirspec.stage1.override_wavelength_range]).
FIXED_SLIT_MASK_GROW_PX = 3


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
    ("G140M", "F070LP"): 1.27,
    ("G140M", "F100LP"): 1.87,
    ("G235M", "F170LP"): 3.17,
}

# Extended assign_wcs wavelength ranges, in METERS, keyed by the CRDS
# wavelengthrange selector "<FILTER>_<GRATING>".
EXTENDED_WAVELENGTHRANGE_M = {
    "F070LP_G140M": [7.0e-7, 5.3e-6],
    "F100LP_G140M": [9.7e-7, 5.3e-6],
    "F170LP_G235M": [1.66e-6, 5.3e-6],
}

# CRDS context the committed extended photom (extended_jwst_nirspec_photom_0015.fits)
# was calibrated under. The extended flats are regenerated live under the *current*
# context; verify_extension_crds_compatibility() refuses to run if the current
# context resolves fflat/sflat/photom to versions newer than the baseline below.
EXTENDED_CALIBRATION_CONTEXT = "jwst_1614.pmap"

# Baseline CRDS reference basenames the calibration was derived against, keyed by
# (DETECTOR, GRATING, FILTER). photom is detector-agnostic and is fixed by the
# committed filename (v0015). fflat (detector-agnostic) and sflat (per-detector)
# basenames are resolved under EXTENDED_CALIBRATION_CONTEXT by
# scripts/capture_extended_calibration_refs.py — re-run it and update this dict if
# the calibration is re-derived against a newer context. A None field means "not yet
# pinned" — the guard warns rather than enforces until it is filled.
EXTENDED_CALIBRATION_REFERENCES = {
    ("NRS1", "G140M", "F070LP"): {"fflat": "jwst_nirspec_fflat_0196.fits",
                                  "sflat": "jwst_nirspec_sflat_0171.fits",
                                  "photom": "jwst_nirspec_photom_0015.fits"},
    ("NRS2", "G140M", "F070LP"): {"fflat": "jwst_nirspec_fflat_0196.fits",
                                  "sflat": "jwst_nirspec_sflat_0200.fits",
                                  "photom": "jwst_nirspec_photom_0015.fits"},
    ("NRS1", "G140M", "F100LP"): {"fflat": "jwst_nirspec_fflat_0200.fits",
                                  "sflat": "jwst_nirspec_sflat_0231.fits",
                                  "photom": "jwst_nirspec_photom_0015.fits"},
    ("NRS2", "G140M", "F100LP"): {"fflat": "jwst_nirspec_fflat_0200.fits",
                                  "sflat": "jwst_nirspec_sflat_0226.fits",
                                  "photom": "jwst_nirspec_photom_0015.fits"},
    ("NRS1", "G235M", "F170LP"): {"fflat": "jwst_nirspec_fflat_0193.fits",
                                  "sflat": "jwst_nirspec_sflat_0229.fits",
                                  "photom": "jwst_nirspec_photom_0015.fits"},
    ("NRS2", "G235M", "F170LP"): {"fflat": "jwst_nirspec_fflat_0193.fits",
                                  "sflat": "jwst_nirspec_sflat_0224.fits",
                                  "photom": "jwst_nirspec_photom_0015.fits"},
}
