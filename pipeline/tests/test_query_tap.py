"""Tests for the CAOM TAP backend of the download tool.

The TAP backend answers in one ADQL query what the JWST Search API needs a
/search plus batched /list_products for. Coverage that the rows it returns
are turned into the same file dicts the Search API path produces: guide-star
products dropped, exp_type enforced per detector, the NIRCam filter read
from each detector's own FILTER/PUPIL, MSA metafiles deduplicated, and a
truncated or rejected query surfaced as an error rather than a short list.
"""

import csv
import io

import pytest
import requests

from campfire_pipeline.common import query
from campfire_pipeline.common.query import (
    MastQueryError,
    _parse_inskeywords,
    _tap_query,
    download_jwst_data,
    search_products_tap,
)

COLUMNS = ["productfilename", "datauri", "contentlength", "contentchecksum",
           "observationid", "inskeywords", "trgposra", "trgposdec",
           "posboundsstcs", "timmin"]


class FakeResponse:
    def __init__(self, text="", status_code=200, content_type="text/csv"):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.ok = status_code < 400
        self.reason = "reason"
        self.url = query.CAOM_TAP_URL


def _csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in COLUMNS})
    return buf.getvalue()


def _row(obsid, filename=None, exp_type="NRC_IMAGE", filt="F115W",
         pupil="CLEAR", size=100):
    return {
        "productfilename": filename or f"{obsid}_uncal.fits",
        "datauri": f"mast:JWST/product/{filename or obsid + '_uncal.fits'}",
        "contentlength": str(size),
        "contentchecksum": "md5:abc",
        "observationid": obsid,
        "inskeywords": (f"DETECTOR=X | EXPTIME=311.3 | EXP_TYPE={exp_type} | "
                        f"FILTER={filt} | PUPIL={pupil} | TSOVISIT=f"),
        "trgposra": "268.46",
        "trgposdec": "65.19",
        "posboundsstcs": "POLYGON 1 2 3 4 5 6",
        "timmin": "61076.5",
    }


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(query.time, "sleep", lambda *_a, **_k: None)


def _serve(monkeypatch, rows, calls=None):
    def fake_get(url, params=None, timeout=None):
        if calls is not None:
            calls.append(params["QUERY"])
        return FakeResponse(_csv(rows))
    monkeypatch.setattr(query.requests, "get", fake_get)


def test_parse_inskeywords():
    kw = _parse_inskeywords("A=1 | EXP_TYPE=NRC_IMAGE | TEMPLATE=NIRCam Imaging")
    assert kw == {"A": "1", "EXP_TYPE": "NRC_IMAGE",
                  "TEMPLATE": "NIRCam Imaging"}
    assert _parse_inskeywords(None) == {}


class TestSearchProductsTap:
    def test_nircam_selection(self, monkeypatch):
        calls = []
        _serve(monkeypatch, [
            _row("jw09263001001_02101_00001_nrca1"),
            # same artifact reached through a second plane
            _row("jw09263001001_02101_00001_nrca1"),
            # guide-star product attached to a science plane
            _row("jw09263001001_02101_00001_nrca1",
                 filename="jw09263001001_gs-acq1_2026001_uncal.fits"),
            # grism exposure: wrong exp_type
            _row("jw09263001001_02101_00001_nrcalong",
                 exp_type="NRC_WFSS", filt="F322W2", pupil="GRISMR"),
            # pupil-mounted narrowband wins over its wide companion
            _row("jw09263001001_03101_00002_nrcb1",
                 filt="F150W2", pupil="F162M"),
        ], calls)
        uncal, aux = search_products_tap(9263, "NIRCAM", "NRC_IMAGE",
                                         obs_ids=[1])
        assert aux == []
        assert [f["filename"] for f in uncal] == [
            "jw09263001001_02101_00001_nrca1_uncal.fits",
            "jw09263001001_03101_00002_nrcb1_uncal.fits",
        ]
        assert [f["filter"] for f in uncal] == ["F115W", "F162M"]
        assert uncal[0]["size"] == 100
        assert uncal[0]["uri"].startswith("mast:JWST/product/")

        fs = uncal[1]["_fileset"]
        assert fs["fileSetName"] == "jw09263001001_03101_00002"
        assert fs["observtn"] == "1"
        assert fs["exposure"] == "2"
        assert fs["s_region"] == "POLYGON ICRS 1 2 3 4 5 6"
        assert fs["duration"] == pytest.approx(311.3)
        assert fs["date_obs"].startswith("2026-")

        assert "o.observationid LIKE 'jw09263001%'" in calls[0]
        assert "o.insname LIKE 'NIRCAM%'" in calls[0]

    def test_nirspec_msa_deduplicated(self, monkeypatch):
        _serve(monkeypatch, [
            _row("jw06585004001_03101_00001_nrs1", exp_type="NRS_MSASPEC"),
            _row("jw06585004001_03101_00001_nrs1", exp_type="NRS_MSASPEC",
                 filename="jw06585004001_01_msa.fits"),
            _row("jw06585004001_03101_00001_nrs2", exp_type="NRS_MSASPEC",
                 filename="jw06585004001_01_msa.fits"),
        ])
        uncal, aux = search_products_tap(6585, "NIRSPEC", "NRS_MSASPEC")
        assert [f["filename"] for f in uncal] == [
            "jw06585004001_03101_00001_nrs1_uncal.fits"]
        assert [f["filename"] for f in aux] == ["jw06585004001_01_msa.fits"]
        assert "filter" not in uncal[0]

    def test_multiple_obs_ored(self, monkeypatch):
        calls = []
        _serve(monkeypatch, [], calls)
        search_products_tap(9263, "NIRCAM", "NRC_IMAGE", obs_ids=[1, 12])
        assert ("(o.observationid LIKE 'jw09263001%' OR "
                "o.observationid LIKE 'jw09263012%')") in calls[0]

    def test_rejects_unsafe_exp_type(self):
        with pytest.raises(ValueError):
            search_products_tap(9263, "NIRCAM", "NRC_IMAGE' OR 1=1 --")


