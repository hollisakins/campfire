"""Unit tests for B5 (epic #210) unconditional intermediate-exposure upload.

Pure functions, no DB/cloud: canonical-exposure discovery, source-id filtering,
exposure_ref derivation, and the no-summary JWST-PID fallback. The end-to-end
upload + registration + auto-draft is exercised live against local Supabase in CI
(`campfire deploy --obs … --local`).
"""
from campfire.deploy.discover import discover_spectrum_exposures
from campfire.deploy.deploy import _filter_exposures_by_source_ids, _jwst_pid_from_obs_cfg
from campfire.deploy.registry import _exposure_ref_for


def _touch(d, name):
    p = d / name
    p.write_bytes(b"\x00")
    return p


def test_discover_spectrum_exposures_excludes_finals(tmp_path):
    # canonical per-exposure intermediates
    _touch(tmp_path, "jw07076020001_04101_00001_nrs1_117757.fits")
    _touch(tmp_path, "jw07076020001_04101_00001_nrs2_117757.fits")
    _touch(tmp_path, "jw07076020001_04101_00002_nrs1_120383.fits")
    # finals + other products that must NOT be picked up
    _touch(tmp_path, "ember_egs_p1_prism_clear_117757_spec.fits")
    _touch(tmp_path, "ember_egs_p1_summary.ecsv")
    _touch(tmp_path, "117757_rgb.png")

    found = discover_spectrum_exposures(tmp_path)
    names = sorted(p.name for p in found)
    assert names == [
        "jw07076020001_04101_00001_nrs1_117757.fits",
        "jw07076020001_04101_00001_nrs2_117757.fits",
        "jw07076020001_04101_00002_nrs1_120383.fits",
    ]
    assert all("_spec.fits" not in n for n in names)


def test_filter_exposures_by_source_ids(tmp_path):
    files = [
        _touch(tmp_path, "jw07076020001_04101_00001_nrs1_117757.fits"),
        _touch(tmp_path, "jw07076020001_04101_00001_nrs2_117757.fits"),
        _touch(tmp_path, "jw07076020001_04101_00002_nrs1_120383.fits"),
    ]
    kept = _filter_exposures_by_source_ids(files, [117757])
    assert sorted(p.name for p in kept) == [
        "jw07076020001_04101_00001_nrs1_117757.fits",
        "jw07076020001_04101_00001_nrs2_117757.fits",
    ]


def test_exposure_ref_is_stem_for_intermediates_else_none():
    assert _exposure_ref_for(
        "nirspec_spectrum_exposure", "jw07076020001_04101_00001_nrs1_117757.fits"
    ) == "jw07076020001_04101_00001_nrs1_117757"
    # finals / other products carry no exposure_ref (NULL; never collide on the
    # partial-unique (product_type, exposure_ref) registry index)
    assert _exposure_ref_for("nirspec_spec", "x_spec.fits") is None
    assert _exposure_ref_for("rgb", "12345_rgb.png") is None


def test_jwst_pid_from_obs_cfg():
    assert _jwst_pid_from_obs_cfg({"data_subdir": "7076"}) == 7076
    assert _jwst_pid_from_obs_cfg({"files": "jw07076020001*"}) == 7076
    assert _jwst_pid_from_obs_cfg({"files": ["jw07076020001*"]}) == 7076
    assert _jwst_pid_from_obs_cfg({}) == 0
