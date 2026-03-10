"""
Astrometric correction for MSA shutter positions.

Cross-matches the MSA planning catalog against a photometric reference catalog
to derive a 2D polynomial correction field, then applies it to shutter
center_ra / center_dec values.  This aligns the shutter overlay with the
imaging astrometry used by the web map tiles.

The reference catalog path is read from imaging.toml (per-field
``reference_catalog`` key).

The workflow mirrors scripts/plot_slits.py:
  1. Load MSA catalog  (products/{obs}/{obs}_msacat.csv)
  2. Load per-field photometric reference catalog (from imaging.toml)
  3. Cross-match within 0.3"
  4. Fit 2nd-order 2D polynomial to (dRA, dDec) offsets
  5. Evaluate the polynomial at each shutter position
"""

from pathlib import Path

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.table import Table
import astropy.units as u
from scipy.optimize import curve_fit


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _poly2d(coords, a0, a1, a2, b0, b1, c0):
    """2nd-order polynomial: a0 + a1*x + a2*y + b0*x^2 + b1*y^2 + c0*x*y."""
    ra, dec = coords
    return a0 + a1 * ra + a2 * dec + b0 * ra**2 + b1 * dec**2 + c0 * ra * dec


def _load_msa_catalog(obs_dir, obs_name):
    """Load MSA planning catalog, returning SkyCoord or None."""
    path = obs_dir / f'{obs_name}_msacat.csv'
    if not path.exists():
        return None
    cat = Table.read(path)[1:]  # first row is a comment/header duplicate
    return SkyCoord(cat['RA'], cat['DEC'], unit='deg')


def _load_reference_catalog(catalog_path):
    """Load a photometric reference catalog FITS file, returning SkyCoord or None.

    Auto-detects RA/Dec column names (tries ra/dec then RA/DEC).
    """
    path = Path(catalog_path)
    if not path.exists():
        return None
    with fits.open(path) as hdul:
        data = hdul[1].data
        cols = [c.name for c in hdul[1].columns]
        if 'ra' in cols:
            return SkyCoord(data['ra'], data['dec'], unit='deg')
        elif 'RA' in cols:
            return SkyCoord(data['RA'], data['DEC'], unit='deg')
        else:
            print(f"  Could not find RA/Dec columns in {catalog_path} (columns: {cols})")
            return None


def _get_reference_catalog_path(field, imaging_config):
    """Extract reference_catalog path for *field* from imaging config dict."""
    field_cfg = imaging_config.get(field, {})
    return field_cfg.get('reference_catalog', '')


def _fit_correction(msa_coords, ref_coords):
    """
    Fit a 2D polynomial mapping MSA positions → reference offsets.

    Returns ``(ra_func, dec_func, n_matches)`` where each function maps
    ``(ra_deg, dec_deg) → offset_arcsec``, or ``None`` if the fit cannot
    be computed (too few matches or convergence failure).
    """
    idx, d2d, _ = msa_coords.match_to_catalog_sky(ref_coords)
    good = d2d < 0.3 * u.arcsec

    n_matches = int(good.sum())
    if n_matches < 6:  # 6 free parameters in poly2d
        return None

    dra = (ref_coords.ra[idx[good]] - msa_coords.ra[good]).to('arcsec').value
    ddec = (ref_coords.dec[idx[good]] - msa_coords.dec[good]).to('arcsec').value
    ra = msa_coords[good].ra.value
    dec = msa_coords[good].dec.value

    try:
        params_ra, _ = curve_fit(_poly2d, (ra, dec), dra)
        params_dec, _ = curve_fit(_poly2d, (ra, dec), ddec)
    except RuntimeError:
        return None

    def ra_correction(r, d):
        return _poly2d((r, d), *params_ra)

    def dec_correction(r, d):
        return _poly2d((r, d), *params_dec)

    return ra_correction, dec_correction, n_matches


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def correct_shutter_positions(shutters_data, obs_dir, obs_name, field, imaging_config):
    """
    Apply astrometric corrections to shutter ``center_ra`` / ``center_dec``.

    Reads the ``reference_catalog`` path for *field* from *imaging_config*
    (the parsed imaging.toml dict).

    Modifies *shutters_data* records **in-place**.

    Returns ``(n_corrected, n_matches)`` — the number of shutter records
    corrected and the number of cross-match pairs that anchored the fit.
    Returns ``(0, 0)`` when corrections are skipped.
    """
    msa_coords = _load_msa_catalog(obs_dir, obs_name)
    if msa_coords is None:
        print(f"  No MSA catalog at {obs_dir / f'{obs_name}_msacat.csv'}, skipping astrometry")
        return 0, 0

    cat_path = _get_reference_catalog_path(field, imaging_config)
    if not cat_path:
        print(f"  No reference_catalog set for field '{field}' in imaging.toml, skipping astrometry")
        return 0, 0

    ref_coords = _load_reference_catalog(cat_path)
    if ref_coords is None:
        print(f"  Reference catalog not found: {cat_path}, skipping astrometry")
        return 0, 0

    # Filter MSA catalog to within 1.5' of observed shutter positions
    # (avoids fitting to distant, unrelated MSA sources)
    shutter_ras = [r['center_ra'] for r in shutters_data]
    shutter_decs = [r['center_dec'] for r in shutters_data]
    obs_coords = SkyCoord(shutter_ras, shutter_decs, unit='deg')
    idx, d2d, _ = msa_coords.match_to_catalog_sky(obs_coords)
    msa_coords = msa_coords[d2d < 1.5 * u.arcmin]

    if len(msa_coords) < 6:
        print(f"  Only {len(msa_coords)} MSA sources near observed shutters, skipping astrometry")
        return 0, 0

    result = _fit_correction(msa_coords, ref_coords)
    if result is None:
        print("  Astrometry fit failed (too few cross-matches or convergence error), skipping")
        return 0, 0

    ra_corr, dec_corr, n_matches = result

    # Apply correction to every shutter record
    n_clipped = 0
    for rec in shutters_data:
        dra = ra_corr(rec['center_ra'], rec['center_dec']) / 3600   # arcsec → deg
        ddec = dec_corr(rec['center_ra'], rec['center_dec']) / 3600
        # Clip extreme outliers (same 0.29" threshold as plot_slits.py)
        if abs(dra) > 8e-5 or abs(ddec) > 8e-5:
            n_clipped += 1
            continue
        rec['center_ra'] += dra
        rec['center_dec'] += ddec

    n_corrected = len(shutters_data) - n_clipped
    return n_corrected, n_matches
