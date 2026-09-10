"""
Photometric catalog cross-matching and deployment.

Cross-matches photometric catalogs to object centroids and populates
the object_photometry table. Also generates P(z) + template SED
JSON sidecars for upload to R2.

Config-driven via $CAMPFIRE_ROOT/config/photometry.toml with per-field
sections specifying catalog paths, column mappings, and flux units
(see ``photometry.example.toml`` next to this module). Photo-z comes from
a single FITS file configured under [field.photoz], in one of two layouts
selected by ``format``: ``lazy`` (native Lazy.jl output, :class:`PhotozData`)
or ``unicorn`` (a UNICORN release ``*_photz_v*.fits`` plus its template cube,
:class:`UnicornPhotozData`).
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

from campfire_layout import KeyScheme, Scope, storage_key
from campfire.deploy.r2 import UploadTask, upload_files_parallel


BATCH_SIZE = 500

# --supersede refuses to retire a previous catalog when the new one matched
# fewer than this fraction of the rows it would delete (see
# deploy_field_photometry).
SUPERSEDE_MIN_RATIO = 0.5


# ---------------------------------------------------------------------------
# Filter wavelengths (microns): (pivot, blue_edge, red_edge)
# ---------------------------------------------------------------------------

FILTER_WAVELENGTHS = {
    'vis': (0.718086, 0.495885, 0.930629),
    'f435w': (0.433444, 0.359500, 0.488300),
    'f606w': (0.596043, 0.462700, 0.717900),
    'f775w': (0.769349, 0.687200, 0.862500),
    'f814w': (0.807304, 0.686800, 0.962600),
    'f850lp': (0.903327, 0.818600, 1.043300),
    # HST WFC3/IR
    'f098m': (0.987520, 0.889000, 1.084297),
    'f105w': (1.055225, 0.900000, 1.207000),
    'f125w': (1.248599, 1.100000, 1.400000),
    'f140w': (1.392306, 1.190000, 1.600000),
    'f160w': (1.536918, 1.390000, 1.700000),
    # JWST NIRCam
    'f070w': (0.703860, 0.624000, 0.781000),
    'f090w': (0.904228, 0.788550, 1.023550),
    'f115w': (1.157002, 0.998200, 1.305200),
    'f140m': (1.406032, 1.304350, 1.505350),
    'f150w': (1.503988, 1.303790, 1.693790),
    'f162m': (1.626389, 1.542000, 1.713000),
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


def catalog_columns_needed(field_config: dict) -> list[str]:
    """The catalog columns a field config actually reads: position, id,
    every configured band's flux/err, and the photo-z scale column."""
    cols = [
        field_config.get('ra_column', 'ra'),
        field_config.get('dec_column', 'dec'),
        field_config.get('id_column', 'id'),
    ]
    for columns in field_config.get('bands', {}).values():
        cols.append(columns.get('flux') or columns.get('f'))
        cols.append(columns.get('err') or columns.get('e'))
    scale_col = (field_config.get('photoz') or {}).get('scale_column')
    if scale_col:
        cols.append(scale_col)
    seen: set[str] = set()
    return [c for c in cols if c and not (c in seen or seen.add(c))]


READ_CATALOG_CHUNK_BYTES = 64 * 1024 * 1024


