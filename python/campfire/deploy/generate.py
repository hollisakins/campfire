"""
Content generation from FITS files.

Produces SVG thumbnails, spectrum JSON (for Plotly), and zfit JSON
(chi2 grid + model). These operations require direct FITS access
because the ECSV doesn't contain spectrum arrays.
"""

import json
from pathlib import Path

import numpy as np
from astropy.io import fits


# ---------------------------------------------------------------------------
# SVG thumbnails
# ---------------------------------------------------------------------------

SVG_WIDTH = 120
SVG_HEIGHT = 40
SVG_PADDING = 3


def convert_fnu_to_flambda(fnu_val: float, wavelength: float) -> float:
    """
    Convert f_nu to f_lambda.

    f_nu in uJy, wavelength in um.
    f_lambda (erg/s/cm2/A) = f_nu (uJy) * 2.998e-19 / lambda_um^2
    """
    return fnu_val * 2.998e-19 / (wavelength * wavelength)


def generate_spectrum_thumbnail_svg(
    wave: list,
    fnu: list,
    flux_unit: str = 'fnu',
    color: str = '#3b82f6',
) -> str:
    """
    Generate an SVG sparkline thumbnail from spectrum data.

    Args:
        wave: Wavelength array (microns)
        fnu: Flux array in f_nu units (may contain NaN/None)
        flux_unit: 'fnu' or 'flambda'
        color: SVG stroke color

    Returns:
        SVG string
    """
    # Filter out invalid values
    valid_pairs = []
    for w, f in zip(wave, fnu):
        if f is not None and not np.isnan(f) and np.isfinite(f):
            flux_val = convert_fnu_to_flambda(float(f), float(w)) if flux_unit == 'flambda' else float(f)
            valid_pairs.append((w, flux_val))

    if not valid_pairs:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" '
            f'width="{SVG_WIDTH}" height="{SVG_HEIGHT}">\n'
            f'  <line x1="{SVG_PADDING}" y1="{SVG_HEIGHT // 2}" '
            f'x2="{SVG_WIDTH - SVG_PADDING}" y2="{SVG_HEIGHT // 2}" '
            f'stroke="{color}" stroke-opacity="0.3" stroke-width="1"/>\n'
            f'</svg>'
        )

    # Downsample to ~100 points
    if len(valid_pairs) > 100:
        step = len(valid_pairs) // 100
        downsampled = [valid_pairs[i] for i in range(0, len(valid_pairs), step)]
        if downsampled[-1] != valid_pairs[-1]:
            downsampled.append(valid_pairs[-1])
        valid_pairs = downsampled

    flux_values = [f for _, f in valid_pairs]
    min_f = min(flux_values)
    max_f = max(flux_values)
    flux_range = max_f - min_f

    if flux_range == 0:
        y = SVG_HEIGHT // 2
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" '
            f'width="{SVG_WIDTH}" height="{SVG_HEIGHT}">\n'
            f'  <line x1="{SVG_PADDING}" y1="{y}" x2="{SVG_WIDTH - SVG_PADDING}" y2="{y}" '
            f'stroke="{color}" stroke-width="1.5"/>\n'
            f'</svg>'
        )

    plot_w = SVG_WIDTH - 2 * SVG_PADDING
    plot_h = SVG_HEIGHT - 2 * SVG_PADDING

    path_points = []
    for i, (_, flux) in enumerate(valid_pairs):
        x = SVG_PADDING + (i / (len(valid_pairs) - 1)) * plot_w
        norm_y = (flux - min_f) / flux_range
        y = SVG_PADDING + (1 - norm_y) * plot_h
        cmd = 'M' if i == 0 else 'L'
        path_points.append(f'{cmd} {x:.1f} {y:.1f}')

    path = ' '.join(path_points)

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" '
        f'width="{SVG_WIDTH}" height="{SVG_HEIGHT}">\n'
        f'  <path d="{path}" fill="none" stroke="{color}" '
        f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>\n'
        f'</svg>'
    )


def generate_thumbnails_from_fits(fits_path: Path) -> dict[str, str]:
    """
    Open a FITS file and return both fnu and flambda SVG thumbnails.

    Returns:
        {'thumbnail_svg_fnu': ..., 'thumbnail_svg_flambda': ...}
    """
    with fits.open(fits_path) as hdul:
        spec1d = hdul['SPEC1D'].data
        wave = spec1d['wave'].tolist()
        fnu = spec1d['fnu'].tolist()

    return {
        'thumbnail_svg_fnu': generate_spectrum_thumbnail_svg(wave, fnu, flux_unit='fnu'),
        'thumbnail_svg_flambda': generate_spectrum_thumbnail_svg(wave, fnu, flux_unit='flambda'),
    }


# ---------------------------------------------------------------------------
# Spectrum JSON (1D + 2D S/N + profile)
# ---------------------------------------------------------------------------

