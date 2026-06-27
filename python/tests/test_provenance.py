"""Client-surface provenance tests.

Covers the read end of the provenance chain: the Spectrum model exposes the
per-spectrum provenance carried from the catalog, and SpectrumCollection's
provenance lens summarises it across a sample and flags heterogeneity (the
sentence a referee or methods section needs). The full pipeline -> catalog ->
client round-trip is covered hop-by-hop across test_deploy_summary (carry),
test_store (sync/query) and pipeline/tests/test_provenance_reader (header read).
"""

from __future__ import annotations

from campfire.models import (
    Object,
    Provenance,
    Spectrum,
    SpectrumCollection,
    ProvenanceSummary,
)


def _spec(sid, grating, crds, reduced_at, cfpipe="0.4.0", jwst="1.14.0"):
    return Spectrum(
        spectrum_id=sid,
        object_id="obj",
        grating=grating,
        cfpipe_version=cfpipe,
        crds_context=crds,
        jwst_version=jwst,
        date_obs="2024-01-01",
        reduced_at=reduced_at,
    )


def test_spectrum_provenance_property():
    s = _spec("a", "PRISM", "jwst_1210.pmap", "2026-06-01T00:00:00+00:00")
    p = s.provenance
    assert isinstance(p, Provenance)
    assert p.cfpipe_version == "0.4.0"
    assert p.crds_context == "jwst_1210.pmap"
    assert p.jwst_version == "1.14.0"
    assert p.date_obs == "2024-01-01"
    assert p.reduced_at == "2026-06-01T00:00:00+00:00"


def test_collection_provenance_homogeneous():
    coll = SpectrumCollection([
        _spec("a", "PRISM", "jwst_1210.pmap", "2026-06-01T00:00:00+00:00"),
        _spec("b", "G395M", "jwst_1210.pmap", "2026-06-01T00:00:00+00:00"),
    ])
    prov = coll.provenance()
    assert isinstance(prov, ProvenanceSummary)
    assert prov.homogeneous
    assert prov.crds_contexts == {"jwst_1210.pmap": 2}
    assert prov.cfpipe_versions == {"0.4.0": 2}


def test_collection_provenance_flags_mixed_crds():
    coll = SpectrumCollection([
        _spec("a", "PRISM", "jwst_1210.pmap", "2026-06-01T00:00:00+00:00"),
        _spec("b", "G395M", "jwst_1322.pmap", "2026-06-10T00:00:00+00:00"),
        _spec("c", "G140M", "jwst_1210.pmap", "2026-06-05T00:00:00+00:00"),
    ])
    prov = coll.provenance()
    assert not prov.homogeneous
    # counts ordered by frequency desc
    assert prov.crds_contexts == {"jwst_1210.pmap": 2, "jwst_1322.pmap": 1}
    # reduced_at range spans the earliest to latest stamp
    assert prov.reduced_at_range == (
        "2026-06-01T00:00:00+00:00",
        "2026-06-10T00:00:00+00:00",
    )
    # the heterogeneity is visible in the printed summary
    assert "HETEROGENEOUS" in repr(prov)


def test_collection_provenance_arrays():
    coll = SpectrumCollection([
        _spec("a", "PRISM", "jwst_1210.pmap", "2026-06-01T00:00:00+00:00"),
        _spec("b", "G395M", "jwst_1322.pmap", "2026-06-10T00:00:00+00:00"),
    ])
    assert list(coll.cfpipe_version) == ["0.4.0", "0.4.0"]
    assert list(coll.crds_context) == ["jwst_1210.pmap", "jwst_1322.pmap"]
    assert list(coll.jwst_version) == ["1.14.0", "1.14.0"]


def test_object_from_dict_maps_provenance():
    """Object.from_dict (the store -> client boundary) carries provenance onto
    each Spectrum, so a synced catalog row is queryable without opening FITS."""
    d = {
        "object_id": "obj",
        "ra": 150.0,
        "dec": 2.0,
        "field": "uds",
        "spectra": [
            {
                "spectrum_id": "a",
                "object_id": "obj",
                "grating": "PRISM",
                "cfpipe_version": "experimental-bkg",
                "crds_context": "jwst_1322.pmap",
                "jwst_version": "1.14.0",
                "date_obs": "2024-01-01",
                "reduced_at": "2026-06-10T00:00:00+00:00",
            }
        ],
    }
    obj = Object.from_dict(d)
    s = obj.spectra[0]
    assert s.cfpipe_version == "experimental-bkg"
    assert s.crds_context == "jwst_1322.pmap"
    assert s.reduced_at == "2026-06-10T00:00:00+00:00"
    assert s.provenance.crds_context == "jwst_1322.pmap"


def test_to_table_includes_provenance():
    coll = SpectrumCollection([
        _spec("a", "PRISM", "jwst_1210.pmap", "2026-06-01T00:00:00+00:00"),
    ])
    tbl = coll.to_table()
    for col in ("cfpipe_version", "crds_context", "jwst_version", "date_obs", "reduced_at"):
        assert col in tbl.colnames
    assert "reduction_version" not in tbl.colnames
