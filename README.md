# CAMPFIRE

**COSMOS Archive of MultiPle-Field Internal Reductions & Extractions**

CAMPFIRE is a data reduction pipeline and web portal for JWST spectroscopy and imaging. 
It contains tools for processing raw JWST data (NIRSpec MSA spectroscopy, NIRCam imaging) 
through custom reduction stages and hosts the resulting products through an interactive 
web interface.

The pipeline and portal were built to support spectroscopic survey work in COSMOS and 
other deep fields, but the pipeline components are general enough to be adapted to 
other JWST programs.

## Repository Structure

This is a monorepo with several components:

| Directory | Description | Stack |
|-----------|-------------|-------|
| [`pipeline/`](pipeline/) | JWST reduction pipeline | Python, `jwst`, `astropy` |
| [`web/`](web/) | Interactive web portal | Next.js, Supabase, Tailwind |
| [`deploy/`](deploy/) | Deployment CLI for uploading products | Python, Supabase, Cloudflare R2 |
| [`python/`](python/) | Python API client *(under construction)* | Python, `httpx` |

Supporting directories:

| Directory | Description |
|-----------|-------------|
| `supabase/` | Database migrations and local dev config |
| `scripts/` | One-off utility scripts (seeding, backfills, etc.) |

See the README in each subdirectory for more detail.

## Quick Start

### Pipeline

```bash
cd pipeline
pip install -e .

# Set up your data directory (raw data, config, and outputs live here, not in the repo)
export CAMPFIRE_ROOT=/path/to/your/data

# Download raw data from MAST -> $CAMPFIRE_ROOT/raw/
cfpipe download --program 6585 --instrument nirspec

# Run the full reduction for an observation (a group of exposures to reduce + stack)
# Outputs to $CAMPFIRE_ROOT/products/<observation_name>/
cfpipe nirspec run --obs <observation_name> --all --processes 4

# Or run individual stages
cfpipe nirspec stage1 --obs <observation_name> --processes 4
cfpipe nirspec stage2a --obs <observation_name>
cfpipe nirspec stage3 --obs <observation_name> --processes 4
cfpipe nirspec zfit --obs <observation_name>
```

Requires Python 3.12+ and a [CRDS](https://jwst-crds.stsci.edu/) cache. 
See [`pipeline/README.md`](pipeline/README.md) for full documentation.

### Web Portal

```bash
cd web
npm install
npm run dev
```

Requires Supabase and R2 credentials in `.env.local`. 
See [`web/README.md`](web/README.md) for setup.

## Contributing

Bug reports and feature requests are welcome via [GitHub Issues](../../issues). If you're 
interested in contributing code, feel free to open a PR — just note that much of the 
configuration (observation definitions, deployment targets) is specific to our survey, 
so pipeline and portal improvements are the most useful contributions.

## License

[MIT](LICENSE)
