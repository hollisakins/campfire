# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains two main components:

1. **CAMPFIRE Data Reduction Pipeline** (`pipeline/`): Processes raw JWST data through preprocessing, extraction, and fitting. Includes both NIRSpec spectroscopy and NIRCam imaging modules.

2. **Frontend for data access through the CAMPFIRE portal** (`web/`): COSMOS Archive of MultiPle-Field Internal Reductions & Extractions - deployment tools and web infrastructure for sharing reduced data with the research team.

## Project Structure

```
campfire/
├── pipeline/                     # Local data reduction (no cloud dependencies)
│   ├── pyproject.toml            # Package definition (campfire-pipeline)
│   ├── campfire_pipeline/        # Installable Python package
│   │   ├── __init__.py
│   │   ├── cli.py                # cfpipe unified CLI (mounts nirspec + nircam)
│   │   ├── config.py             # Config loading, env setup
│   │   ├── common/               # Instrument-agnostic utilities
│   │   │   ├── io.py             # log(), files_to_glob()
│   │   │   ├── wcs.py            # Bounding box + DQ helpers
│   │   │   ├── spectral.py       # Wavelength math, resampling, LSF
│   │   │   └── query.py          # MAST API download
│   │   ├── nirspec/              # NIRSpec pipeline
│   │   │   ├── cli.py            # campfire-nirspec Click CLI
│   │   │   ├── engine.py         # ReductionEngine orchestrator
│   │   │   ├── observation.py    # Observation dataclass
│   │   │   ├── metafile.py       # MetaFile dataclass (MSA metadata)
│   │   │   ├── constants.py      # Grating limits, default configs
│   │   │   ├── stage1.py         # Detector1Pipeline + background
│   │   │   ├── stage2.py         # WCS + nodded bkg subtraction
│   │   │   ├── stage3.py         # Spec3Pipeline + optimal extraction
│   │   │   ├── extraction.py     # Profile functions + 1D combination
│   │   │   ├── redshift_fitting.py # Chi-squared solvers + redshift fitting
│   │   │   ├── templates.py      # Template grid generation
│   │   │   ├── slits.py          # MSA slit geometry
│   │   │   └── plots.py          # Stage-specific QA plotting
│   │   ├── nircam/               # NIRCam pipeline
│   │   │   ├── cli.py            # campfire-nircam Click CLI
│   │   │   ├── engine.py         # ReductionEngine orchestrator
│   │   │   ├── field.py          # Field dataclass (field + filters + tiles)
│   │   │   ├── constants.py      # Filter lists, detector geometry, defaults
│   │   │   ├── stage1.py         # Detector1Pipeline + snowball/wisp/striping
│   │   │   ├── stage2.py         # Image2Pipeline + edge/sky/variance/masks
│   │   │   ├── stage3.py         # JHAT + bad pixels + skymatch + resample
│   │   │   └── bkgsub.py         # Tiered background subtraction
│   │   └── metadata/             # Product metadata & summary
│   │       ├── reader.py         # FITS metadata extraction
│   │       └── summary.py        # Observation summary ECSV
│   ├── reduction.py              # Backwards-compat shim (imports + main)
│   └── observations.toml         # Observation definitions
│
├── web/                          # Web frontend and deployment architecture
│   ├── app/                      # Core Next.js app
│   ├── components/               # Custom React components
│   ├── lib/                      # Supabase/R2 connections
│   └── middleware.ts
│
├── scripts/                      # Deployment and utility scripts
│   ├── deploy.py                 # Deploy to CAMPFIRE
│   └── config.toml               # Deployment credentials (gitignored)
│
└── templates/                    # SED fitting templates
```

## Development Commands

### Running the Pipeline

