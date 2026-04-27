"""
Photometric catalog cross-matching and deployment.

Cross-matches photometric catalogs to object centroids and populates
the object_photometry table. Also generates P(z) + template SED
JSON sidecars for upload to R2.

Config-driven via $CAMPFIRE_ROOT/config/photometry.toml with per-field
sections specifying catalog paths, column mappings, and flux units.
Photo-z comes from a single Lazy.jl FITS file configured under
[field.photoz].
"""

import json
import math
import tempfile
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

from campfire.deploy.r2 import UploadTask, upload_files_parallel


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
# Photo-z: Lazy.jl FITS reader
# ---------------------------------------------------------------------------

class PhotozData:
    """Pre-loaded Lazy.jl photo-z data for efficient per-object lookups.

    Lazy.jl FITS structure:
      - Ext 1: main table (ID, z_best, chi2, coeffs per source)
      - Ext pz_ext: P(z) column where row 0 = z-grid, row N+1 = P(z) for source N
      - Ext templates_ext: template basis functions with z grid; rest-frame
        wavelength in Angstroms from template[0][0]
    """

    def __init__(self, photoz_config: dict):
        self.label = photoz_config.get('label', 'Photo-z')
        self.color = photoz_config.get('color', '#999999')

        pz_file = photoz_config['file']
        id_col = photoz_config.get('id_column', 'ID')
        pz_ext = photoz_config.get('pz_ext', 2)
        templates_ext = photoz_config.get('templates_ext', 3)

        self.z_best_col = photoz_config.get('z_best_column', 'z_best')
        self.chi2_col = photoz_config.get('chi2_column', 'chi2')
        self.coeffs_col = photoz_config.get('coeffs_column', 'coeffs')

        print(f"  Loading photo-z data: {pz_file}")
        self.main = fits.getdata(pz_file, ext=1)
        self.pz_table = fits.getdata(pz_file, ext=pz_ext)
        self.z_grid = self.pz_table['Pz'][0].tolist()
        self.templates = fits.getdata(pz_file, ext=templates_ext)

        # Build ID → row index lookup
        self._id_to_idx: dict[int | str, int] = {}
        for i, v in enumerate(self.main[id_col]):
            self._id_to_idx[int(v) if isinstance(v, (int, np.integer)) else v] = i

        template_names = [n for n in self.templates.dtype.names if n != 'z']
        self._template_names = template_names
        # Rest-frame wavelength grid in µm (same for all templates)
        self._lam_rest = self.templates[template_names[0]][0] / 1e4  # Å → µm

        print(f"    {len(self._id_to_idx)} sources loaded")

    def lookup(self, catalog_id: int | str) -> dict | None:
        """Look up photo-z metadata for a catalog ID.

        Returns dict with z_best, chi2, z_err_lo, z_err_hi, or None if
        the ID is not in the photo-z file.
        """
        idx = self._id_to_idx.get(catalog_id)
        if idx is None:
            return None

        z_best = float(self.main[self.z_best_col][idx])
        if not np.isfinite(z_best):
            return None

        result: dict = {
            'z_best': z_best,
            'chi2': float(self.main[self.chi2_col][idx]),
        }

        # Confidence intervals if available
        if 'z_l68' in self.main.dtype.names:
            v = float(self.main['z_l68'][idx])
            if np.isfinite(v):
                result['z_err_lo'] = v
        if 'z_u68' in self.main.dtype.names:
            v = float(self.main['z_u68'][idx])
            if np.isfinite(v):
                result['z_err_hi'] = v

        return result

    def generate_sidecar(self, catalog_id: int | str) -> dict | None:
        """Generate P(z) + template sidecar JSON for one object.

        Returns flat dict with label, color, z_best, chi2, z_grid, pz,
        template_wav, template_flux_ujy. Returns None if ID not found.
        """
        idx = self._id_to_idx.get(catalog_id)
        if idx is None:
            return None

        z_best = float(self.main[self.z_best_col][idx])
        chi2 = float(self.main[self.chi2_col][idx])

        result: dict = {
            'label': self.label,
            'color': self.color,
            'z_best': z_best,
            'chi2': chi2,
        }

        # P(z) distribution
        pz_vals = self.pz_table['Pz'][idx + 1]
        pz_max = np.max(pz_vals)
        if pz_max > 0:
            pz_vals = (pz_vals / pz_max).tolist()
        else:
            pz_vals = pz_vals.tolist()
        result['z_grid'] = self.z_grid
        result['pz'] = pz_vals

        # Best-fit template SED
        if self.coeffs_col in self.main.dtype.names:
            coeffs = self.main[self.coeffs_col][idx]
            iz_best = np.argmin(np.abs(z_best - self.templates['z']))
            templates = np.array([
                self.templates[tn][iz_best] for tn in self._template_names
            ])
            lam_obs = self._lam_rest * (1 + z_best)
            fnu = np.dot(templates.T, coeffs)

            valid = np.isfinite(fnu) & (fnu > 0)
            if np.any(valid):
                result['template_wav'] = lam_obs[valid].tolist()
                result['template_flux_ujy'] = fnu[valid].tolist()  # already µJy

        return result


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


