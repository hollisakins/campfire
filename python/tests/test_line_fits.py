"""Emission-line catalog: deploy row building, sync store round-trip, CSV pivot.

The pure parts (store, export pivot, client table) run everywhere; the
``_lines.fits`` → row path reuses the pipeline reader and is guarded by
``importorskip`` like the other cross-package tests.
"""
import json

import numpy as np
import pytest

from campfire.db.export import export_lines_csv, line_columns_for, pivot_line_fit
from campfire.db.store import LINE_FIT_EXPORT_COLUMNS, LocalStore
from campfire.flags import LineFlags


def _record(spectrum_id=1, **over):
    rec = dict(
        spectrum_id=spectrum_id, spectrum_name=f"obs1_g395m_f290lp_{spectrum_id}",
        target_id=f"obs1_{spectrum_id}", grating="G395M", program_slug="ember-uds",
        observation="obs1", field="uds", current_object_id="CAMPFIRE-J1",
        z_used=5.5, z_source="inspected", z_quality=4, object_id="CAMPFIRE-J1", object_version=3,
        z_fit=5.5012, z_fit_err=5e-5, dv=54.0, dv_err=2.5, sigma_v=117.0, sigma_v_err=4.0,
        kin_source="anchor", n_lines=8, n_detected=5, n_broad=0, chi2=520.0, dof=512,
        lines={
            "Halpha": {"label": "Hα", "component": "narrow", "wave_rest": 6564.61, "wave_obs": 4.267,
                       "flux": 5e-18, "flux_err": 1e-19, "snr": 50.0, "ew_rest": 76.9, "ew_rest_err": 3.0,
                       "cont": 1e-20, "cont_err": 1e-22, "flags": 0},
            "OIII5007": {"label": "[OIII]λ5007", "component": "narrow", "wave_rest": 5008.24,
                         "wave_obs": 3.255, "flux": 6e-18, "flux_err": 9e-20, "snr": 64.0,
                         "ew_rest": 92.0, "ew_rest_err": 2.0, "cont": 1e-20, "cont_err": 1e-22,
                         "flags": 0},
            "NII6583": {"label": "[NII]λ6583", "component": "narrow", "wave_rest": 6585.27,
                        "wave_obs": 4.28, "flux": None, "flux_err": None, "snr": None,
                        "ew_rest": None, "ew_rest_err": None, "cont": None, "cont_err": None,
                        "flags": int(LineFlags.BLENDED), "blend_into": "Halpha"},
            "SII6725": {"label": "[SII]λλ6716,6731", "component": "doublet", "wave_rest": 6724.5,
                        "wave_obs": 4.37, "flux": 7e-19, "flux_err": 1.2e-19, "snr": 5.8,
                        "ew_rest": 10.8, "ew_rest_err": 1.9, "cont": 1e-20, "cont_err": 1e-22,
                        "flags": int(LineFlags.RESOLVED)},
        },
        fit_version="1", cfpipe_version="1.2.0", f_lsf=1.5, spectrum_hash="a" * 64,
        fitted_at="2026-09-09T00:00:00+00:00", stale_redshift=False, stale_spectrum=False,
        created_at="2026-09-09T00:00:00+00:00", updated_at="2026-09-09T00:00:00+00:00",
    )
    rec.update(over)
    return rec


def test_export_pivot_orders_by_wavelength_and_keeps_nulls():
    recs = [_record(1), _record(2, lines={"Hbeta": {"wave_rest": 4862.69, "flux": 1e-18, "flux_err": 1e-19,
                                                     "ew_rest": 20.0, "ew_rest_err": 1.0, "flags": 16}})]
    names = line_columns_for(recs)
    assert names == ["Hbeta", "OIII5007", "Halpha", "NII6583", "SII6725"]
    row = pivot_line_fit(recs[0], names)
    assert row["f_Halpha"] == 5e-18 and row["e_Halpha"] == 1e-19
    # a doublet total is a column like any line, with the resolved flag
    assert row["f_SII6725"] == 7e-19 and LineFlags(row["flag_SII6725"]) & LineFlags.RESOLVED
    assert row["ew_OIII5007"] == 92.0 and row["flag_OIII5007"] == 0
    assert row["f_NII6583"] is None and row["flag_NII6583"] == LineFlags.BLENDED
    assert row["f_Hbeta"] is None                      # not measured in this spectrum
    assert row["z_used"] == 5.5 and row["spectrum_name"] == "obs1_g395m_f290lp_1"
    assert LineFlags(row["flag_NII6583"]) & LineFlags.BLENDED


