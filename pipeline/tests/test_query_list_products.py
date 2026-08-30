"""Tests for ``/list_products`` empty-``200`` handling in the download tool.

MAST intermittently answers ``200`` with an empty ``products`` array for
filesets that the search just matched — a fileset always has products, so an
empty list is a transient failure wearing a success code. Regression coverage
that such a response is retried and, if it never fills in, surfaced as a
``MastTransientError`` rather than silently shrinking the product list.
"""

import pytest
import requests

from campfire_pipeline.common import query
from campfire_pipeline.common.query import (
    MastTransientError,
    _list_products_request,
    list_products_batched,
)


class FakeResponse:
    def __init__(self, products, status_code=200):
        self._products = products
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return {"products": self._products}


def _product(fileset):
    return {"filename": f"{fileset}_nrca1_uncal.fits", "uri": "u", "size": 1}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(query.time, "sleep", lambda *_a, **_k: None)


class TestListProductsRequest:
    def test_empty_200_is_retried_and_recovers(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(params["dataset_ids"])
            if len(calls) < 3:
                return FakeResponse([])
            return FakeResponse([_product("jw001")])

        monkeypatch.setattr(query.requests, "get", fake_get)
        products = _list_products_request(["jw001"], {})
        assert len(products) == 1
        assert len(calls) == 3

    def test_persistent_empty_200_raises(self, monkeypatch):
        monkeypatch.setattr(query.requests, "get",
                            lambda *a, **k: FakeResponse([]))
        with pytest.raises(MastTransientError):
            _list_products_request(["jw001", "jw002"], {}, max_retries=2)

    def test_empty_batch_is_not_an_error(self, monkeypatch):
        # An empty request list legitimately has no products; only a
        # *non-empty* batch coming back empty is a transient failure.
        monkeypatch.setattr(query.requests, "get",
                            lambda *a, **k: FakeResponse([]))
        assert _list_products_request([], {}, max_retries=1) == []


class TestListProductsBatched:
    def test_returns_products_when_every_batch_answers(self, monkeypatch):
        def fake_get(url, params=None, headers=None, timeout=None):
            names = params["dataset_ids"].split(",")
            return FakeResponse([_product(n) for n in names])

        monkeypatch.setattr(query.requests, "get", fake_get)
        filesets = [{"fileSetName": f"jw{i:03d}"} for i in range(4)]
        products = list_products_batched(filesets, batch_size=2, workers=2)
        assert len(products) == 4

    def test_one_persistently_empty_batch_fails_the_run(self, monkeypatch):
        # The failure mode is per-request, so in a multi-batch run some
        # batches answer normally while others come back empty. The empty
        # ones must not be silently dropped just because the run as a whole
        # collected some products.
        def fake_get(url, params=None, headers=None, timeout=None):
            names = params["dataset_ids"].split(",")
            if "jw003" in names:
                return FakeResponse([])
            return FakeResponse([_product(n) for n in names])

        monkeypatch.setattr(query.requests, "get", fake_get)
        filesets = [{"fileSetName": f"jw{i:03d}"} for i in range(4)]
        with pytest.raises(MastTransientError) as exc:
            list_products_batched(filesets, batch_size=1, workers=2,
                                  max_rounds=2)
        assert "1 of 4" in str(exc.value)

    def test_no_filesets_returns_empty(self, monkeypatch):
        monkeypatch.setattr(query.requests, "get",
                            lambda *a, **k: pytest.fail("should not query"))
        assert list_products_batched([]) == []
