"""
Redshift confidence calculation and best-redshift decision tree.
"""

import numpy as np


# Grating wavelength priority for tiebreaking: lower value = higher priority
GRATING_PRIORITY = {
    'G395M': 0, 'G395H': 1,
    'G235M': 2, 'G235H': 3,
    'G140M': 4, 'G140H': 5,
}


def calculate_redshift_confidence(z_array, chi2_array, zbest):
    """
    Calculate redshift confidence based on chi-squared distribution.

    Quality flag is set to 0 by default and will be updated during visual inspection.

    Parameters:
    -----------
    z_array : array
        Redshift grid
    chi2_array : array
        Chi-squared values corresponding to redshift grid
    zbest : float
        Best-fit redshift

    Returns:
    --------
    dict : Confidence metrics
        {
            'redshift': float - Best redshift rounded to 4 decimal places
            'redshift_quality': int - Quality flag (0 = unreviewed, to be set during inspection)
            'chi2_min': float - Minimum chi-squared value
            'confidence': float - Confidence percentage
        }
    """
    try:
        # Calculate confidence using numerically stable method
        # Offset chi2 by minimum to prevent underflow in exp(-large_number)
        chi2_min_val = np.min(chi2_array)
        chi2_offset = chi2_array - chi2_min_val  # Minimum becomes 0
        pz = np.exp(-chi2_offset)  # Convert to probability (max prob = 1.0)

        # Calculate confidence within ±0.03 of best redshift
        confidence_mask = np.abs(z_array - zbest) <= 0.03
        confidence = np.sum(pz[confidence_mask]) / np.sum(pz) * 100

        # Quality flag is 0 by default - will be set during visual inspection
        z_quality = 0   # Default: unreviewed, to be set during visual inspection

        return {
            'redshift': round(zbest, 4),        # Round to 4 decimal places
            'redshift_quality': z_quality,      # Default 0, updated during inspection
            'chi2_min': float(chi2_min_val),
            'confidence': float(confidence)
        }

    except Exception as e:
        # If confidence calculation fails, return safe defaults
        return {
            'redshift': round(zbest, 4),
            'redshift_quality': 0,  # Default 0 even for failed calculations
            'chi2_min': float(np.min(chi2_array)) if len(chi2_array) > 0 else 0.0,
            'confidence': 0.0
        }


def _grating_sort_key(name: str, data: dict) -> tuple:
    """Sort key for ranking gratings: highest SNR > longest exposure > wavelength priority."""
    snr = -(data.get('signal_to_noise') or 0)
    exposure = -(data.get('exposure_time', 0))
    wavelength = GRATING_PRIORITY.get(name, 99)
    return (snr, exposure, wavelength)


def determine_best_redshift(zfit_data_by_grating: dict[str, dict]) -> float | None:
    """
    Apply decision tree to choose the best redshift for an object from multiple spectra.

    Decision logic:
    1. If PRISM available and no gratings: use PRISM
    2. If gratings available and no PRISM: use best grating
    3. If both PRISM and gratings available:
       - Check if they agree (|z_prism - z_grating| < 0.1)
       - If agree: use grating (more precise)
       - If disagree: use PRISM (more robust)

    Best grating ranking: highest max SNR > longest exposure > wavelength
    priority (G395 > G235 > G140).

    Args:
        zfit_data_by_grating: Dict mapping grating names to zfit data dicts.
            Each dict should contain 'redshift', and optionally
            'exposure_time' and 'signal_to_noise' for ranking.

    Returns:
        Best redshift value, or None if no valid data
    """
    if not zfit_data_by_grating:
        return None

    # Separate PRISM from gratings
    prism_data = zfit_data_by_grating.get('PRISM')
    grating_data = {g: d for g, d in zfit_data_by_grating.items() if g != 'PRISM'}

    # Case 1: Only PRISM
    if prism_data and not grating_data:
        return prism_data['redshift']

    # Case 2: Only gratings (no PRISM)
    if grating_data and not prism_data:
        best = min(grating_data, key=lambda g: _grating_sort_key(g, grating_data[g]))
        return grating_data[best]['redshift']

    # Case 3: Both PRISM and gratings
    if prism_data and grating_data:
        z_prism = prism_data['redshift']

        best = min(grating_data, key=lambda g: _grating_sort_key(g, grating_data[g]))
        z_grating = grating_data[best]['redshift']

        # Check agreement
        if abs(z_prism - z_grating) < 0.1:
            return z_grating  # Agree: use grating (more precise)
        else:
            return z_prism    # Disagree: use PRISM (more robust)

    return None
