"""
Compute MSA shutter slit geometry (centers and position angles) from NIRSpec spec files.

Extracts the slit computation logic from plot_slits.py into a reusable module.
Parses the shutter_state string from the exposure table to determine which
shutters are open, producing one rectangle per open shutter.
"""

from astropy.table import Table, vstack
from astropy.coordinates import SkyCoord
import astropy.units as u
import numpy as np

from campfire_pipeline.nirspec.constants import (
    FIXED_SLIT_SIZE_ARCSEC, MSA_SHUTTER_SIZE_ARCSEC,
)

# Sky-frame rotation applied to V3PA to get the aperture position angle
# (V3IdlYAngle for NRS_FULL_MSA). The fixed slits share the focal-plane
# orientation closely enough that this is reused for them; the residual
# per-aperture SIAF difference is <~1 deg and below the visual tolerance of the
# cutout overlay.
_V3IDL_Y_ANGLE_DEG = 138.5


def get_source_pos(spec_file):
    """Extract median source RA/Dec from HDU 7 exposure table."""
    exp = Table.read(spec_file, hdu=7)
    ra = np.median(exp['source_ra'][exp['source_ra'] != 0])
    dec = np.median(exp['source_dec'][exp['source_dec'] != 0])
    return ra, dec


def get_exposure_table(spec_file):
    """
    Read exposure table from HDU 7, filtering to one detector side.

    For multi-shutter slitlets with both NRS1 and NRS2 rows, keeps only NRS2
    to avoid double-counting.
    """
    exp = Table.read(spec_file, hdu=7)
    if 'SHUTTER-SLITLET' not in exp['nod_type'][0]:
        raise NotImplementedError(f"Only N-SHUTTER-SLITLET types supported, got {exp['nod_type'][0]}")

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

    Parses the shutter_state string from each exposure row to determine
    which shutters are open. No deduplication is performed — the caller
    receives one entry per open shutter per exposure row.

    Parameters
    ----------
    spec_files : list of str
        Paths to *_spec.fits files for this object (one per grating/exposure).
    corrected_pos : tuple of (ra, dec), optional
        Astrometrically-corrected source position. If None, reads from FITS.

    Returns
    -------
    list of dict
        Each dict has keys: center_ra, center_dec, position_angle, shutter_idx,
        shutter_state, v3pa.
        position_angle is in degrees, sky frame (V3PA + 138.5).
        shutter_idx is relative to the source shutter (source = 0).
        shutter_state is 'source' or 'open'.
    """
    # Build combined exposure table (no deduplication)
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

        # Compute center of the shutter containing the source
        shutter_c = source_c.directional_offset_by(pa, dy).directional_offset_by(pa - 90 * u.deg, dx)

        # Parse shutter_state string to determine open shutters.
        # Each character is a shutter: 'x' = source, '1' = open background.
        # String is ordered from +PA end to -PA end.
        shutter_state_str = str(t['shutter_state'])
        src_pos = shutter_state_str.index('x')

        for i, char in enumerate(shutter_state_str):
            if char not in ('x', '1'):
                continue

            shutter_idx = src_pos - i
            offset = shutter_idx * 0.53 * u.arcsec

            if shutter_idx == 0:
                c = shutter_c
            else:
                c = shutter_c.directional_offset_by(pa, offset)

            results.append({
                'center_ra': float(c.ra.deg),
                'center_dec': float(c.dec.deg),
                'position_angle': float(pa.to('deg').value) % 360,
                'shutter_idx': shutter_idx,
                'shutter_state': 'source' if char == 'x' else 'open',
                'v3pa': float(t['v3pa']),
                'aperture_name': 'MSA',
                'aperture_width_arcsec': float(MSA_SHUTTER_SIZE_ARCSEC[0]),
                'aperture_height_arcsec': float(MSA_SHUTTER_SIZE_ARCSEC[1]),
            })

    return results


def compute_fixed_slit_apertures(spec_file, corrected_pos=None):
    """Compute the on-sky aperture rectangle(s) for a fixed-slit exposure set.

    Fixed-slit exposures place the target in a single rectangular aperture
    (e.g. S200A2), not a multi-shutter MSA slitlet, so there is one rectangle per
    exposure row — centered on the source, sized by the slit's SIAF dimensions,
    at the slit position angle. (Identical rows across nods are collapsed at
    deploy time, the same way MSA dither positions are.)

    Parameters
    ----------
    spec_file : str
        Path to a ``*_spec.fits`` file whose EXPOSURES HDU carries the
        ``fixed_slit`` / ``slit_name`` columns written by stage 3.
    corrected_pos : tuple of (ra, dec), optional
        Astrometrically-corrected source position. If None, reads from FITS.

    Returns
    -------
    list of dict
        Same keys as :func:`compute_slit_centers` plus ``aperture_name`` and the
        per-slit ``aperture_width_arcsec`` / ``aperture_height_arcsec``.
        ``shutter_idx`` is 0 and ``shutter_state`` is ``'source'`` for every row.
    """
    exp = Table.read(spec_file, hdu=7)

    if corrected_pos is not None:
        source_ra, source_dec = corrected_pos
    else:
        source_ra, source_dec = get_source_pos(spec_file)
    source_c = SkyCoord(source_ra, source_dec, unit='deg')

    results = []
    for t in exp:
        slit_name = (str(t['slit_name']).strip().upper()
                     if 'slit_name' in exp.colnames else '')
        width, height = FIXED_SLIT_SIZE_ARCSEC.get(
            slit_name, (0.20, 3.20))  # default to the common 0.2x3.2 slit

        pa = (t['v3pa'] - 360 + _V3IDL_Y_ANGLE_DEG) * u.deg

        results.append({
            'center_ra': float(source_c.ra.deg),
            'center_dec': float(source_c.dec.deg),
            'position_angle': float(pa.to('deg').value) % 360,
            'shutter_idx': 0,
            'shutter_state': 'source',
            'v3pa': float(t['v3pa']),
            'aperture_name': slit_name or 'FIXEDSLIT',
            'aperture_width_arcsec': float(width),
            'aperture_height_arcsec': float(height),
        })

    return results
