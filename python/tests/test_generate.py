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
    SPECTRUM_1D_KEYS,
    generate_spectrum_json,
    generate_spectrum_jsons,
    generate_spectrum_mbkg_json,
    generate_spectrum_products,
    generate_zfit_json,
    mbkg_row_fields,
    read_spectrum_data,
    read_spectrum_mbkg_data,
    spectrum_1d_payload,
)


def _strict_constant(x):
    """parse_constant hook that mirrors JS ``JSON.parse`` — reject NaN/Infinity."""
    raise ValueError(f"invalid JSON constant: {x}")


def _write_spec_fits(path, *, opt, fnu=None, sci=None, fnu_mbkg=None, mbkg_wave=None,
                     sci_mbkg=None, mbkg_2d=True, prof1d_mbkg_opt=None, header=None):
    """Build a minimal spectrum FITS with the HDUs read_spectrum_data expects.

    ``fnu_mbkg`` adds the master-background spectrum: SPEC1D_MBKG on SPEC1D's
    wave (or ``mbkg_wave``) and, unless ``mbkg_2d`` is False, SCI_MBKG (SCI x 2,
    or ``sci_mbkg``) / ERR_MBKG; ``prof1d_mbkg_opt`` adds PROF1D_MBKG.
    ``header`` sets primary cards."""
    n_spatial = len(opt)
    n_wave = 4
    wave = np.linspace(1.0, 5.0, n_wave)
    fnu = np.ones(n_wave) if fnu is None else np.asarray(fnu, dtype=float)
    fnu_err = np.full(n_wave, 0.1)
    sci = np.ones((n_spatial, n_wave)) if sci is None else np.asarray(sci, dtype=float)
    err = np.full((n_spatial, n_wave), 0.1)
    ypos = np.arange(n_spatial, dtype=float)

    hdu0 = fits.PrimaryHDU()
    for card, value in (header or {}).items():
        hdu0.header[card] = value
    spec1d = fits.BinTableHDU.from_columns([
        fits.Column(name="wave", format="D", array=wave),
        fits.Column(name="fnu", format="D", array=fnu),
        fits.Column(name="fnu_err", format="D", array=fnu_err),
    ], name="SPEC1D")
    prof1d = fits.BinTableHDU.from_columns([
        fits.Column(name="ypos", format="D", array=ypos),
        fits.Column(name="opt", format="D", array=np.asarray(opt, dtype=float)),
    ], name="PROF1D")
    hdus = [
        hdu0,
        spec1d,
        fits.ImageHDU(data=sci, name="SCI"),
        fits.ImageHDU(data=err, name="ERR"),
        prof1d,
    ]
    if fnu_mbkg is not None:
        hdus.append(fits.BinTableHDU.from_columns([
            fits.Column(name="wave", format="D",
                        array=wave if mbkg_wave is None else np.asarray(mbkg_wave, dtype=float)),
            fits.Column(name="fnu", format="D", array=np.asarray(fnu_mbkg, dtype=float)),
            fits.Column(name="fnu_err", format="D", array=np.full(n_wave, 0.2)),
        ], name="SPEC1D_MBKG"))
        if mbkg_2d:
            hdus.append(fits.ImageHDU(data=2 * sci if sci_mbkg is None else np.asarray(sci_mbkg, dtype=float),
                                      name="SCI_MBKG"))
            hdus.append(fits.ImageHDU(data=err, name="ERR_MBKG"))
        if prof1d_mbkg_opt is not None:
            hdus.append(fits.BinTableHDU.from_columns([
                fits.Column(name="ypos", format="D", array=ypos),
                fits.Column(name="opt", format="D", array=np.asarray(prof1d_mbkg_opt, dtype=float)),
            ], name="PROF1D_MBKG"))
    fits.HDUList(hdus).writeto(path)


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
    json_path, json_1d_path, _, thumbs = generate_spectrum_products(fits_path, tmp_path)
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


# ---------------------------------------------------------------------------
# 1-D sidecar (perf T2-D2, #508)
# ---------------------------------------------------------------------------