def _upsert_photometry(client: Client, records: list[dict]) -> int:
    """Batch-upsert photometry records on (field, catalog_name, catalog_id).

    Returns count upserted.
    """
    total = 0
    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        client.table('object_photometry').upsert(
            batch, on_conflict='field,catalog_name,catalog_id',
        ).execute()
        total += len(batch)
    return total


def _reconcile_existing_rows(
    client: Client,
    field: str,
    catalog_name: str,
    restricted_object_db_ids: set[int],
    upsert_keys: set[tuple[int, str]],
    key_to_obj: dict[tuple[str, str], tuple[int, float, float]],
    now: str,
) -> tuple[int, int]:
    """Reconcile existing photometry rows owned by restricted objects against
    the current deduped match set.

    For each existing DB row whose `object_id` is in *restricted_object_db_ids*:

    - If the row's catalog source is being re-upserted by the current run
      (key in *upsert_keys*) → no action; the upsert handles it.
    - Else if the row's catalog source is matched to a different object now
      (key in *key_to_obj* but points elsewhere) → re-route: update the FK
      in place. No R2 upload — photo_z and payload are unchanged.
    - Else (catalog source absent from current match set) → delete. Genuine
      orphan: catalog source removed upstream, or all candidate centroids
      drifted out of match radius.

    Scoped to a single catalog_name so multi-catalog fields are not
    cross-contaminated.

    Returns (n_deleted, n_rerouted).
    """
    if not restricted_object_db_ids:
        return 0, 0

    ids = list(restricted_object_db_ids)
    existing: list[dict] = []
    for i in range(0, len(ids), BATCH_SIZE):
        chunk = ids[i:i + BATCH_SIZE]
        resp = (
            client.table('object_photometry')
            .select('id, object_id, catalog_name, catalog_id')
            .eq('field', field)
            .eq('catalog_name', catalog_name)
            .in_('object_id', chunk)
            .execute()
        )
        if resp.data:
            existing.extend(resp.data)

    rows_to_delete: list[int] = []
    reroutes: list[tuple[int, int, float, float]] = []
    for r in existing:
        if (r['object_id'], r['catalog_id']) in upsert_keys:
            continue  # current run will upsert this row
        new = key_to_obj.get((r['catalog_name'], r['catalog_id']))
        if new is None:
            rows_to_delete.append(r['id'])
            continue
        new_id, new_ra, new_dec = new
        if new_id != r['object_id']:
            reroutes.append((r['id'], new_id, new_ra, new_dec))
        # else: dedup still points to same object but row isn't in upsert_keys.
        # Shouldn't happen in normal flow, but harmless — leave as-is.

    for row_id, new_id, new_ra, new_dec in reroutes:
        client.table('object_photometry').update({
            'object_id': new_id,
            'ra': new_ra,
            'dec': new_dec,
            'updated_at': now,
        }).eq('id', row_id).execute()

    n_deleted = 0
    for i in range(0, len(rows_to_delete), BATCH_SIZE):
        chunk = rows_to_delete[i:i + BATCH_SIZE]
        client.table('object_photometry').delete().in_('id', chunk).execute()
        n_deleted += len(chunk)

    return n_deleted, len(reroutes)