def read_catalog(path: str, fmt: str, columns: list[str], hdu: int = 1) -> Table:
    """Read *columns* of a catalog into a Table.

    A FITS binary table is streamed through in fixed-size row chunks and
    only the requested columns are kept: a UNICORN release is 300+ columns
    and 4–12 GB, far more than the ~40 columns a deploy needs, and
    ``Table.read`` (or a memory-map touched column by column) would pull the
    whole file into RAM / the page cache — enough to get the process killed
    on a laptop. The file is read with plain sequential reads and, where the
    platform supports it, with caching disabled (``F_NOCACHE`` on macOS), so
    the resident footprint is the kept columns plus one chunk.

    The raw records are decoded the way ``Table.read`` would decode them:
    character columns become stripped ``str``, logical columns ``bool``, and
    ``TSCAL`` / ``TZERO`` scaling is applied (so an unsigned-int convention
    or a scaled flux column keeps its physical value). Bit-array and
    variable-length columns are not supported and raise.

    Columns absent from the file are skipped (the payload builder already
    tolerates a missing band); the caller checks the position columns.
    Other formats go through ``Table.read`` unchanged.
    """
    if fmt != 'fits':
        return Table.read(path, format=fmt)

    if _is_gzipped(path):
        # datLoc is an offset into the decompressed stream; there is no
        # streaming path for a gzipped table.
        print(f"    NOTE: {Path(path).name} is gzipped; reading it whole with Table.read")
        return Table.read(path, format='fits', hdu=hdu)

    with fits.open(path, memmap=True, lazy_load_hdus=True) as hdul:
        table_hdu = hdul[hdu]
        if not isinstance(table_hdu, fits.BinTableHDU) or isinstance(table_hdu, fits.CompImageHDU):
            # Default hdu=1 but the table lives elsewhere (an image first), or
            # an ASCII table: use the first binary table, else fall back.
            bin_tables = [i for i, h in enumerate(hdul)
                          if isinstance(h, fits.BinTableHDU) and not isinstance(h, fits.CompImageHDU)]
            if not bin_tables:
                print(f"    NOTE: {Path(path).name} HDU {hdu} is not a binary table; "
                      f"reading it whole with Table.read")
                return Table.read(path, format='fits', hdu=hdu)
            print(f"    NOTE: HDU {hdu} of {Path(path).name} is not a binary table; "
                  f"using HDU {bin_tables[0]}")
            table_hdu = hdul[bin_tables[0]]
        info = table_hdu.fileinfo()
        offset, span = info['datLoc'], info['datSpan']
        # ColDefs.dtype describes the record in native byte order; the bytes
        # on disk are FITS big-endian.
        row_dtype = table_hdu.columns.dtype.newbyteorder('>')
        n_rows = int(table_hdu.header['NAXIS2'])
        naxis1 = int(table_hdu.header['NAXIS1'])
        decoders = {
            col.name: _fits_column_decoder(col)
            for col in table_hdu.columns if col.name in columns
        }
    row_size = row_dtype.itemsize
    if row_size != naxis1:
        raise ValueError(f"{path}: record dtype is {row_size} bytes but NAXIS1={naxis1}")
    if row_size * n_rows > span:
        raise ValueError(f"{path}: table data shorter than NAXIS1×NAXIS2")

    present = set(row_dtype.names)
    keep = [c for c in columns if c in present]
    parts: dict[str, list[np.ndarray]] = {c: [] for c in keep}
    rows_per_chunk = max(1, READ_CATALOG_CHUNK_BYTES // row_size)

    with open(path, 'rb', buffering=0) as f:
        _disable_read_cache(f.fileno())
        f.seek(offset)
        remaining = n_rows
        while remaining > 0:
            n = min(rows_per_chunk, remaining)
            buf = f.read(n * row_size)
            if len(buf) != n * row_size:
                raise ValueError(f"{path}: short read inside the table data")
            rec = np.frombuffer(buf, dtype=row_dtype)
            for c in keep:
                # Decode out of the chunk (a copy in native byte order) so
                # the buffer can be released.
                parts[c].append(decoders[c](rec[c]))
            remaining -= n

    out = Table()
    for c in keep:
        out[c] = (np.concatenate(parts[c]) if parts[c]
                  else decoders[c](np.empty(0, dtype=row_dtype[c])))
    return out


def _is_gzipped(path: str) -> bool:
    with open(path, 'rb') as f:
        return f.read(2) == b'\x1f\x8b'


# FITS integer "pseudo-unsigned" convention: TZERO = 2**(bits-1) on a signed
# column means the values are unsigned of the same width. Table.read gives
# those dtypes back; a float would change an id column's str() and lose
# 64-bit low bits. (The signed-byte convention, TZERO = -128 on 'B', is
# left as the scaled float astropy also returns.)
_UNSIGNED_CONVENTION = {'I': (1 << 15, np.uint16), 'J': (1 << 31, np.uint32), 'K': (1 << 63, np.uint64)}


def _fits_column_decoder(col):
    """A function turning the raw on-disk values of a FITS table column into
    what ``Table.read`` would hand back: ``str`` for ``A``, ``bool`` for
    ``L``, unsigned integers for the TZERO = 2**(bits-1) convention, scaled
    floats for any other ``TSCAL``/``TZERO``, native-order numbers
    otherwise."""
    fmt = str(col.format)
    code = fmt.lstrip('0123456789')[:1]
    if code in ('X', 'P', 'Q'):
        raise ValueError(
            f"Column '{col.name}' has FITS format {fmt}, which read_catalog "
            f"does not support (bit arrays and variable-length arrays)")
    bscale = col.bscale if col.bscale not in (None, '') else 1
    bzero = col.bzero if col.bzero not in (None, '') else 0

    def decode(raw: np.ndarray) -> np.ndarray:
        if code == 'A':
            # Fixed-width ASCII, space padded on disk.
            return np.char.rstrip(raw.astype(str))
        if code == 'L':
            return raw.astype(np.uint8) == ord('T')
        vals = raw.astype(raw.dtype.newbyteorder('='), copy=True)
        if bscale == 1 and code in _UNSIGNED_CONVENTION and bzero == _UNSIGNED_CONVENTION[code][0]:
            offset, utype = _UNSIGNED_CONVENTION[code]
            # Wrapping add in the unsigned type: signed + 2**(bits-1).
            return vals.view(utype) + utype(offset)
        if bscale != 1 or bzero != 0:
            vals = vals.astype(np.float64) * bscale + bzero
        return vals

    return decode


def _disable_read_cache(fd: int) -> None:
    """Ask the OS not to keep this file's pages in the cache. Best effort:
    macOS honours F_NOCACHE; elsewhere only a POSIX_FADV_SEQUENTIAL hint is
    given (read-ahead, earlier eviction), so the cache still fills there."""
    try:
        import fcntl
        if hasattr(fcntl, 'F_NOCACHE'):
            fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
    except Exception:
        pass
    try:
        import os
        if hasattr(os, 'posix_fadvise'):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
    except Exception:
        pass


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
    max_flux_err: float | None = None,
) -> dict:
    """Build JSONB payload for one object's photometry.

    band_config: mapping of band_name → {flux: col, err: col}
    max_flux_err: optional ceiling on the *catalog-unit* error; a band whose
        error exceeds it is dropped. Catalogs mark "no coverage" with a huge
        error rather than NaN (UNICORN writes ~1e12 nJy), and such a band
        must not reach the SED as a real measurement.
    """
    bands = {}
    for band_name, columns in band_config.items():
        flux_col = columns.get('flux') or columns.get('f')
        err_col = columns.get('err') or columns.get('e')

        if flux_col not in catalog_row or err_col not in catalog_row:
            continue

        raw_flux = float(catalog_row[flux_col])
        raw_err = float(catalog_row[err_col])

        if max_flux_err is not None and (
            not np.isfinite(raw_err) or abs(raw_err) > max_flux_err
        ):
            continue

        flux_ujy, err_ujy = convert_flux_to_ujy(raw_flux, raw_err, flux_unit)

        if not np.isfinite(flux_ujy) or not np.isfinite(err_ujy):
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

    def prefetch(self, catalog_ids) -> int:
        """No-op: the Lazy.jl file is fully loaded at construction."""
        return 0

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

    def generate_sidecar(
        self, catalog_id: int | str, scale: float | None = None,
    ) -> dict | None:
        """Generate P(z) + template sidecar JSON for one object.

        Returns flat dict with label, color, z_best, chi2, z_grid, pz,
        template_wav, template_flux_ujy. Returns None if ID not found.
        *scale* is accepted for interface parity and ignored.
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
# Photo-z: UNICORN release reader
# ---------------------------------------------------------------------------

def _squeeze_leading(arr: np.ndarray, ndim: int) -> np.ndarray:
    """Drop leading singleton axes until *arr* has *ndim* dimensions.

    A one-row FITS table column with TDIM comes back as ``(1, ...)``; the
    template cube and its grids are stored that way.
    """
    while arr.ndim > ndim and arr.shape[0] == 1:
        arr = arr[0]
    return arr


def coerce_catalog_id(raw) -> int | None:
    """The integer a catalog id refers to, or None when it is not integral.

    UNICORN ids are integers; a catalog whose id column is text (``'000123'``
    still counts, ``'GN-123'`` does not) simply gets no photo-z rather than
    an exception in the per-object loop. One rule shared by the deploy's
    prefetch gate and the reader's lookups so they can never disagree.
    """
    if isinstance(raw, (bool, np.bool_)):
        return None
    if isinstance(raw, (int, np.integer)):
        return int(raw)
    if isinstance(raw, (float, np.floating)):
        return int(raw) if np.isfinite(raw) and float(raw).is_integer() else None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _photoz_key(raw):
    """The value the deploy hands a photo-z reader for a catalog id.

    Numeric ids become ``int`` (``None`` for NaN or a non-integral float);
    text ids are passed through untouched so each reader applies its own
    rule — :class:`PhotozData` keys a text id column by the raw string,
    :class:`UnicornPhotozData` coerces it with :func:`coerce_catalog_id`.
    """
    if isinstance(raw, (bool, np.bool_)):
        return None
    if isinstance(raw, (int, float, np.integer, np.floating)):
        return coerce_catalog_id(raw)
    return raw


def _thin_to(n_max: int, *arrays: np.ndarray) -> tuple[np.ndarray, ...]:
    """Subsample parallel arrays to at most *n_max* points (uniform stride)."""
    n = len(arrays[0])
    if n <= n_max:
        return arrays
    step = int(math.ceil(n / n_max))
    return tuple(a[::step] for a in arrays)


class UnicornPhotozData:
    """Pre-loaded UNICORN release photo-z data (a Lazy.jl run packaged by
    Finkelstein et al.), with the same ``lookup`` / ``generate_sidecar``
    surface as :class:`PhotozData`.

    ``<prefix>_photz_v<ver>.fits`` layout:
      - ext 1: per-source scalars — ``ID``, ``ZA`` (chi²-minimum redshift),
        ``ZM``, ``CHIA``, ``ZL68`` / ``ZU68`` (68 % bounds), ``COEFFS[n_templ]``,
        ``Z_LOWZ``, ``COEFFS_LOWZ``, …
      - ext 2: best-fit model flux per filter (nJy) — unused here
      - ext 3: one row per redshift-grid point: ``ZGRID`` scalar plus
        ``PZ[n_obj]`` and ``CHI2[n_obj]`` arrays, so P(z) for source *i* is
        the column ``PZ[:, i]``
      - ext 4: the z < 7 restricted run — unused here

    The template library is a separate cube (``unicorn_templates_fiducial.fits``):
    ext 1 ``WAVE`` (rest-frame Å), ext 2 ``ZGRID``, ext 3 ``TNAME``, ext 4
    ``FLUX`` in nJy with one table row per template holding a contiguous
    ``(n_z, n_wave)`` block (astropy sees ``(n_templ, n_z, n_wave)``). That is
    the only layout accepted — the plane at a grid redshift is then ``n_templ``
    positioned reads of ``n_wave`` floats, and a differently written cube
    fails loudly instead of being read whole. The model SED at ``ZA`` is
    ``coeffs @ FLUX[:, iz, :]``.

    Memory: a release is 4–8 GB, most of it the P(z) block (``n_z`` rows of
    ``n_obj`` floats) and the 1.3 GB cube, and a memory map touched for a
    few thousand sources drags gigabytes through the page cache — enough to
    get a deploy killed on a laptop. So the scalars are copied out once and
    the file closed; P(z) for the sources of interest is gathered by one
    streaming pass over the ``PZ`` span of every row (:meth:`prefetch`,
    called by the deploy with every matched id) with caching disabled; and
    template planes are fetched with positioned reads and a small LRU.
    Nothing large stays mapped.

    Config keys (``[field.photoz]``): ``format = "unicorn"``, ``file``,
    ``templates``; optional ``label``, ``color``, ``id_column`` (ID),
    ``z_best_column`` (ZA), ``chi2_column`` (CHIA), ``z_lo_column`` (ZL68),
    ``z_hi_column`` (ZU68), ``coeffs_column`` (COEFFS), ``pz_ext`` (3),
    ``template_wav_min_um`` / ``template_wav_max_um`` (0.2 / 6.0, observed
    frame), ``max_template_points`` (2500).
    """

    _PLANE_CACHE = 128

    def __init__(self, photoz_config: dict):
        self.label = photoz_config.get('label', 'UNICORN photo-z')
        self.color = photoz_config.get('color', '#8e6bb8')

        self._pz_file = photoz_config['file']
        templates_file = photoz_config.get('templates')
        id_col = photoz_config.get('id_column', 'ID')
        z_col = photoz_config.get('z_best_column', 'ZA')
        chi2_col = photoz_config.get('chi2_column', 'CHIA')
        zlo_col = photoz_config.get('z_lo_column', 'ZL68')
        zhi_col = photoz_config.get('z_hi_column', 'ZU68')
        coeffs_col = photoz_config.get('coeffs_column', 'COEFFS')
        pz_ext = photoz_config.get('pz_ext', 3)
        self._wav_min = float(photoz_config.get('template_wav_min_um', 0.2))
        self._wav_max = float(photoz_config.get('template_wav_max_um', 6.0))
        self._max_points = int(photoz_config.get('max_template_points', 2500))

        print(f"  Loading UNICORN photo-z data: {self._pz_file}")
        with fits.open(self._pz_file, memmap=True, lazy_load_hdus=True) as hdul:
            main = hdul[1].data
            names = set(main.dtype.names)
            for col in (id_col, z_col, chi2_col, coeffs_col):
                if col not in names:
                    raise ValueError(
                        f"UNICORN photo-z file lacks column '{col}' (ext 1 has "
                        f"{sorted(names)[:12]}…)")
            self._ids = np.asarray(main[id_col]).astype(np.int64)
            self._z = np.asarray(main[z_col], dtype=float)
            self._chi2 = np.asarray(main[chi2_col], dtype=float)
            self._zlo = np.asarray(main[zlo_col], dtype=float) if zlo_col in names else None
            self._zhi = np.asarray(main[zhi_col], dtype=float) if zhi_col in names else None
            self._coeffs = np.asarray(main[coeffs_col], dtype=float)  # (n_obj, n_templ)

            # P(z) block: remember its on-disk layout for the streaming pass.
            pz_hdu = hdul[pz_ext]
            pz_dtype = pz_hdu.columns.dtype.newbyteorder('>')
            if 'ZGRID' not in pz_dtype.names or 'PZ' not in pz_dtype.names:
                raise ValueError("UNICORN P(z) extension needs ZGRID and PZ columns")
            n_z = int(pz_hdu.header['NAXIS2'])
            if pz_dtype['PZ'].shape != (len(self._ids),):
                raise ValueError(
                    f"UNICORN P(z) rows carry {pz_dtype['PZ'].shape} values; "
                    f"expected ({len(self._ids)},)")
            self._pz_layout = (
                pz_hdu.fileinfo()['datLoc'], pz_dtype.itemsize,
                pz_dtype.fields['PZ'][1], pz_dtype['PZ'].base, n_z,
            )
            # ZGRID is one scalar per row: a strided read of n_z pages, cheap.
            self.z_grid = np.asarray(pz_hdu.data['ZGRID'], dtype=float).ravel()

        self._id_to_idx: dict[int, int] = {
            int(v): i for i, v in enumerate(self._ids)
        }
        self._pz_cache: dict[int, np.ndarray] = {}

        # Template cube (optional: without it sidecars carry P(z) only).
        self._templates_file = None
        self._plane_cache: dict[int, np.ndarray] = {}
        if templates_file:
            print(f"  Loading UNICORN template cube: {templates_file}")
            with fits.open(templates_file, memmap=True, lazy_load_hdus=True) as thdul:
                wave = _squeeze_leading(np.asarray(thdul[1].data['WAVE']), 1)
                zgrid_t = _squeeze_leading(np.asarray(thdul[2].data['ZGRID']), 1)
                cube_hdu = thdul[4]
                cube_dtype = cube_hdu.columns.dtype.newbyteorder('>')
                n_rows = int(cube_hdu.header['NAXIS2'])
                col_name = cube_dtype.names[0]
                row_shape = tuple(cube_dtype[col_name].shape)
                cube_offset = cube_hdu.fileinfo()['datLoc']
            n_t = int(self._coeffs.shape[1])
            n_z, n_w = len(zgrid_t), len(wave)
            if n_rows != n_t or row_shape != (n_z, n_w):
                raise ValueError(
                    f"Template cube must have one row per template with a "
                    f"({n_z}, {n_w}) block; found {n_rows} rows of {row_shape} "
                    f"(n_templ={n_t} from COEFFS)")
            self._templates_file = templates_file
            self._cube_layout = (
                cube_offset, cube_dtype.itemsize, cube_dtype[col_name].base, n_t, n_w,
            )
            self._lam_rest = np.asarray(wave, dtype=float)   # Å
            self._zgrid_t = np.asarray(zgrid_t, dtype=float)

        print(f"    {len(self._id_to_idx)} sources loaded")

    # -- P(z) -------------------------------------------------------------

    def prefetch(self, catalog_ids) -> int:
        """Gather P(z) for *catalog_ids* in one streaming pass over the block.

        Rows are read sequentially in ~64 MB chunks with caching disabled,
        and only the wanted columns are kept (n_z × n_wanted floats). Ids
        not in the release are ignored. Returns the number gathered.
        """
        wanted: set[int] = set()
        n_bad = 0
        for cid in catalog_ids:
            cid_int = coerce_catalog_id(cid)
            if cid_int is None:
                n_bad += 1
                continue
            idx = self._id_to_idx.get(cid_int)
            if idx is not None and idx not in self._pz_cache:
                wanted.add(idx)
        if n_bad:
            print(f"    WARNING: {n_bad} catalog ids are not integers and get no "
                  f"UNICORN photo-z")
        wanted = sorted(wanted)
        if not wanted:
            return 0
        offset, row_size, pz_offset, pz_dtype, n_z = self._pz_layout
        n_obj = len(self._ids)
        span = n_obj * pz_dtype.itemsize
        sel = np.asarray(wanted)
        out = np.empty((n_z, len(sel)), dtype=np.float32)
        with open(self._pz_file, 'rb', buffering=0) as f:
            _disable_read_cache(f.fileno())
            for i in range(n_z):
                # Only the PZ field of each row; CHI2 (twice its size) is skipped.
                f.seek(offset + i * row_size + pz_offset)
                buf = f.read(span)
                if len(buf) != span:
                    raise ValueError(f"{self._pz_file}: short read inside the P(z) block")
                out[i] = np.frombuffer(buf, dtype=pz_dtype)[sel]
        for j, idx in enumerate(wanted):
            self._pz_cache[idx] = out[:, j].copy()
        return len(wanted)

    def _pz_for(self, idx: int) -> np.ndarray:
        if idx not in self._pz_cache:
            self.prefetch([int(self._ids[idx])])
        return self._pz_cache[idx]

    # -- Templates --------------------------------------------------------

    def _templates_at(self, z: float) -> np.ndarray:
        """Template basis at the grid redshift nearest *z*, shape (n_templ, n_wave), nJy."""
        iz = int(np.argmin(np.abs(self._zgrid_t - z)))
        plane = self._plane_cache.get(iz)
        if plane is None:
            plane = self._read_plane(iz)
            if len(self._plane_cache) >= self._PLANE_CACHE:
                self._plane_cache.pop(next(iter(self._plane_cache)))
            self._plane_cache[iz] = plane
        return plane

    def _read_plane(self, iz: int) -> np.ndarray:
        """Templates at grid index *iz*: n_templ positioned reads of n_wave values."""
        offset, row_size, base, n_t, n_w = self._cube_layout
        span = n_w * base.itemsize
        plane = np.empty((n_t, n_w), dtype=float)
        with open(self._templates_file, 'rb', buffering=0) as f:
            _disable_read_cache(f.fileno())
            for t in range(n_t):
                f.seek(offset + t * row_size + iz * span)
                buf = f.read(span)
                if len(buf) != span:
                    raise ValueError(f"{self._templates_file}: short read inside the template cube")
                plane[t] = np.frombuffer(buf, dtype=base)
        return plane

    # -- Public surface ---------------------------------------------------

    def lookup(self, catalog_id: int | str) -> dict | None:
        """Photo-z scalars for a catalog ID, or None if absent / non-finite.

        ``z_err_lo`` / ``z_err_hi`` are the absolute 68 % bounds (the web
        panel prints them as a range), matching :class:`PhotozData`.
        """
        cid = coerce_catalog_id(catalog_id)
        idx = self._id_to_idx.get(cid) if cid is not None else None
        if idx is None:
            return None
        z_best = float(self._z[idx])
        if not np.isfinite(z_best):
            return None
        result: dict = {'z_best': z_best, 'chi2': float(self._chi2[idx])}
        if self._zlo is not None and np.isfinite(self._zlo[idx]):
            result['z_err_lo'] = float(self._zlo[idx])
        if self._zhi is not None and np.isfinite(self._zhi[idx]):
            result['z_err_hi'] = float(self._zhi[idx])
        return result

    def generate_sidecar(
        self, catalog_id: int | str, scale: float | None = None,
    ) -> dict | None:
        """P(z) + template-SED sidecar (same keys as :class:`PhotozData`).

        *scale* multiplies the model SED (UNICORN's ``SCALE_MODEL`` maps the
        fitted model onto the corrected fluxes of sources whose Kron aperture
        was replaced; 1 for everything else).
        """
        cid = coerce_catalog_id(catalog_id)
        idx = self._id_to_idx.get(cid) if cid is not None else None
        if idx is None:
            return None
        z_best = float(self._z[idx])
        if not np.isfinite(z_best):
            return None

        result: dict = {
            'label': self.label,
            'color': self.color,
            'z_best': z_best,
            'chi2': float(self._chi2[idx]),
        }

        pz = np.asarray(self._pz_for(idx), dtype=float)
        pz = np.where(np.isfinite(pz), pz, 0.0)
        pz_max = pz.max() if pz.size else 0.0
        if pz_max > 0:
            pz = pz / pz_max
        result['z_grid'] = np.round(self.z_grid, 4).tolist()
        result['pz'] = np.round(pz, 5).tolist()

        if self._templates_file is not None:
            coeffs = self._coeffs[idx]
            fnu_njy = coeffs @ self._templates_at(z_best)
            if scale is not None and np.isfinite(scale) and scale > 0:
                fnu_njy = fnu_njy * scale
            lam_obs = self._lam_rest * (1.0 + z_best) / 1e4  # µm, observed
            valid = (
                np.isfinite(fnu_njy) & (fnu_njy > 0)
                & (lam_obs >= self._wav_min) & (lam_obs <= self._wav_max)
            )
            if np.any(valid):
                lam, fnu = _thin_to(self._max_points, lam_obs[valid], fnu_njy[valid] / 1e3)
                result['template_wav'] = np.round(lam, 5).tolist()
                result['template_flux_ujy'] = [float(f'{v:.5g}') for v in fnu]

        return result


def load_photoz(photoz_config: dict):
    """Instantiate the photo-z reader named by ``[field.photoz].format``."""
    fmt = str(photoz_config.get('format', 'lazy')).lower()
    if fmt == 'unicorn':
        return UnicornPhotozData(photoz_config)
    if fmt in ('lazy', 'lazy.jl'):
        return PhotozData(photoz_config)
    raise ValueError(f"Unknown photoz format '{fmt}' (expected 'lazy' or 'unicorn')")


def photoz_input_files(photoz_config: dict) -> list[str]:
    """Paths a photo-z config needs on disk (for the pre-flight existence check)."""
    files = [photoz_config.get('file')]
    if str(photoz_config.get('format', 'lazy')).lower() == 'unicorn':
        files.append(photoz_config.get('templates'))
    return [f for f in files if f]


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


def _supersede_other_catalogs(
    client: Client,
    field: str,
    keep_catalog_name: str,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Delete every photometry row in *field* whose catalog_name differs from
    *keep_catalog_name*.

    The upsert key includes ``catalog_name``, so a new release (e.g.
    ``UNICORN EGS v0.98``) lands beside the rows of the one it replaces
    (``UNICORN EGS v0.9``), and ``--prune`` only cleans within one catalog.
    This is the explicit retirement of the old release, run after the new
    rows are in place so no object loses photometry in between.

    With *dry_run* nothing is deleted; the counts are still gathered so a
    ``--dry-run --supersede`` previews what would go.

    Under the default login session this runs through RLS, whose SELECT
    policy hides rows with ``object_id IS NULL`` (orphans left by a field
    rebuild's relink). Those stay until a service-role run or a manual
    delete; the deploy prints nothing about them because it cannot see them.

    Returns ``{catalog_name: n_rows}`` for the retired (or to-be-retired)
    catalogs.
    """
    page_size = 1000
    offset = 0
    to_delete: dict[str, list[int]] = defaultdict(list)
    while True:
        resp = (
            client.table('object_photometry')
            .select('id, catalog_name')
            .eq('field', field)
            .neq('catalog_name', keep_catalog_name)
            .order('id')
            .range(offset, offset + page_size - 1)
            .execute()
        )
        rows = resp.data
        if not rows:
            break
        for r in rows:
            to_delete[r['catalog_name']].append(r['id'])
        if len(rows) < page_size:
            break
        offset += page_size

    deleted: dict[str, int] = {}
    for name, ids in to_delete.items():
        if not dry_run:
            for i in range(0, len(ids), BATCH_SIZE):
                chunk = ids[i:i + BATCH_SIZE]
                client.table('object_photometry').delete().in_('id', chunk).execute()
        deleted[name] = len(ids)
    return deleted


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
    supersede: bool = False,
    supersede_force: bool = False,
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
        supersede: When True (and `restrict_to_object_db_ids` is None), after
            upsert delete every row in the field belonging to a *different*
            catalog_name — the explicit retirement of a previous release.
            Refused when the new release matched fewer than
            ``SUPERSEDE_MIN_RATIO`` × the rows it would retire (a wrong column
            name, path or radius looks exactly like that) unless
            `supersede_force` is set.
        supersede_force: Retire the other catalogs even when the new match
            count is far below theirs.

    Returns:
        Dict with keys: n_objects, n_matched, n_bands, n_pz, n_superseded
    """
    # Empty restriction: nothing to do, skip all I/O.
    empty = {'n_objects': 0, 'n_matched': 0, 'n_bands': 0, 'n_pz': 0, 'n_superseded': 0}
    if restrict_to_object_db_ids is not None and not restrict_to_object_db_ids:
        return empty

    field_config = load_field_config(photometry_config_path, field)
    if field_config is None:
        print(f"  No photometry config for field '{field}'. Skipping.")
        return empty

    # Load catalog
    catalog_path = field_config['catalog']
    catalog_name = field_config.get('catalog_name', Path(catalog_path).stem)
    fmt = field_config.get('format', 'fits')
    flux_unit = field_config.get('flux_unit', 'uJy')
    ra_col = field_config.get('ra_column', 'ra')
    dec_col = field_config.get('dec_column', 'dec')
    id_col = field_config.get('id_column', 'id')
    radius = field_config.get('match_radius_arcsec', 0.3)
    max_flux_err = field_config.get('max_flux_err')
    band_config = field_config.get('bands', {})
    photoz_config = field_config.get('photoz')
    scale_col = (photoz_config or {}).get('scale_column')

    print(f"  Loading catalog: {catalog_path}")
    wanted = catalog_columns_needed(field_config)
    catalog = read_catalog(catalog_path, fmt, wanted)
    for col in (ra_col, dec_col):
        if col not in catalog.colnames:
            raise ValueError(f"Catalog {catalog_path} has no '{col}' column")
    missing = [c for c in wanted if c not in catalog.colnames]
    if missing:
        print(f"    WARNING: {len(missing)} configured columns absent from the "
              f"catalog: {missing[:8]}{'…' if len(missing) > 8 else ''}")
    print(f"    {len(catalog)} sources, {len(band_config)} bands configured")

    # Photo-z is loaded lazily after cross-match (only if any kept match
    # would actually use it — Lazy.jl FITS load is expensive).

    # Fetch objects
    print(f"  Fetching objects for field '{field}'...")
    objects = _fetch_field_objects(client, field)
    if not objects:
        print(f"  No objects in field '{field}'. Nothing to do.")
        return empty
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
        n_would_retire = 0
        if supersede and restrict_to_object_db_ids is None:
            would = _supersede_other_catalogs(client, field, catalog_name, dry_run=True)
            for name, n in would.items():
                print(f"    Would retire {n} rows of '{name}'")
            n_would_retire = sum(would.values())
            if n_would_retire:
                print(f"    New matches / rows to retire: {n_reported} / {n_would_retire} "
                      f"= {n_reported / n_would_retire:.2f}"
                      + ("" if n_reported >= SUPERSEDE_MIN_RATIO * n_would_retire
                         else f"  (below {SUPERSEDE_MIN_RATIO}: --supersede would be "
                              f"refused without --force)"))
            if not matches:
                print("    (no matches: --supersede would be skipped, not run)")
        return {
            'n_objects': len(objects),
            'n_matched': n_reported,
            'n_bands': len(band_config),
            'n_pz': 0,
            'n_superseded': n_would_retire,
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
    photoz: PhotozData | UnicornPhotozData | None = None
    if include_photoz and kept_matches and photoz_config:
        needed = photoz_input_files(photoz_config)
        missing = [f for f in needed if not Path(f).exists()]
        if needed and not missing:
            photoz = load_photoz(photoz_config)
        else:
            print(f"  WARNING: Photo-z input not found: {missing or '(no file configured)'}")
    elif include_photoz and kept_matches and not photoz_config:
        print(f"  No [photoz] config for field '{field}'. Skipping photo-z.")

    # Gather P(z) for every kept match up front (one streaming pass for a
    # UNICORN release; a no-op for a fully loaded Lazy.jl file) so the
    # per-object loop never touches the multi-GB block.
    if photoz is not None:
        ids_needed = []
        for _obj_idx, cat_idx, _dist in kept_matches:
            raw = catalog[id_col][cat_idx] if id_col in catalog.colnames else cat_idx
            key = _photoz_key(raw)
            if key is not None:
                ids_needed.append(key)
        print(f"  Gathering P(z) for {len(ids_needed)} matched sources...")
        n_got = photoz.prefetch(ids_needed)
        if n_got:
            print(f"    {n_got} found in the photo-z release")

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
        pz_key = _photoz_key(cat_id_raw)
        cat_id = str(cat_id_raw)

        # Build photometry payload
        payload = build_photometry_payload(
            cat_row, band_config, flux_unit, max_flux_err=max_flux_err,
        )

        # Photo-z from Lazy.jl
        photo_z = None
        photo_z_err_lo = None
        photo_z_err_hi = None
        has_pz = False

        if photoz is not None and pz_key is not None:
            pz_meta = photoz.lookup(pz_key)
            if pz_meta is not None:
                photo_z = pz_meta['z_best']
                photo_z_err_lo = pz_meta.get('z_err_lo')
                photo_z_err_hi = pz_meta.get('z_err_hi')

                # Generate P(z) sidecar
                scale = None
                if scale_col and scale_col in cat_row:
                    scale = float(cat_row[scale_col])
                sidecar = photoz.generate_sidecar(pz_key, scale=scale)
                if sidecar is not None:
                    has_pz = True
                    n_pz += 1
                    r2_key = storage_key('photometry_pz', Scope(field=field, object_id=obj['object_id']), scheme=KeyScheme.CANONICAL)
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

    # Upload P(z) sidecars to OSN (epic #210 / #216 — deploy → OSN). photometry_pz
    # is a migrated data-bucket product, so it writes CANONICAL keys to OSN with
    # backend='osn', matching the migrated registry row in place (see the NIRSpec
    # paths in deploy.py).
    if upload_tasks:
        print(f"  Uploading {len(upload_tasks)} P(z) sidecars to OSN...")
        uploaded_keys: set[str] = set()
        success, failed, errors = upload_files_parallel(
            deploy_config, upload_tasks, desc="P(z) sidecars",
            succeeded_out=uploaded_keys, backend='osn',
        )
        if failed:
            print(f"    WARNING: {failed} sidecar uploads failed")
            for err in errors[:5]:
                print(f"      {err}")

        # Storage registry (#214): index the P(z) sidecars that landed.
        if uploaded_keys:
            from campfire.deploy.registry import (
                build_registry_rows, upsert_storage_objects,
            )
            from campfire.deploy.supabase import get_user_id_from_token
            reg_rows = build_registry_rows(
                upload_tasks,
                backend='osn',
                uploaded_by=get_user_id_from_token(deploy_config),
                succeeded_keys=uploaded_keys,
            )
            upsert_storage_objects(client, reg_rows)

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

    n_superseded = 0
    if supersede and restrict_to_object_db_ids is None:
        if not records:
            # A cross-match that found nothing is far more likely a wrong
            # column name, path or radius than an empty field; retiring the
            # previous catalog on top of it would silently wipe the field's
            # photometry. Leave the old rows in place.
            print(f"  WARNING: no rows were upserted for '{catalog_name}'; "
                  f"skipping --supersede so the existing catalogs stay.")
        else:
            would = _supersede_other_catalogs(client, field, catalog_name, dry_run=True)
            n_would = sum(would.values())
            if not would:
                print(f"  No other catalogs in field '{field}' to retire")
            elif len(records) < SUPERSEDE_MIN_RATIO * n_would and not supersede_force:
                print(f"  WARNING: '{catalog_name}' matched {len(records)} objects but "
                      f"retiring {', '.join(would)} would delete {n_would} rows "
                      f"(ratio {len(records) / n_would:.2f} < {SUPERSEDE_MIN_RATIO}). "
                      f"That looks like a misconfigured cross-match; skipping "
                      f"--supersede. Pass --force to retire anyway.")
            else:
                print(f"  Retiring other catalogs in field '{field}' "
                      f"(keeping '{catalog_name}')...")
                retired = _supersede_other_catalogs(client, field, catalog_name)
                for name, n in retired.items():
                    print(f"    Deleted {n} rows of '{name}'")
                n_superseded = sum(retired.values())

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
        'n_superseded': n_superseded,
    }
