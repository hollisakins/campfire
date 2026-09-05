# campfire-etc — empirical NIRSpec/MSA ETC and spectrum simulator

An exposure-time calculator for JWST NIRSpec/MSA fitted to the CAMPFIRE archive
rather than to a simulated instrument: the per-pixel noise of ~77,000 real
reduced spectra as a function of total and per-exposure time, for PRISM and
every grating/filter combination, plus the two things the official ETC leaves
out — how much of a real galaxy's light reaches the extracted spectrum, and
where sources land in their shutters. It answers "what S/N or depth will this
observation deliver" and can generate mock extracted spectra for any SED.

It ships as a Python package (numpy only), a CLI, and an **MCP server** so an
AI agent can be asked "what S/N do we get on a m_AB = 27 galaxy with G395M in
50 ks" or "make me a mock PRISM spectrum of this SED" directly.

Interactive report and calculator: <https://claude.ai/code/artifact/15c73c7d-1281-43a6-bbc1-2b20825648c8>

## Using the MCP server

**Hosted (nothing to install):**

```bash
claude mcp add --transport http campfire-etc https://campfire-etc.hollisakins.com/mcp
```

Any MCP client that speaks streamable HTTP (Claude Desktop / claude.ai
connectors, Cursor, ...) can use the same URL. Mock spectra larger than a few
thousand values come back as a one-hour download link instead of inline
arrays.

**With the Claude Code plugin** (adds a skill that knows how to phrase results
for proposals and when to use which tool):

```
/plugin marketplace add hollisakins/claude-astro-tools
/plugin install nirspec-etc@claude-astro-tools
```

