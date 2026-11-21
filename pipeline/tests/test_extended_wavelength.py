"""Extended-wavelength reference construction (``nirspec/extended_wavelength.py``).

Synthetic flats stand in for the CRDS files: four fast-variation tables at HDUs
5/10/15/20 for an F-flat; a cube, a WAVELENGTH table at HDU 4 and a zero-padded
flat table at HDU 5 for an S-flat. CRDS is monkeypatched. Byte parity with
``generate_extended_cals()`` on the real jwst_1614 references was checked when the
port was written; these tests pin the construction's behaviour.
"""
import os
import sys
import types

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.nirspec import extended_wavelength as ew


def _flat_table(wave, data, n=None, error=True):
    """One-row flat table; ``n`` zero-pads the arrays like the CRDS S-flats."""
    n = n or len(wave)
    w = np.zeros(n, "float32"); w[:len(wave)] = wave
    d = np.zeros(n, "float32"); d[:len(data)] = data
    cols = {"slit_name": ["ANY"], "nelem": [len(wave)], "wavelength": [w], "data": [d]}
    if error:
        cols["error"] = [np.zeros(n, "float32")]
    return fits.table_to_hdu(Table(cols))


def _write_fflat(path, wave, data, filt):
    hdus = [fits.PrimaryHDU(header=fits.Header({"GRATING": "G140M", "FILTER": filt}))]
    for i in range(1, 21):
        hdus.append(_flat_table(wave, data) if i in (5, 10, 15, 20)
                    else fits.ImageHDU(np.ones((2, 2), "float32")))
    fits.HDUList(hdus).writeto(path)


def _write_sflat(path, wave, data, filt, det, nplanes):
    prim = fits.PrimaryHDU(header=fits.Header({"GRATING": "G140M", "FILTER": filt, "DETECTOR": det}))
    sci = fits.ImageHDU(np.ones((nplanes, 2, 2), "float32"), name="SCI")
    for k in range(1, nplanes + 1):
        sci.header[f"FLAT_{k:02}"] = 1.0 + 0.1 * k
    planes = fits.BinTableHDU(Table({"wavelength": np.linspace(1.0, 1.5, nplanes).astype("float32")}),
                              name="WAVELENGTH")
    fits.HDUList([prim, sci, fits.ImageHDU(np.zeros((2, 2))), fits.ImageHDU(np.zeros((2, 2))),
                  planes, _flat_table(wave, data, n=len(wave) + 50, error=False)]).writeto(path)


def _table(path, hdu):
    t = Table.read(path, hdu=hdu)
    return np.asarray(t["wavelength"][0], float), np.asarray(t["data"][0], float), int(t["nelem"][0])


# A G140M/F100LP-like flat: 0.97-1.896 um, NaN in its last samples like the 1614 F-flats.
F100_WAVE = np.round(np.arange(0.97, 1.8965, 0.0064), 4)
F100_DATA = np.linspace(2.0, 1.0, len(F100_WAVE))
F100_DATA[F100_WAVE > 1.884] = np.nan
F070_WAVE = np.round(np.arange(0.70, 1.2765, 0.0064), 4)
F070_DATA = np.linspace(3.0, 2.5, len(F070_WAVE))


def test_extend_holds_the_sample_nearest_the_hold_wavelength(tmp_path):
    src, out = tmp_path / "f.fits", tmp_path / "ext.fits"
    _write_fflat(src, F100_WAVE, F100_DATA, "F100LP")
    ew._build_extended_fflat(str(src), "G140M", "F100LP", str(out))
    hold = ew.FLAT_HOLD_WAVELENGTH_UM[("G140M", "F100LP")]
    expected = F100_DATA[np.argmin(np.abs(F100_WAVE - hold))]
    for hdu in (5, 10, 15, 20):
        w, d, nelem = _table(out, hdu)
        assert nelem == len(w)
        assert w[-1] == pytest.approx(5.3, abs=1e-6)
        assert np.all(np.diff(w) > 0)
        assert np.allclose(d[w >= hold], expected)   # the NaN tail is overwritten too
        assert np.allclose(d[w < hold], F100_DATA[F100_WAVE < hold])


