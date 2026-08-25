"""Regression tests for deploy-side spectrum JSON generation.

Guards against the failure that hid `snake_ls1_ddt` on the web portal: a
non-finite optimal-extraction weight leaked into `profile_fit` and was written
by ``json.dump`` as the bare token ``NaN``. That is invalid JSON, so the browser
(and the Next.js ``/api/spectrum`` route via ``response.json()``) rejected the
whole payload and the spectrum silently failed to render.
"""

import json

import numpy as np
import pytest
from astropy.io import fits

from campfire.deploy.generate import (
    generate_spectrum_json,
    generate_spectrum_products,
    generate_zfit_json,
    read_spectrum_data,
)


def _strict_constant(x):
    """parse_constant hook that mirrors JS ``JSON.parse`` — reject NaN/Infinity."""
    raise ValueError(f"invalid JSON constant: {x}")


def _write_spec_fits(path, *, opt, fnu=None, sci=None):
    """Build a minimal spectrum FITS with the HDUs read_spectrum_data expects."""
    n_spatial = len(opt)
    n_wave = 4
    wave = np.linspace(1.0, 5.0, n_wave)
    fnu = np.ones(n_wave) if fnu is None else np.asarray(fnu, dtype=float)
    fnu_err = np.full(n_wave, 0.1)
    sci = np.ones((n_spatial, n_wave)) if sci is None else np.asarray(sci, dtype=float)
    err = np.full((n_spatial, n_wave), 0.1)
    ypos = np.arange(n_spatial, dtype=float)

    hdu0 = fits.PrimaryHDU()
    spec1d = fits.BinTableHDU.from_columns([
        fits.Column(name="wave", format="D", array=wave),
        fits.Column(name="fnu", format="D", array=fnu),
        fits.Column(name="fnu_err", format="D", array=fnu_err),
    ], name="SPEC1D")
    prof1d = fits.BinTableHDU.from_columns([
        fits.Column(name="ypos", format="D", array=ypos),
        fits.Column(name="opt", format="D", array=np.asarray(opt, dtype=float)),
    ], name="PROF1D")
    fits.HDUList([
        hdu0,
        spec1d,
        fits.ImageHDU(data=sci, name="SCI"),
        fits.ImageHDU(data=err, name="ERR"),
        prof1d,
    ]).writeto(path)


def test_nonfinite_profile_weight_yields_valid_json(tmp_path):
    """A NaN optimal-extraction weight must not corrupt the JSON payload."""
    fits_path = tmp_path / "obj_prism_clear_1_spec.fits"
    # opt[0] non-finite (masked/edge pixel) — this is exactly what broke snake.
    _write_spec_fits(fits_path, opt=[np.nan, 1.0, 2.0, 1.0, 0.5])

    json_path = generate_spectrum_json(fits_path, tmp_path)
    text = json_path.read_text()

    # Must be valid *strict* JSON (browser JSON.parse equivalent).
    data = json.loads(text, parse_constant=_strict_constant)
    assert "NaN" not in text
    # The offending weight is coerced to 0, mirroring snr_2d / profile handling.
    assert data["profile_fit"][0] == 0.0
    assert all(np.isfinite(v) for v in data["profile_fit"])


def test_json_dump_rejects_unsanitized_nonfinite(tmp_path):
    """allow_nan=False is a backstop: a leak in any *future* field fails loudly."""
    with pytest.raises(ValueError):
        json.dump({"x": float("nan")}, open(tmp_path / "x.json", "w"), allow_nan=False)


def test_nonfinite_flux_becomes_null(tmp_path):
    """Existing contract: non-finite fnu is emitted as JSON null, not NaN."""
    fits_path = tmp_path / "obj_prism_clear_2_spec.fits"
    _write_spec_fits(fits_path, opt=[1.0, 1.0, 1.0, 1.0, 0.5],
                     fnu=[1.0, np.nan, 3.0, 4.0])

    json_path = generate_spectrum_json(fits_path, tmp_path)
    data = json.loads(json_path.read_text(), parse_constant=_strict_constant)
    assert data["fnu"][1] is None