def test_spectrum_1d_payload_drops_only_the_2d_array():
    data = {k: [1] for k in SPECTRUM_1D_KEYS}
    data['snr_2d'] = [[1, 2], [3, 4]]
    out = spectrum_1d_payload(data)
    assert 'snr_2d' not in out
    assert set(out) == set(SPECTRUM_1D_KEYS)


def test_generate_spectrum_jsons_writes_full_and_1d_siblings(tmp_path):
    fits_path = tmp_path / "obs_prism_clear_1_spec.fits"
    _write_spec_fits(fits_path, opt=[0.2, 1.0, 0.2])

    json_path, json_1d_path = generate_spectrum_jsons(fits_path, tmp_path)

    # Layout siblings of the FITS: <stem>.json and <stem>_1d.json
    assert json_path.name == "obs_prism_clear_1_spec.json"
    assert json_1d_path.name == "obs_prism_clear_1_spec_1d.json"

    full = json.loads(json_path.read_text(), parse_constant=_strict_constant)
    oned = json.loads(json_1d_path.read_text(), parse_constant=_strict_constant)
    assert 'snr_2d' in full
    assert 'snr_2d' not in oned
    assert set(oned) == set(full) - {'snr_2d'}
    for k in oned:
        assert oned[k] == full[k]
    assert json_1d_path.stat().st_size < json_path.stat().st_size


def test_generate_spectrum_products_returns_the_1d_sidecar_too(tmp_path):
    fits_path = tmp_path / "obs_prism_clear_2_spec.fits"
    _write_spec_fits(fits_path, opt=[0.2, 1.0, 0.2])
    json_path, json_1d_path, json_mbkg_path, row_fields = generate_spectrum_products(fits_path, tmp_path)
    assert json_path.exists() and json_1d_path.exists()
    assert json_1d_path.name.endswith("_spec_1d.json")
    assert json_mbkg_path is None
    assert set(row_fields) == {'thumbnail_svg_fnu', 'thumbnail_svg_flambda', 'has_mbkg_spectrum',
                               'mbkg_preferred', 'mbkg_reason', 'n_exp', 'n_exp_bkg'}
    assert row_fields['has_mbkg_spectrum'] is False
    assert row_fields['mbkg_preferred'] is None and row_fields['n_exp'] is None


# ---------------------------------------------------------------------------
# Master-background spectrum (_spec_mbkg.json, the plot's master-background switch)
# ---------------------------------------------------------------------------

def test_master_background_spectrum_gets_its_own_json(tmp_path):
    fits_path = tmp_path / "obs_prism_clear_3_spec.fits"
    _write_spec_fits(fits_path, opt=[0.2, 1.0, 0.2], fnu_mbkg=[1.5, np.nan, 2.0, 2.5],
                     prof1d_mbkg_opt=[0.1, 1.0, 0.3],
                     header={'MBKGPREF': True, 'MBKGWHY': 'single_exposure', 'NEXP': 1, 'NEXPBKG': 0})
    json_path, json_1d_path, json_mbkg_path, row_fields = generate_spectrum_products(fits_path, tmp_path)
    assert json_mbkg_path.name == "obs_prism_clear_3_spec_mbkg.json"
    nodded = json.loads(json_path.read_text(), parse_constant=_strict_constant)
    mbkg = json.loads(json_mbkg_path.read_text(), parse_constant=_strict_constant)
    # the nodded payloads are untouched; the master-background one has their shape
    assert 'fnu_mbkg' not in nodded
    assert 'fnu_mbkg' not in json.loads(json_1d_path.read_text())
    assert set(mbkg) == set(nodded)
    assert mbkg['wave'] == nodded['wave']
    assert mbkg['fnu'] == [1.5, None, 2.0, 2.5]
    assert mbkg['fnu_err'] == [0.2] * 4
    # its 2-D S/N is SCI_MBKG / ERR_MBKG (= 2 x the nodded), its weights PROF1D_MBKG
    assert mbkg['snr_2d'] == [[2 * v for v in row] for row in nodded['snr_2d']]
    assert mbkg['profile_fit'] == [0.1, 1.0, 0.3]
    assert row_fields['has_mbkg_spectrum'] is True
    assert row_fields['mbkg_preferred'] is True
    assert row_fields['mbkg_reason'] == 'single_exposure'
    assert (row_fields['n_exp'], row_fields['n_exp_bkg']) == (1, 0)


