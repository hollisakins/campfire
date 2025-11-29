# CAMPFIRE Migration Summary

Discussed/iterated with Claude to develop plan for migration of CAMPFIRE data repository and integration into core NIRSpec data reduction pipeline. 

## Current State

**What CAMPFIRE is:**
- Internal archive/repository for JWST NIRCam imaging and NIRSpec spectroscopy
- Web portal for sharing reduced data with research team
- Currently: Static HTML/JS site hosted on CANDIDE cluster webserver
- Serves NIRCam images (direct download links) and NIRSpec spectra (searchable table with filters, tags, detail pages with plots)

**Current Problems:**
- CANDIDE cluster networking unreliable/slow
- No proper authentication (simple password protection only)
- Can't support user-specific features (proprietary data access, user comments/flags)
- External form workflow for adding comments

## New Architecture

**Target Infrastructure:**
- **Frontend:** Static HTML/JS on Cloudflare Pages (free)
- **Backend:** FastAPI on Fly.io (~free tier)
- **Database:** Supabase PostgreSQL (free tier) - for metadata, auth, comments
- **File Storage:** Cloudflare R2 (S3-compatible) - for FITS files
- **CDN:** BunnyCDN (existing setup, ~$1-6/month)

**Estimated cost:** $0-15/month initially, scales to ~$40-50/month at 1000 spectra

## Database Schema (Already Set Up in Supabase)

**Core tables:**
- `programs` - JWST program metadata (program_id, program_name, pi_name, description)
- `objects` - Astronomical objects (object_id, program_id, field, ra, dec, redshift, redshift_quality, spectral_features, object_flags, dq_flags)
- `spectra` - Individual spectra linked to objects (object_id FK, grating, fits_path, reduction_version, signal_to_noise)
- `nircam_images` - NIRCam mosaics (field, tile, filter, pixel_scale, version, extension, file_path)

**User management:**
- `user_profiles` - Extended user info (full_name, is_group_account, can_comment)
- `user_program_access` - Program-level access control (user_id, program_id)

**Comments & audit:**
- `comments` - User comments on objects (with soft delete for audit trail)
- `flag_audit_log` - Tracks all flag changes (who changed what when)
- `flag_definitions` - Metadata for UI rendering of flags (from TOML file)

**Key design decisions:**
- Objects and spectra are separate tables (one object can have multiple gratings)
- Flags stored as bitmasks (redshift_quality is single value 0-4, others are bitmask integers)
- Row-level security policies enforce program access automatically
- Group accounts (shared program logins) have `can_comment=FALSE`
- Individual user accounts can add comments/flags

**Already created:**
- First group account: `ember@campfire.internal` for Program 7076 (EMBER)
- All flag definitions loaded from TOML into database

## Pipeline Integration Strategy

**NOT migrating old data directly** - instead, building deployment into the reduction pipeline:

1. Reduction pipeline produces FITS files
2. Upload FITS to Cloudflare R2
3. Update Supabase objects/spectra tables
4. CAMPFIRE web interface automatically shows new data

**Key Python components needed:**
```python
class CampfireDeployer:
    - Upload FITS to R2 (boto3 S3-compatible)
    - Upsert objects table (supabase-py client)
    - Create spectra entries (linked to objects via FK)
    - Support multiple reduction versions per object
```

**R2 file structure:**
```
{reduction_version}/{object_id}/{grating}.fits
# e.g., v2.0_global/GOODS-N-12345/G395M.fits
```

**Spectra table supports:**
- Multiple gratings per object (PRISM, G140M, G235M, G395M)
- Multiple reduction versions (v1.0, v2.0_global, v2.0_local) 
- Unique constraint: (object_id, grating, reduction_version)

## Next Steps for Claude Code

### 1. Set up Cloudflare R2
- Create bucket `campfire-spectra`
- Get credentials (Account ID, Access Key, Secret Key)
- Test upload with boto3

### 2. Create `campfire_deploy.py` module
- `CampfireDeployer` class with R2 and Supabase clients
- `deploy_object()` method: upserts object, uploads FITS files, creates spectra entries
- `deploy_batch()` for bulk operations
- Configuration management (credentials in config file, not hardcoded)

### 3. Integration script
- Add to end of reduction pipeline
- Call `deployer.deploy_object()` with:
  - Object metadata (object_id, program_id, field, ra, dec, redshift, etc.)
  - List of spectra files (grating, local_path, signal_to_noise)
  - Reduction version string

### 4. Test deployment
- Create test FITS files
- Deploy test object to Supabase
- Verify in database that object and spectra entries created
- Verify FITS files uploaded to R2

### 5. Batch deployment (optional)
- Script to deploy existing reduced data if needed
- Read from current data structure, deploy to new infrastructure

## Configuration Needs

**Supabase:**
- URL: `https://yourproject.supabase.co`
- Service role key (for backend, keep secret)

**Cloudflare R2:**
- Account ID
- Access Key ID
- Secret Access Key
- Bucket name: `campfire-spectra`

**Reduction pipeline info:**
- How are reduced FITS files currently organized?
- What metadata is available per object/spectrum?
- Python environment (dependencies)?

## Future Features (Not Immediate)

- Interactive Plotly.js plotting (reads FITS in browser)
- FastAPI backend for auth and data serving
- Python client library for programmatic access
- Frontend updates to use API instead of direct file links

## Questions for Claude Code Session

1. Where are your reduced FITS files currently stored?
2. What does your reduction pipeline look like? (single script, multiple steps, what triggers it?)
3. What metadata do you currently track that's not in the schema? (Any additional fields needed?)
4. Do you have existing code for reading/parsing your reduced data?
5. Should we handle existing reduced data, or just new reductions going forward?