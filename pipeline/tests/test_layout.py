"""Golden conformance test for the campfire_layout contract (issue #213, PR-2).

Runs the *same* fixture the TS arm (``web/lib/layout.test.ts``) checks, so a
python↔TS divergence fails both. Also asserts the cross-package agreement that
``test_layout_212.py`` established: the layout module, the deploy resolver, and
the pipeline ``Observation`` all resolve the NIRSpec products dir identically.
"""

import json
from pathlib import Path

import pytest

import campfire_layout as L
from campfire_layout import KeyScheme, LayoutError, Scope

_FIXTURE = Path(__file__).parents[2] / "layout" / "conformance" / "layout_golden.json"


def _load():
    with open(_FIXTURE) as f:
        return json.load(f)


GOLDEN = _load()


def _ids(cases):
    return [c["product_type"] for c in cases]


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=_ids(GOLDEN["cases"]))
def test_case(case):
    pt = case["product_type"]
    scope = Scope.from_dict(case["scope"])
    fn = case["filename"]

    # --- storage keys ---
    if case["key_legacy"] is not None:
        assert L.storage_key(pt, scope, fn, scheme=KeyScheme.LEGACY) == case["key_legacy"]
    if case["key_canonical"] is not None:
        assert L.storage_key(pt, scope, fn, scheme=KeyScheme.CANONICAL) == case["key_canonical"]

    # --- local relpath (or raises for key-only products) ---
    if case["relpath"] is not None:
        assert L.local_relpath(pt, scope, fn) == case["relpath"]
        # parse_relpath recovers the same product and rebuilds the same relpath.
        pk = L.parse_relpath(case["relpath"])
        assert pk.product_type == pt
        assert L.local_relpath(pk.product_type, pk.scope, pk.filename) == case["relpath"]
    else:
        with pytest.raises(LayoutError):
            L.local_relpath(pt, scope, fn)

    # --- bucket ---
    if case["bucket"] is not None:
        assert L.bucket_for(pt) == case["bucket"]
    else:
        with pytest.raises(LayoutError):
            L.bucket_for(pt)

    # --- lifecycle ---
    ref = case["relpath"] or case["key_legacy"] or case["key_canonical"]
    assert L.tree_class(ref).value == case["tree_class"]

    # --- bijection round-trips ---
    bkt = case["bucket"] or "data"
    if case["key_legacy"] is not None and case["relpath"] is not None:
        assert L.key_to_relpath(case["key_legacy"], bucket=bkt) == case["relpath"]
        assert L.relpath_to_key(case["relpath"], scheme=KeyScheme.LEGACY) == case["key_legacy"]
    if case["key_canonical"] is not None and case["relpath"] is not None:
        assert L.key_to_relpath(case["key_canonical"], bucket=bkt) == case["relpath"]
        assert L.relpath_to_key(case["relpath"], scheme=KeyScheme.CANONICAL) == case["key_canonical"]
    # Reserved products (no legacy key) emit the canonical form even in LEGACY scheme.
    if case["key_legacy"] is None and case["key_canonical"] is not None and case["relpath"] is not None:
        assert L.relpath_to_key(case["relpath"], scheme=KeyScheme.LEGACY) == case["key_canonical"]

    # parse_key recovers the product for ANY cloud-backed key (legacy + canonical),
    # including key-only products like photometry whose canonical form is
    # 'data/' + legacy prefix rather than 'data/' + relpath.
    for key in (case["key_legacy"], case["key_canonical"]):
        if key is not None:
            assert L.parse_key(key, bucket=bkt).product_type == pt


def test_siblings():
    for sib in GOLDEN["siblings"]:
        assert L.derive_sibling(sib["from"], sib["to"]) == sib["expect"]


def test_known_keys():
    for key in GOLDEN["known_keys"]:
        assert L.is_known_key(key) is True, key


def test_unknown_keys():
    for key in GOLDEN["unknown_keys"]:
        assert L.is_known_key(key) is False, key


def test_foreign_root_keys_are_rejected():
    """Keys that are not part of the contract: ``data/archive/…``, ``data/dr/…``
    and ``dr/…`` as CLOUD keys (the release tree is local), plus ``dr/<tag>/meta``
    as a relpath. Rejected by parse, classify, allowlists and compression."""
    for key in GOLDEN["foreign_root_keys"]:
        for bucket in (None, "data", "tiles"):
            with pytest.raises(LayoutError):
                L.parse_key(key, bucket=bucket)
        if not key.startswith("dr/DR") or "/products/" not in key:
            with pytest.raises(LayoutError):
                L.parse_relpath(key)
        assert L.is_known_key(key) is False, key
        assert L.is_readable_key(key) is False, key
        assert L.is_writable_key(key) is False, key
        assert L.is_compressed_key(key) is False, key


