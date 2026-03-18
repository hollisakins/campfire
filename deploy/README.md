# CAMPFIRE Deploy

CLI tool for deploying pipeline products to the CAMPFIRE web portal. Reads 
FITS files, metadata, and derived products from the pipeline output directory, 
uploads to Cloudflare R2, and inserts records into Supabase.

## Installation

```bash
cd deploy
pip install -e .
```

## Usage

### Full Deployment

```bash
# Deploy an observation (FITS + JSON + RGB + SED + shutters)
cfdeploy --obs <observation_name>

# Deploy multiple observations
cfdeploy --obs obs1 obs2 obs3

# Dry run (validate without uploading)
cfdeploy --obs <observation_name> --dry-run

# Deploy specific sources only
cfdeploy --obs <observation_name> --source-ids 12345 67890
```

### Individual Product Subcommands

```bash
cfdeploy rgb         --obs <obs>              # Generate + upload RGB cutout images
cfdeploy sed         --obs <obs>              # Generate + upload SED plots
cfdeploy json        --obs <obs>              # Regenerate + upload spectrum JSON
cfdeploy zfit        --obs <obs>              # Deploy redshift fitting JSON
cfdeploy thumbnails  --obs <obs>              # Regenerate spectrum thumbnails
cfdeploy shutters    --obs <obs>              # Deploy MSA shutter geometry
cfdeploy slits       --obs <obs>              # Deploy slit geometry (legacy)
cfdeploy sync-programs                        # Upsert programs from programs.toml
cfdeploy tiles       --field <field> --filter f444w  # Generate + upload map tiles
```

Most subcommands support `--dry-run` and `--source-ids` for filtering.

## Configuration

Credentials can be provided via environment variables (preferred) or a gitignored `config.toml`:

**Environment variables:**
- `CAMPFIRE_SUPABASE_URL`, `CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY`
- `CAMPFIRE_R2_ACCOUNT_ID`, `CAMPFIRE_R2_ACCESS_KEY_ID`, `CAMPFIRE_R2_SECRET_ACCESS_KEY`, `CAMPFIRE_R2_BUCKET_NAME`
- Optional for tiles: `CAMPFIRE_R2_TILES_*` (separate bucket)

**Config file** (auto-discovered at `$CAMPFIRE_ROOT/config/deploy.toml`):
```toml
[supabase]
url = "..."
service_role_key = "..."

[r2]
account_id = "..."
access_key_id = "..."
secret_access_key = "..."
bucket_name = "..."
```

Environment variables take priority over config file values.

## Data Flow

```
$CAMPFIRE_ROOT/products/{obs_name}/     deploy reads FITS + metadata
        │                                        │
        └──── cfdeploy ─────────────────────────►├──► Cloudflare R2 (FITS, images, JSON)
                                                 └──► Supabase (catalog records)
```

The deploy tool expects pipeline products to include a `{obs_name}_summary.ecsv` metadata file as the primary source of object properties.
