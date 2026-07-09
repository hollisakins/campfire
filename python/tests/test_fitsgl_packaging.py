"""FitsGL packaging smoke test (epic #337, Phase 1 / #338).

The `campfire[fitsgl]` extra wires in FitsGL's `fitsgl-py` producer as a library.
FitsGL isn't on PyPI, so it's only importable when the extra (or a side-by-side
editable checkout) is installed — the test skips otherwise. When it *is* present,
it pins the exact public entrypoints CAMPFIRE calls as a library so a breaking
rename in FitsGL surfaces here rather than deep in a deploy run.
"""

import importlib

import pytest

fitsgl = pytest.importorskip(
    "fitsgl",
    reason="FitsGL not installed — run `pip install -e .[fitsgl]` "
    "(or an editable side-by-side `fitsgl-py` checkout) to exercise this.",
)


# (module, attribute) pairs CAMPFIRE consumes; see docs/design-fitsgl-integration.md §4.
LIBRARY_ENTRYPOINTS = [
    ("fitsgl.build", "build_dataset"),
    ("fitsgl.build_pyramid", "build_pyramid"),
    ("fitsgl.deploy", "deploy_dataset"),
]


@pytest.mark.parametrize("module_name, attr", LIBRARY_ENTRYPOINTS)
def test_library_entrypoint_is_callable(module_name, attr):
    module = importlib.import_module(module_name)
    obj = getattr(module, attr, None)
    assert obj is not None, f"{module_name}.{attr} is missing"
    assert callable(obj), f"{module_name}.{attr} is not callable"
