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
    monkeypatch.setattr(stage1, "_open_fixed_slits", lambda model: [])

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


def test_mask_slits_masks_fixed_slit_footprints(monkeypatch):
    """A fixed slit is masked by its own (grown) footprint, not as a band of rows."""
    from types import SimpleNamespace

    import gwcs.wcs
    import jwst.datamodels
    from jwst.assign_wcs import nirspec
    from jwst.msaflagopen import msaflagopen_step

    monkeypatch.setattr(AssignWcsStep, "run", _raise_no_data)
    monkeypatch.setattr(
        msaflagopen_step.MSAFlagOpenStep, "get_reference_file",
        lambda self, model, reftype: "msaoper.json",
    )
    monkeypatch.setattr(
        AssignWcsStep, "get_reference_file", lambda self, model, reftype: f"{reftype}.asdf",
    )
    monkeypatch.setattr("jwst.msaflagopen.msaflag_open.create_slitlets", lambda ref: [])

    # S200B1 lands on the detector; S200A1 projects off it (jwst then hands back
    # a sliver at the edge whose WCS is all NaN).
    on_detector = SimpleNamespace(name="S200B1", slit_id=5)
    off_detector = SimpleNamespace(name="S200A1", slit_id=1)
    monkeypatch.setattr(stage1, "_open_fixed_slits", lambda model: [on_detector, off_detector])
    boxes = {5: ((400, 800), (930, 960)), 1: ((2633.1, 2047.5), (1000, 1030))}

    class FakeWCS:
        def __init__(self, pipeline):
            self.bounding_box = None

        def __call__(self, x, y, selector):
            value = np.zeros(x.shape) if selector == 5 else np.full(x.shape, np.nan)
            return value, value, value, value

    monkeypatch.setattr(gwcs.wcs, "WCS", FakeWCS)
    monkeypatch.setattr(nirspec, "slitlets_wcs", lambda model, refs, slits: None)
    monkeypatch.setattr(nirspec, "generate_compound_bbox", lambda model, slits: boxes)
    monkeypatch.setattr(
        jwst.datamodels, "ImageModel",
        lambda: SimpleNamespace(meta=SimpleNamespace(
            wcs=None, wcsinfo=None, exposure=SimpleNamespace(type=None))),
    )

    model = _model()
    out = stage1.mask_slits(model, "", np.full(model.data.shape, True))

    grow = stage1.FIXED_SLIT_MASK_GROW_PX
    assert not out[930:961, 400:801].any()               # the footprint
    assert not out[930 - grow, 600] and out[930 - grow - 1, 600]   # grown by `grow` rows
    assert not out[945, 800 + grow] and out[945, 800 + grow + 1]
    assert out[1024, 100] and out[990, 1500]             # the rest of the old band is background
    assert out[1000:1031].all()                          # nothing from the off-detector slit


def _record_detector1(monkeypatch, tmp_path, status="COMPLETE"):
    """Detector1Pipeline.call stub recording the steps and writing a rate header.

    *status* is what clean_flicker_noise reports whenever it is asked to mask
    science regions itself or is handed a mask; otherwise it reports COMPLETE.
    """
    from astropy.io import fits
    from jwst.pipeline import Detector1Pipeline

    calls = []

    def fake_call(uncal_file, save_results=True, steps=None):
        calls.append(steps)
        flicker = steps["clean_flicker_noise"]
        masked = flicker["mask_science_regions"] or "user_mask" in flicker
        hdr = fits.Header()
        hdr["S_CLNFNS"] = status if masked else "COMPLETE"
        fits.PrimaryHDU(header=hdr).writeto(
            tmp_path / uncal_file.replace("_uncal.fits", "_rate.fits"), overwrite=True)

    monkeypatch.setattr(Detector1Pipeline, "call", staticmethod(fake_call))
    return calls


def test_detector1_reruns_when_flicker_step_reports_failed(monkeypatch, tmp_path):
    """jwst >= 3.0 marks the step FAILED instead of raising (#681)."""
    calls = _record_detector1(monkeypatch, tmp_path, status="FAILED")
    stage1.run_stage1_single_uncal(
        "x_nrs2_uncal.fits", str(tmp_path), cleanup_uncal=False, cleanup_rateints=False)
    # no uncal on disk, so no trace mask: the step is first asked to mask itself
    assert [c["clean_flicker_noise"]["mask_science_regions"] for c in calls] == [True, False]
    assert "user_mask" not in calls[1]["clean_flicker_noise"]


def test_detector1_passes_trace_mask_and_explicit_parameters(monkeypatch, tmp_path):
    from jwst.pipeline import Detector1Pipeline

    calls = _record_detector1(monkeypatch, tmp_path)
    mask_file = tmp_path / "x_nrs1_flickermask.fits"
    seen = {}

    def fake_mask(uncal_file, workspace_dir, override_wavelength_range=None):
        seen["override"] = override_wavelength_range
        mask_file.write_bytes(b"")
        return str(mask_file)

    monkeypatch.setattr(stage1, "_write_flicker_mask", fake_mask)
    stage1.run_stage1_single_uncal(
        "x_nrs1_uncal.fits", str(tmp_path), cleanup_uncal=False, cleanup_rateints=False,
        override_wavelength_range={"PRISM": [0.5, 5.6]}, flicker_n_sigma=1.5)

    assert len(calls) == 1
    flicker = calls[0]["clean_flicker_noise"]
    assert flicker["user_mask"] == str(mask_file)
    assert flicker["mask_science_regions"] is False      # the user mask replaces the step's own
    assert (flicker["fit_method"], flicker["background_method"], flicker["n_sigma"]) == (
        "median", "median", 1.5)
    assert seen["override"] == {"PRISM": [0.5, 5.6]}
    assert not mask_file.exists()                        # removed once Detector1 is done
    if "picture_frame" in Detector1Pipeline.step_defs:   # jwst >= 2.0
        assert calls[0]["picture_frame"] == {"skip": True}


def test_detector1_builds_no_mask_when_flicker_cleaning_is_off(monkeypatch, tmp_path):
    calls = _record_detector1(monkeypatch, tmp_path)
    monkeypatch.setattr(
        stage1, "_write_flicker_mask",
        lambda *a, **k: pytest.fail("no mask is needed when the step is skipped"))
    stage1.run_stage1_single_uncal(
        "x_nrs1_uncal.fits", str(tmp_path), do_clean_flicker_noise=False,
        cleanup_uncal=False, cleanup_rateints=False)
    assert calls[0]["clean_flicker_noise"]["skip"] is True
    assert "user_mask" not in calls[0]["clean_flicker_noise"]
