# CAMPFIRE API Implementation Summary

## What Was Built

I've implemented a complete REST API and Python client for CAMPFIRE that allows users to query the database and download spectra programmatically from within Python notebooks.

### 1. Database Layer

**New Migration:** `/web/supabase/migrations/010_add_api_keys.sql`

- `api_keys` table for managing user API keys
- SHA-256 hashed storage for security
- Row-level security policies
- Helper functions: `validate_api_key()` and `update_api_key_last_used()`
- Support for key expiration and rate limiting

### 2. REST API (Next.js)

**New Files:**
- `/web/lib/api-auth.ts` - API key authentication utilities
- `/web/lib/api-helpers.ts` - Helper functions for program access
- `/web/app/api/v1/objects/route.ts` - Query objects endpoint
- `/web/app/api/v1/spectra/route.ts` - Download spectra endpoint

**Endpoints:**

#### `GET /api/v1/objects`
Query objects with extensive filtering:
- Programs, fields, gratings, observations
- Redshift ranges, quality, and SNR ranges
- Spectral features, object flags, DQ flags (bit masks)
- Visual inspection status
- Cone search (RA, Dec, radius)
- Text search on object_id
- Pagination and sorting

Returns:
```json
{
  "data": [...],
  "pagination": {
    "total": 1234,
    "limit": 100,
    "offset": 0
  }
}
```

#### `GET /api/v1/spectra?path=<fits_path>&redirect=true`
Download spectrum files:
- Validates user access via API key
- Generates signed R2 URLs
- Can redirect directly or return URL as JSON

**Authentication:**
- All endpoints require `Authorization: Bearer sk_live_...` header
- API keys validated against database
- User access controlled via existing program access infrastructure

### 3. Python Client Package

**Structure:**
```
python/
├── campfire/
│   ├── __init__.py
│   ├── client.py       # Main Campfire class
│   ├── exceptions.py   # Custom exceptions
├── examples/
│   └── quickstart.ipynb
├── pyproject.toml
└── README.md
```

**Main Class: `Campfire`**

```python
from campfire import Campfire

cf = Campfire(api_key='sk_live_...')  # or from env var

# Query objects
results = cf.query_objects(
    programs=['EMBER-UDS'],
    redshift_range=(2.0, 4.0),
    redshift_quality=[2, 3],
    inspected_only=True
)

# Download spectra
paths = cf.download_spectra(
    table=results,
    download_dir='./spectra/',
    gratings=['PRISM']
)
```

**Features:**
- Returns `astropy.table.Table` objects (no pandas!)
- Smart file caching (skip re-downloads)
- Progress bars via `tqdm`
- Batch downloads
- Cone search support
- Minimal dependencies: `astropy`, `requests`, `tqdm`

## Next Steps

### 1. Apply Database Migration

Run the migration in Supabase SQL Editor:

```sql
-- Execute /web/supabase/migrations/010_add_api_keys.sql
```

This creates the API keys infrastructure.

### 2. Create API Key Management UI (Optional)

Add a page in the web portal where users can:
- Generate new API keys
- View existing keys (masked)
- Revoke keys
- See last used timestamps

Suggested location: `/web/app/profile/api-keys/page.tsx`

Example implementation:
```typescript
// Generate new API key (server action)
'use server'
import { generateApiKey } from '@/lib/api-auth';
import { createClient } from '@/lib/supabase/server';

export async function createApiKey(name: string) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) throw new Error('Not authenticated');

  const { key, prefix, hash } = generateApiKey();

  // Save to database
  await supabase.from('api_keys').insert({
    user_id: user.id,
    key_hash: hash,
    key_prefix: prefix,
    name: name
  });

  // Return the unhashed key ONCE (user must save it)
  return key;
}
```

### 3. Manual API Key Creation (For Testing)

Temporarily create keys via SQL:

