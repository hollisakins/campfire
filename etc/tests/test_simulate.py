import json

import numpy as np
import pytest

import campfire_etc as ce
from campfire_etc.model import FNU_UJY_TO_FLAM, ModelError
from campfire_etc.sed import flat_sed, parse_sed, power_law_sed
from campfire_etc.simulate import correlated_noise, write_spectrum

M = ce.load_model()
E = ce.make_exposure("nrsirs2", 13, 1, 6)


def test_parse_sed_units_and_normalisation():
    s = parse_sed({"wave": [10000, 20000, 30000], "flux": [25.0, 25.0, 25.0], "wave_unit": "angstrom", "flux_unit": "abmag"})
    assert s.range_um == (1.0, 3.0)
    assert s.fnu_at(2.0)[0] == pytest.approx(10 ** (-0.4 * (25 - 23.9)))
    s2 = parse_sed({"wave": [1, 2, 3], "flux": [1, 2, 3], "flux_unit": "nJy", "redshift": 1.0,
                    "normalize": {"magnitude_ab": 27, "band": "f277w"}})
    assert s2.range_um == (2.0, 6.0)
    assert s2.fnu_at(2.76)[0] == pytest.approx(10 ** (-0.4 * (27 - 23.9)))
    s3 = parse_sed({"wave": [1.0, 2.0], "flux": [1e-19, 1e-19], "flux_unit": "flam"})
    assert s3.fnu_at(2.0)[0] == pytest.approx(1e-19 * 4 / FNU_UJY_TO_FLAM)
    with pytest.raises(ModelError):
        parse_sed({"wave": [1, 2], "flux": [1]})
    with pytest.raises(ModelError, match="only available"):
        parse_sed({"file": "x.txt"}, allow_files=False)
    assert parse_sed(None) is None


def test_flat_source_reproduces_model():
    d = M.get("prism")
    r = ce.simulate(d, E, flat_sed(magnitude_ab=24.0), morphology="point", placement="centred", margin=1.0, seed=0)
    assert r["n_pixels"] > 300
    assert np.allclose(r["model_ujy"], 10 ** (-0.4 * (24 - 23.9)), rtol=1e-6)
    resid = (r["flux_ujy"][0] - r["model_ujy"]) / r["err_ujy"]
    assert abs(resid.mean()) < 0.2 and 0.8 < resid.std() < 1.2


def test_line_flux_is_conserved():
    d = M.get("g395m")
    F = 5e-18
    r = ce.simulate(d, E, None, [{"wave_um": 4.0, "flux_cgs": F, "fwhm_kms": 300}], morphology="point", seed=0)
    w, dl, model = r["wave_um"], r["dlambda_um"], r["model_ujy"]
    flam = model / w ** 2 * FNU_UJY_TO_FLAM            # erg/s/cm2/A per pixel
    total = np.sum(flam * dl * 1e4)
    assert total == pytest.approx(F, rel=0.01)
    assert r["lines"][0]["status"] == "ok" and r["lines"][0]["snr"] > 5
    # analytic line S/N agrees with the mock's own S/N over the +-1 FWHM window
    sel = np.abs(w - 4.0) < r["lines"][0]["fwhm_um"]
    assert np.sum(model[sel]) / np.sqrt(np.sum(r["err_ujy"][sel] ** 2)) == pytest.approx(r["lines"][0]["snr"], rel=0.3)


def test_line_outside_and_recovery_split():
    d = M.get("prism")
    r = ce.simulate(d, E, flat_sed(magnitude_ab=26), [{"wave_um": 7.0, "flux_cgs": 1e-18}], seed=0)
    assert r["lines"][0]["status"].startswith("outside")
    r2 = ce.simulate(d, E, flat_sed(magnitude_ab=26), [{"wave_um": 3.0, "flux_cgs": 1e-18}], morphology="typical", line_recovery=1.0, seed=0)
    assert r2["source"]["line_recovery"] == 1.0 and r2["source"]["flux_recovery"] < 1.0


