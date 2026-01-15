# Refactoring Summary

**Date**: November 4, 2025

## Completed: Phases 1 & 2 - Infrastructure Restructuring

### What Changed

Successfully restructured the NIRSpec project to separate pipeline and CAMPFIRE concerns:

#### 1. Pipeline Package (`pipeline/`)
- **Moved files**:
  - `reduction.py` → `pipeline/reduction.py`
  - `fitting.py` → `pipeline/fitting.py`
  - `plots.py` → `pipeline/plots.py`
- **Created**: `pipeline/__init__.py` (makes it importable)

#### 2. Scripts Directory (`scripts/`)
- **Created**: `scripts/reduce.py` - CLI wrapper for running the pipeline
- **Usage**: `python scripts/reduce.py --obs ember_uds_p4 --extract`

#### 3. CAMPFIRE Package (`campfire/`)
- **Created**: Package structure for deployment tools
  - `campfire/__init__.py`
  - `campfire/config.toml.example` - Template for credentials
  - `campfire/deploy/` - Future deployment modules
  - `campfire/backend/` - Future FastAPI service
- **Moved**: `web/` → `campfire/web/` (frontend code)

#### 4. Git Configuration
- **Created**: `.gitignore` to exclude:
  - `campfire/config.toml` (secrets)
  - Large data directories
  - Python cache files

#### 5. Documentation
- **Updated**: `CLAUDE.md` with new structure and data flow

### What Stayed the Same

Files that remain in root directory (unchanged):
- `config_v0.1.toml`, `config_v0.2.toml` - Pipeline configuration
- `observations.toml` - Observation definitions
- `versions.toml` - Version tracking
- `migration_plan.md` - CAMPFIRE migration plan
- `deploy_to_candide.sh` - Existing deployment script

Directories unchanged:
- `data/` - Raw and processed data
- `templates/` - SED templates
- `_archive/` - Archived work

## Key Design Decisions

### Clean Separation of Concerns

**Pipeline** (`pipeline/`):
- Local computation only
- Reads: `data/raw/`, `templates/`, config files
- Writes: `data/extractions/` with FITS + metadata JSON sidecars
- Dependencies: `msaexp`, `jwst`, `astropy`, `scipy`
- **No knowledge of cloud infrastructure**

**CAMPFIRE** (`campfire/`):
- Cloud deployment only
- Reads: `data/extractions/`, metadata JSON files
- Writes: Cloudflare R2 (FITS), Supabase (database)
- Dependencies: `boto3`, `supabase-py`, `astropy` (FITS reading)
- **No knowledge of reduction algorithms**

### Filesystem as Interface

The contract between pipeline and CAMPFIRE:

```
data/extractions/{version}/{obs_name}/
├── _pipeline/
│   └── extraction_summary_{obs}.csv
└── {source_id}/
    ├── {obs}_{grating}-{filter}_{source_id}_spec.fits
    └── metadata.json  # Program info, RA, Dec, etc.
```

Pipeline writes metadata JSON sidecars, CAMPFIRE reads them.

## Next Steps (Not Yet Implemented)

### Phase 3: Implement Deployment Tools
1. `campfire/deploy/metadata.py` - Extract metadata from pipeline outputs
2. `campfire/deploy/r2.py` - Cloudflare R2 client
3. `campfire/deploy/supabase.py` - Supabase database client
4. `campfire/deploy/deployer.py` - Main orchestrator
5. `scripts/deploy.py` - CLI wrapper

### Phase 4: Infrastructure Setup
1. Set up Cloudflare R2 bucket
2. Set up Cloudflare Pages for frontend
3. Create `campfire/config.toml` with credentials
4. Test deployment with small observation

### Phase 5: Pipeline Integration
1. Add metadata JSON export to `pipeline/reduction.py`
2. Define metadata schema (program info, source properties)
3. Test end-to-end: reduction → metadata → deployment

## Testing Status

- **Pipeline**: Not yet tested (requires running reduction)
- **Scripts**: `scripts/reduce.py` created but not tested
- **CAMPFIRE**: Skeleton only, no functionality implemented

## Migration Notes

The old workflow:
```bash
python reduction.py --obs ember_uds_p4 --extract
```

The new workflow:
```bash
python scripts/reduce.py --obs ember_uds_p4 --extract
```

All command-line arguments remain the same.