def _prune_photometry(
    client: Client,
    field: str,
    catalog_name: str,
    kept_catalog_ids: set[str],
) -> int:
    """Delete photometry rows for *catalog_name* in *field* whose
    catalog_id is not in *kept_catalog_ids*.

    Scoped to a single catalog so other catalogs deployed to the same
    field are untouched.
    """
    total = 0
    page_size = 1000
    offset = 0
    to_delete: list[int] = []
    while True:
        resp = (
            client.table('object_photometry')
            .select('id, catalog_id')
            .eq('field', field)
            .eq('catalog_name', catalog_name)
            .order('id')
            .range(offset, offset + page_size - 1)
            .execute()
        )
        rows = resp.data
        if not rows:
            break
        for r in rows:
            if r['catalog_id'] not in kept_catalog_ids:
                to_delete.append(r['id'])
        if len(rows) < page_size:
            break
        offset += page_size

    for i in range(0, len(to_delete), BATCH_SIZE):
        chunk = to_delete[i:i + BATCH_SIZE]
        client.table('object_photometry').delete().in_('id', chunk).execute()
        total += len(chunk)
    return total


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def deploy_field_photometry(
    client: Client,
    field: str,
    photometry_config_path: Path,
    deploy_config: dict,
    *,
    include_photoz: bool = True,
    dry_run: bool = False,
    restrict_to_object_db_ids: set[int] | None = None,
    prune: bool = False,
) -> dict:
    """
    Photometry deploy for a field.

    Cross-matches the configured photometric catalog against object centroids
    in the field, upserts `object_photometry` rows on
    `(field, catalog_name, catalog_id)`, and uploads P(z) sidecars to R2.

    Args:
        client: Supabase client (service role)
        field: Field name
        photometry_config_path: Path to photometry.toml
        deploy_config: Deploy config dict (for R2 upload credentials)
        include_photoz: Whether to extract photo-z and upload P(z) sidecars
        dry_run: Print stats without writing
        restrict_to_object_db_ids: When set, limits upserts and sidecar uploads
            to rows whose `object_id` is in this set. Cross-matching still runs
            against the full field for correct global dedup. When `None`, the
            full field is processed (standalone CLI behavior). An empty set
            triggers an early exit before any catalog/photo-z load.
        prune: When True (and `restrict_to_object_db_ids` is None), after
            upsert delete rows whose `(catalog_name, catalog_id)` is not in
            the current match set. Used to clean up after upstream catalog
            regenerations.

    Returns:
        Dict with keys: n_objects, n_matched, n_bands, n_pz
    """
    # Empty restriction: nothing to do, skip all I/O.
    if restrict_to_object_db_ids is not None and not restrict_to_object_db_ids:
        return {'n_objects': 0, 'n_matched': 0, 'n_bands': 0, 'n_pz': 0}

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
    photoz_config = field_config.get('photoz')

    print(f"  Loading catalog: {catalog_path}")
    catalog = Table.read(catalog_path, format=fmt if fmt != 'fits' else None)
    print(f"    {len(catalog)} sources, {len(band_config)} bands configured")

    # Photo-z is loaded lazily after cross-match (only if any kept match
    # would actually use it — Lazy.jl FITS load is expensive).

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
        if restrict_to_object_db_ids is not None:
            n_kept_dry = sum(
                1 for obj_idx, _, _ in matches
                if objects[obj_idx]['id'] in restrict_to_object_db_ids
            )
            print(f"    Restricted to {n_kept_dry} matches "
                  f"({len(restrict_to_object_db_ids)} changed objects)")
            n_reported = n_kept_dry
        else:
            n_reported = len(matches)
        return {
            'n_objects': len(objects),
            'n_matched': n_reported,
            'n_bands': len(band_config),
            'n_pz': 0,
        }

    # De-duplicate: when multiple objects match the same catalog source,
    # keep only the closest match (unique constraint on field+catalog_name+catalog_id).
    # Dedup runs against the *full* match set (not just restricted ids) so the
    # catalog-source → closest-object mapping stays globally correct even when
    # only a subset of objects is being upserted.
    matches.sort(key=lambda m: m[2])  # sort by distance
    seen_cat_idx: set[int] = set()
    unique_matches = []
    for obj_idx, cat_idx, dist in matches:
        if cat_idx not in seen_cat_idx:
            seen_cat_idx.add(cat_idx)
            unique_matches.append((obj_idx, cat_idx, dist))
    if len(unique_matches) < len(matches):
        print(f"    De-duplicated: {len(matches)} → {len(unique_matches)} "
              f"(kept closest match per catalog source)")
    matches = unique_matches

    # Partition: kept = matches we will upsert + upload sidecars for.
    if restrict_to_object_db_ids is not None:
        kept_matches = [
            m for m in matches
            if objects[m[0]]['id'] in restrict_to_object_db_ids
        ]
        print(f"    Restricted to {len(kept_matches)} matches "
              f"for {len(restrict_to_object_db_ids)} changed objects")
    else:
        kept_matches = matches

    # Lazy photo-z load: only pay the FITS load if we actually have rows to
    # process. Skipped entirely when kept_matches is empty.
    photoz: PhotozData | None = None
    if include_photoz and kept_matches and photoz_config:
        photoz_file = photoz_config.get('file')
        if photoz_file and Path(photoz_file).exists():
            photoz = PhotozData(photoz_config)
        else:
            print(f"  WARNING: Photo-z file not found: {photoz_file}")
    elif include_photoz and kept_matches and not photoz_config:
        print(f"  No [photoz] config for field '{field}'. Skipping photo-z.")

    # Build records + P(z) sidecars (only for kept matches)
    now = datetime.now(timezone.utc).isoformat()
    records = []
    upload_tasks: list[UploadTask] = []
    n_pz = 0
    tmpdir = tempfile.mkdtemp(prefix='campfire_pz_')

    for obj_idx, cat_idx, dist in kept_matches:
        obj = objects[obj_idx]
        cat_row = {col: catalog[col][cat_idx] for col in catalog.colnames}
        # Catalog ID: use the raw value for photo-z lookup (usually int),
        # stringify for the DB record
        cat_id_raw = cat_row.get(id_col, cat_idx)
        cat_id_int = int(cat_id_raw) if isinstance(cat_id_raw, (int, float, np.integer, np.floating)) else cat_id_raw
        cat_id = str(cat_id_raw)

        # Build photometry payload
        payload = build_photometry_payload(cat_row, band_config, flux_unit)

        # Photo-z from Lazy.jl
        photo_z = None
        photo_z_err_lo = None
        photo_z_err_hi = None
        has_pz = False

        if photoz is not None:
            pz_meta = photoz.lookup(cat_id_int)
            if pz_meta is not None:
                photo_z = pz_meta['z_best']
                photo_z_err_lo = pz_meta.get('z_err_lo')
                photo_z_err_hi = pz_meta.get('z_err_hi')

                # Generate P(z) sidecar
                sidecar = photoz.generate_sidecar(cat_id_int)
                if sidecar is not None:
                    has_pz = True
                    n_pz += 1
                    r2_key = f"photometry/{field}/{obj['object_id']}_pz.json"
                    local_path = Path(tmpdir) / f"{obj['object_id']}_pz.json"
                    local_path.write_text(
                        json.dumps(sidecar, separators=(',', ':')),
                    )
                    upload_tasks.append(UploadTask(local_path, r2_key, 'application/json'))

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

    # Upload P(z) sidecars to R2
    if upload_tasks:
        print(f"  Uploading {len(upload_tasks)} P(z) sidecars to R2...")
        success, failed, errors = upload_files_parallel(
            deploy_config, upload_tasks, desc="P(z) sidecars",
        )
        if failed:
            print(f"    WARNING: {failed} sidecar uploads failed")
            for err in errors[:5]:
                print(f"      {err}")

    # Build a (catalog_name, catalog_id) → (obj_db_id, ra, dec) map from the
    # *full* deduped match set. Used both for existing-row reconciliation
    # (re-route vs. delete) and for --prune (which catalog_ids are still
    # present).
    key_to_obj: dict[tuple[str, str], tuple[int, float, float]] = {}
    for obj_idx, cat_idx, _ in matches:
        cat_id_raw = catalog[id_col][cat_idx] if id_col in catalog.colnames else cat_idx
        cat_id_str = str(cat_id_raw)
        obj = objects[obj_idx]
        key_to_obj[(catalog_name, cat_id_str)] = (obj['id'], obj['ra'], obj['dec'])

    # Reconcile existing rows owned by restricted objects against the new
    # match set. This catches three cases that pure upsert misses:
    #   1. Object lost a catalog source it used to own (centroid drift) and
    #      no other object now owns it → delete.
    #   2. Object lost a source that dedup re-assigned to an unchanged object
    #      → re-route the FK in place (no R2 upload).
    #   3. Object's prior row is being re-upserted by this run → no action.
    if restrict_to_object_db_ids is not None and restrict_to_object_db_ids:
        upsert_keys: set[tuple[int, str]] = {
            (rec['object_id'], rec['catalog_id']) for rec in records
        }
        n_deleted, n_rerouted = _reconcile_existing_rows(
            client, field, catalog_name,
            restrict_to_object_db_ids, upsert_keys, key_to_obj, now,
        )
        if n_rerouted:
            print(f"    Re-routed {n_rerouted} photometry rows to new owners")
        if n_deleted:
            print(f"    Deleted {n_deleted} orphaned rows (no catalog match)")

    if records:
        print(f"  Upserting {len(records)} photometry rows...")
        _upsert_photometry(client, records)
    else:
        print(f"  No photometry rows to upsert.")

    if prune and restrict_to_object_db_ids is None:
        kept_catalog_ids = {cat_id for (_cn, cat_id) in key_to_obj.keys()}
        print(f"  Pruning rows in catalog '{catalog_name}' not in current match set...")
        n_pruned = _prune_photometry(client, field, catalog_name, kept_catalog_ids)
        if n_pruned:
            print(f"    Pruned {n_pruned} stale rows")

    # Sync denormalized columns to objects
    print(f"  Syncing photo_z to objects table...")
    resp = client.rpc('sync_photometry_to_objects', {'p_field': field}).execute()
    n_synced = resp.data if isinstance(resp.data, int) else 0
    print(f"    Updated {n_synced} objects")

    # Clean up temp files
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    return {
        'n_objects': len(objects),
        'n_matched': len(kept_matches),
        'n_bands': len(band_config),
        'n_pz': n_pz,
    }
