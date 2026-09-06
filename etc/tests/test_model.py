"""Regression tests for the calculator core against the published 2026.09
depth tables (computed independently by the original analysis scripts)."""

import numpy as np
import pytest

import campfire_etc as ce
from campfire_etc.model import ModelError, READOUT_GROUP_S

M = ce.load_model("2026.09")   # pinned: the numbers below are this model's
PRISM = M.get("prism")

# From analysis/dispersers/prism_clear/payload.json (model 2026.09): T = 10 ks,
# t_exp = 1000 s, centred point source, at lam_out[0:3] = 0.65, 1.15, 1.65 um.
PRISM_10KS = {
    "wave": [0.65, 1.15, 1.65],
    "sig_pix_nJy": [4.478753349136493, 2.3863491496791363, 2.7839844861158],
    "sig_opt_nJy": [9.07684003911615, 3.7692652895902965, 4.598341659178076],
    "sig_3px_nJy": [8.780724575228819, 3.7494817683726933, 4.525093007347401],
    "ab5_pix": [27.257738284274907, 28.211933226226726, 27.996071898064727],
    "ab5_res": [27.559081603874006, 28.638400953203544, 28.5125307029586],
    "line5": [4.308436403031944e-18, 1.5890023017433559e-18, 9.71047231719418e-19],
}


def test_manifest_and_loading():
    assert M.version == "2026.09"
    assert set(M.dispersers) == {"prism_clear", "g140m_f070lp", "g140m_f100lp", "g140h_f100lp",
                                 "g235m_f170lp", "g235h_f170lp", "g395m_f290lp", "g395h_f290lp"}
    assert ce.model.available_versions()["latest"] == "2026.09"


def test_disperser_aliases():
    assert M.get("PRISM/CLEAR").key == "prism_clear"
    assert M.get("g395m").key == "g395m_f290lp"
    assert M.get("G140M-F100LP").key == "g140m_f100lp"
    with pytest.raises(ModelError, match="ambiguous"):
        M.get("g140m")
    with pytest.raises(ModelError, match="unknown"):
        M.get("g999x")


def test_exposure_timing():
    e = ce.make_exposure("nrsirs2", 13, 1, 6)
    assert e.per_exposure_s == pytest.approx(13 * READOUT_GROUP_S["nrsirs2"])
    assert e.total_s == pytest.approx(6 * e.per_exposure_s)
    d = ce.make_exposure(total_s=10000, per_exposure_s=1000)
    assert d.direct and d.total_s == 10000
    with pytest.raises(ModelError):
        ce.make_exposure("nrsirs2", None)
    with pytest.raises(ModelError):
        ce.make_exposure(total_s=500, per_exposure_s=1000)
    with pytest.raises(ModelError):
        ce.make_exposure("bogus", 10)


def test_prism_depths_match_published_tables():
    e = ce.make_exposure(total_s=10000, per_exposure_s=1000)
    w = np.array(PRISM_10KS["wave"])
    opt = ce.continuum(PRISM, e, w, None, extraction="optimal")
    box = ce.continuum(PRISM, e, w, None, extraction="3px")
    assert opt["sig_pix"] * 1e3 == pytest.approx(PRISM_10KS["sig_pix_nJy"], rel=1e-9)
    assert opt["sig_1d"] * 1e3 == pytest.approx(PRISM_10KS["sig_opt_nJy"], rel=1e-9)
    assert box["sig_1d"] * 1e3 == pytest.approx(PRISM_10KS["sig_3px_nJy"], rel=1e-9)
    assert opt["ab5_pix"] == pytest.approx(PRISM_10KS["ab5_pix"], abs=1e-9)
    assert opt["ab5_res"] == pytest.approx(PRISM_10KS["ab5_res"], abs=1e-9)
    assert opt["line5"] == pytest.approx(PRISM_10KS["line5"], rel=1e-9)


