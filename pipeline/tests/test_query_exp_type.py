"""Tests for exposure-type selection in the download tool.

A NIRSpec program can mix MSA and fixed-slit exposures, and the reduction
handles both, so ``cfpipe download`` fetches both by default (issue 259)
without ``--exp-type``. Coverage that the default reaches every selection
path: the TAP query, the Search API fan-out, the per-product filter and the
post-download header purge.
"""

import pytest

from campfire_pipeline.common import query
from campfire_pipeline.common.query import (
    _purge_mismatched_uncals,
    filter_products,
    normalize_exp_types,
    search_filesets,
    search_products_tap,
)

from test_query_tap import _row, _serve


class TestNormalizeExpTypes:
    def test_instrument_defaults(self):
        assert normalize_exp_types(None, "NIRSPEC") == ("NRS_MSASPEC",
                                                        "NRS_FIXEDSLIT")
        assert normalize_exp_types((), "nirspec") == ("NRS_MSASPEC",
                                                      "NRS_FIXEDSLIT")
        assert normalize_exp_types(None, "NIRCAM") == ("NRC_IMAGE",)
        assert normalize_exp_types(None) == ()

    def test_single_and_many(self):
        assert normalize_exp_types("nrs_fixedslit", "NIRSPEC") == (
            "NRS_FIXEDSLIT",)
        assert normalize_exp_types(["NRS_MSASPEC", "nrs_msaspec",
                                    "NRS_FIXEDSLIT"]) == ("NRS_MSASPEC",
                                                          "NRS_FIXEDSLIT")

    def test_rejects_unsafe(self):
        with pytest.raises(ValueError):
            normalize_exp_types(["NRS_MSASPEC", "X' OR 1=1 --"])


class TestTapDefaults:
    def test_nirspec_default_keeps_msa_and_fixed_slit(self, monkeypatch):
        calls = []
        _serve(monkeypatch, [
            _row("jw01210001001_03101_00001_nrs1", exp_type="NRS_MSASPEC"),
            _row("jw01210002001_03101_00001_nrs1", exp_type="NRS_FIXEDSLIT"),
            _row("jw01210003001_03101_00001_nrs1", exp_type="NRS_IFU"),
        ], calls)
        uncal, _ = search_products_tap(1210, "NIRSPEC")
        assert [f["exp_type"] for f in uncal] == ["NRS_MSASPEC",
                                                  "NRS_FIXEDSLIT"]
        assert ("(o.inskeywords LIKE '%EXP_TYPE=NRS_MSASPEC%' OR "
                "o.inskeywords LIKE '%EXP_TYPE=NRS_FIXEDSLIT%')") in calls[0]

    def test_explicit_single_type_still_narrows(self, monkeypatch):
        _serve(monkeypatch, [
            _row("jw01210001001_03101_00001_nrs1", exp_type="NRS_MSASPEC"),
            _row("jw01210002001_03101_00001_nrs1", exp_type="NRS_FIXEDSLIT"),
        ])
        uncal, _ = search_products_tap(1210, "NIRSPEC", "NRS_FIXEDSLIT")
        assert [f["exp_type"] for f in uncal] == ["NRS_FIXEDSLIT"]


class TestSearchApiDefaults:
    def test_fans_out_one_search_per_exp_type(self, monkeypatch):
        payloads = []

        class Resp:
            ok = True

            def __init__(self, exp):
                self._exp = exp

            def json(self):
                return {"totalResults": 1,
                        "results": [{"fileSetName": f"fs_{self._exp}"}]}

        def fake_post(url, json=None, headers=None):
            payloads.append(json)
            exp = next(c["exp_type"] for c in json["conditions"]
                       if "exp_type" in c)
            return Resp(exp)

        monkeypatch.setattr(query.requests, "post", fake_post)
        filesets = search_filesets(1210, "NIRSPEC")
        assert [f["fileSetName"] for f in filesets] == [
            "fs_NRS_MSASPEC", "fs_NRS_FIXEDSLIT"]
        assert len(payloads) == 2


class TestFilterProducts:
    def test_keeps_every_requested_type(self):
        products = [
            {"filename": f"{e}_uncal.fits", "exp_type": e}
            for e in ("NRS_MSASPEC", "NRS_FIXEDSLIT", "NRS_IFU")
        ]
        uncal, _ = filter_products(products, "NIRSPEC",
                                   exp_type=("NRS_MSASPEC", "NRS_FIXEDSLIT"))
        assert [f["exp_type"] for f in uncal] == ["NRS_MSASPEC",
                                                  "NRS_FIXEDSLIT"]


class TestPurge:
    def test_removes_only_unrequested_types(self, tmp_path):
        from astropy.io import fits
        files = []
        for e in ("NRS_MSASPEC", "NRS_FIXEDSLIT", "NRS_IFU"):
            path = tmp_path / f"{e}_uncal.fits"
            hdu = fits.PrimaryHDU()
            hdu.header["EXP_TYPE"] = e
            hdu.writeto(path)
            files.append({"_path": path})
        removed = _purge_mismatched_uncals(
            files, ("NRS_MSASPEC", "NRS_FIXEDSLIT"))
        assert removed == 1
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "NRS_FIXEDSLIT_uncal.fits", "NRS_MSASPEC_uncal.fits"]
