"""In-process tests of the MCP tools (needs the `mcp` extra)."""

import json

import anyio
import pytest

pytest.importorskip("mcp")

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

import campfire_etc.server as srv  # noqa: E402


def call(name, **kw):
    async def go():
        r = await srv.server.call_tool(name, kw)
        assert not r.is_error
        return r.structured_content if r.structured_content is not None else json.loads(r.content[0].text)
    return anyio.run(go)


def test_tool_listing():
    tools = anyio.run(srv.server.list_tools)
    names = {t.name for t in tools}
    assert {"list_dispersers", "model_info", "exposure_time", "depth", "continuum_snr", "line_snr", "simulate_spectrum"} <= names
    sim = next(t for t in tools if t.name == "simulate_spectrum")
    assert "ctx" not in sim.input_schema["properties"]
    prompts = anyio.run(srv.server.list_prompts)
    assert [p.name for p in prompts] == ["nirspec_etc_guide"]


def test_list_and_info():
    r = call("list_dispersers")
    assert r["model_version"] == srv.MODEL.version and len(r["dispersers"]) == 8
    r = call("model_info", disperser="g140h")
    assert any("borrowed" in c for c in r["disperser"]["caveats"])
    assert r["disperser"]["pandeia_comparison"]["runs"]


def test_depth_and_snr_tools():
    r = call("depth", disperser="prism", readout="nrsirs2", ngroups=13, nexp=6)
    assert r["exposure"]["total_s"] == pytest.approx(5689.6, abs=0.1)
    assert "5-sigma continuum limit" in r["proposal_sentence"]
    assert all("ab_5sigma_per_res_element" in row for row in r["rows"] if row.get("in_coverage", True))
    r = call("continuum_snr", disperser="prism", readout="nrsirs2", ngroups=13, nexp=6, magnitude_ab=27.0, wave_of_interest_um=2.5)
    h = r["headline"]
    assert h["wave_um"] == 2.5 and 2 < h["snr_per_pixel"] < 4 and h["snr_per_res_element"] > h["snr_per_pixel"]
    assert h["time_for_target_snr"]["total_s"] > 0
    assert "43%" in r["proposal_sentence"] or "46%" in r["proposal_sentence"]
    r = call("line_snr", disperser="g395m", readout="nrsirs2", ngroups=19, nexp=36, wave_um=3.5, flux_cgs=2e-18, fwhm_kms=150, continuum_magnitude_ab=27.5)
    assert r["line"]["snr"] > 10 and r["line"]["limit_5sigma_cgs"] < 2e-18


def test_sed_and_power_law_inputs():
    r = call("continuum_snr", disperser="prism", total_s=10000, per_exposure_s=1000, morphology="point", placement="centred", margin=1.0,
             sed={"wave": [0.3, 1, 2, 3, 4, 5, 6], "flux": [1, 2, 3, 3, 2, 1, 1], "flux_unit": "nJy", "normalize": {"magnitude_ab": 27, "band": "f277w"}})
    assert r["source"]["flux_recovery"] == 1.0 and r["headline"]["snr_per_res_element"] > 5
    r2 = call("continuum_snr", disperser="prism", total_s=10000, per_exposure_s=1000, magnitude_ab=27, beta=-2.0, beta_wave_um=2.0)
    assert "power law" in r2["source"]["description"]


def test_errors_are_reported():
    async def go():
        with pytest.raises(ToolError, match="ambiguous"):
            await srv.server.call_tool("continuum_snr", {"disperser": "g140m", "magnitude_ab": 27, "total_s": 1000, "per_exposure_s": 1000})
        with pytest.raises(ToolError, match="outside"):
            await srv.server.call_tool("line_snr", {"disperser": "g395m", "wave_um": 1.0, "flux_cgs": 1e-18, "total_s": 1000, "per_exposure_s": 1000})
        with pytest.raises(ToolError, match="only works with a local"):
            await srv.server.call_tool("simulate_spectrum", {"disperser": "prism", "magnitude_ab": 26, "total_s": 1000, "per_exposure_s": 1000, "output_path": "/tmp/x.txt"})
    anyio.run(go)


def test_simulate_inline_and_download(monkeypatch):
    srv._FILES.clear()
    r = call("simulate_spectrum", disperser="prism", readout="nrsirs2", ngroups=13, nexp=6, magnitude_ab=26, seed=1,
             lines=[{"wave_um": 3.5, "flux_cgs": 3e-18, "fwhm_kms": 100}])
    assert len(r["wave_um"]) == r["n_pixels"] and len(r["flux"]) == 1 and r["lines"][0]["snr"] > 5
    assert r["download_url"].endswith(r["download_url"].split("/")[-1]) and len(srv._FILES) == 1
    fid = r["download_url"].split("/")[-1]
    data = srv._FILES[fid][0].decode()
    assert data.startswith("# %ECSV") and str(r["n_pixels"]) not in ""  # ecsv text
    big = call("simulate_spectrum", disperser="g140h", readout="nrsirs2", ngroups=19, nexp=36, magnitude_ab=25, seed=1, output_format="npz")
    assert big.get("arrays_inlined") is False and "wave_um" not in big and big["download_format"] == "npz"
    # local mode writes files instead
    monkeypatch.setattr(srv, "LOCAL_FILES", True)
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "mock.txt")
        loc = call("simulate_spectrum", disperser="prism", magnitude_ab=26, total_s=5000, per_exposure_s=1000, seed=1, output_path=p)
        assert loc["output_path"] == p and os.path.exists(p) and "download_url" not in loc
