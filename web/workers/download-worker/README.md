# CAMPFIRE Download Worker

Cloudflare Worker for streaming FITS file downloads from R2 storage.

## Overview

This worker handles bulk downloads of FITS spectroscopic data files from the CAMPFIRE catalog. It:

1. Receives JWT-signed requests from the Next.js frontend
2. Verifies token authenticity and expiration
3. Fetches FITS files from R2 storage
4. Streams them as a ZIP archive to the user
5. Handles up to 200 objects (~600 FITS files, ~180MB)

## Architecture

```
Next.js Frontend
    ↓ (generates JWT token)
Cloudflare Worker
    ↓ (fetches files)
R2 Bucket (campfire)
    ↓ (streams ZIP)
User Download
```

## Features

- **JWT Authentication:** Secure token-based access
- **Streaming ZIP:** No memory buffering, handles large datasets
- **CORS Support:** Works with multiple origins
- **Error Handling:** Graceful degradation for missing files
- **Performance:** <30s for 200 objects

## Files

- `src/index.ts` - Main worker handler
- `src/auth.ts` - JWT verification
- `src/zip.ts` - ZIP streaming logic
- `wrangler.toml` - Configuration
- `DEPLOYMENT_GUIDE.md` - Step-by-step deployment instructions

## Quick Start

```bash
# Install dependencies
npm install

# Test locally
wrangler dev

# Deploy to production
wrangler deploy

# Monitor logs
wrangler tail
```

## Configuration

### Environment Variables

Set in `wrangler.toml`:
- `ALLOWED_ORIGINS` - Comma-separated list of allowed origins

### Secrets

Set via Wrangler CLI:
- `JWT_SECRET` - Shared secret for JWT verification

### Bindings

- `R2_BUCKET` - Bound to `campfire` R2 bucket

## Development

### Local Testing

```bash
wrangler dev
```

Worker runs on `http://localhost:8787`

### Deploy

```bash
wrangler deploy
```

Deploys to `download.campfire.hollisakins.com`

### Monitoring

```bash
wrangler tail
```

View real-time logs from production.

## Limits

- **Max Objects:** 200 (configurable in Next.js)
- **Token TTL:** 10 minutes
- **Worker CPU Time:** 30 seconds (soft limit)
- **Max ZIP Size:** ~180MB (with current 300KB/file average)

## Security

- JWT tokens expire after 10 minutes
- Tokens are single-use (time-limited)
- R2 bucket is not publicly accessible
- CORS restricted to allowed origins
- No sensitive data in tokens (only file paths)

## Future Enhancements

- **Durable Objects:** For downloads >200 objects
- **Progress Tracking:** WebSocket updates during generation
- **Resume Support:** Allow interrupted downloads to resume
- **Caching:** Cache popular download sets

## Troubleshooting

See `DEPLOYMENT_GUIDE.md` for detailed troubleshooting steps.

Common issues:
- Token expiration → Regenerate token
- Missing files → Check R2 paths
- CORS errors → Update ALLOWED_ORIGINS

## License

MIT - CAMPFIRE Project