**Local (writes mock spectra straight to disk; needs [uv](https://docs.astral.sh/uv/)):**

```bash
claude mcp add campfire-etc -- uvx --from "campfire-etc[mcp] @ git+https://github.com/hollisakins/campfire#subdirectory=etc" campfire-etc serve
```

Tools: `list_dispersers`, `model_info`, `exposure_time`, `depth`,
`continuum_snr`, `line_snr`, `simulate_spectrum`; prompt `nirspec_etc_guide`.
Every tool takes the disperser as `prism`, `g395m`, `G140M/F100LP`, ... and the
setup as readout + groups + integrations + exposures (or `total_s` +
`per_exposure_s`). Defaults are the archive medians for a real galaxy
(morphology `typical`, placement `typical`, `margin=1.1`); use `point` /
`centred` / `margin=1.0` for a best case or to compare with pandeia.

## Python and CLI

```bash
pip install "campfire-etc @ git+https://github.com/hollisakins/campfire#subdirectory=etc"   # or: pip install -e etc
```

```python
import campfire_etc as ce
m = ce.load_model()                       # bundled model, latest version
d = m.get("g395m")
e = ce.make_exposure("nrsirs2", 19, 1, 36)                       # 49.9 ks
F, rec, _ = ce.source_flux(d, [4.0], magnitude_ab=27, morphology="typical")
mult, _ = ce.resolve_placement(d, "typical")
c = ce.continuum(d, e, [4.0], F, placement_mult=mult, margin=1.1)
print(c["snr_per_res_element"] if "snr_per_res_element" in c else c["snr_res"], c["ab5_res"])
r = ce.simulate(d, e, ce.sed.power_law_sed(26, 4.0, -2.0), [{"wave_um": 4.5, "flux_cgs": 2e-18, "fwhm_kms": 200}], seed=1)
```

```bash
campfire-etc info --disperser prism
campfire-etc snr --disperser prism --readout nrsirs2 --ngroups 13 --nexp 6 --mag 27 --wave 1.5 2.5 4.0
campfire-etc line --disperser g395m --total 50000 --texp 1386 --wave 3.5 --flux 2e-18 --fwhm-kms 150 --mag 27.5
campfire-etc simulate --disperser prism --readout nrsirs2 --ngroups 13 --nexp 6 --sed my_sed.txt --sed-flux-unit flam \
    --sed-wave-unit angstrom --redshift 7 --norm-mag 27 --norm-wave 2.0 --line 4.0,3e-18,150 --out mock.ecsv
campfire-etc serve                 # MCP over stdio
campfire-etc serve --http --port 8080 --allowed-host localhost   # MCP over streamable HTTP at /mcp
```

Extras: `[mcp]` (server), `[fits]` (FITS SED input / mock output),
`[build]` (rebuilding the model), `[dev]` (tests). FITS/ECSV/npz/json/text
input and output are supported for SEDs and mock spectra.

## The model, briefly

Per disperser, the per-pixel variance of a centred faint point source in the
drizzled 2-D spectrum is

    σ²_pix(λ) = A(λ)/T + B(λ)/(T·t_exp²)      [µJy², T and t_exp in s]

(background photon noise + read noise falling as t_exp⁻³ per exposure), fitted
in 0.1 µm bins to per-observation noise curves in the standard 3-shutter-slitlet,
3-nod, undithered configuration, with no noise floor needed to 100 ks. On top:
the measured 1-D/2-D noise ratio for optimal and 3-px extractions, the
adjacent-pixel correlation (binned S/N uses n_eff = n(1 + 2ρ(n−1)/n)), a
shutter-placement multiplier (quartic in the |x|, |y| offsets), a source-Poisson
term g(λ)F/T, and flux-recovery fractions vs spatial size from matches to total
photometry. Mock spectra sample the SED on the native pixel grid (integrated
from the dispersion reference curve), smooth it to the instrumental resolution,
add emission lines analytically (Gaussian in λ, integrated over pixel edges),
and draw noise with the measured neighbour correlation. Emission-line S/N
integrates ±1 FWHM (instrumental resolution and velocity width in quadrature).

Caveats: the model describes CAMPFIRE reductions (other reductions differ by
10–20% in per-pixel noise; the scalings are instrumental); exposure-time
scalings are inferred across programs, so dispersers with few observations
(G140H) constrain the read-noise term weakly and borrow it from their sibling;
only IRS2 readouts occur in the archive (NRS/NRSRAPID are extrapolated); MSA
nod-subtracted data only. `model_info` lists them per disperser.

## Model versions

Models live in `campfire_etc/models/` as `nirspec-<version>.json` (schema 1,
~200 kB for all eight dispersers) with `manifest.json` naming the default.
Versions are dated by the archive snapshot (`2026.09` = built 2026-09-02 from
77k spectra). Every tool result carries `model_version`. Adding a model never
changes an existing file; bump the package version and republish the server
when a new default lands.

`CAMPFIRE_ETC_MODEL=<version|path>` selects a model for the server; the CLI
takes `--model`.

## Rebuilding the model as the archive grows

Needs a local CAMPFIRE archive (`$CAMPFIRE_ROOT/products/nirspec` plus
`meta/spectra.csv` and `meta/photometry.csv` from `campfire sync`), the
`build` extra, and the NIRSpec dispersion reference files (found automatically
in a monorepo checkout or an installed `campfire-pipeline`). In the `campfire`
conda environment:

```bash
pip install -e "etc[build]"
campfire-etc build all prism_clear --workdir etc-build --processes 10   # PRISM first (others borrow from it)
campfire-etc build all all --workdir etc-build --processes 10           # ~25 min harvest for the full archive
campfire-etc build assemble --workdir etc-build --version 2026.12 --pandeia path/to/etc_results_all.json \
    --notes "archive snapshot 2026-12-01"
pytest etc                                                              # the regression numbers are for 2026.09; update them if the default moves
```

Steps: `harvest` (per-spectrum statistics, the slow step; skipped by `all`
when `arrays.npz` exists), `fit`, `phot`, `depth`, `figs`, then `assemble`,
which writes `models/nirspec-<version>.json` and updates the manifest
(`--no-latest` registers without making it the default). Pitfalls learned the
first time: MAD/clipped variances on <20 px per bin are biased low (the
bundled Monte-Carlo table corrects it); MSA bar rows are less noisy than
open-shutter rows, so never use fixed row offsets as "blank"; emission lines
contaminate on-source statistics, so per-observation curves pool the median of
per-spectrum variances. The pandeia comparison (`--pandeia`) is optional and
comes from a separate `pandeia.engine` run (see the report's ETC page).

## Hosting

`Dockerfile` + `fly.toml` run the streamable-HTTP transport on one auto-stopping
Fly.io machine; `.github/workflows/etc-server.yml` tests every PR touching
`etc/` and deploys on merge to `main` (secret `FLY_API_TOKEN`). The server is
stateless and unauthenticated (it holds no private data); downloads of mock
spectra live in memory for an hour. `CAMPFIRE_ETC_PUBLIC_URL` sets the base for
download links and `CAMPFIRE_ETC_ALLOWED_HOSTS` the Host headers accepted
(DNS-rebinding protection; unset disables the check for local use). The image
works unchanged on Cloud Run or any container host.

## Layout

```
etc/
├── campfire_etc/
│   ├── model.py        noise model, exposure timing, depth / S/N arithmetic (numpy only)
│   ├── sed.py          SED input and unit handling
│   ├── simulate.py     mock spectra (LSF, pixel grid, correlated noise, file output)
│   ├── server.py       MCP tools, prompt, HTTP routes
│   ├── cli.py          campfire-etc
│   ├── models/         versioned noise-model JSON + manifest
│   └── build/          archive harvest → fit → phot → depth → figs → assemble
├── tests/
├── Dockerfile, fly.toml
└── README.md
```