```bash
# Install the pipeline package (editable mode)
cd pipeline && pip install -e .

# Unified CLI (after pip install -e .)
cfpipe nirspec stage1   --obs ember_uds_p4 --processes 4
cfpipe nirspec stage2a  --obs ember_uds_p4 --source-ids 12345 67890
cfpipe nirspec stage2b  --obs ember_uds_p4 --source-ids 12345 67890
cfpipe nirspec stage3   --obs ember_uds_p4 --source-ids 12345 --processes 4
cfpipe nirspec zfit    --obs ember_uds_p4 --overwrite
cfpipe nirspec summary --obs ember_uds_p4
cfpipe nirspec run     --obs ember_uds_p4 --all --processes 4
cfpipe nirspec make-templates

cfpipe nircam stage1 --field cosmos --filters f444w f150w --processes 4
cfpipe nircam stage2 --field cosmos --filters f444w --processes 4
cfpipe nircam stage3 --field cosmos --filters f444w
cfpipe nircam run    --field cosmos --all --processes 4

# Instrument-agnostic commands
cfpipe config                                         # print default config to stdout
cfpipe config > my_config.toml                        # export for customization
cfpipe info                                           # show paths, CRDS, versions
cfpipe download --program 6585 --instrument nirspec   # download from MAST
cfpipe download --program 1727 --instrument nircam --dry-run

# Direct instrument entry points (also available)
campfire-nirspec stage1 --obs ember_uds_p4 -p 4
campfire-nircam  stage1 --field cosmos --filters f444w -p 4

# Backwards-compatible usage (still works)
cd pipeline
python reduction.py --obs ember_uds_p4 --stage1 --stage2a --stage2b --stage3

# The pipeline expects Python 3.12+ and uses libraries like:
# - jwst (JWST pipeline), msaexp
# - astropy, numpy, scipy, matplotlib
# - click, numba, spectres
# - jhat, snowblind, photutils (NIRCam)
```

### Deploying to CAMPFIRE

```bash
# Deploy specific observation to cloud
python scripts/deploy.py --obs ember_uds_p4 --version v0.2

# Dry run (validate without uploading)
python scripts/deploy.py --obs ember_uds_p4 --dry-run
```


## Configuration

### Pipeline Configuration

Defaults are shipped as package data in `campfire_pipeline/data/config_default.toml`. No config file is required — defaults alone are sufficient to run. Export defaults with `cfpipe config > my_config.toml`.

**Config resolution order** (later wins):
1. Package defaults (`config_default.toml`)
2. User config: explicit `--config` path, or auto-discovered at `$CAMPFIRE_ROOT/config/config.toml` / `./config.toml`
3. Per-observation/field overrides (from `observations.toml` / `fields.toml`)

**Sections** (instrument-namespaced):
- `[pipeline]`: Version settings
- `[environment]`: CRDS server settings for calibration data
- `[paths]`: Directories for data and outputs
- `[logging]`: Log level and format
- `[nirspec.stage1]`, `[nirspec.stage2]`, `[nirspec.stage3]`: NIRSpec per-stage parameters
- `[nirspec.redshift_fitting]`: Redshift fitting parameters
- `[nirspec.template_grids.*]`: Template grid definitions
- `[nircam.stage1.*]`, `[nircam.stage2.*]`, `[nircam.stage3.*]`: NIRCam per-stage/step parameters

**Design rules:**
- Config is purely parametric — controls *how* things run, never *whether*
- `--overwrite` and `--processes` are CLI-only flags, never in config
- No `run` toggles — stages run all sub-steps, auto-skip via output detection

### Observations (`$CAMPFIRE_ROOT/config/observations.toml`)

Defines NIRSpec observation configurations including data files, gratings, and source IDs.

### Fields (`$CAMPFIRE_ROOT/config/fields.toml`)

Defines NIRCam field configurations including file globs, filters, and tile/WCS geometry.

### CAMPFIRE Configuration (`scripts/config.toml`)

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

## Database Architecture (Supabase)

### Schema Overview

The CAMPFIRE web portal uses Supabase (PostgreSQL) for structured data with the following core tables:

- **`objects`**: Main table for NIRSpec spectroscopic objects
  - Primary key: `object_id` (text, e.g., "ember_uds_p4_123456")
  - Core columns: `ra`, `dec`, `redshift`, `redshift_quality`, `field`, `grating`
  - Generated column: `redshift` computed from COALESCE of manual/auto redshifts
  - Foreign key: `program_id` → `programs` table
  - FITS file references: `spec1d_file`, `spec2d_file` (Cloudflare R2 URLs)
  - Metadata flags: `spectral_features`, `object_flags`, `dq_flags` (bit flags)
  - Inspection tracking: `inspected_by`, `inspected_at`

- **`programs`**: JWST program metadata
  - Primary key: `program_id` (integer)
  - Columns: `program_name`, `pi_name`, `is_proprietary`

- **`users`**: Authentication and access control
  - Supabase Auth integration
  - Access codes for proprietary data

### RPC Functions

**`get_spectra_filtered`**: High-performance server-side filtering, sorting, and pagination
- Supports all filter types (programs, fields, gratings, redshift ranges, coordinate search, flags)
- Adaptive coordinate search: uses Haversine formula for great-circle distance
- Optimized sorting: carries sort columns through CTEs to avoid JSONB extraction overhead
- Returns: JSON array of objects with embedded program data + pagination metadata