def test_export_lines_csv(tmp_path):
    path = tmp_path / "lines.csv"
    cols = export_lines_csv([_record(1)], path)
    assert cols[: len(LINE_FIT_EXPORT_COLUMNS)] == list(LINE_FIT_EXPORT_COLUMNS)
    assert "f_Halpha" in cols and "flag_NII6583" in cols
    text = path.read_text().splitlines()
    assert len(text) == 2 and text[0].startswith("spectrum_name,")
    # empty catalog still writes a header
    cols = export_lines_csv([], tmp_path / "empty.csv")
    assert cols == list(LINE_FIT_EXPORT_COLUMNS)


def test_store_roundtrip_and_filters(tmp_path):
    store = LocalStore(tmp_path / "campfire.db")
    n = store.upsert_line_fits([_record(1), _record(2, z_quality=2, grating="PRISM", stale_redshift=True)])
    assert n == 2
    assert store.get_max_line_fits_updated_at() == "2026-09-09T00:00:00+00:00"
    rows = store.query_line_fits()
    assert len(rows) == 2 and isinstance(rows[0]["lines"], dict)
    assert rows[0]["lines"]["Halpha"]["flux"] == 5e-18
    assert [r["spectrum_id"] for r in store.query_line_fits(min_quality=3)] == [1]
    assert [r["spectrum_id"] for r in store.query_line_fits(gratings=["prism"])] == [2]
    assert [r["spectrum_id"] for r in store.query_line_fits(exclude_stale=True)] == [1]
    assert store.query_line_fits(observations=["nope"]) == []
    # re-upsert replaces the row wholesale (a dropped line does not linger)
    store.upsert_line_fits([_record(1, lines={"Hbeta": {"wave_rest": 4862.69, "flux": 1.0}})])
    rows = store.query_line_fits(target_ids=["obs1_1"])
    assert list(rows[0]["lines"]) == ["Hbeta"]
    # purge drops rows the server no longer returns
    assert store.purge_stale_line_fits("2999-01-01T00:00:00+00:00") == 2
    store.close()


def test_client_query_lines_wide_and_nested(tmp_path):
    from campfire.client import Campfire

    store = LocalStore(tmp_path / "campfire.db")
    store.upsert_line_fits([_record(1), _record(2)])
    store.close()

    cf = Campfire.__new__(Campfire)
    cf._local = LocalStore(tmp_path / "campfire.db")
    cf._log_local_use = lambda: None
    wide = cf.query_lines(min_quality=3)
    assert len(wide) == 2 and "f_Halpha" in wide.colnames and "flag_NII6583" in wide.colnames
    assert wide["f_Halpha"][0] == pytest.approx(5e-18)
    nested = cf.query_lines(wide=False)
    assert "lines" in nested.colnames and nested["lines"][0]["Halpha"]["snr"] == 50.0
    empty = cf.query_lines(observations=["nope"])
    assert len(empty) == 0 and "z_used" in empty.colnames
    cf._local.close()


def test_sync_apply_line_fits(tmp_path):
    from campfire.sync import _apply_line_fits

    store = LocalStore(tmp_path / "campfire.db")
    count, purged = _apply_line_fits(store, ([_record(7)], 1, []), None, "2000-01-01T00:00:00+00:00")
    assert count == 1 and purged == 0
    assert store.query_line_fits()[0]["spectrum_id"] == 7
    store.close()


# ---------------------------------------------------------------------------
# _lines.fits → spectrum_line_fits row (needs campfire_pipeline)
# ---------------------------------------------------------------------------

