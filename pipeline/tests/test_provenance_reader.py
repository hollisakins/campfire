"""Pipeline-side provenance tests (the write end of the chain).

read_fits_metadata authors provenance once from the FITS primary header and
hands it on verbatim. These tests pin the two defects this feature fixed:

  * cfpipe_version comes from the config-aware CMPFRVER card, so a
    ``[pipeline].version`` override flows through (it is not recomputed from a
    config-less package __version__);
  * reduced_at comes from CMPFRTIM (the real reduction time), not a
    summary-build wall-clock — and the earliest CMPFRTIM across an
    observation's products is what the observation-level metadata reports.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.metadata.reader import read_fits_metadata
from campfire_pipeline.metadata.summary import _earliest_reduced_at


def _write_spec_fits(path: Path, *, cmpfrver: str, cmpfrtim: str | None,
                     crds_ctx: str = "jwst_1210.pmap", cal_ver: str = "1.14.0") -> Path:
    ph = fits.PrimaryHDU()
    ph.header["PROGRAM"] = 1234
    ph.header["PI_NAME"] = "Tester"
    ph.header["DATE-OBS"] = "2024-01-01"
    ph.header["EFFEXPTM"] = 3600.0
    ph.header["CAL_VER"] = cal_ver
    ph.header["CRDS_CTX"] = crds_ctx
    ph.header["CMPFRVER"] = cmpfrver
    if cmpfrtim is not None:
        ph.header["CMPFRTIM"] = cmpfrtim

    sci = fits.ImageHDU(name="SCI")
    sci.header["SRCRA"] = 150.0
    sci.header["SRCDEC"] = 2.0

    cols = fits.ColDefs([
        fits.Column(name="wave", format="E", array=np.array([1.0, 2.0, 3.0])),
        fits.Column(name="fnu", format="E", array=np.array([1.0, 2.0, 3.0])),
        fits.Column(name="fnu_err", format="E", array=np.array([0.1, 0.1, 0.1])),
    ])
    spec = fits.BinTableHDU.from_columns(cols, name="SPEC1D")
    fits.HDUList([ph, sci, spec]).writeto(path, overwrite=True)
    return path


def test_cfpipe_version_from_header_carries_override(tmp_path):
    """The override string lands in cfpipe_version (not a recomputed git
    string), and the collapsed reduction_version key is gone."""
    p = _write_spec_fits(
        tmp_path / "ember_uds_p4_prism_clear_100_spec.fits",
        cmpfrver="experimental-bkg",
        cmpfrtim="2026-06-27T12:00:00+00:00",
    )
    meta = read_fits_metadata(p, "ember_uds_p4")
    assert meta["cfpipe_version"] == "experimental-bkg"
    assert "reduction_version" not in meta


def test_reduced_at_from_cmpfrtim(tmp_path):
    p = _write_spec_fits(
        tmp_path / "ember_uds_p4_prism_clear_100_spec.fits",
        cmpfrver="0.4.0",
        cmpfrtim="2026-06-27T12:00:00+00:00",
    )
    meta = read_fits_metadata(p, "ember_uds_p4")
    assert meta["reduced_at"] == "2026-06-27T12:00:00+00:00"
    # the other provenance fields ride along verbatim from the header
    assert meta["crds_context"] == "jwst_1210.pmap"
    assert meta["jwst_version"] == "1.14.0"


def test_reduced_at_absent_when_no_cmpfrtim(tmp_path):
    p = _write_spec_fits(
        tmp_path / "ember_uds_p4_prism_clear_100_spec.fits",
        cmpfrver="0.4.0",
        cmpfrtim=None,
    )
    meta = read_fits_metadata(p, "ember_uds_p4")
    assert meta["reduced_at"] is None


@pytest.mark.parametrize("values,expected", [
    (["2026-06-10T00:00:00+00:00", "2026-06-01T00:00:00+00:00"], "2026-06-01T00:00:00+00:00"),
    ([None, "2026-06-05T00:00:00+00:00", None], "2026-06-05T00:00:00+00:00"),
    ([None, "", "None"], None),
    ([], None),
])
def test_earliest_reduced_at(values, expected):
    assert _earliest_reduced_at(values) == expected