def read_spectrum_data(fits_path: Path) -> dict:
    """
    Read spectrum data from a FITS file for JSON export.

    Returns 1D data (wave, fnu, fnu_err), 2D S/N heatmap data,
    and cross-dispersion profile data.
    """
    with fits.open(fits_path) as hdul:
        return _spectrum_data_from_hdul(hdul)


def _spectrum_data_from_hdul(hdul) -> dict:
    """JSON-export payload from an already-open HDUList (single-read path)."""
    spec1d = hdul['SPEC1D'].data
    sci = hdul['SCI'].data
    err = hdul['ERR'].data
    prof1d = hdul['PROF1D'].data

    # 1D spectrum. Any non-finite flux (NaN *or* ±inf) becomes JSON null —
    # inf is just as invalid as NaN under strict JSON, and json.dump below
    # runs with allow_nan=False.
    wave = [round(x, 6) for x in spec1d['wave'].tolist()]
    fnu = [None if not np.isfinite(x) else round(float(x), 6) for x in spec1d['fnu']]
    fnu_err = [None if not np.isfinite(x) else round(float(x), 6) for x in spec1d['fnu_err']]

    # 2D S/N
    with np.errstate(divide='ignore', invalid='ignore'):
        snr_2d = sci / err
        snr_2d = np.where(np.isfinite(snr_2d), snr_2d, 0)
    snr_2d_list = [[round(x, 2) for x in row] for row in snr_2d.tolist()]

    # Cross-dispersion profile
    with np.errstate(divide='ignore', invalid='ignore'):
        collapsed = np.nanmedian(sci, axis=1)

    ypos = prof1d['ypos']
    opt_weight = prof1d['opt']

    # An inf weight passes a bare `> 0` cut and would poison the centroid
    # (and with it every centered pixel), so require finiteness up front.
    valid_opt = np.isfinite(opt_weight) & np.isfinite(ypos) & (opt_weight > 0)
    if np.any(valid_opt):
        cen = np.average(ypos[valid_opt], weights=opt_weight[valid_opt])
    else:
        cen = np.median(ypos)

    pix_centered = ypos - cen
    # Backstop for the no-valid-weight fallback (all-NaN ypos): still never
    # let a non-finite coordinate reach the JSON.
    pix_centered = np.where(np.isfinite(pix_centered), pix_centered, 0.0)

    with np.errstate(divide='ignore', invalid='ignore'):
        collapsed_norm = collapsed / np.nanmax(np.abs(collapsed[valid_opt])) if np.any(valid_opt) else collapsed
        collapsed_norm = np.where(np.isfinite(collapsed_norm), collapsed_norm, 0)
        # Normalize by the finite max — np.nanmax ignores NaN but not inf,
        # and an inf denominator would flatten the whole profile to zero.
        opt_max = float(np.max(opt_weight[valid_opt])) if np.any(valid_opt) else 0.0
        opt_norm = opt_weight / opt_max if opt_max > 0 else opt_weight
        # opt weights can be non-finite at masked/edge pixels; a NaN here would
        # be serialized as the bare token `NaN` (invalid JSON) and make the
        # browser's JSON.parse reject the whole spectrum payload. Guard it the
        # same way as snr_2d / collapsed_norm above.
        opt_norm = np.where(np.isfinite(opt_norm), opt_norm, 0)

    return {
        'wave': wave,
        'fnu': fnu,
        'fnu_err': fnu_err,
        'snr_2d': snr_2d_list,
        'n_spatial': sci.shape[0],
        'n_wave': sci.shape[1],
        'profile': [round(float(x), 3) for x in collapsed_norm.tolist()],
        'profile_fit': [round(float(x), 3) for x in opt_norm.tolist()],
        'profile_pix': [round(float(x), 2) for x in pix_centered.tolist()],
    }


# Keys of the spectrum payload that make up the 1-D sidecar (perf T2-D2,
# #508): everything but the 2-D S/N array, which is 80-95 % of the bytes. The
# web fetches `<stem>_1d.json` first and paints the primary trace, then the
# full `<stem>.json` for the heatmap.
SPECTRUM_1D_KEYS = (
    'wave', 'fnu', 'fnu_err', 'n_spatial', 'n_wave',
    'profile', 'profile_fit', 'profile_pix',
)


def spectrum_1d_payload(data: dict) -> dict:
    """The 1-D-only subset of a spectrum JSON payload (no ``snr_2d``)."""
    return {k: data[k] for k in SPECTRUM_1D_KEYS if k in data}


