# CAMPFIRE Pipeline

JWST data reduction pipeline for NIRSpec MSA spectroscopy and NIRCam imaging. 
Essentially a wrapper around the official STScI [`jwst`](https://github.com/spacetelescope/jwst) calibration pipeline,
but with several custom stages for improved background subtraction, 1/f noise subtraction, spectral extraction, and redshift fitting.

## Installation

```bash
cd pipeline
pip install -e .
```

Requires Python 3.12+ and the following environment:
- A [CRDS](https://jwst-crds.stsci.edu/) cache (`CRDS_PATH` and `CRDS_SERVER_URL` env vars)
- Raw JWST data downloaded from MAST

Key dependencies: `jwst`, `astropy`, `numpy`, `scipy`, `matplotlib`, `click`
- For redshift fitting: `numba`, `spectres`, `bagpipes`,
- For NIRCam processing: `jhat`, `snowblind`, `photutils`

## CLI Usage

The pipeline installs a unified `cfpipe` CLI with subcommands for each instrument:

### NIRSpec

```bash
# Full reduction (all stages)
cfpipe nirspec run --obs <observation_name> --all --processes 4

# Individual stages
cfpipe nirspec stage1        --obs <obs> --processes 4    # Detector-level calibration
cfpipe nirspec stage2a       --obs <obs>                   # WCS assignment + pathloss
cfpipe nirspec stage2b       --obs <obs>                   # Nodded background subtraction
cfpipe nirspec stage3        --obs <obs> --processes 4     # Extraction + combination
cfpipe nirspec zfit          --obs <obs>                   # Redshift fitting
cfpipe nirspec summary       --obs <obs>                   # Generate metadata summary
cfpipe nirspec detect-stuck  --obs <obs> --processes 4     # Auto-detect stuck shutters

# Manual masks for shorts/artifacts on rate files (DS9 polygons → DQ DO_NOT_USE)
cfpipe nirspec mask edit     --obs <obs>                   # Draw polygons in DS9 (saves to observations.toml)
cfpipe nirspec mask apply    --obs <obs>                   # Restore + re-bkgsub with current masks
cfpipe nirspec mask validate --obs <obs>                   # Parse and report pixel counts
cfpipe nirspec mask clear    --obs <obs> --exposure <basename>  # Remove a mask entry

# Template grid generation (one-time)
cfpipe nirspec make-templates
```

#### Manual masks

Some NIRSpec exposures show mild detector-level artifacts (shorts, etc.) that
are easy to spot on rate files but not in the bkg-subtracted spectra. Draw
polygon regions around them in DS9 (image coords); they are stored inline in
`observations.toml` and OR'd as `DO_NOT_USE` into the rate file DQ array
before stage1 background subtraction.

```toml
[my_obs.masks]
"jw01234001001_nrs1" = """
image
polygon(120,540,180,540,180,610,120,610)
"""
```

DS9 is a soft dependency: `mask edit` falls back to manual instructions if
`ds9`/`xpaset` aren't on PATH. Stage 2a auto-detects stale masks (via the
`CFMASKSH` header sentinel) and re-applies them transparently.

### NIRCam

*Somewhat under construction*

```bash
# Full reduction
cfpipe nircam run --field <field_name> --all --processes 4

# Individual stages
cfpipe nircam stage1 --field <field> --filters f444w f150w --processes 4
cfpipe nircam stage2 --field <field> --filters f444w --processes 4
cfpipe nircam stage3 --field <field> --filters f444w
```

### General Commands

```bash
cfpipe config                                         # Print default config
cfpipe config > my_config.toml                        # Export for customization
cfpipe info                                           # Show paths, CRDS, versions
cfpipe download --program 6585 --instrument nirspec   # Download uncals from MAST
```

Direct instrument entry points are also available: `campfire-nirspec`, `campfire-nircam`.

## Data Directory (`$CAMPFIRE_ROOT`)

The pipeline separates source code from data. All raw data, intermediate products, configuration, and 
outputs live under a single root directory set by the `$CAMPFIRE_ROOT` environment variable. 
This keeps large data files (often hundreds of GB) out of the repository.

A typical layout:

```
$CAMPFIRE_ROOT/
├── config/
│   ├── config.toml          # Pipeline configuration overrides
│   ├── observations.toml    # NIRSpec observation definitions
│   └── fields.toml          # NIRCam field definitions
├── data/                    # Raw uncalibrated FITS from MAST
├── products/                # Pipeline outputs (per-observation)
└── cache/                   # Cached pipeline data 
    ├── crds/                # CRDS cache, set by $CRDS_PATH
    └── templates/           # Cached grids for redshift fitting
```

### Observations

An **observation** is the fundamental unit of NIRSpec reduction — a group of exposures that will be reduced 
together and stacked into final 1D/2D spectral products. Critically, all exposures included in an observation 
must have been planned using the same MSA catalog, or else mismatched IDs may be stacked. Each observation is 
defined as an entry in `observations.toml` with a glob pattern pointing to raw data and any observation-specific 
reduction configuration. A single JWST program may produce multiple observations (e.g., different pointings).

NIRCam uses **fields** as the analogous, though somewhat less compilcated, concept — a defined region with 
associated filters and tile geometry, defined in `fields.toml`.

See `observations.example.toml` and `fields.example.toml` in the `pipeline/` directory for the format.

## Configuration

Defaults are shipped as package data (`campfire_pipeline/data/config_default.toml`). No config file is required. Export and customize with `cfpipe config > my_config.toml`.

**Resolution order** (later wins):
1. Package defaults
2. User config: `--config` flag, or auto-discovered at `$CAMPFIRE_ROOT/config/config.toml`
3. Per-observation/field overrides (from `observations.toml` / `fields.toml`)

Config sections are namespaced by instrument: `[nirspec.stage1]`, `[nirspec.stage2]`, `[nircam.stage1.snowball]`, etc. Config controls *how* stages run (parameters, thresholds), never *whether* they run — that's determined by CLI flags and output detection.

### Calibration Data

The package ships with calibration reference data in `campfire_pipeline/data/`:
- `config_default.toml` — default pipeline configuration
- NIRSpec grating dispersion tables (FITS)
- `inoue14_igm.hdf5` — IGM absorption model (Inoue+14)
- `jades_dr4_empirical_wavecorr.asdf` — empirical wavelength correction

## Package Structure

```
campfire_pipeline/
├── cli.py                  # cfpipe unified CLI (mounts nirspec + nircam)
├── config.py               # Config loading, env setup
├── common/                 # Instrument-agnostic utilities
│   ├── io.py               #   Logging, file discovery
│   ├── wcs.py              #   Bounding box + DQ helpers
│   ├── spectral.py         #   Wavelength math, resampling, LSF
│   ├── query.py            #   MAST API download
│   ├── igm.py              #   IGM absorption (Inoue+14)
│   └── parallel.py         #   Parallel job dispatch
├── nirspec/                # NIRSpec spectroscopy
│   ├── cli.py              #   Click CLI
│   ├── engine.py           #   ReductionEngine orchestrator
│   ├── observation.py      #   Observation dataclass
│   ├── metafile.py         #   MSA metadata handling
│   ├── constants.py        #   Grating wavelength limits, defaults
│   ├── stage1.py           #   Detector1Pipeline + background
│   ├── stage2.py           #   WCS + nodded background subtraction
│   ├── stage3.py           #   Spec3Pipeline + optimal extraction
│   ├── extraction.py       #   Profile fitting + 1D combination
│   ├── redshift_fitting.py #   Template-based chi-squared fitting
│   ├── templates.py        #   Template grid generation
│   ├── slits.py            #   MSA slit geometry
│   ├── stuck_shutters.py   #   Stuck shutter auto-detection
│   └── plots.py            #   Stage-specific QA plots
├── nircam/                 # NIRCam imaging
│   ├── cli.py              #   Click CLI
│   ├── engine.py           #   ReductionEngine orchestrator
│   ├── field.py            #   Field dataclass (field + filters + tiles)
│   ├── constants.py        #   Filter lists, detector geometry
│   ├── stage1.py           #   Detector1Pipeline + snowball/wisp/striping
│   ├── stage2.py           #   Image2Pipeline + edge/sky/variance/masks
│   ├── stage3.py           #   JHAT alignment + skymatch + resample
│   └── bkgsub.py           #   Tiered background subtraction
└── metadata/               # Product metadata
    ├── reader.py           #   FITS metadata extraction
    ├── summary.py          #   Observation summary tables (ECSV)
    └── shutters.py         #   MSA shutter state tables (ECSV)
```