def test_build_row_from_pipeline_product(tmp_path):
    pytest.importorskip("campfire_pipeline")
    from campfire_pipeline.nirspec.linefit import (
        LINEFIT_VERSION, LineFitConfig, fit_lines, make_r_function,
    )
    from campfire_pipeline.nirspec.linefit_stage import write_lines_file
    from campfire_pipeline.nirspec.redshift_reference import RedshiftEntry
    from campfire.deploy.lines import build_line_fit_rows

    # tiny synthetic G395M spectrum with one bright line
    rng = np.random.default_rng(0)
    wave = np.arange(2.87, 5.2, 0.00179)
    r_of = make_r_function([2.87, 5.2], [700, 1300])
    z = 5.5
    mu = 6564.61 * (1 + z) * 1e-4
    sig = mu / r_of(mu)[0] / 2.3548
    flam = 1e-20 + 5e-18 * np.exp(-0.5 * ((wave - mu) / sig) ** 2) / (sig * 1e4 * np.sqrt(2 * np.pi))
    conv = 2.99792458e-19 / wave ** 2
    fnu = (flam + rng.normal(0, 2e-21, wave.size)) / conv
    err = np.full_like(wave, 2e-21) / conv
    res = fit_lines(wave, fnu, err, z, r_of, LineFitConfig(), grating='g395m')
    res['wave'] = wave

    spec = tmp_path / "obs1_g395m_f290lp_10_spec.fits"
    spec.write_bytes(b"x")
    entry = RedshiftEntry("obs1_10", z, 4, "CAMPFIRE-J1", 3, "2026-01-01T00:00:00Z")
    for name, src in (("obs1_g395m_f290lp_10_lines.fits", "inspected"),
                      ("obs1_g395m_f290lp_11_lines.fits", "auto")):
        write_lines_file(tmp_path / name, res, z_source=src, entry=entry, spec_path=str(spec),
                         spec_hash="b" * 64, grating="g395m", filt="f290lp", source_id="10",
                         target_id="obs1_10", f_lsf=1.5, cfpipe_version="1.2.3")

    paths = sorted(tmp_path.glob("*_lines.fits"))
    rows, refused = build_line_fit_rows(paths, program_slug="ember-uds", observation="obs1")
    assert len(rows) == 1 and refused == ["obs1_g395m_f290lp_11_lines.fits"]
    row = rows[0]
    assert row["target_id"] == "obs1_10" and row["grating"] == "G395M"
    assert row["z_used"] == pytest.approx(z) and row["z_source"] == "inspected"
    assert row["z_quality"] == 4 and row["object_id"] == "CAMPFIRE-J1" and row["object_version"] == 3
    assert row["fit_version"] == LINEFIT_VERSION and row["cfpipe_version"] == "1.2.3"
    assert row["f_lsf"] == 1.5
    assert row["spectrum_hash"] == "b" * 64 and row["n_lines"] >= 1
    assert row["lines"]["Halpha"]["flux"] > 4e-18 and row["lines"]["Halpha"]["label"] == "Hα"
    json.dumps(row, allow_nan=False)      # the upsert payload is JSON-clean
    rows, refused = build_line_fit_rows(paths, program_slug="ember-uds", observation="obs1",
                                        allow_auto_z=True)
    assert len(rows) == 2 and refused == []
    assert {r["z_source"] for r in rows} == {"inspected", "auto"}


def test_fetch_all_line_fits_tolerates_missing_endpoint(monkeypatch):
    """An older server without /sync/lines must not break `campfire sync`."""
    from unittest.mock import MagicMock

    from campfire.api.client import APIClient
    from campfire.exceptions import NotFoundError

    client = APIClient.__new__(APIClient)
    client._session = MagicMock()
    client._page_size = 100

    def _raise(*a, **k):
        raise NotFoundError("no such route")

    monkeypatch.setattr(client, "_paginate_sync_endpoint", _raise)
    with pytest.warns(UserWarning, match="sync/lines"):
        assert client.fetch_all_line_fits() == ([], 0, [])


def test_non_release_line_fit_versions(tmp_path):
    """Line-fit products carrying a .dev / dirty CMPFRVER trip the deploy gate."""
    from astropy.io import fits

    from campfire.deploy.lines import (
        confirm_non_release_line_fits, line_fit_versions, non_release_line_fit_versions,
    )

    for name, ver in (("a_lines.fits", "1.2.3"), ("b_lines.fits", "1.2.4.dev3+gabc1234"),
                      ("c_lines.fits", "1.2.3")):
        fits.PrimaryHDU(header=fits.Header({"CMPFRVER": ver})).writeto(tmp_path / name)
    paths = sorted(tmp_path.glob("*_lines.fits"))
    assert line_fit_versions(paths) == ["1.2.3", "1.2.4.dev3+gabc1234"]
    assert non_release_line_fit_versions(paths) == ["1.2.4.dev3+gabc1234"]
    # release-only products pass silently; dev products need approval
    assert confirm_non_release_line_fits(paths[:1], dry_run=False, auto_approve=False) is True
    assert confirm_non_release_line_fits(paths, dry_run=False, auto_approve=True) is True
    assert confirm_non_release_line_fits(paths, dry_run=True, auto_approve=False) is True