def test_master_background_spectrum_without_its_own_profile_uses_prof1d(tmp_path):
    fits_path = tmp_path / "obs_prism_clear_6_spec.fits"
    _write_spec_fits(fits_path, opt=[0.2, 1.0, 0.2], fnu_mbkg=[1.0] * 4)
    mbkg = read_spectrum_mbkg_data(fits_path)
    assert mbkg['profile_fit'] == read_spectrum_data(fits_path)['profile_fit']


def test_no_master_background_spectrum_writes_no_json(tmp_path):
    fits_path = tmp_path / "obs_prism_clear_4_spec.fits"
    _write_spec_fits(fits_path, opt=[0.2, 1.0, 0.2])
    assert generate_spectrum_mbkg_json(fits_path, tmp_path) is None
    assert not (tmp_path / "obs_prism_clear_4_spec_mbkg.json").exists()


def test_row_defaults_are_the_fields_of_a_file_without_a_master_background_spectrum(tmp_path):
    # A read file's spectra record carries these before its own fields; an unread
    # file's record carries none of them, so its row keeps what an earlier deploy stored.
    from campfire.deploy.generate import MBKG_ROW_DEFAULTS
    fits_path = tmp_path / "obs_prism_clear_8_spec.fits"
    _write_spec_fits(fits_path, opt=[0.2, 1.0, 0.2])
    with fits.open(fits_path) as hdul:
        assert mbkg_row_fields(hdul) == MBKG_ROW_DEFAULTS


def test_deploy_json_updates_rows_only_where_the_sidecar_landed(tmp_path):
    from campfire.deploy.deploy import update_mbkg_rows
    from .fakes import FakePostgrest
    a, b = tmp_path / "obs_prism_clear_1_spec.fits", tmp_path / "obs_prism_clear_2_spec.fits"
    for p in (a, b):
        _write_spec_fits(p, opt=[0.2, 1.0, 0.2], fnu_mbkg=[1.0] * 4,
                         header={'MBKGPREF': True, 'MBKGWHY': 'extended', 'NEXP': 3, 'NEXPBKG': 2})
    rows = [{"target_id": t, "grating": "prism", "has_mbkg_spectrum": False} for t in ("t1", "t2")]
    sb = FakePostgrest({"spectra": rows})
    summary = [{"spec_file": a.name, "object_id": "t1", "grating": "prism"},
               {"spec_file": b.name, "object_id": "t2", "grating": "prism"}]
    keys = {a.name: "ka", b.name: "kb"}
    fields_of = {}
    for p in (a, b):
        with fits.open(p) as hdul:
            fields_of[p.name] = mbkg_row_fields(hdul)
    assert update_mbkg_rows(sb, summary, fields_of, keys, uploaded_keys={"ka"}) == 1   # kb failed to upload
    got = {r["target_id"]: r for r in sb.rows("spectra")}
    assert got["t1"]["has_mbkg_spectrum"] is True and got["t1"]["mbkg_reason"] == "extended"
    assert got["t2"]["has_mbkg_spectrum"] is False                                  # kept as it was


def test_master_background_reason_only_when_preferred(tmp_path):
    fits_path = tmp_path / "obs_prism_clear_7_spec.fits"
    _write_spec_fits(fits_path, opt=[0.2, 1.0, 0.2], fnu_mbkg=[1.0] * 4,
                     header={'MBKGPREF': False, 'MBKGWHY': '', 'NEXP': 3, 'NEXPBKG': 1})
    with fits.open(fits_path) as hdul:
        fields = mbkg_row_fields(hdul)
    assert fields['mbkg_preferred'] is False and fields['mbkg_reason'] is None
    assert (fields['n_exp'], fields['n_exp_bkg']) == (3, 1)


