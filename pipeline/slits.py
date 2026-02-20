"""
Compute MSA shutter slit geometry (centers and position angles) from NIRSpec spec files.

Extracts the slit computation logic from plot_slits.py into a reusable module.
Each 3-shutter slitlet produces 3 rectangles (shutter_idx -1, 0, 1) of fixed
dimensions 0.22" x 0.46".
"""

from astropy.table import Table, vstack
from astropy.coordinates import SkyCoord
import astropy.units as u
import numpy as np


def get_source_pos(spec_file):
    """Extract median source RA/Dec from HDU 7 exposure table."""
    exp = Table.read(spec_file, hdu=7)
    ra = np.median(exp['source_ra'][exp['source_ra'] != 0])
    dec = np.median(exp['source_dec'][exp['source_dec'] != 0])
    return ra, dec


def get_exposure_table(spec_file):
    """
    Read exposure table from HDU 7, filtering to one detector side.

    For 3-shutter slitlets with both NRS1 and NRS2 rows, keeps only NRS2
    to avoid double-counting.
    """
    exp = Table.read(spec_file, hdu=7)
    if exp['nod_type'][0] != '3-SHUTTER-SLITLET':
        raise NotImplementedError(f"Only 3-SHUTTER-SLITLET supported, got {exp['nod_type'][0]}")

    nrs1_files = [f for f in exp['filename'] if 'nrs1' in f]
    nrs2_files = [f for f in exp['filename'] if 'nrs2' in f]
    if len(nrs1_files) == len(exp) or len(nrs2_files) == len(exp):
        pass  # all one detector
    else:
        nrs2 = np.array(['nrs2' in f for f in exp['filename']], dtype=bool)
        exp = exp[nrs2]

    return exp


def compute_slit_centers(spec_files, corrected_pos=None):
    """
    Compute slit rectangle centers and position angles for one object.

    Parameters
    ----------
    spec_files : list of str
        Paths to *_spec.fits files for this object (one per grating/exposure).
    corrected_pos : tuple of (ra, dec), optional
        Astrometrically-corrected source position. If None, reads from FITS.

    Returns
    -------
    list of dict
        Each dict has keys: center_ra, center_dec, position_angle, shutter_idx.
        position_angle is in degrees, sky frame (V3PA + 138.5, no field rotation).
    """
    # Build combined exposure table
    exp = get_exposure_table(spec_files[0])
    for spec_file in spec_files[1:]:
        exp = vstack([exp, get_exposure_table(spec_file)])

    if corrected_pos is not None:
        source_ra, source_dec = corrected_pos
    else:
        source_ra, source_dec = get_source_pos(spec_files[0])

    source_c = SkyCoord(source_ra, source_dec, unit='deg')

    results = []

    for t in exp:
        # Position angle on the sky for the MSA slitlet.
        # V3PA + V3IdlYAngle (138.5° for NRS_FULL_MSA) gives the aperture PA.
        pa = (t['v3pa'] - 360 + 138.5) * u.deg

        # Source position within the shutter (in shutter units)
        if ~np.isfinite(t['source_xpos']) or t['source_xpos'] == 0:
            dx = np.nanmedian(exp['source_xpos']) * 0.27 * u.arcsec
        else:
            dx = t['source_xpos'] * 0.27 * u.arcsec
        if ~np.isfinite(t['source_ypos']) or t['source_ypos'] == 0:
            dy = np.nanmedian(exp['source_ypos']) * 0.53 * u.arcsec
        else:
            dy = t['source_ypos'] * 0.53 * u.arcsec

        # Compute shutter center from source position + offset
        shutter_c = source_c.directional_offset_by(-pa, dy).directional_offset_by(90 * u.deg - pa, dx)

        # Adjust for shutter state (which of the 3 shutters the source is in)
        match t['shutter_state']:
            case '1x1':
                pass
            case '11x':
                shutter_c = shutter_c.directional_offset_by(-pa, 0.53 * u.arcsec)
            case 'x11':
                shutter_c = shutter_c.directional_offset_by(-pa, -0.53 * u.arcsec)

        # Generate all 3 shutters in the slitlet
        for shutter_idx in [-1, 0, 1]:
            c = shutter_c.directional_offset_by(-pa, shutter_idx * 0.53 * u.arcsec)
            results.append({
                'center_ra': float(c.ra.deg),
                'center_dec': float(c.dec.deg),
                'position_angle': float(pa.to('deg').value) % 360,
                'shutter_idx': shutter_idx,
            })

    return results