@pytest.mark.parametrize("case", GOLDEN["archive_keys"], ids=lambda c: c["key"].split("/")[-1])
def test_archive_keys_parse_read_only(case):
    """``archive/<sha256>/<relpath>`` (P1): parses with root 'archive', rebuilds to
    itself (never to the live key), is readable but never writable, has no
    siblings by string surgery, and classifies as RELEASE."""
    key = case["key"]
    pk = L.parse_key(key)
    assert pk.product_type == case["product_type"]
    assert pk.filename == case["filename"]
    assert pk.root == L.ROOT_ARCHIVE and pk.root_id == case["root_id"]
    assert not pk.is_live
    for k, v in case["scope"].items():
        assert getattr(pk.scope, k) == v
    assert L.key_for_parsed(pk) == key
    assert L.archive_key(pk.product_type, pk.scope, pk.filename, content_hash="sha256:" + case["root_id"]) == key
    assert L.key_to_relpath(key) == case["relpath"]
    # The live key is a different object: rebuilding a LIVE parse of the same relpath gives it.
    live = L.parse_relpath(case["relpath"])
    assert live.is_live and L.key_for_parsed(live) == case["live_canonical"]
    assert L.is_readable_key(key) is True and L.is_known_key(key) is True
    assert L.is_writable_key(key) is False
    assert L.is_compressed_key(key) is case["compressed"]
    assert L.tree_class(key) is L.LifecycleClass.RELEASE
    with pytest.raises(LayoutError):
        L.derive_sibling(key, "zfit")
    with pytest.raises(LayoutError):
        L.relpath_to_key("dr/DR2026.1/" + case["relpath"])


def test_archive_rejected_keys():
    for key in GOLDEN["archive_rejected_keys"]:
        with pytest.raises(LayoutError):
            L.parse_key(key)
        assert L.is_readable_key(key) is False and L.is_writable_key(key) is False


@pytest.mark.parametrize("case", GOLDEN["dr_relpaths"], ids=lambda c: c["relpath"].split("/")[1])
def test_dr_relpaths_parse_locally_only(case):
    pk = L.parse_relpath(case["relpath"])
    assert pk.product_type == case["product_type"] and pk.filename == case["filename"]
    assert pk.root == L.ROOT_DR and pk.root_id == case["root_id"]
    for k, v in case["scope"].items():
        assert getattr(pk.scope, k) == v
    assert L.tree_class(case["relpath"]) is L.LifecycleClass.RELEASE
    assert L.dr_relpath(case["root_id"], "products/" + case["relpath"].split("/products/", 1)[1]) == case["relpath"]
    with pytest.raises(LayoutError):
        L.relpath_to_key(case["relpath"])
    with pytest.raises(LayoutError):
        L.key_for_parsed(pk)
    with pytest.raises(LayoutError):
        L.parse_key("data/" + case["relpath"])


def test_dr_rejected_relpaths():
    for rp in GOLDEN["dr_rejected_relpaths"]:
        with pytest.raises(LayoutError):
            L.parse_relpath(rp)


def test_archive_rejected_hashes_and_products():
    spec = L.Scope(obs="ember_egs_p1")
    for h in GOLDEN["archive_rejected_hashes"]:
        with pytest.raises(LayoutError):
            L.archive_hex(h)
        with pytest.raises(LayoutError):
            L.archive_key("nirspec_spec", spec, "ember_egs_p1_prism_clear_15182_spec.fits", content_hash=h)
    assert L.archive_hex("SHA256:" + "A" * 64) == "a" * 64 == L.archive_hex("a" * 64)
    for ptype in GOLDEN["archive_rejected_products"]:
        assert L.is_archivable(ptype) is False
    assert L.is_archivable("nirspec_spec") and L.is_archivable("nircam_mosaic")


def test_dr_tree_helpers(tmp_path):
    assert L.is_dr_tag("DR2026.1") and not L.is_dr_tag("2026.1") and not L.is_dr_tag("DR2026")
    assert L.dr_dir("DR2026.1", root=tmp_path) == tmp_path / "dr" / "DR2026.1"
    assert L.roots(tmp_path).dr == tmp_path / "dr"
    with pytest.raises(LayoutError):
        L.dr_dir("nope", root=tmp_path)