def test_scalings():
    e1 = ce.make_exposure(total_s=10000, per_exposure_s=1000)
    e4 = ce.make_exposure(total_s=40000, per_exposure_s=1000)
    w = np.array([2.0])
    s1 = ce.continuum(PRISM, e1, w)["sig_1d"][0]; s4 = ce.continuum(PRISM, e4, w)["sig_1d"][0]
    assert s4 == pytest.approx(s1 / 2)                      # sigma ~ T^-1/2 at fixed t_exp
    long = ce.make_exposure(total_s=10000, per_exposure_s=2000)
    assert ce.continuum(PRISM, long, w)["sig_1d"][0] < s1     # longer exposures beat read noise
    # gratings are more read-noise limited than PRISM
    g = M.get("g395m")
    r_prism = ce.continuum(PRISM, e1, np.array([4.0]))["sig_1d"][0] / ce.continuum(PRISM, long, np.array([4.0]))["sig_1d"][0]
    r_grat = ce.continuum(g, e1, np.array([4.0]))["sig_1d"][0] / ce.continuum(g, long, np.array([4.0]))["sig_1d"][0]
    assert r_grat > r_prism > 1


def test_outside_coverage_is_nan_and_placement():
    e = ce.make_exposure(total_s=10000, per_exposure_s=1000)
    g = M.get("g395m")
    c = ce.continuum(g, e, np.array([1.0, 4.0]))
    assert np.isnan(c["sig_1d"][0]) and np.isfinite(c["sig_1d"][1])
    assert PRISM.placement_multiplier(0, 0) == 1.0
    assert PRISM.placement_multiplier(0.45, 0) > 1.5
    m, _ = ce.resolve_placement(PRISM, "typical"); assert m == pytest.approx(PRISM.data["mult_median"])
    m, _ = ce.resolve_placement(PRISM, "centred"); assert m == 1.0
    m, _ = ce.resolve_placement(PRISM, [0.2, 0.1]); assert m == pytest.approx(PRISM.placement_multiplier(0.2, 0.1))


def test_source_flux_and_recovery():
    w = np.array([2.0, 3.0])
    F, rec, label = ce.source_flux(PRISM, w, magnitude_ab=23.9, morphology="point")
    assert rec == 1.0 and F == pytest.approx([1.0, 1.0])      # AB 23.9 = 1 uJy
    F, rec, _ = ce.source_flux(PRISM, w, flux_njy=100, morphology="typical")
    assert 0.3 < rec < 0.6 and F[0] == pytest.approx(0.1 * rec)
    _, rec_c, _ = ce.source_flux(PRISM, w, flux_njy=100, morphology="compact")
    _, rec_e, _ = ce.source_flux(PRISM, w, flux_njy=100, morphology="extended")
    assert rec_c > rec > rec_e
    _, rec_o, _ = ce.source_flux(PRISM, w, flux_njy=100, recovery=0.8)
    assert rec_o == 0.8
    with pytest.raises(ModelError):
        ce.source_flux(PRISM, w, magnitude_ab=25, flux_njy=10)


def test_snr_and_time_for_snr():
    e = ce.make_exposure("nrsirs2", 13, 1, 6)
    w = np.array([2.5])
    F, rec, _ = ce.source_flux(PRISM, w, magnitude_ab=27.0, morphology="typical")
    c = ce.continuum(PRISM, e, w, F, placement_mult=PRISM.data["mult_median"], margin=1.1)
    assert 2 < c["snr_pix"][0] < 4 and c["snr_res"][0] > c["snr_pix"][0]
    t = ce.time_for_snr(e, float(c["snr_res"][0]), 10.0)
    e2 = e.scaled(t)
    c2 = ce.continuum(PRISM, e2, w, F, placement_mult=PRISM.data["mult_median"], margin=1.1)
    assert c2["snr_res"][0] == pytest.approx(10.0, rel=0.05)   # nexp rounding