@pytest.mark.parametrize("kwargs, match", [
    (dict(mbkg_wave=np.linspace(1.0, 5.0, 4) + 1e-4), "SPEC1D_MBKG wave differs"),
    (dict(mbkg_2d=False), "SPEC1D_MBKG without SCI_MBKG"),
    (dict(sci_mbkg=np.ones((2, 4))), "SCI_MBKG shape"),
    (dict(header={'MBKGPREF': True, 'MBKGWHY': 'neighbor_scan'}), "MBKGWHY 'neighbor_scan'"),
])
def test_inconsistent_master_background_spectrum_is_refused(tmp_path, kwargs, match):
    fits_path = tmp_path / "obs_prism_clear_5_spec.fits"
    _write_spec_fits(fits_path, opt=[0.2, 1.0, 0.2], fnu_mbkg=[1.0] * 4, **kwargs)
    with pytest.raises(ValueError, match=match):
        generate_spectrum_products(fits_path, tmp_path)


# ---------------------------------------------------------------------------
# Line-fit sidecar (_lines.json, the spectrum plot's "Lines" overlay)
# ---------------------------------------------------------------------------

def _write_lines_fits(path, *, wave, model, cont, header=None):
    """A minimal _lines.fits: PRIMARY provenance, LINES (one narrow line, one
    blended one), MODEL on the given grid. Mirrors what linefit_stage writes
    without importing the pipeline."""
    hdu0 = fits.PrimaryHDU()
    hdu0.header["LFITVER"] = "2"
    hdu0.header["ZUSED"] = 5.5
    hdu0.header["ZSRC"] = "inspected"
    hdu0.header["ZQUAL"] = 4
    hdu0.header["ZFIT"] = 5.5012
    hdu0.header["DVGLOB"] = 54.0
    hdu0.header["SIGGLOB"] = 117.0
    hdu0.header["CHI2"] = 520.0
    hdu0.header["DOF"] = 512
    hdu0.header["NLINES"] = 2
    hdu0.header["NDETECT"] = 1
    for k, v in (header or {}).items():
        hdu0.header[k] = v
    lines = fits.BinTableHDU.from_columns([
        fits.Column(name="name", format="12A", array=np.array(["Halpha", "NII6583"])),
        fits.Column(name="component", format="8A", array=np.array(["narrow", "narrow"])),
        fits.Column(name="wave_obs", format="D", array=np.array([4.267, np.nan])),
        fits.Column(name="flux", format="D", array=np.array([5e-18, np.nan])),
        fits.Column(name="flux_err", format="D", array=np.array([1e-19, np.nan])),
        fits.Column(name="snr", format="D", array=np.array([50.0, np.nan])),
        fits.Column(name="flags", format="J", array=np.array([0, 2])),
        fits.Column(name="blend_into", format="12A", array=np.array(["", "Halpha"])),
    ], name="LINES")
    model_hdu = fits.BinTableHDU.from_columns([
        fits.Column(name="wave", format="D", array=np.asarray(wave, dtype=float)),
        fits.Column(name="model", format="D", array=np.asarray(model, dtype=float)),
        fits.Column(name="cont", format="D", array=np.asarray(cont, dtype=float)),
    ], name="MODEL")
    fits.HDUList([hdu0, lines, model_hdu]).writeto(path)


