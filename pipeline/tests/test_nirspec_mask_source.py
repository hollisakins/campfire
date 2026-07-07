"""P3b: manual masks come from reference/nirspec/<obs>/masks/*.reg, not the TOML.

Verifies the read-source move (Observation.setup_workspace_directory) and the
reference-dir writer (masks.write_reference_masks) the local editors now use.
"""
import os

import pytest

pytest.importorskip("campfire_pipeline")
from campfire_pipeline.nirspec.observation import Observation  # noqa: E402
from campfire_pipeline.nirspec import masks  # noqa: E402

OBS = "ember_egs_p1"
STEM = "jw07076020001_04101_00001_nrs1"


def _observation():
    return Observation(name=OBS, field="egs", program="ember", program_id=7076,
                       data_subdir="7076", files=["jw*"], gratings=["PRISM"])


def test_manual_masks_loaded_from_reference_reg(tmp_path):
    ref_masks = tmp_path / "reference" / "nirspec" / OBS / "masks"
    ref_masks.mkdir(parents=True)
    (ref_masks / f"{STEM}.reg").write_text("# header\nimage\npolygon(1,1,10,1,10,10)\n")

    o = _observation()
    o.setup_workspace_directory(str(tmp_path / "raw"), str(tmp_path / "products"))

    # keyed by the _rate_basename stem, carrying the file content
    assert STEM in o.manual_masks
    assert "polygon(1,1,10,1,10,10)" in o.manual_masks[STEM]


def test_manual_masks_empty_when_no_masks_dir(tmp_path):
    # No reference masks/ dir → empty (proves nothing else, e.g. a TOML [masks]
    # table, feeds manual_masks anymore).
    o = _observation()
    o.setup_workspace_directory(str(tmp_path / "raw"), str(tmp_path / "products"))
    assert o.manual_masks == {}


def test_write_reference_masks_writes_and_clears(tmp_path):
    class _Obs:
        reference_dir = str(tmp_path / "reference" / "nirspec" / OBS)

    obs = _Obs()
    reg_path = os.path.join(obs.reference_dir, "masks", f"{STEM}.reg")

    masks.write_reference_masks(obs, {STEM: "image\npolygon(1,1,2,1,2,2)"})
    assert os.path.exists(reg_path)
    assert "polygon(1,1,2,1,2,2)" in open(reg_path).read()

    masks.write_reference_masks(obs, {STEM: None})  # clear
    assert not os.path.exists(reg_path)