def test_extend_refuses_a_nan_held_sample(tmp_path):
    data = F100_DATA.copy()
    data[F100_WAVE > 1.86] = np.nan
    src = tmp_path / "f.fits"
    _write_fflat(src, F100_WAVE, data, "F100LP")
    with pytest.raises(ValueError, match="hold wavelength"):
        ew._build_extended_fflat(str(src), "G140M", "F100LP", str(tmp_path / "ext.fits"))


def test_sflat_drops_padding_and_moves_the_last_plane(tmp_path):
    src, out = tmp_path / "s.fits", tmp_path / "ext.fits"
    data = np.linspace(1.0, 0.9, len(F100_WAVE))
    _write_sflat(src, F100_WAVE, data, "F100LP", "NRS1", nplanes=12)
    ew._build_extended_sflat(str(src), "G140M", "F100LP", str(out))
    w, d, nelem = _table(out, 5)
    assert np.all(w > 0) and np.all(np.diff(w) > 0) and nelem == len(w)
    assert w[-1] == pytest.approx(5.3, abs=1e-6)
    with fits.open(out) as h:
        assert h[1].header["FLAT_12"] == 5.3            # the last plane, not FLAT_05
        assert h[1].header["FLAT_05"] == pytest.approx(1.5)
        assert h[4].data[-1][0] == pytest.approx(5.3)


def test_f070lp_is_stitched_onto_the_extended_f100lp(tmp_path):
    f100, f070 = tmp_path / "f100.fits", tmp_path / "f070.fits"
    _write_fflat(f100, F100_WAVE, F100_DATA, "F100LP")
    _write_fflat(f070, F070_WAVE, F070_DATA, "F070LP")
    ext100, ext070 = tmp_path / "ext100.fits", tmp_path / "ext070.fits"
    ew._build_extended_fflat(str(f100), "G140M", "F100LP", str(ext100))
    ew._build_stitched_fflat(str(f070), str(ext100), str(ext070))
    w100, d100, _ = _table(ext100, 5)
    w, d, nelem = _table(ext070, 5)
    assert nelem == len(w) and np.all(np.diff(w) > 0)
    seam = len(F070_WAVE)                    # first F100LP sample
    half = ew._STITCH_HALF_WINDOW
    # Own flat below the smoothed seam window, the extended F100LP flat above it.
    assert np.allclose(w[:seam], F070_WAVE)
    assert np.allclose(d[:seam - half], F070_DATA[:seam - half])
    red = w100 > w[seam - 1]
    assert np.array_equal(w[seam:], w100[red])
    assert np.allclose(d[seam + half:], d100[red][half:])
    assert w[-1] == pytest.approx(5.3, abs=1e-6)


def test_stitch_drops_nan_before_smoothing_the_seam(tmp_path):
    """The F070LP F-flat ends in NaN: those samples go, the seam stays finite."""
    data = F070_DATA.copy()
    data[F070_WAVE > 1.26] = np.nan
    f100, f070 = tmp_path / "f100.fits", tmp_path / "f070.fits"
    _write_fflat(f100, F100_WAVE, F100_DATA, "F100LP")
    _write_fflat(f070, F070_WAVE, data, "F070LP")
    ext100, ext070 = tmp_path / "ext100.fits", tmp_path / "ext070.fits"
    ew._build_extended_fflat(str(f100), "G140M", "F100LP", str(ext100))
    ew._build_stitched_fflat(str(f070), str(ext100), str(ext070))
    w, d, nelem = _table(ext070, 5)
    assert np.all(np.isfinite(d)) and nelem == len(w)
    assert not np.any((w > 1.26) & (w <= F070_WAVE.max() + 1e-6))
    assert np.all(np.diff(w) > 0)


def test_cache_names_carry_the_version_and_the_stitch_source():
    assert ew._extended_output_path("/c/jwst_nirspec_fflat_0200.fits", "/x") == \
        f"/x/extended_v{ew._EXTENSION_VERSION}_jwst_nirspec_fflat_0200.fits"
    assert ew._extended_output_path("/c/jwst_nirspec_sflat_0171.fits", "/x",
                                    stitched_onto="/c/jwst_nirspec_sflat_0231.fits") == \
        f"/x/extended_v{ew._EXTENSION_VERSION}_jwst_nirspec_sflat_0171+0231.fits"