def test_live_parses_default_to_live_root():
    for case in GOLDEN["cases"]:
        if case.get("key_canonical"):
            pk = L.parse_key(case["key_canonical"], bucket=case.get("bucket") or None) if case.get("bucket") else None
            if pk is not None:
                assert pk.is_live and pk.root_id is None
                assert L.key_for_parsed(pk) == case["key_canonical"]


def test_sibling_roundtrips():
    """A NIRSpec spectrum key and its four sidecars (what the web's sidecar
    resolver derives) form a clique under derive_sibling in both schemes."""
    for rt in GOLDEN["sibling_roundtrips"]:
        spec, sidecars = rt["spec"], rt["sidecars"]
        assert set(sidecars) == {"spectrum_json", "spectrum_1d_json", "zfit", "nirspec_lines_json"}
        assert L.parse_key(spec).product_type == "nirspec_spec"
        for kind, key in sidecars.items():
            assert L.derive_sibling(spec, kind) == key, (rt["scheme"], kind)
            assert L.derive_sibling(key, "nirspec_spec") == spec, (rt["scheme"], kind)
            assert L.parse_key(key).product_type == kind
            for other, other_key in sidecars.items():
                assert L.derive_sibling(key, other) == other_key, (rt["scheme"], kind, other)


def test_compressed_keys():
    """Gzip-on-upload: a '.fits.gz' mosaic key parses to the plain local
    filename / relpath, rebuilds to the same key, and is_compressed_key agrees;
    uncompressed siblings in the same directory pass through untouched."""
    for row in GOLDEN["compressed_keys"]:
        key = row["key"]
        pk = L.parse_key(key)
        assert pk.product_type == row["product_type"]
        assert pk.scope == Scope.from_dict(row["scope"])
        assert pk.filename == row["filename"]
        assert L.is_compressed_key(key) is row["compressed"]
        assert L.key_to_relpath(key) == row["relpath"]
        assert L.parse_relpath(row["relpath"]).filename == row["filename"]
        assert L.storage_key(pk.product_type, pk.scope, pk.filename, scheme=KeyScheme.CANONICAL) == key
        assert L.relpath_to_key(row["relpath"], scheme=KeyScheme.CANONICAL) == key


def test_generated_web_products_json_is_current():
    """web/lib/layout-products.json (the TS PRODUCTS table) is generated from this
    registry by scripts/sync_layout_products.py; a stale copy means the two arms
    no longer share one table. Same check CI runs with ``--check``."""
    import importlib.util

    script = _FIXTURE.parents[2] / "scripts" / "sync_layout_products.py"
    spec = importlib.util.spec_from_file_location("sync_layout_products", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DST.exists(), f"{mod.DST} missing: run `python {script}`"
    assert mod.DST.read_text(encoding="utf-8") == mod.render(mod.build()), (
        f"{mod.DST} is stale: run `python {script}` and commit the result")


def test_presign_allowlist_rejects_traversal():
    # The presign route's hardening: a fuzzed traversal key must never be signed.
    assert L.is_known_key("spectra/ember/../../secret.fits") is False
    assert L.is_known_key("data/../../../etc/passwd") is False


def test_every_product_has_a_golden_case():
    # Guards against adding a product to the registry without a fixture row.
    covered = {c["product_type"] for c in GOLDEN["cases"]}
    missing = set(L.PRODUCTS) - covered
    assert not missing, f"products with no golden case: {sorted(missing)}"


def test_cross_package_agreement(tmp_path, monkeypatch):
    """The module, deploy resolver, and pipeline Observation agree on the NIRSpec
    products dir — the contract the three-way footgun used to violate."""
    monkeypatch.setenv("CAMPFIRE_ROOT", str(tmp_path))
    obs = "ember_egs_p1"
    expected = L.dir_for("nirspec_spec", Scope(obs=obs))

    from campfire.deploy import config as dconfig
    (tmp_path / "products" / "nirspec" / obs).mkdir(parents=True)
    assert dconfig.resolve_obs_dir(obs) == expected

    from campfire_pipeline.nirspec.observation import Observation
    o = Observation(name=obs, field="egs", program="ember", program_id=7076,
                    data_subdir="7076", files=["jw*"], gratings=["PRISM"])
    o.setup_workspace_directory(str(tmp_path / "raw"), str(tmp_path / "products"))
    assert Path(o.workspace_dir) == expected
