"""
Photometric catalog cross-matching and deployment.

Cross-matches photometric catalogs to object centroids and populates
the object_photometry table. Also generates P(z) + template SED
JSON sidecars for upload to R2.

Config-driven via $CAMPFIRE_ROOT/config/photometry.toml with per-field
sections specifying catalog paths, column mappings, and flux units.
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import tomllib
from astropy.coordinates import SkyCoord, search_around_sky
from astropy.io import fits
from astropy.table import Table
import astropy.units as u
from supabase import Client


BATCH_SIZE = 500


# ---------------------------------------------------------------------------
# Filter wavelengths (microns): (pivot, blue_edge, red_edge)
# Extracted from deploy/campfire_deploy/generate_sed.py
# ---------------------------------------------------------------------------

FILTER_WAVELENGTHS = {
    'vis': (0.718086, 0.495885, 0.930629),
    'f435w': (0.433444, 0.359500, 0.488300),
    'f606w': (0.596043, 0.462700, 0.717900),
    'f814w': (0.807304, 0.686800, 0.962600),
    'f098m': (0.987520, 0.889000, 1.084297),
    'f090w': (0.904228, 0.788550, 1.023550),
    'f115w': (1.157002, 0.998200, 1.305200),
    'f140m': (1.406032, 1.304350, 1.505350),
    'f150w': (1.503988, 1.303790, 1.693790),
    'f182m': (1.846590, 1.695500, 2.000500),
    'f200w': (1.993392, 1.723400, 2.258400),
    'f210m': (2.096375, 1.961600, 2.232600),
    'f250m': (2.503802, 2.393530, 2.616900),
    'f277w': (2.769332, 2.365900, 3.216190),
    'f300m': (2.989121, 2.770356, 3.250592),
    'f335m': (3.363887, 3.118640, 3.642920),
    'f356w': (3.576787, 3.070000, 4.078020),
    'f360m': (3.626058, 3.322680, 3.902360),
    'f410m': (4.084378, 3.775340, 4.402310),
    'f430m': (4.281818, 4.122610, 4.444200),
    'f444w': (4.415974, 3.802370, 5.099550),
    'f460m': (4.630470, 4.465820, 4.813090),
    'f480m': (4.819237, 4.582030, 5.088740),
    'f770w': (7.663456, 6.475000, 8.830000),
    # Ground-based filters (approximate pivot wavelengths)
    'u': (0.3551, 0.3100, 0.4000),
    'g': (0.4810, 0.3950, 0.5600),
    'r': (0.6230, 0.5500, 0.7000),
    'i': (0.7640, 0.6900, 0.8400),
    'z': (0.9060, 0.8200, 1.0000),
    'y': (0.9910, 0.9300, 1.0600),
    'Y': (1.0210, 0.9600, 1.0900),
    'J': (1.2520, 1.1500, 1.3500),
    'H': (1.6440, 1.4900, 1.8000),
    'Ks': (2.1590, 1.9900, 2.3200),
}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_field_config(
    photometry_config_path: Path,
    field: str,
) -> dict | None:
    """Load photometry.toml and return the [field] section, or None."""
    with open(photometry_config_path, 'rb') as f:
        config = tomllib.load(f)

    return config.get(field)


# ---------------------------------------------------------------------------
# Flux conversion
# ---------------------------------------------------------------------------

def convert_flux_to_ujy(value: float, error: float, from_unit: str) -> tuple[float, float]:
    """Convert flux + error to microjansky.

    Supported source units: uJy, nJy, Jy, AB_mag.
    Returns (flux_ujy, error_ujy). NaN propagated.
    """
    if not np.isfinite(value) or not np.isfinite(error):
        return float('nan'), float('nan')

    match from_unit.lower():
        case 'ujy' | 'µjy':
            return value, error
        case 'njy':
            return value / 1000.0, error / 1000.0
        case 'jy':
            return value * 1e6, error * 1e6
        case 'ab_mag' | 'abmag' | 'mag':
            # AB mag → µJy: f_µJy = 10^((23.9 - m) / 2.5)
            flux_ujy = 10 ** ((23.9 - value) / 2.5)
            # Error propagation: δf = f * ln(10) / 2.5 * δm
            error_ujy = flux_ujy * (math.log(10) / 2.5) * abs(error)
            return flux_ujy, error_ujy
        case _:
            raise ValueError(f"Unknown flux unit: {from_unit}")


# ---------------------------------------------------------------------------
# Cross-matching
# ---------------------------------------------------------------------------

def crossmatch_catalog(
    catalog_ra: np.ndarray,
    catalog_dec: np.ndarray,
    object_ra: np.ndarray,
    object_dec: np.ndarray,
    radius_arcsec: float,
) -> list[tuple[int, int, float]]:
    """Cross-match catalog positions to object centroids.

    Returns list of (object_idx, catalog_idx, distance_arcsec) tuples.
    For each object, only the closest catalog match within radius is kept.
    """
    cat_coords = SkyCoord(ra=catalog_ra * u.deg, dec=catalog_dec * u.deg)
    obj_coords = SkyCoord(ra=object_ra * u.deg, dec=object_dec * u.deg)

    # search_around_sky finds all pairs within radius
    idx_cat, idx_obj, sep, _ = search_around_sky(
        cat_coords, obj_coords, radius_arcsec * u.arcsec,
    )

    # For each object, keep only the closest catalog match
    best: dict[int, tuple[int, float]] = {}
    for ic, io, s in zip(idx_cat, idx_obj, sep):
        ic_int, io_int = int(ic), int(io)
        dist = s.arcsec
        if io_int not in best or dist < best[io_int][1]:
            best[io_int] = (ic_int, dist)

    # Check for ambiguous matches (multiple objects matching same catalog source)
    cat_to_objects: dict[int, list[int]] = defaultdict(list)
    for obj_idx, (cat_idx, _) in best.items():
        cat_to_objects[cat_idx].append(obj_idx)

    ambiguous = {k: v for k, v in cat_to_objects.items() if len(v) > 1}
    if ambiguous:
        print(f"    WARNING: {len(ambiguous)} catalog sources match multiple objects")

    return [(obj_idx, cat_idx, dist) for obj_idx, (cat_idx, dist) in best.items()]


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------

def build_photometry_payload(
    catalog_row: dict,
    band_config: dict,
    flux_unit: str,
) -> dict:
    """Build JSONB payload for one object's photometry.

    band_config: mapping of band_name → {flux: col, err: col}
    """
    bands = {}
    for band_name, columns in band_config.items():
        flux_col = columns.get('flux') or columns.get('f')
        err_col = columns.get('err') or columns.get('e')

        if flux_col not in catalog_row or err_col not in catalog_row:
            continue

        raw_flux = float(catalog_row[flux_col])
        raw_err = float(catalog_row[err_col])

        flux_ujy, err_ujy = convert_flux_to_ujy(raw_flux, raw_err, flux_unit)

        if not np.isfinite(flux_ujy):
            continue

        # Look up wavelength info
        wav_info = FILTER_WAVELENGTHS.get(band_name.lower())
        if wav_info:
            wav, wav_min, wav_max = wav_info
        else:
            # Unknown filter — skip wavelength info, frontend can still display
            wav, wav_min, wav_max = None, None, None

        band_data: dict = {
            'flux': round(flux_ujy, 6),
            'flux_err': round(err_ujy, 6),
        }
        if wav is not None:
            band_data['wav'] = wav
            band_data['wav_min'] = wav_min
            band_data['wav_max'] = wav_max

        bands[band_name] = band_data

    return {
        'flux_unit': 'uJy',
        'bands': bands,
    }


# ---------------------------------------------------------------------------
# P(z) sidecar generation
# ---------------------------------------------------------------------------

def generate_pz_sidecar(
    catalog_id: int | str,
    pz_runs: list[dict],
) -> dict | None:
    """Generate P(z) + template sidecar JSON for one object.

    Each pz_run dict has keys: name, label, file, id_column, z_best_column,
    chi2_column, coeffs_column, pz_ext, templates_ext, color.

    Returns dict ready for JSON serialization, or None if no runs match.
    """
    runs = {}

    for run_cfg in pz_runs:
        run_file = run_cfg.get('file')
        if not run_file or not Path(run_file).exists():
            continue

        id_col = run_cfg.get('id_column', 'ID')
        z_best_col = run_cfg.get('z_best_column', 'z_best')
        chi2_col = run_cfg.get('chi2_column', 'chi2')
        coeffs_col = run_cfg.get('coeffs_column', 'coeffs')
        pz_ext = run_cfg.get('pz_ext', 2)
        templates_ext = run_cfg.get('templates_ext', 3)

        try:
            data = fits.getdata(run_file, ext=1)
            idx_arr = np.where(data[id_col] == catalog_id)[0]
            if len(idx_arr) == 0:
                continue
            idx = idx_arr[0]

            z_best = float(data[z_best_col][idx])
            chi2 = float(data[chi2_col][idx])

            run_result: dict = {
                'label': run_cfg.get('label', run_cfg['name']),
                'color': run_cfg.get('color', '#999999'),
                'z_best': z_best,
                'chi2': chi2,
            }

            # P(z) grid
            pz_data = fits.getdata(run_file, ext=pz_ext)
            z_grid = pz_data['Pz'][0].tolist()
            pz_vals = pz_data['Pz'][idx + 1]
            pz_max = np.max(pz_vals)
            if pz_max > 0:
                pz_vals = (pz_vals / pz_max).tolist()
            else:
                pz_vals = pz_vals.tolist()
            run_result['z_grid'] = z_grid
            run_result['pz'] = pz_vals

            # Best-fit template SED
            if coeffs_col in data.dtype.names:
                coeffs = data[coeffs_col][idx]
                templates_data = fits.getdata(run_file, ext=templates_ext)
                template_names = [n for n in templates_data.dtype.names if n != 'z']
                iz_best = np.argmin(np.abs(z_best - templates_data['z']))
                templates = np.array([
                    templates_data[tn][iz_best] for tn in template_names
                ])
                lam_rest = templates_data[template_names[0]][0] / 1e4  # Angstrom → µm
                lam_obs = lam_rest * (1 + z_best)
                fnu = np.dot(templates.T, coeffs)

                # Filter to valid range
                valid = np.isfinite(fnu) & (fnu > 0)
                if np.any(valid):
                    run_result['template_wav'] = lam_obs[valid].tolist()
                    run_result['template_flux_ujy'] = (fnu[valid] * 1e6).tolist()  # Jy → µJy

            runs[run_cfg['name']] = run_result

        except Exception as e:
            print(f"    WARNING: Failed to extract P(z) from {run_file} for "
                  f"catalog_id={catalog_id}: {e}")
            continue

    if not runs:
        return None

    return {'runs': runs}


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def _fetch_field_objects(client: Client, field: str) -> list[dict]:
    """Fetch id, ra, dec for all objects in a field."""
    all_objects = []
    page_size = 1000
    offset = 0

    while True:
        resp = (
            client.table('objects')
            .select('id, object_id, ra, dec')
            .eq('field', field)
            .order('id')
            .range(offset, offset + page_size - 1)
            .execute()
        )
        all_objects.extend(resp.data)
        if len(resp.data) < page_size:
            break
        offset += page_size

    return all_objects


def _clear_field_photometry(client: Client, field: str) -> int:
    """Delete existing photometry for a field. Returns count deleted."""
    total = 0
    while True:
        resp = (
            client.table('object_photometry')
            .select('id')
            .eq('field', field)
            .order('id')
            .limit(BATCH_SIZE)
            .execute()
        )
        if not resp.data:
            break
        ids = [row['id'] for row in resp.data]
        client.table('object_photometry').delete().in_('id', ids).execute()
        total += len(ids)
    return total


def _insert_photometry(
    client: Client,
    records: list[dict],
) -> int:
    """Batch-insert photometry records. Returns count inserted."""
    total = 0
    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        client.table('object_photometry').insert(batch).execute()
        total += len(batch)
    return total


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def deploy_field_photometry(
    client: Client,
    field: str,
    photometry_config_path: Path,
    *,
    upload_pz: bool = True,
    r2_uploader=None,
    dry_run: bool = False,
) -> dict:
    """
    Full photometry deploy for a field.

    Args:
        client: Supabase client (service role)
        field: Field name
        photometry_config_path: Path to photometry.toml
        upload_pz: Whether to generate and upload P(z) sidecars
        r2_uploader: Callable(local_path, r2_key) for uploading sidecars
        dry_run: Print stats without writing

    Returns:
        Dict with keys: n_objects, n_matched, n_bands, n_pz
    """
    field_config = load_field_config(photometry_config_path, field)
    if field_config is None:
        print(f"  No photometry config for field '{field}'. Skipping.")
        return {'n_objects': 0, 'n_matched': 0, 'n_bands': 0, 'n_pz': 0}

    # Load catalog
    catalog_path = field_config['catalog']
    catalog_name = field_config.get('catalog_name', Path(catalog_path).stem)
    fmt = field_config.get('format', 'fits')
    flux_unit = field_config.get('flux_unit', 'uJy')
    ra_col = field_config.get('ra_column', 'ra')
    dec_col = field_config.get('dec_column', 'dec')
    id_col = field_config.get('id_column', 'id')
    radius = field_config.get('match_radius_arcsec', 0.3)
    band_config = field_config.get('bands', {})
    photo_z_config = field_config.get('photo_z', {})
    pz_runs = field_config.get('pz_runs', [])

    print(f"  Loading catalog: {catalog_path}")
    catalog = Table.read(catalog_path, format=fmt if fmt != 'fits' else None)
    print(f"    {len(catalog)} sources, {len(band_config)} bands configured")

    # Fetch objects
    print(f"  Fetching objects for field '{field}'...")
    objects = _fetch_field_objects(client, field)
    if not objects:
        print(f"  No objects in field '{field}'. Nothing to do.")
        return {'n_objects': 0, 'n_matched': 0, 'n_bands': 0, 'n_pz': 0}
    print(f"    {len(objects)} objects")

    # Cross-match
    cat_ra = np.array(catalog[ra_col], dtype=float)
    cat_dec = np.array(catalog[dec_col], dtype=float)
    obj_ra = np.array([o['ra'] for o in objects])
    obj_dec = np.array([o['dec'] for o in objects])

    print(f"  Cross-matching with radius={radius}\"...")
    matches = crossmatch_catalog(cat_ra, cat_dec, obj_ra, obj_dec, radius)
    print(f"    {len(matches)} matches out of {len(objects)} objects")

    if dry_run:
        return {
            'n_objects': len(objects),
            'n_matched': len(matches),
            'n_bands': len(band_config),
            'n_pz': 0,
        }

    # Build records
    now = datetime.now(timezone.utc).isoformat()
    records = []
    n_pz = 0

    for obj_idx, cat_idx, dist in matches:
        obj = objects[obj_idx]
        cat_row = {col: catalog[col][cat_idx] for col in catalog.colnames}
        cat_id = str(cat_row.get(id_col, cat_idx))

        # Build photometry payload
        payload = build_photometry_payload(cat_row, band_config, flux_unit)

        # Extract photo-z
        photo_z = None
        photo_z_err_lo = None
        photo_z_err_hi = None
        if photo_z_config:
            z_col = photo_z_config.get('column')
            if z_col and z_col in cat_row:
                z_val = float(cat_row[z_col])
                if np.isfinite(z_val):
                    photo_z = z_val
                    lo_col = photo_z_config.get('err_lo_column')
                    hi_col = photo_z_config.get('err_hi_column')
                    if lo_col and lo_col in cat_row:
                        v = float(cat_row[lo_col])
                        if np.isfinite(v):
                            photo_z_err_lo = v
                    if hi_col and hi_col in cat_row:
                        v = float(cat_row[hi_col])
                        if np.isfinite(v):
                            photo_z_err_hi = v

        # Generate P(z) sidecar
        has_pz = False
        if upload_pz and pz_runs:
            sidecar = generate_pz_sidecar(cat_row.get(id_col, cat_idx), pz_runs)
            if sidecar is not None:
                has_pz = True
                n_pz += 1
                if r2_uploader:
                    r2_key = f"photometry/{field}/{obj['object_id']}_pz.json"
                    sidecar_json = json.dumps(sidecar, separators=(',', ':'))
                    r2_uploader(sidecar_json.encode(), r2_key, 'application/json')

        record = {
            'object_id': obj['id'],
            'field': field,
            'ra': obj['ra'],
            'dec': obj['dec'],
            'catalog_name': catalog_name,
            'catalog_id': cat_id,
            'match_distance_arcsec': round(dist, 4),
            'photometry': payload,
            'photo_z': photo_z,
            'photo_z_err_lo': photo_z_err_lo,
            'photo_z_err_hi': photo_z_err_hi,
            'has_pz': has_pz,
            'updated_at': now,
        }
        records.append(record)

    # Write to database
    print(f"  Clearing existing photometry for field '{field}'...")
    n_deleted = _clear_field_photometry(client, field)
    if n_deleted:
        print(f"    Deleted {n_deleted} existing rows")

    print(f"  Inserting {len(records)} photometry rows...")
    _insert_photometry(client, records)

    # Sync denormalized columns to objects
    print(f"  Syncing photo_z to objects table...")
    resp = client.rpc('sync_photometry_to_objects', {'p_field': field}).execute()
    n_synced = resp.data if isinstance(resp.data, int) else 0
    print(f"    Updated {n_synced} objects")

    return {
        'n_objects': len(objects),
        'n_matched': len(matches),
        'n_bands': len(band_config),
        'n_pz': n_pz,
    }
