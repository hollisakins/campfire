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

from campfire.deploy.generate import generate_spectrum_json, read_spectrum_data


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