class TestTapQuery:
    def test_truncated_result_raises(self, monkeypatch):
        monkeypatch.setattr(query, "TAP_MAXREC", 2)
        _serve(monkeypatch, [_row("a"), _row("b")])
        with pytest.raises(MastQueryError, match="row cap"):
            _tap_query("SELECT 1")

    def test_votable_error_raises(self, monkeypatch):
        monkeypatch.setattr(query.requests, "get", lambda *a, **k: FakeResponse(
            "<VOTABLE>QUERY_STATUS ERROR</VOTABLE>",
            content_type="text/xml"))
        with pytest.raises(MastQueryError, match="ERROR"):
            _tap_query("SELECT 1")

    def test_5xx_retried(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                return FakeResponse("busy", status_code=503)
            return FakeResponse(_csv([_row("a")]))

        monkeypatch.setattr(query.requests, "get", fake_get)
        assert len(_tap_query("SELECT 1")) == 1
        assert len(calls) == 3

    def test_persistent_5xx_raises(self, monkeypatch):
        monkeypatch.setattr(query.requests, "get",
                            lambda *a, **k: FakeResponse("busy", 503))
        with pytest.raises(requests.HTTPError):
            _tap_query("SELECT 1", max_retries=2)


class TestDownloadEndpoint:
    @pytest.mark.parametrize("uri, url, key", [
        ("mast:JWST/product/jw1_uncal.fits", query.MAST_DOWNLOAD_URL, "uri"),
        ("jw1/jw1_uncal.fits", f"{query.BASE_URL}/retrieve_product",
         "product_name"),
    ])
    def test_uri_form_selects_endpoint(self, monkeypatch, tmp_path, uri, url,
                                       key):
        seen = {}

        class Streamed:
            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size=None):
                yield b"data"

        def fake_get(u, params=None, **_k):
            seen["url"], seen["params"] = u, params
            return Streamed()

        monkeypatch.setattr(query.requests, "get", fake_get)

        class Bar:
            def update(self, n):
                pass

        info = {"filename": "jw1_uncal.fits", "uri": uri,
                "_path": tmp_path / "jw1_uncal.fits"}
        status, _, err = query._download_one(info, None, Bar(),
                                             query.threading.Lock())
        assert (status, err) == ("downloaded", None)
        assert seen == {"url": url, "params": {key: uri}}


class TestDownloadBackendRouting:
    def test_target_falls_back_to_search(self, monkeypatch, tmp_path):
        seen = []

        def fake_search(*args):
            seen.append(args)
            return None

        monkeypatch.setattr(query, "_select_via_search", fake_search)
        monkeypatch.setattr(query, "search_products_tap",
                            lambda *a, **k: pytest.fail("TAP used for --target"))
        download_jwst_data(1, "NIRCAM", "NRC_IMAGE", download_dir=tmp_path,
                           dry_run=True, targets=["M1"], backend="tap")
        assert len(seen) == 1

    def test_tap_dry_run_applies_filters(self, monkeypatch, tmp_path, capsys):
        _serve(monkeypatch, [
            _row("jw09263001001_02101_00001_nrca1", filt="F115W"),
            _row("jw09263001001_02101_00001_nrcalong", filt="F356W"),
        ])
        download_jwst_data(9263, "NIRCAM", "NRC_IMAGE",
                           download_dir=tmp_path / "raw", dry_run=True,
                           filters=["F356W"], backend="tap")
        out = capsys.readouterr().out
        assert "nircam/9263/f356w/jw09263001001_02101_00001_nrcalong_uncal.fits" in out
        assert "nrca1" not in out.split("Files to download")[1]

    def test_search_backend_resolves_detector_filters(self, monkeypatch,
                                                      tmp_path, capsys):
        fileset = {"fileSetName": "jw01234001001_02101_00001",
                   "opticalElements": "F115W;CLEAR, F444W;CLEAR",
                   "filter": "F115W"}
        monkeypatch.setattr(query, "search_filesets",
                            lambda *a, **k: [fileset])
        monkeypatch.setattr(query, "list_products_batched", lambda *a, **k: [
            {"filename": f"jw01234001001_02101_00001_{d}_uncal.fits",
             "uri": "u", "size": 1, "exp_type": "NRC_IMAGE"}
            for d in ("nrca1", "nrcalong")
        ])
        download_jwst_data(1234, "NIRCAM", "NRC_IMAGE",
                           download_dir=tmp_path / "raw", dry_run=True,
                           backend="search")
        out = capsys.readouterr().out
        assert "nircam/1234/f115w/jw01234001001_02101_00001_nrca1_uncal.fits" in out
        assert "nircam/1234/f444w/jw01234001001_02101_00001_nrcalong_uncal.fits" in out

    def test_unknown_backend_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            download_jwst_data(1, download_dir=tmp_path, backend="nope")
