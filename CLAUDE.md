# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains two main components:

1. **Custom CAMPFIRE wrapper around the NIRSpec Data Reduction Pipeline** (`pipeline/`): Processes raw JWST NIRSpec data through preprocessing, spectrum extraction, and redshift fitting phases.

2. **Frontend for data access through the CAMPFIRE portal** (`web/`): COSMOS Archive of MultiPle-Field Internal Reductions & Extractions - deployment tools and web infrastructure for sharing reduced data with the research team.

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

### Deploying to CAMPFIRE

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

