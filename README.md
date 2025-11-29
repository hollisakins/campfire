# CAMPFIRE

**COSMOS Archive of MultiPle-Field Internal Reductions & Extractions**

CAMPFIRE is a data processing pipeline and web portal for JWST NIRSpec spectroscopy and NIRCam imaging from COSMOS and other deep fields.

## Project Structure

```
campfire/
├── pipeline/          # NIRSpec data reduction pipeline
│   ├── reduction.py   # Preprocessing & spectrum extraction
│   ├── fitting.py     # Redshift fitting
│   └── plots.py       # Visualization tools
├── web/               # Next.js web application
│   ├── app/           # App pages & API routes
│   ├── components/    # React components
│   └── lib/           # Utilities & database
├── scripts/           # CLI tools for reduction & deployment
└── docs/              # Documentation
```

## Components

### Pipeline (`pipeline/`)
Local NIRSpec data reduction pipeline using msaexp and the JWST calibration pipeline. Processes raw MSA data through preprocessing, extraction, and redshift fitting stages.

### Web Portal (`web/`)
Next.js application with:
- NIRSpec spectroscopy catalog browser
- Interactive spectrum viewer with redshift fitting
- NIRCam imaging data access
- User authentication and access control
- Bulk download functionality

### Scripts (`scripts/`)
- `reduce.py` - Run the data reduction pipeline
- `deploy.py` - Deploy processed data to the web portal (future)

## Development

### Web Application

```bash
cd web
npm install
npm run dev
```

### Data Pipeline

```bash
# Run reduction on an observation
python scripts/reduce.py --obs <observation_name> --extract

# With preprocessing
python scripts/reduce.py --obs <observation_name> --preprocess --extract
```

## Deployment

- **Production**: Deployed from `main` branch via Vercel
- **Staging**: Deployed from `develop` branch via Vercel
- **Database**: Supabase PostgreSQL
- **Storage**: Cloudflare R2 for FITS files

## Configuration

- `pipeline/config.toml` - Pipeline configuration (safe to commit)
- `pipeline/observations.toml` - Observation definitions
- `scripts/config.toml` - Deployment credentials (gitignored)

See [CLAUDE.md](./CLAUDE.md) for detailed project documentation.