```sql
-- In Supabase SQL Editor
-- Hash generated with: echo -n 'sk_live_YOUR_RANDOM_STRING' | shasum -a 256

INSERT INTO api_keys (user_id, key_hash, key_prefix, name)
VALUES (
  'YOUR_USER_UUID',  -- Get from auth.users table
  'SHA256_HASH_OF_YOUR_KEY',
  'sk_live_abc123...',
  'Test Key'
);
```

To generate a key hash in Python:
```python
import hashlib
api_key = 'sk_live_YOUR_RANDOM_STRING'
key_hash = hashlib.sha256(api_key.encode()).hexdigest()
print(key_hash)
```

### 4. Test the API

```bash
# Test authentication
curl -H "Authorization: Bearer sk_live_..." \
  https://campfire.vercel.app/api/v1/objects?limit=5

# Should return JSON with objects
```

### 5. Install Python Package

```bash
cd python
pip install -e .

# Set API key
export CAMPFIRE_API_KEY=sk_live_...

# Test in Python
python -c "from campfire import Campfire; cf = Campfire(); print(cf.query_objects(limit=5))"
```

### 6. Documentation Updates

Consider adding:
- API documentation page to web portal
- OpenAPI/Swagger spec
- Usage examples in CLAUDE.md
- Rate limiting documentation

## Design Decisions

### Why REST API Instead of Direct DB Access?

While direct Supabase access would be simpler, the REST API provides:
- Better security (no DB credentials in client)
- API versioning capability
- Centralized rate limiting
- Easier to add caching layer
- Can track usage metrics

### Why Astropy Tables?

Per your requirements:
- No pandas dependency
- Native to astronomy workflows
- Better for FITS/astronomical data
- Smaller dependency footprint

### Authentication Flow

```
User → API Key → REST API → Supabase RPC → Database
                    ↓
                Check user program access
                    ↓
                Filter results by RLS
```

This reuses the existing RLS infrastructure while adding API key layer.

## Security Considerations

1. **API keys are hashed** - SHA-256, never stored plaintext
2. **Program access enforced** - Leverages existing user_program_access table
3. **RLS policies** - Database-level security still applies
4. **Signed URLs** - R2 downloads use temporary signed URLs (1 hour expiry)
5. **Rate limiting** - Database column exists, not yet enforced (add if needed)

## Known Limitations

1. **No API key UI** - Must create keys manually via SQL for now
2. **No rate limiting enforcement** - Column exists but not active
3. **No usage tracking** - Could add api_usage_log table
4. **Program names vs IDs** - API currently accepts program IDs, could map names
5. **FITS structure assumptions** - Example notebook assumes specific FITS table structure

## Future Enhancements

### Phase 2: Enhanced Features
- API key management UI
- Usage dashboard
- Rate limiting enforcement
- Caching layer (Redis)
- Batch operations endpoint
- Async download support in Python client

### Phase 3: Advanced Features
- Webhook notifications for new data
- Streaming large queries
- GraphQL endpoint (optional)
- Python package on PyPI
- CLI tool (`campfire query --programs EMBER-UDS`)

## File Structure Summary

```
campfire/
├── web/
│   ├── app/api/v1/
│   │   ├── objects/route.ts         [NEW]
│   │   └── spectra/route.ts         [NEW]
│   ├── lib/
│   │   ├── api-auth.ts              [NEW]
│   │   └── api-helpers.ts           [NEW]
│   └── supabase/migrations/
│       └── 010_add_api_keys.sql     [NEW]
│
└── python/                          [NEW]
    ├── campfire/
    │   ├── __init__.py
    │   ├── client.py
    │   └── exceptions.py
    ├── examples/
    │   └── quickstart.ipynb
    ├── pyproject.toml
    └── README.md
```

## Questions?

- **How to handle program names vs IDs?** Currently API accepts IDs. Could add name lookup.
- **Rate limiting threshold?** Default 60 req/min per key. Adjust?
- **Key expiration policy?** No default expiration. Add one?
- **Public vs private data?** Currently respects existing access control.