def test_inf_flux_becomes_null(tmp_path):
    """Issue #482: ±inf in fnu/fnu_err crashed deploy with
    'Out of range float values are not JSON compliant: inf'. Both must be
    emitted as JSON null, exactly like NaN."""
    fits_path = tmp_path / "obj_prism_clear_3_spec.fits"
    _write_spec_fits(fits_path, opt=[1.0, 1.0, 1.0, 1.0, 0.5],
                     fnu=[1.0, np.inf, -np.inf, 4.0])

    # generate_spectrum_products is the single-read path deploy actually uses
    # (the one that raised in the field).
    json_path, thumbs = generate_spectrum_products(fits_path, tmp_path)
    data = json.loads(json_path.read_text(), parse_constant=_strict_constant)
    assert data["fnu"][1] is None
    assert data["fnu"][2] is None
    assert data["fnu"][0] == 1.0
    # Thumbnails must also survive inf flux (finite points only).
    assert "<svg" in thumbs["thumbnail_svg_fnu"]


def test_inf_profile_weight_keeps_centered_axis(tmp_path):
    """An inf extraction weight passes a bare `> 0` cut; it must not poison
    the centroid (collapsing profile_pix to all zeros) nor the profile_fit
    normalization (nanmax ignores NaN but not inf)."""
    fits_path = tmp_path / "obj_prism_clear_4_spec.fits"
    _write_spec_fits(fits_path, opt=[np.inf, 1.0, 2.0, 1.0, 0.5])

    json_path = generate_spectrum_json(fits_path, tmp_path)
    data = json.loads(json_path.read_text(), parse_constant=_strict_constant)

    # Centroid from the finite weights only: axis stays centered, not zeroed.
    assert any(v != 0.0 for v in data["profile_pix"])
    assert data["profile_pix"] == sorted(data["profile_pix"])
    # The inf weight itself is coerced to 0; the rest normalize to finite max.
    assert data["profile_fit"][0] == 0.0
    assert max(data["profile_fit"]) == 1.0


def _write_zfit_fits(path, *, chi2, model_fnu, zconf=8.0):
    z = np.linspace(0.0, 10.0, len(chi2))
    model_wave = np.linspace(1.0, 5.0, len(model_fnu))
    hdu0 = fits.PrimaryHDU()
    hdu0.header["ZCONF"] = zconf
    chi2_hdu = fits.BinTableHDU.from_columns([
        fits.Column(name="z", format="D", array=z),
        fits.Column(name="chi2", format="D", array=np.asarray(chi2, dtype=float)),
    ], name="CHI2")
    model_hdu = fits.BinTableHDU.from_columns([
        fits.Column(name="wav", format="D", array=model_wave),
        fits.Column(name="fnu", format="D", array=np.asarray(model_fnu, dtype=float)),
    ], name="MODEL")
    fits.HDUList([hdu0, chi2_hdu, model_hdu]).writeto(path)


def test_zfit_nonfinite_values_become_null(tmp_path):
    """Issue #482 (zfit side): non-finite chi2/model values must serialize as
    null and must not poison the best-fit selection."""
    zfit_path = tmp_path / "obj_prism_clear_1_zfit.fits"
    _write_zfit_fits(
        zfit_path,
        chi2=[np.inf, 5.0, np.nan, 2.0, 9.0],
        model_fnu=[1.0, np.nan, np.inf, 4.0],
    )

    json_path = generate_zfit_json(zfit_path, tmp_path)
    text = json_path.read_text()
    data = json.loads(text, parse_constant=_strict_constant)

    assert data["chi2_grid"][0] is None
    assert data["chi2_grid"][2] is None
    assert data["model_fnu"][1] is None
    assert data["model_fnu"][2] is None
    # Best fit picks the finite minimum, skipping the inf/NaN grid points.
    assert data["chi2_min"] == 2.0
    assert data["redshift"] == 7.5
    assert data["confidence"] == 8.0


def test_zfit_all_nonfinite_chi2_yields_null_best_fit(tmp_path):
    """Degenerate zfit (no finite chi2) still writes valid JSON with null
    best-fit scalars rather than crashing or emitting NaN."""
    zfit_path = tmp_path / "obj_prism_clear_2_zfit.fits"
    _write_zfit_fits(zfit_path, chi2=[np.nan, np.inf], model_fnu=[1.0, 2.0])

    json_path = generate_zfit_json(zfit_path, tmp_path)
    data = json.loads(json_path.read_text(), parse_constant=_strict_constant)
    assert data["redshift"] is None
    assert data["chi2_min"] is None
