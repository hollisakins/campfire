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