### Performance Optimizations

Critical indexes for scaling to 10k+ objects:

```sql
-- Core lookup and filtering
CREATE INDEX idx_objects_program_id ON objects(program_id);
CREATE INDEX idx_objects_field ON objects(field);
CREATE INDEX idx_objects_grating ON objects(grating);
CREATE INDEX idx_objects_redshift_quality ON objects(redshift_quality);

-- Coordinate search (for cone search queries)
CREATE INDEX idx_objects_ra ON objects(ra);
CREATE INDEX idx_objects_dec ON objects(dec);

-- Redshift filtering (uses generated column)
CREATE INDEX idx_objects_redshift_generated ON objects(redshift) WHERE redshift IS NOT NULL;

-- Text search (trigram for fuzzy matching)
CREATE INDEX idx_objects_object_id_trgm ON objects USING gin (object_id gin_trgm_ops);

-- Flag filtering
CREATE INDEX idx_objects_spectral_features ON objects(spectral_features);
CREATE INDEX idx_objects_object_flags ON objects(object_flags);
CREATE INDEX idx_objects_dq_flags ON objects(dq_flags);

-- Inspection tracking
CREATE INDEX idx_objects_inspected_by ON objects(inspected_by);
```

**Key Performance Notes**:
- Search uses debouncing (500ms) to reduce database load
- Adaptive sorting: client-side for small result sets (<5000), server-side for larger
- Coordinate search uses indexed ra/dec with Haversine calculation
- Text search uses GIN trigram index for fuzzy matching

## Deployment & Version Control

### Git Workflow

The repository uses a **trunk-based development** model with automatic deployments:

- **`main` branch** → Production (campfire.vercel.app)
  - Protected branch
  - Deploys automatically to production on push
  - Only updated via merge from `develop`

- **`develop` branch** → Staging
  - Main development branch
  - Deploys automatically to preview URL on push
  - Used for testing before production

- **Feature branches** → Preview deployments
  - Create from `develop` for experimental work
  - Each branch gets its own preview URL
  - Delete after merging to `develop`

### Deployment Process

**Infrastructure:**
- **Frontend**: Next.js deployed on Vercel
- **Database**: Supabase PostgreSQL (hosted)
- **Storage**: Cloudflare R2 for FITS files
- **Auth**: Supabase Auth with email/password

**Vercel Configuration:**
- Root directory: `web/`
- Production branch: `main`
- Framework: Next.js (auto-detected)
- Build command: `npm run build`
- Install command: `npm install`

**Environment Variables (set in Vercel):**
```bash
# Supabase
NEXT_PUBLIC_SUPABASE_URL=https://xxx.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJ...
SUPABASE_SERVICE_ROLE_KEY=eyJ...  # Server-only

# Cloudflare R2
R2_ACCOUNT_ID=xxx
R2_ACCESS_KEY_ID=xxx
R2_SECRET_ACCESS_KEY=xxx
R2_BUCKET_NAME=campfire-data
R2_PUBLIC_URL=https://data.campfire.com
```

### Development Workflow

1. **Make changes** on a feature branch or `develop`
   ```bash
   git checkout develop
   git pull origin develop
   # Make changes...
   git add .
   git commit -m "Description of changes"
   git push origin develop
   ```

2. **Test in preview deployment**
   - Vercel automatically deploys `develop` branch
   - Test functionality at preview URL
   - Verify database migrations if applicable

3. **Test production build locally** ⚠️ **REQUIRED before pushing to main**
   ```bash
   cd web
   npm run build
   ```
   - This catches TypeScript errors, ESLint issues, and build failures
   - Fix any errors before proceeding
   - Only warnings are acceptable for deployment

4. **Deploy to production**
   ```bash
   git checkout main
   git merge develop
   git push origin main
   ```

5. **Database migrations** (via Supabase CLI)
   ```bash
   # Check migration status (local vs remote)
   supabase migration list

   # Push pending migrations to remote
   supabase db push

   # Reset local database (applies all migrations + seed)
   cd supabase && supabase db reset
   ```
   - Migration files located in: `supabase/migrations/`
   - Test locally with `supabase db reset` before pushing to remote
   - Use `supabase migration repair --status applied <version>` to fix history mismatches

### Important Notes

- **Pipeline code** (`pipeline/`) is local-only, not deployed
- **Large files** (FITS, raw data) are gitignored and stored in R2
- **Secrets** never committed (use Vercel env vars or gitignored config.toml)
- **Database schema** tracked in `supabase/migrations/`