def test_binning_modes():
    e = ce.make_exposure(total_s=10000, per_exposure_s=1000)
    w = np.array([3.0])
    pix = ce.continuum(PRISM, e, w, 0.1, bin_mode="pixel")
    res = ce.continuum(PRISM, e, w, 0.1, bin_mode="resolution")
    r10 = ce.continuum(PRISM, e, w, 0.1, bin_mode="R", bin_value=10)
    dl = ce.continuum(PRISM, e, w, 0.1, bin_mode="dlambda", bin_value=0.5)
    fine = ce.continuum(PRISM, e, w, 0.1, bin_mode="R", bin_value=5000)   # finer than a pixel -> 1 pixel
    assert pix["snr_bin"][0] == pytest.approx(pix["snr_pix"][0])
    assert fine["snr_bin"][0] == pytest.approx(pix["snr_pix"][0])
    assert pix["snr_bin"][0] < res["snr_bin"][0] < r10["snr_bin"][0] < dl["snr_bin"][0]
    with pytest.raises(ModelError):
        ce.continuum(PRISM, e, w, 0.1, bin_mode="R")


def test_line_limit_consistency():
    e = ce.make_exposure(total_s=20000, per_exposure_s=1000)
    r = ce.line(PRISM, e, 3.0, 1e-18, 0.0)
    r5 = ce.line(PRISM, e, 3.0, r["limit_5sigma_cgs"], 0.0)
    assert 4.5 < r5["snr"] <= 5.0            # own photon noise lowers it slightly below 5
    broad = ce.line(PRISM, e, 3.0, 1e-18, 3000.0)
    assert broad["snr"] < r["snr"] and broad["window_px"] > r["window_px"]
    with pytest.raises(ModelError, match="outside"):
        ce.line(M.get("g395m"), e, 1.0, 1e-18)
    # the S/N of a 5-sigma limit found from continuum() agrees with line()
    c = ce.continuum(PRISM, e, np.array([3.0]))
    assert c["line5"][0] == pytest.approx(r["limit_5sigma_cgs"], rel=1e-9)
    # the limit is a total flux: with half the line lost to slit losses a line
    # at the reported limit still reaches ~5 sigma, and the limit doubles
    half = ce.line(PRISM, e, 3.0, 1e-18, 0.0, line_recovery=0.5)
    assert half["limit_5sigma_cgs"] == pytest.approx(2 * r["limit_5sigma_cgs"], rel=1e-9)
    at_limit = ce.line(PRISM, e, 3.0, half["limit_5sigma_cgs"], 0.0, line_recovery=0.5)
    assert 4.5 < at_limit["snr"] <= 5.0
    c_half = ce.continuum(PRISM, e, np.array([3.0]), line_recovery=0.5)
    assert c_half["line5"][0] == pytest.approx(half["limit_5sigma_cgs"], rel=1e-9)


def test_pixel_grid_matches_dispersion():
    for key, d in M.dispersers.items():
        w, dl = d.pixel_grid()
        assert np.all(np.diff(w) > 0)
        assert np.allclose(np.diff(w), 0.5 * (dl[1:] + dl[:-1]), rtol=0.05)
        lo, hi = d.coverage
        assert w[0] >= lo and w[-1] <= hi + dl[-1]


def test_extraction_aliases_are_consistent():
    e = ce.make_exposure(total_s=10000, per_exposure_s=1000)
    w = np.array([2.5])
    for alias in ("opt", "OPTIMAL", "optimal"):
        F, rec, _ = ce.source_flux(PRISM, w, magnitude_ab=27, morphology="typical", extraction=alias)
        c = ce.continuum(PRISM, e, w, F, extraction=alias)
        F0, rec0, _ = ce.source_flux(PRISM, w, magnitude_ab=27, morphology="typical", extraction="optimal")
        c0 = ce.continuum(PRISM, e, w, F0, extraction="optimal")
        assert rec == rec0 and c["snr_pix"][0] == pytest.approx(c0["snr_pix"][0])
    _, rec3, _ = ce.source_flux(PRISM, w, magnitude_ab=27, morphology="typical", extraction="boxcar")
    _, rec3b, _ = ce.source_flux(PRISM, w, magnitude_ab=27, morphology="typical", extraction="3px")
    assert rec3 == rec3b != rec0
    with pytest.raises(ModelError, match="extraction"):
        ce.continuum(PRISM, e, w, extraction="5px")
    with pytest.raises(ModelError, match="extraction"):
        ce.line(PRISM, e, 2.5, 1e-18, extraction="5px")
