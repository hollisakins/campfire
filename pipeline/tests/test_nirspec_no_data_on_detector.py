"""Stage 1 on an exposure with no open shutter on the detector (#681).

`jwst` raises ``NoDataOnDetectorError`` from ``assign_wcs`` when nothing
projects onto a detector (typically NRS2). Stage 1 calls ``assign_wcs`` in three
places; each must carry on rather than leave the exposure without a rate file.
"""

import numpy as np
import pytest

pytest.importorskip("jwst")

from jwst.assign_wcs import AssignWcsStep  # noqa: E402
from jwst.assign_wcs.util import NoDataOnDetectorError  # noqa: E402
from jwst.datamodels import ImageModel  # noqa: E402

from campfire_pipeline.nirspec import stage1  # noqa: E402


def _raise_no_data(self, *args, **kwargs):
    raise NoDataOnDetectorError("No open slits fall on detector NRS2.")


def _model():
    model = ImageModel(data=np.zeros((2048, 2048), dtype=np.float32))
    model.meta.instrument.name = "NIRSPEC"
    model.meta.instrument.detector = "NRS2"
    model.meta.instrument.filter = "CLEAR"
    model.meta.instrument.grating = "PRISM"
    model.meta.exposure.type = "NRS_MSASPEC"
    return model


@pytest.fixture
def detector1_calls(monkeypatch, tmp_path):
    """Detector1Pipeline.call stub that fails like jwst does while masking."""
    from jwst.pipeline import Detector1Pipeline

    calls = []

    def fake_call(uncal_file, save_results=True, steps=None):
        mask = steps["clean_flicker_noise"]["mask_science_regions"]
        calls.append(mask)
        if mask and not steps["clean_flicker_noise"]["skip"]:
            raise NoDataOnDetectorError("No open slits fall on detector NRS2.")
        (tmp_path / uncal_file.replace("_uncal.fits", "_rate.fits")).write_bytes(b"")

    monkeypatch.setattr(Detector1Pipeline, "call", staticmethod(fake_call))
    return calls


def test_detector1_reruns_without_science_mask(detector1_calls, tmp_path):
    ok = stage1.run_stage1_single_uncal(
        "jw01747017001_03101_00001_nrs2_uncal.fits",
        str(tmp_path),
        cleanup_uncal=False,
        cleanup_rateints=False,
    )
    assert ok == 1
    assert detector1_calls == [True, False]
    assert (tmp_path / "jw01747017001_03101_00001_nrs2_rate.fits").exists()


def test_detector1_does_not_rerun_when_masking_is_off(monkeypatch, tmp_path):
    """The error from anywhere else is a real failure, reported as before."""
    from jwst.pipeline import Detector1Pipeline

    calls = []

    def fake_call(uncal_file, save_results=True, steps=None):
        calls.append(steps["clean_flicker_noise"]["mask_science_regions"])
        raise NoDataOnDetectorError()

    monkeypatch.setattr(Detector1Pipeline, "call", staticmethod(fake_call))
    ok = stage1.run_stage1_single_uncal(
        "x_nrs2_uncal.fits", str(tmp_path), mask_science_regions=False,
        cleanup_uncal=False, cleanup_rateints=False,
    )
    assert ok == 0
    assert calls == [False]


def test_mask_slits_without_science_masks_nothing_extra(monkeypatch):
    from jwst.msaflagopen import msaflagopen_step

    monkeypatch.setattr(AssignWcsStep, "run", _raise_no_data)
    monkeypatch.setattr(
        msaflagopen_step.MSAFlagOpenStep, "get_reference_file",
        lambda self, model, reftype: "msaoper.json",
    )
    # assign_wcs raised, so meta.ref_file is empty and jwst's
    # create_reference_filename_dictionary (left unstubbed) would fail on it:
    # the names must come from the step's own CRDS lookup instead.
    looked_up = []
    monkeypatch.setattr(
        AssignWcsStep, "get_reference_file",
        lambda self, model, reftype: looked_up.append(reftype) or f"{reftype}.asdf",
    )
    monkeypatch.setattr("jwst.msaflagopen.msaflag_open.create_slitlets", lambda ref: [])

    model = _model()
    mask = np.full(model.data.shape, True)
    mask[:10] = False
    out = stage1.mask_slits(model, "", mask.copy())
    assert np.array_equal(out, mask)
    assert looked_up == list(AssignWcsStep.reference_file_types)


def test_prefetch_survives_no_data(monkeypatch, tmp_path):
    from jwst.msaflagopen.msaflagopen_step import MSAFlagOpenStep

    rate = tmp_path / "jw01747017001_03101_00001_nrs2_rate.fits"
    _model().save(rate)

    fetched = []
    monkeypatch.setattr(AssignWcsStep, "run", _raise_no_data)
    monkeypatch.setattr(
        MSAFlagOpenStep, "get_reference_file",
        lambda self, model, reftype: fetched.append(reftype),
    )
    monkeypatch.setattr(stage1, "_get_pictureframe_file", lambda f: fetched.append("pictureframe"))

    stage1._prefetch_crds_references([str(rate)])
    assert fetched == ["msaoper", "pictureframe"]