@pytest.fixture
def fake_crds(tmp_path, monkeypatch):
    """CRDS stand-in resolving synthetic flats per (detector, filter)."""
    refdir = tmp_path / "refs"
    refdir.mkdir()
    files = {}
    for filt, wave, data, fnum in (("F100LP", F100_WAVE, F100_DATA, "0200"),
                                   ("F070LP", F070_WAVE, F070_DATA, "0196")):
        files[("fflat", filt)] = str(refdir / f"jwst_nirspec_fflat_{fnum}.fits")
        _write_fflat(files[("fflat", filt)], wave, data, filt)
        for det, snum in (("NRS1", {"F100LP": "0231", "F070LP": "0171"}[filt]),):
            files[("sflat", filt, det)] = str(refdir / f"jwst_nirspec_sflat_{snum}.fits")
            _write_sflat(files[("sflat", filt, det)], wave, np.linspace(1, 0.9, len(wave)), filt, det,
                         nplanes=12 if filt == "F100LP" else 5)
    calls = []

    def getreferences(params, reftypes, observatory):
        calls.append((params["FILTER"], tuple(reftypes)))
        out = {}
        for r in reftypes:
            if r == "fflat":
                out[r] = files[("fflat", params["FILTER"])]
            elif r == "sflat":
                out[r] = files[("sflat", params["FILTER"], params["DETECTOR"])]
            else:
                out[r] = "/nonexistent/jwst_nirspec_wavelengthrange_0008.asdf"
        return out

    monkeypatch.setitem(sys.modules, "crds", types.SimpleNamespace(getreferences=getreferences))
    monkeypatch.setattr(ew, "_build_extended_wavelengthrange",
                        lambda src, out: open(out, "w").close())
    monkeypatch.setenv("CAMPFIRE_ROOT", str(tmp_path / "root"))
    return calls


def _rate(tmp_path, filt, det="NRS1"):
    path = tmp_path / f"{det}_{filt}_rate.fits"
    fits.PrimaryHDU(header=fits.Header({"DETECTOR": det, "GRATING": "G140M", "FILTER": filt,
                                        "EXP_TYPE": "NRS_MSASPEC"})).writeto(path)
    return str(path)


def test_build_maps_f070lp_to_stitched_flats(tmp_path, fake_crds):
    refs = ew.build_extended_reference_files([_rate(tmp_path, "F070LP")])
    entry = refs[("NRS1", "G140M", "F070LP")]
    v = ew._EXTENSION_VERSION
    assert os.path.basename(entry["fflat"]) == f"extended_v{v}_jwst_nirspec_fflat_0196+0200.fits"
    assert os.path.basename(entry["sflat"]) == f"extended_v{v}_jwst_nirspec_sflat_0171+0231.fits"
    cache = os.path.dirname(entry["fflat"])
    # The extended F100LP flats it stitched on were built (and cached) on the way.
    assert os.path.exists(os.path.join(cache, f"extended_v{v}_jwst_nirspec_fflat_0200.fits"))
    assert os.path.exists(os.path.join(cache, f"extended_v{v}_jwst_nirspec_sflat_0231.fits"))
    assert ("F100LP", ("fflat", "sflat")) in fake_crds


def test_build_shares_the_f100lp_flats_between_configs(tmp_path, fake_crds):
    refs = ew.build_extended_reference_files(
        [_rate(tmp_path, "F070LP"), _rate(tmp_path, "F100LP")])
    f100 = refs[("NRS1", "G140M", "F100LP")]
    assert os.path.basename(f100["fflat"]).endswith("_jwst_nirspec_fflat_0200.fits")
    assert "+" not in os.path.basename(f100["fflat"])


def test_guard_checks_the_stitch_source_filter(tmp_path, monkeypatch):
    seen = []

    def getrecommendations(params, reftypes, observatory, fast):
        seen.append(params["FILTER"])
        base = ew.EXTENDED_CALIBRATION_REFERENCES[(params["DETECTOR"], params["GRATING"], params["FILTER"])]
        recs = dict(base)
        if params["FILTER"] == "F100LP":
            recs["fflat"] = "jwst_nirspec_fflat_0168.fits"   # drifted
        return recs

    monkeypatch.setitem(sys.modules, "crds", types.SimpleNamespace(
        getrecommendations=getrecommendations, get_context_name=lambda obs: "jwst_test.pmap"))
    with pytest.raises(RuntimeError, match="F100LP"):
        ew.verify_extension_crds_compatibility([_rate(tmp_path, "F070LP")])
    assert sorted(seen) == ["F070LP", "F100LP"]