def test_generate_lines_json_converts_model_to_fnu_and_nulls_gaps(tmp_path):
    from campfire.deploy.generate import convert_fnu_to_flambda, generate_lines_json

    lines_path = tmp_path / "obs_g395m_f290lp_10_lines.fits"
    wave = [3.0, 4.0, 4.267, 5.0]
    # NaN outside the fitted windows: a gap, never a false zero.
    _write_lines_fits(lines_path, wave=wave, model=[np.nan, 1e-20, 6e-18, np.nan],
                      cont=[np.nan, 1e-20, 1e-20, np.nan],
                      header={"OBJID": "CAMPFIRE-J1", "OBJVER": 3, "SPECHASH": "sha256:" + "b" * 64,
                              "CMPFRTIM": "2026-09-09T00:00:00+00:00"})

    json_path = generate_lines_json(lines_path, tmp_path)
    assert json_path.name == "obs_g395m_f290lp_10_lines.json"
    text = json_path.read_text()
    data = json.loads(text, parse_constant=_strict_constant)
    assert "NaN" not in text

    assert data["fit_version"] == "2" and data["z_used"] == 5.5
    assert data["z_source"] == "inspected" and data["z_quality"] == 4
    assert data["object_id"] == "CAMPFIRE-J1" and data["object_version"] == 3
    assert data["spectrum_hash"] == "sha256:" + "b" * 64 and data["fitted_at"] == "2026-09-09T00:00:00+00:00"
    assert data["sigma_v"] == 117.0 and data["dof"] == 512 and data["n_detected"] == 1
    assert data["wave"] == wave
    assert data["model_fnu"][0] is None and data["model_fnu"][3] is None
    assert data["cont_fnu"][0] is None
    # f_nu (uJy) round-trips through the web's fnu -> flambda conversion.
    assert convert_fnu_to_flambda(data["model_fnu"][2], 4.267) == pytest.approx(6e-18, rel=1e-5)
    assert convert_fnu_to_flambda(data["cont_fnu"][1], 4.0) == pytest.approx(1e-20, rel=1e-5)
    # Compact line summary: names + placement + S/N, nulls for the blended member.
    assert [l["name"] for l in data["lines"]] == ["Halpha", "NII6583"]
    ha, nii = data["lines"]
    assert ha["component"] == "narrow" and ha["wave_obs"] == 4.267 and ha["snr"] == 50.0
    assert ha["blend_into"] is None and ha["flags"] == 0
    assert nii["flux"] is None and nii["snr"] is None and nii["blend_into"] == "Halpha" and nii["flags"] == 2


def test_lines_upload_tasks_pair_each_product_with_its_sidecar(tmp_path):
    from campfire.deploy.lines import lines_upload_tasks

    lines_path = tmp_path / "obs_prism_clear_7_lines.fits"
    _write_lines_fits(lines_path, wave=[1.0, 2.0], model=[1e-20, 2e-20], cont=[1e-20, 1e-20])
    temp = tmp_path / "tmp"
    temp.mkdir()
    tasks = lines_upload_tasks("obs", [lines_path], temp)
    assert [t.r2_key for t in tasks] == [
        "data/products/nirspec/obs/obs_prism_clear_7_lines.fits",
        "data/products/nirspec/obs/obs_prism_clear_7_lines.json",
    ]
    assert tasks[1].local_path == temp / "obs_prism_clear_7_lines.json"
    assert tasks[1].local_path.exists()


def test_lines_upload_tasks_refuse_auto_redshift_fits_unless_allowed(tmp_path, capsys):
    """The provenance gate holds for the bytes too: an --allow-auto product is
    neither uploaded nor given the sidecar the portal plots, unless the deploy
    opts in with --allow-auto-z (same as the catalog row)."""
    from campfire.deploy.lines import lines_upload_tasks, split_auto_line_fits

    inspected = tmp_path / "obs_prism_clear_1_lines.fits"
    auto = tmp_path / "obs_prism_clear_2_lines.fits"
    _write_lines_fits(inspected, wave=[1.0, 2.0], model=[1e-20, 2e-20], cont=[1e-20, 1e-20])
    _write_lines_fits(auto, wave=[1.0, 2.0], model=[1e-20, 2e-20], cont=[1e-20, 1e-20], header={"ZSRC": "auto"})
    assert split_auto_line_fits([inspected, auto]) == ([inspected], [auto])

    temp = tmp_path / "tmp"
    temp.mkdir()
    tasks = lines_upload_tasks("obs", [inspected, auto], temp)
    assert [t.local_path.name for t in tasks] == ["obs_prism_clear_1_lines.fits", "obs_prism_clear_1_lines.json"]
    assert not (temp / "obs_prism_clear_2_lines.json").exists()
    assert "auto (uninspected) redshift" in capsys.readouterr().out

    tasks = lines_upload_tasks("obs", [inspected, auto], temp, allow_auto_z=True)
    assert len(tasks) == 4 and (temp / "obs_prism_clear_2_lines.json").exists()
