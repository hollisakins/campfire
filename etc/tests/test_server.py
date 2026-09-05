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
    # limits are total fluxes: a point source has a fainter limit than a typical galaxy
    pt = call("line_snr", disperser="g395m", readout="nrsirs2", ngroups=19, nexp=36, wave_um=3.5, flux_cgs=2e-18, morphology="point")
    assert pt["line"]["limit_5sigma_cgs"] < r["line"]["limit_5sigma_cgs"]
    assert "model_version" in call("exposure_time", readout="nrsirs2", ngroups=13)


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


def test_public_base_ignores_unlisted_forwarded_host(monkeypatch):
    class Ctx:
        def __init__(self, headers): self.headers = headers
    monkeypatch.setattr(srv, "PUBLIC_URL", "https://etc.example.org")
    monkeypatch.setattr(srv, "ALLOWED_HOSTS", ["etc.example.org", "etc.fly.dev"])
    assert srv._public_base(Ctx({"host": "etc.fly.dev", "x-forwarded-proto": "https"})) == "https://etc.fly.dev"
    assert srv._public_base(Ctx({"x-forwarded-host": "evil.example", "host": "etc.example.org"})) == "https://etc.example.org"
    assert srv._public_base(Ctx({"x-forwarded-host": "evil.example", "host": "also.evil"})) == "https://etc.example.org"
    # userinfo / path smuggling through an otherwise allow-listed prefix
    assert srv._public_base(Ctx({"x-forwarded-host": "etc.example.org:80@evil.example", "host": "also.evil"})) == "https://etc.example.org"
    assert srv._public_base(Ctx({"x-forwarded-host": "etc.example.org/x", "host": "etc.example.org:8443"})) == "https://etc.example.org:8443"
    assert srv._public_base(Ctx({"host": "etc.example.org", "x-forwarded-proto": "javascript"})) == "https://etc.example.org"
    monkeypatch.setattr(srv, "ALLOWED_HOSTS", [])
    assert srv._public_base(Ctx({"x-forwarded-host": "evil.example", "host": "127.0.0.1:8000", "x-forwarded-proto": "http"})) == "http://127.0.0.1:8000"
    assert srv._public_base(Ctx({"host": "127.0.0.1:8000@evil.example"})) == "https://etc.example.org"
    assert srv._public_base(None) == "https://etc.example.org"


def test_file_ids_are_validated_before_replay():
    assert srv._FID_RE.match("48ee65ec055ee8.JiiYIUsNB9M0GlBv")
    assert srv._FID_RE.match("JiiYIUsNB9M0GlBv")
    for bad in ("x;app=other.JiiYIUsNB9M0GlBv", "48ee65ec055ee8.short", "48ee65ec055ee8.JiiYIUsNB9M0GlB%0A", "../etc/passwd", ""):
        assert srv._FID_RE.match(bad) is None, bad
    fid = srv._store_file(b"x", "text/plain", "a.txt")
    assert srv._FID_RE.match(fid)
    srv._FILES.clear()


def test_input_size_caps():
    async def go():
        with pytest.raises(ToolError, match="limit is 100"):
            await srv.server.call_tool("simulate_spectrum", {"disperser": "prism", "magnitude_ab": 26, "total_s": 1000, "per_exposure_s": 1000,
                                                             "lines": [{"wave_um": 2.0, "flux_cgs": 1e-18}] * 101})
        with pytest.raises(ToolError, match="limit is 20000"):
            n = 20001
            await srv.server.call_tool("continuum_snr", {"disperser": "prism", "total_s": 1000, "per_exposure_s": 1000,
                                                         "sed": {"wave": [1.0] * n, "flux": [1.0] * n}})
    anyio.run(go)


def test_file_store_is_bounded(monkeypatch):
    srv._FILES.clear()
    monkeypatch.setattr(srv, "FILE_STORE_MAX_BYTES", 5000)
    ids = [srv._store_file(b"x" * 1500, "text/plain", "a.txt") for _ in range(6)]
    assert sum(len(v[0]) for v in srv._FILES.values()) <= 5000
    assert ids[-1] in srv._FILES and ids[0] not in srv._FILES
    with pytest.raises(ValueError, match="download limit"):
        srv._store_file(b"x" * (srv.FILE_MAX_BYTES + 1), "text/plain", "big.txt")
    srv._FILES.clear()


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


def test_cli_allowed_host_flag_sets_link_allow_list(monkeypatch):
    import types
    from campfire_etc import cli
    captured = {}
    monkeypatch.setattr(srv, "build_http_app", lambda **kw: captured.setdefault("kw", kw) or object())
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", types.SimpleNamespace(run=lambda *a, **k: captured.setdefault("ran", True)))
    monkeypatch.setattr(srv, "ALLOWED_HOSTS", [])
    args = types.SimpleNamespace(http=True, allowed_host="A.example,b.example", public_url=None, host="127.0.0.1", port=8000)
    cli.cmd_serve(args)
    assert srv.ALLOWED_HOSTS == ["a.example", "b.example"] and captured["kw"]["allowed_hosts"] == ["A.example", "b.example"]
