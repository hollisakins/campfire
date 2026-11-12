"""Modules moved to ``campfire_pipeline.imaging`` keep their old import path.

The old ``campfire_pipeline.nircam.<name>`` must be the *same module object* as
``campfire_pipeline.imaging.<name>``, not a copy or a star re-export: code and
tests monkeypatch attributes through the old path, and a second module object
would silently split that state.
"""

import importlib

import pytest

MOVED = [
    'geometry', 'manifest', 'mosaic_exp', 'bkgsub', 'drizzle',
    'refcat.compare', 'refcat.extract', 'refcat.hsc', 'refcat.io',
    'refcat.merge', 'refcat.motion', 'refcat.query',
    'align.detect', 'align.footprint', 'align.histmatch',
]


@pytest.mark.parametrize('name', MOVED)
def test_old_path_is_the_same_module(name):
    new = importlib.import_module(f'campfire_pipeline.imaging.{name}')
    old = importlib.import_module(f'campfire_pipeline.nircam.{name}')
    assert old is new
    # `from campfire_pipeline.nircam[.sub] import <name>` resolves to it as well
    parent, _, leaf = f'campfire_pipeline.nircam.{name}'.rpartition('.')
    assert getattr(importlib.import_module(parent), leaf) is new


def test_refcat_package_exports_are_the_shared_objects():
    # The old package keeps its own __init__ (its CLI lives there), re-exporting
    # the same function objects as the shared package.
    import campfire_pipeline.imaging.refcat as new
    import campfire_pipeline.nircam.refcat as old
    for name in new.__all__:
        assert getattr(old, name) is getattr(new, name)


def test_private_names_survive_the_alias():
    # Underscore names are not exported by `import *`; the alias keeps them.
    from campfire_pipeline.nircam import manifest
    assert callable(manifest._resample_config_hash)


def test_imaging_does_not_import_an_instrument_at_module_top():
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import campfire_pipeline.imaging.geometry\n"
        "import campfire_pipeline.imaging.manifest\n"
        "import campfire_pipeline.imaging.mosaic_exp\n"
        "import campfire_pipeline.imaging.drizzle\n"
        "import campfire_pipeline.imaging.exposure\n"
        "import campfire_pipeline.imaging.grid\n"
        "import campfire_pipeline.imaging.mosaic_io\n"
        "import campfire_pipeline.imaging.refcat\n"
        "import campfire_pipeline.imaging.refcat.extract\n"
        "import campfire_pipeline.imaging.refcat.merge\n"
        "import campfire_pipeline.imaging.refcat.motion\n"
        "import campfire_pipeline.imaging.align.detect\n"
        "import campfire_pipeline.imaging.align.footprint\n"
        "import campfire_pipeline.imaging.align.histmatch\n"
        "bad = [m for m in sys.modules if m.startswith(("
        "'campfire_pipeline.nircam', 'campfire_pipeline.hst'))]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == '', f'imaging pulled in instrument modules: {out}'
