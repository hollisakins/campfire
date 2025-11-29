# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains two main components:

1. **Custom CAMPFIRE wrapper around the NIRSpec Data Reduction Pipeline** (`campfire-pipeline/`): Processes raw JWST NIRSpec data through preprocessing, spectrum extraction, and redshift fitting phases.

2. **Frontend for data access through the CAMPFIRE portal** (`campfire-web/`): COSMOS Archive of MultiPle-Field Internal Reductions & Extractions - deployment tools and web infrastructure for sharing reduced data with the research team.

## Project Structure

```
campfire/
├── pipeline/            # Local data reduction (no cloud dependencies)
│   ├── reduction.py              # Preprocessing and spectrum extraction
│   ├── fitting.py                # Redshift fitting
│   ├── plots.py                  # Visualization
│   ├── data/                     # Filesystem interface between pipeline and CAMPFIRE
│   │   └── {program_id}/         # Raw JWST data
│   ├── products/                 # Filesystem interface between pipeline and CAMPFIRE
│   │   └── {observation_name}/   # Raw JWST data
│   ├── config.toml               # Pipeline configuration (no secrets)
│   └── observations.toml         # Observation definitions
│
├── web           # Web frontend and deployment architecture
│   ├── app/               # Core Next.js app
│   ├── components/        # Custom React components
│   ├── lib/               # Supabase/R2 connections
│   └── middleware.ts
│
├── scripts/               # CLI entry points
│   ├── reduce.py          # Run reduction pipeline
│   ├── deploy.py          # Deploy to CAMPFIRE (to be implemented)
│   └── config.toml        # Deployment credentials (gitignored)
│
└── templates/             # SED fitting templates (might move)
```

## Development Commands

### Running the Pipeline

```bash
# Basic usage
python scripts/reduce.py --obs ember_uds_p4 --extract

# With preprocessing
python scripts/reduce.py --obs capers_cosmos_p1 --preprocess --extract

# Parallel processing
python scripts/reduce.py --obs ember_uds_p4 --extract --processes 4

# The pipeline expects Python 3.12+ and uses libraries like:
# - msaexp (for NIRSpec processing)
# - jwst (JWST pipeline)
# - astropy, numpy, scipy, matplotlib
```

### Deploying to CAMPFIRE (Future)

```bash
# Deploy specific observation to cloud
python scripts/deploy.py --obs ember_uds_p4 --version v0.2

# Dry run (validate without uploading)
python scripts/deploy.py --obs ember_uds_p4 --dry-run
```


## Configuration

### Pipeline Configuration (`campfire-pipeline/config.toml`)

Main configuration file for data reduction (safe to commit):
- `[pipeline]`: Version settings
- `[environment]`: CRDS server settings for calibration data
- `[paths]`: Directories for data and outputs
- `[preprocessing]`: Preprocessing parameters
- `[extractions]`: Extraction parameters

### Observations (`campfire-pipeline/observations.toml`)

Defines observation configurations including data files, gratings, and source IDs.

### CAMPFIRE Configuration (`campfire-web/config.toml`)

Deployment credentials (gitignored, never commit):
- `[cloudflare_r2]`: R2 storage credentials
- `[supabase]`: Database credentials
- `[deployment]`: Deployment settings

## Data Flow & Interface

The pipeline and CAMPFIRE communicate via the filesystem:

1. **CAMPFIRE-pipeline writes**: `campfire-pipeline/products/{obs_name}/`
   - FITS files
   - Metadata

2. **Deployment script reads**: Metadata JSON sidecars and FITS files
   - Discovers available observations and sources
   - Extracts program info, source properties from JSON
   - Uploads FITS to Cloudflare R2
   - Inserts records into Supabase database

## Development Notes

- Pipeline code should remain local-only (no cloud API dependencies)
- Metadata flows through JSON sidecar files in `products/`
- All secrets go in `campfire/config.toml` (gitignored)