def write_spectrum_jsons(data: dict, stem: str, output_dir: Path) -> tuple[Path, Path]:
    """Write the full spectrum JSON and its 1-D sidecar; returns both paths.

    ``stem`` is the FITS stem (``<...>_spec``), so the files land as
    ``<stem>.json`` and ``<stem>_1d.json`` — the ``spectrum_json`` /
    ``spectrum_1d_json`` layout siblings of the FITS.
    """
    json_path = output_dir / f'{stem}.json'
    json_1d_path = output_dir / f'{stem}_1d.json'
    with open(json_path, 'w') as f:
        # allow_nan=False: a non-finite value would otherwise be written as the
        # bare token `NaN`/`Infinity`, which is invalid JSON. The web render path
        # (/api/spectrum -> response.json(); browser JSON.parse) rejects such a
        # payload and the spectrum silently fails to render. Fail loudly here at
        # deploy time instead; arrays that can legitimately be non-finite are
        # sanitized in read_spectrum_data().
        json.dump(data, f, allow_nan=False)
    with open(json_1d_path, 'w') as f:
        json.dump(spectrum_1d_payload(data), f, allow_nan=False)
    return json_path, json_1d_path


def generate_spectrum_json(fits_path: Path, output_dir: Path) -> Path:
    """Generate the full spectrum JSON (and its 1-D sidecar) for Plotly.

    Returns the full JSON path; see :func:`generate_spectrum_jsons` for both.
    """
    return generate_spectrum_jsons(fits_path, output_dir)[0]


def generate_spectrum_jsons(fits_path: Path, output_dir: Path) -> tuple[Path, Path]:
    """Generate the full spectrum JSON and its 1-D sidecar from one FITS read."""
    data = read_spectrum_data(fits_path)
    return write_spectrum_jsons(data, fits_path.stem, output_dir)


def generate_spectrum_products(
    fits_path: Path, output_dir: Path,
) -> tuple[Path, Path, dict[str, str]]:
    """Generate ALL per-final derivatives in a single FITS read.

    One open yields the spectrum JSON + its 1-D sidecar (uploaded to storage)
    and the fnu/flambda SVG thumbnails (embedded in the spectra rows). Deploy
    previously ran two separate passes — 'Generating content' then
    'Generating thumbnails' — each re-opening every final off NFS.

    Returns ``(json_path, json_1d_path, thumbnails_dict)``.
    """
    with fits.open(fits_path) as hdul:
        spec1d = hdul['SPEC1D'].data
        wave_raw = spec1d['wave'].tolist()
        fnu_raw = spec1d['fnu'].tolist()
        data = _spectrum_data_from_hdul(hdul)

    json_path, json_1d_path = write_spectrum_jsons(data, fits_path.stem, output_dir)

    thumbs = {
        'thumbnail_svg_fnu': generate_spectrum_thumbnail_svg(
            wave_raw, fnu_raw, flux_unit='fnu'),
        'thumbnail_svg_flambda': generate_spectrum_thumbnail_svg(
            wave_raw, fnu_raw, flux_unit='flambda'),
    }
    return json_path, json_1d_path, thumbs


# ---------------------------------------------------------------------------
# Zfit JSON (chi2 grid + best-fit model)
# ---------------------------------------------------------------------------

def generate_zfit_json(zfit_path: Path, output_dir: Path) -> Path:
    """
    Generate JSON file with redshift fitting results.

    Includes best-fit redshift, chi2, confidence, full chi2 vs z curve,
    and best-fit model spectrum.
    """
    with fits.open(zfit_path) as hdul:
        primary = hdul['PRIMARY'].header

        chi2_data = hdul['CHI2'].data
        z_arr = np.asarray(chi2_data['z'], dtype=float)
        chi2_arr = np.asarray(chi2_data['chi2'], dtype=float)
        z_grid = [round(float(z), 4) for z in z_arr.tolist()]
        # Non-finite chi2 (masked/failed grid points) → JSON null; a bare
        # NaN/Infinity token would make the browser reject the whole payload.
        chi2_grid = [None if not np.isfinite(c) else round(float(c), 2) for c in chi2_arr]

        finite_chi2 = np.isfinite(chi2_arr)
        if np.any(finite_chi2):
            min_idx = int(np.argmin(np.where(finite_chi2, chi2_arr, np.inf)))
            z_best = round(float(z_arr[min_idx]), 4)
            chi2_min = round(float(chi2_arr[min_idx]), 2)
        else:
            z_best = None
            chi2_min = None

        zconf = float(primary.get('ZCONF', 0.0))
        confidence = round(zconf, 1) if np.isfinite(zconf) else None

        model_data = hdul['MODEL'].data
        model_wave = [None if not np.isfinite(x) else round(float(x), 6)
                      for x in model_data['wav']]
        model_fnu = [None if not np.isfinite(x) else round(float(x), 6)
                     for x in model_data['fnu']]

        data = {
            'redshift': z_best,
            'chi2_min': chi2_min,
            'confidence': confidence,
            'z_grid': z_grid,
            'chi2_grid': chi2_grid,
            'model_wave': model_wave,
            'model_fnu': model_fnu,
        }

    json_path = output_dir / (zfit_path.stem + '.json')
    with open(json_path, 'w') as f:
        # allow_nan=False — see generate_spectrum_json.
        json.dump(data, f, allow_nan=False)
    return json_path