def test_seed_reproducible_and_wave_range():
    d = M.get("prism")
    a = ce.simulate(d, E, flat_sed(magnitude_ab=26), seed=7, wave_range=[1.0, 2.0])
    b = ce.simulate(d, E, flat_sed(magnitude_ab=26), seed=7, wave_range=[1.0, 2.0])
    assert np.array_equal(a["flux_ujy"], b["flux_ujy"])
    assert a["wave_um"][0] >= 1.0 and a["wave_um"][-1] <= 2.0
    with pytest.raises(ModelError):
        ce.simulate(d, E, flat_sed(magnitude_ab=26), wave_range=[9.0, 9.1])
    with pytest.raises(ModelError):
        ce.simulate(d, E, None, None)


def test_noise_correlation():
    sig = np.ones(100000); rho = np.full(100000, 0.3)
    n = correlated_noise(sig, rho, np.random.default_rng(1), 1)[0]
    assert np.corrcoef(n[:-1], n[1:])[0, 1] == pytest.approx(0.3, abs=0.02)
    assert n.std() == pytest.approx(1.0, abs=0.02)
    n0 = correlated_noise(sig, np.zeros(100000), np.random.default_rng(1), 1)[0]
    assert abs(np.corrcoef(n0[:-1], n0[1:])[0, 1]) < 0.02


def test_lsf_smooths_sed_features():
    d = M.get("g395m")
    w = np.linspace(2.8, 5.2, 5000)
    f = np.where(np.abs(w - 4.0) < 0.0005, 100.0, 1.0)          # a 1-nm spike, unresolved by NIRSpec
    sed = ce.SED(w, f)
    r = ce.simulate(d, E, sed, morphology="point", seed=0, wave_range=[3.9, 4.1])
    peak = r["model_ujy"].max()
    assert 1.0 < peak < 100.0                                  # smeared over ~a resolution element
    assert r["model_ujy"][np.abs(r["wave_um"] - 4.0) > 0.02].max() < 1.5


def test_lsf_short_segments():
    from campfire_etc.simulate import _lsf_convolve
    # segment shorter than the kernel: a flat input must come back flat and aligned
    for n in (3, 10, 40, 300):
        out = _lsf_convolve(np.full(n, 2.0), np.full(n, 4.7))
        assert np.allclose(out, 2.0)
    # a step stays a step at the right place
    x = np.r_[np.zeros(20), np.ones(20)]
    out = _lsf_convolve(x, np.full(40, 2.0))
    assert out[0] < 0.01 and out[-1] > 0.99 and abs(out[19] - 0.5) < 0.15
    # a tiny wave_range through simulate() still works
    d = M.get("g395m")
    r = ce.simulate(d, E, flat_sed(magnitude_ab=24), morphology="point", wave_range=[4.0, 4.004], seed=0)
    assert 2 <= r["n_pixels"] <= 4 and np.allclose(r["model_ujy"], 10 ** (-0.4 * (24 - 23.9)), rtol=1e-6)


def test_power_law_and_write(tmp_path):
    d = M.get("prism")
    r = ce.simulate(d, E, power_law_sed(26.0, 2.0, -1.0), seed=0, n_realizations=2)
    assert r["model_ujy"][0] < r["model_ujy"][-1]              # f_nu ~ lambda^(beta+2) rises for beta = -1
    for ext in ("txt", "ecsv", "npz", "json"):
        p = write_spectrum(r, str(tmp_path / f"mock.{ext}"), "nJy")
        assert (tmp_path / f"mock.{ext}").exists()
    back = np.loadtxt(tmp_path / "mock.txt")
    assert back.shape == (r["n_pixels"], 5)
    assert np.allclose(back[:, 1], r["model_ujy"] * 1e3, rtol=1e-4)
    j = json.load(open(tmp_path / "mock.json"))
    assert len(j["flux"]) == 2
    with pytest.raises(ModelError):
        write_spectrum(r, str(tmp_path / "x.txt"), "erg")
