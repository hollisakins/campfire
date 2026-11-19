"""NIRCam persistence: snowblind's cross-exposure flagging and the time both steps use.

snowblind >= 1 reads each exposure's saturated pixels from its own ``dq``, so
the step runs on the canonical files alone; the ``_jump.fits`` sidecar is the
jackknife step's input and this step only cleans it up.
"""

import os

import numpy as np
import pytest

pytest.importorskip("jwst")
snowblind = pytest.importorskip("snowblind")
if not hasattr(snowblind, "persist") or not hasattr(snowblind.persist, "packaged_time"):
    pytest.skip("needs snowblind >= 1 (packaged persistence times)", allow_module_level=True)

from jwst.datamodels import ImageModel  # noqa: E402

from campfire_pipeline.common import cfp  # noqa: E402
from campfire_pipeline.nircam.steps import persistence  # noqa: E402


def test_time_packaged_is_per_detector():
    a = persistence.persistence_time_for("NRCALONG")
    b = persistence.persistence_time_for("nrcb4", "packaged")
    assert a == pytest.approx(snowblind.persist.packaged_time("NRCALONG"))
    assert b == pytest.approx(snowblind.persist.packaged_time("NRCB4"))
    assert a != b


def test_time_explicit_and_fallback():
    assert persistence.persistence_time_for("NRCALONG", 2500) == 2500.0
    assert persistence.persistence_time_for("NRCALONG", "1800") == 1800.0
    # a detector snowblind has no measurement for gets its default
    assert persistence.persistence_time_for("NOSUCHDET") == snowblind.persist.DEFAULT_TIME


def _exposure(path, start_mjd, saturated=None):
    model = ImageModel(data=np.zeros((16, 16), dtype=np.float32))
    model.dq = np.zeros((16, 16), dtype=np.uint32)
    if saturated is not None:
        model.dq[saturated] = 2  # SATURATED
    model.meta.instrument.name = "NIRCAM"
    model.meta.instrument.detector = "NRCALONG"
    model.meta.exposure.start_time = start_mjd
    model.meta.exposure.end_time = start_mjd + 300.0 / 86400.0
    model.meta.filename = os.path.basename(path)
    model.save(path)
    model.close()


def test_step_runs_without_jump_sidecars_and_cleans_them_up(tmp_path):
    first = str(tmp_path / "jw00001001001_02101_00001_nrcalong.fits")
    second = str(tmp_path / "jw00001001001_02101_00002_nrcalong.fits")
    _exposure(first, 60000.0, saturated=(slice(4, 6), slice(4, 6)))
    _exposure(second, 60000.0 + 600.0 / 86400.0)

    # One exposure still has a sidecar (jackknife is off, so nothing waits on
    # it), the other has none at all: neither stops the step.
    sidecar = persistence._jump_path(first)
    open(sidecar, "wb").close()

    persistence.persistence_step(
        [first, second], None, {"time": 2500, "jackknife_enabled": False}, overwrite=True)

    with ImageModel(second) as later, ImageModel(first) as earlier:
        flagged = (later.dq & 1) > 0                      # DO_NOT_USE
        assert flagged[4:6, 4:6].all() and flagged.sum() == 4
        assert not ((earlier.dq & 1) > 0).any()           # nothing before it to flag from
    assert cfp.has_step(first, "CFP_PERS") and cfp.has_step(second, "CFP_PERS")
    assert not os.path.exists(sidecar)                    # removed: jackknife is not waiting on it


def test_step_keeps_sidecar_while_jackknife_is_pending(tmp_path):
    first = str(tmp_path / "jw00001001001_02101_00001_nrcalong.fits")
    _exposure(first, 60000.0)
    sidecar = persistence._jump_path(first)
    open(sidecar, "wb").close()

    persistence.persistence_step(
        [first], None, {"time": "packaged", "jackknife_enabled": True}, overwrite=True)

    assert os.path.exists(sidecar)                        # no CFP_JACK stamp yet
