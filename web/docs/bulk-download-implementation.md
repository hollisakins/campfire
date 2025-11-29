# CAMPFIRE Bulk Download Implementation Plan

## Overview

This document outlines the implementation of bulk download functionality for filtered spectra results in the CAMPFIRE web application. Users will be able to download:
1. **CSV table** - Metadata for all filtered results (unlimited)
2. **FITS files** - Spectroscopic data files as a ZIP archive (capped at 200 objects)

## Architecture

```
┌─────────────────────┐
│   Next.js (Vercel)  │
│                     │
│  ┌──────────────┐   │
│  │ SpectraTable │   │  User clicks download
│  │   Component  │   │         │
│  └──────┬───────┘   │         │
│         │           │         ▼
│         │           │  ┌─────────────────┐
│         │           │  │  Download       │
│         │           │  │  Buttons        │
│         │           │  └────┬────────────┘
│         │           │       │
│         ▼           │       │
│  ┌──────────────┐   │       │
│  │   Server     │   │       │
│  │   Actions    │   │       │
│  │              │   │       │
│  │ • CSV gen    │◄──┼───────┘ (CSV)
│  │ • Token gen  │   │
│  └──────┬───────┘   │
└─────────┼───────────┘
          │
          │ (FITS - redirect to Worker)
          │
          ▼
┌─────────────────────────────┐
│   Cloudflare Worker         │
│   (download.campfire.*)     │
│                             │
│  1. Verify JWT token        │
│  2. Fetch FITS from R2      │
│  3. Stream ZIP response     │
└──────────┬──────────────────┘
           │
           │ (fetch files)
           │
           ▼
    ┌─────────────┐
    │ R2 Bucket   │
    │ (FITS data) │
    └─────────────┘
```

## Components

### 1. UI Components

#### 1.1 Download Buttons Component

**Location:** `web/components/spectra/DownloadTableButtons.tsx`

**Purpose:** Render download buttons above the SpectraTable

**Features:**
- CSV download button (always enabled)
- FITS ZIP download button (disabled if >200 results)
- Warning message if results exceed limit
- Loading states during generation

**Props:**
```typescript
interface DownloadTableButtonsProps {
  totalCount: number;
  filters: AdvancedFilterOptions;
  page: number;
  pageSize: number;
  sortColumn: SortColumn;
  sortDirection: SortDirection;
  isFullDataset: boolean;
}
```

**UI Design:**
```
┌─────────────────────────────────────────────────┐
│  Download Results:                              │
│  ┌──────────────┐  ┌──────────────┐           │
│  │ 📄 CSV Table │  │ 📦 FITS ZIP  │           │
│  └──────────────┘  └──────────────┘           │
│                                                 │
│  ⚠️ Note: FITS download limited to 200 objects │
│     (Current results: 1,847 objects)           │
└─────────────────────────────────────────────────┘
```

#### 1.2 Integration into SpectraTable

**Location:** `web/app/spectra/page.tsx`

Add download buttons above the table results:

```typescript
{!loading && !error && spectra.length > 0 && (
  <>
    <DownloadTableButtons
      totalCount={totalCount}
      filters={filters}
      page={page}
      pageSize={pageSize}
      sortColumn={sortColumn}
      sortDirection={sortDirection}
      isFullDataset={isFullDataset}
    />

    <SpectraTable ... />
  </>
)}
```

### 2. Server Actions

#### 2.1 CSV Download Action

**Location:** `web/lib/actions/download.ts`

**Function:** `downloadCSV(filters, sortColumn, sortDirection)`

**Process:**
1. Query Supabase with filters (no limit - get all results)
2. Map to CSV format with columns:
   - object_id
   - field
   - ra
   - dec
   - redshift
   - redshift_quality
   - max_snr
   - num_gratings
   - last_inspected_at
   - last_inspected_by
   - distance (if coordinate search active)
3. Format as CSV string
4. Return as downloadable blob

**Implementation Notes:**
- Use `papaparse` library for CSV generation
- Handle null values appropriately
- Include header row with column names
- Filename: `campfire_spectra_YYYYMMDD_HHMMSS.csv`

**Performance:**
- Should handle 10k+ rows easily
- Memory usage: ~1-2MB per 10k rows
- Execution time: <5 seconds for 10k rows

#### 2.2 FITS Token Generation Action

**Location:** `web/lib/actions/download.ts`

**Function:** `generateFitsDownloadToken(filters, sortColumn, sortDirection)`

**Process:**
1. Query Supabase with filters (limit: 200 objects)
2. Extract all FITS paths from spectra array
3. Create payload with:
   - R2 object keys (FITS paths)
   - Metadata (object IDs, gratings)
   - Expiration timestamp (10 minutes)
4. Sign JWT with shared secret
5. Return Worker URL with token

**Payload Structure:**
```typescript
interface DownloadPayload {
  files: Array<{
    key: string;           // R2 object key
    filename: string;      // Download filename (e.g., "cosmos_ddt_66964_prism.fits")
    objectId: string;      // For organization
    grating: string;       // For organization
  }>;
  exp: number;             // Expiration timestamp
  totalSize?: number;      // Optional: total expected size in bytes
}
```

**Security:**
- JWT signed with HS256
- 10 minute expiration
- Shared secret stored in env vars (both Next.js and Worker)
- Token cannot be reused after expiration

### 3. Cloudflare Worker

#### 3.1 Worker Setup

**Location:** `web/workers/download-worker/`

**Structure:**
```
workers/download-worker/
├── src/
│   ├── index.ts          # Main worker code
│   ├── zip.ts            # ZIP streaming logic
│   └── auth.ts           # JWT verification
├── wrangler.toml         # Worker configuration
├── package.json          # Dependencies
└── tsconfig.json         # TypeScript config
```

#### 3.2 Worker Code

**Main Handler (`src/index.ts`):**

```typescript
export interface Env {
  R2_BUCKET: R2Bucket;
  JWT_SECRET: string;
  ALLOWED_ORIGINS: string; // Comma-separated
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // CORS handling
    if (request.method === 'OPTIONS') {
      return handleCORS(request, env);
    }

    try {
      // Extract and verify token
      const url = new URL(request.url);
      const token = url.searchParams.get('token');

      if (!token) {
        return new Response('Missing token', { status: 400 });
      }

      // Verify JWT
      const payload = await verifyToken(token, env.JWT_SECRET);

      if (payload.exp < Date.now()) {
        return new Response('Token expired', { status: 401 });
      }

      // Stream ZIP response
      const { readable, writable } = new TransformStream();

      // Start ZIP generation in background
      generateZipArchive(payload.files, writable, env.R2_BUCKET)
        .catch(err => {
          console.error('ZIP generation error:', err);
        });

      // Return streaming response
      return new Response(readable, {
        headers: {
          'Content-Type': 'application/zip',
          'Content-Disposition': 'attachment; filename="campfire_spectra.zip"',
          'Access-Control-Allow-Origin': getAllowedOrigin(request, env),
        },
      });

    } catch (error) {
      console.error('Worker error:', error);
      return new Response('Internal server error', { status: 500 });
    }
  }
};
```

**ZIP Streaming Logic (`src/zip.ts`):**

```typescript
import { ZipWriter } from '@zip.js/zip.js';

export async function generateZipArchive(
  files: DownloadFile[],
  writable: WritableStream,
  bucket: R2Bucket
): Promise<void> {
  const writer = writable.getWriter();

  try {
    // Use streaming ZIP library
    // Note: Need to evaluate best library for Workers environment
    // Options: fflate, archiver-stream, or custom implementation

    for (const file of files) {
      // Fetch from R2
      const object = await bucket.get(file.key);

      if (!object) {
        console.warn(`File not found: ${file.key}`);
        continue;
      }

      // Stream file into ZIP
      const data = await object.arrayBuffer();

      // Add to ZIP with proper filename
      // Implementation depends on chosen library
    }

    // Finalize ZIP
    await writer.close();

  } catch (error) {
    await writer.abort(error);
    throw error;
  }
}
```

**JWT Verification (`src/auth.ts`):**

```typescript
import { jwtVerify } from 'jose';

export async function verifyToken(
  token: string,
  secret: string
): Promise<DownloadPayload> {
  const encoder = new TextEncoder();
  const secretKey = encoder.encode(secret);

  const { payload } = await jwtVerify(token, secretKey, {
    algorithms: ['HS256'],
  });

  return payload as DownloadPayload;
}
```

#### 3.3 Worker Configuration

**wrangler.toml:**

```toml
name = "campfire-download"
main = "src/index.ts"
compatibility_date = "2024-01-01"

# Environment bindings
[env.production]
r2_buckets = [
  { binding = "R2_BUCKET", bucket_name = "campfire-data" }
]

[env.production.vars]
ALLOWED_ORIGINS = "https://campfire.yourdomain.com"

# Secrets (set via CLI)
# wrangler secret put JWT_SECRET
# wrangler secret put R2_ACCESS_KEY_ID (if needed)
# wrangler secret put R2_SECRET_ACCESS_KEY (if needed)

# Routes
routes = [
  { pattern = "download.campfire.yourdomain.com/*", zone_name = "yourdomain.com" }
]
```

### 4. Configuration & Secrets

#### 4.1 Environment Variables

**Next.js (`.env.local`):**
```bash
# Existing vars...
NEXT_PUBLIC_WORKER_DOWNLOAD_URL=https://download.campfire.yourdomain.com
WORKER_JWT_SECRET=<generated-secret>  # Must match Worker secret
```

**Cloudflare Worker Secrets:**
```bash
# Set via Wrangler CLI
wrangler secret put JWT_SECRET --env production
# Enter the same secret as WORKER_JWT_SECRET above

# If R2 bucket requires auth (usually auto-bound)
wrangler secret put R2_ACCESS_KEY_ID --env production
wrangler secret put R2_SECRET_ACCESS_KEY --env production
```

#### 4.2 Shared Secret Generation

Run once to generate shared secret:
```bash
node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
```

Store this value in both Next.js and Worker environments.

### 5. Implementation Steps

#### Phase 1: CSV Download (Simpler, implement first)

**Step 1.1:** Create download action
- [ ] Create `web/lib/actions/download.ts`
- [ ] Implement `downloadCSV()` function
- [ ] Add papaparse dependency: `npm install papaparse @types/papaparse`
- [ ] Test with various filter combinations

**Step 1.2:** Create UI component
- [ ] Create `web/components/spectra/DownloadTableButtons.tsx`
- [ ] Implement CSV download button with loading state
- [ ] Add appropriate icons (lucide-react)

**Step 1.3:** Integrate into page
- [ ] Update `web/app/spectra/page.tsx`
- [ ] Pass necessary props to DownloadTableButtons
- [ ] Test end-to-end

**Estimated time:** 2-3 hours

#### Phase 2: FITS ZIP Download (More complex)

**Step 2.1:** Set up Worker project
- [ ] Install Wrangler CLI: `npm install -g wrangler`
- [ ] Create `web/workers/download-worker/` directory
- [ ] Initialize: `wrangler init download-worker`
- [ ] Set up TypeScript config
- [ ] Install dependencies: `npm install jose fflate` (or chosen ZIP library)

**Step 2.2:** Implement Worker code
- [ ] Create `src/index.ts` with main handler
- [ ] Create `src/auth.ts` with JWT verification
- [ ] Create `src/zip.ts` with ZIP streaming
- [ ] Configure `wrangler.toml` with R2 bindings
- [ ] Test locally: `wrangler dev`

**Step 2.3:** Implement token generation
- [ ] Add `generateFitsDownloadToken()` to `web/lib/actions/download.ts`
- [ ] Install jose for JWT: `npm install jose`
- [ ] Test token generation and verification

**Step 2.4:** Update UI component
- [ ] Add FITS ZIP download button to DownloadTableButtons
- [ ] Add warning message for >200 results
- [ ] Implement loading state and error handling
- [ ] Test with various result counts

**Step 2.5:** Deploy Worker
- [ ] Set up Cloudflare DNS subdomain: `download.campfire.yourdomain.com`
- [ ] Deploy: `wrangler deploy --env production`
- [ ] Set secrets: `wrangler secret put JWT_SECRET --env production`
- [ ] Test in production

**Estimated time:** 4-6 hours

### 6. Testing Plan

#### 6.1 CSV Download Tests

- [ ] **Small dataset** (10 objects) - verify all columns present
- [ ] **Large dataset** (1000+ objects) - verify no truncation
- [ ] **With coordinate search** - verify distance column included
- [ ] **Special characters** - verify proper CSV escaping
- [ ] **Null values** - verify handled correctly
- [ ] **Different sort orders** - verify results match table

#### 6.2 FITS ZIP Download Tests

- [ ] **Single object, single grating** - verify ZIP structure
- [ ] **Single object, multiple gratings** - verify all files included
- [ ] **50 objects** - verify reasonable download time (<30s)
- [ ] **200 objects (max)** - verify no timeout
- [ ] **201 objects** - verify button disabled with warning
- [ ] **Token expiration** - verify 401 error after 10 minutes
- [ ] **Invalid token** - verify 401 error
- [ ] **Missing R2 file** - verify graceful handling
- [ ] **CORS** - verify works from production domain

#### 6.3 Edge Cases

- [ ] No results (empty dataset)
- [ ] Network interruption during ZIP download
- [ ] R2 bucket temporarily unavailable
- [ ] Very large individual FITS files (>5MB)
- [ ] Concurrent downloads (multiple users)

### 7. File Size Estimations

**Per Object:**
- FITS files: ~300KB per grating
- Average 2-3 gratings per object
- Total: ~600-900KB per object

**Download Sizes:**
- 50 objects: ~30-45MB
- 100 objects: ~60-90MB
- 200 objects: ~120-180MB

**Bandwidth:**
- Worker CPU time: ~0.1-0.2s per file (fetch + ZIP)
- Total for 200 objects (600 files): ~60-120s CPU time
- Transfer time: depends on user's connection

### 8. Error Handling

#### 8.1 User-Facing Errors

| Error | Message | Resolution |
|-------|---------|------------|
| Too many results | "Cannot download FITS for more than 200 objects. Please refine your filters." | Disable button, show warning |
| Token expired | "Download link expired. Please try again." | Regenerate token |
| R2 unavailable | "Unable to access data storage. Please try again later." | Retry logic |
| Worker timeout | "Download taking longer than expected. Please try again with fewer objects." | Reduce batch size |
| Network error | "Download interrupted. Please check your connection and try again." | Resume support (future) |

#### 8.2 Logging

**Next.js Server Actions:**
- Log download requests with filter parameters
- Track success/failure rates
- Monitor generation times

**Cloudflare Worker:**
- Log token verification failures
- Track R2 fetch errors
- Monitor ZIP generation progress
- Alert on high error rates

### 9. Performance Optimizations

#### 9.1 Immediate

- **Parallel R2 fetches**: Fetch multiple files concurrently (limit: 10)
- **Streaming compression**: Don't buffer entire ZIP in memory
- **Early error detection**: Validate all files exist before starting ZIP

#### 9.2 Future

- **Resume support**: Allow interrupted downloads to resume
- **Caching**: Cache frequently downloaded object sets (30 min TTL)
- **CDN caching**: Cache ZIP files for popular filter combinations
- **Compression**: Use ZIP compression levels (tradeoff: CPU vs size)

### 10. Monitoring & Metrics

Track the following metrics:

**Usage Metrics:**
- CSV downloads per day/week
- FITS downloads per day/week
- Average objects per download
- Peak download times

**Performance Metrics:**
- CSV generation time (p50, p95, p99)
- FITS ZIP generation time (p50, p95, p99)
- Worker CPU time per download
- R2 fetch latencies

**Error Metrics:**
- Token expiration rate
- R2 fetch failures
- Worker timeouts
- User-cancelled downloads

### 11. Future Enhancements (Durable Objects)

When usage grows or users consistently hit limits, consider upgrading to Durable Objects:

#### 11.1 Architecture Changes

```
User clicks download (>200 objects)
  ↓
Create Durable Object job (ID: job_abc123)
  ↓
Return job ID to user
  ↓
Show progress UI: "Preparing download... 45%"
  ↓
Durable Object:
  - Fetches files in background (no timeout)
  - Builds ZIP incrementally
  - Stores in temp R2 location
  ↓
When complete:
  - Notify user (WebSocket or polling)
  - Provide download link (valid 24 hours)
  ↓
User downloads ZIP
```

#### 11.2 Implementation Requirements

- **Job queue system**: Track active/completed jobs
- **Progress tracking**: Update status every N files
- **Notifications**: Email or in-app notification when ready
- **Temporary storage**: R2 bucket for completed ZIPs (auto-delete after 24h)
- **UI updates**: Job list page, progress indicators
- **Cleanup cron**: Remove old jobs and temp files

#### 11.3 Cost Analysis

**Current (Regular Worker):**
- $0/month for <100k requests
- Minimal cost for typical usage

**With Durable Objects:**
- ~$5-10/month for moderate usage (100-500 large downloads/month)
- Worth it if users consistently need 200+ object downloads

### 12. Documentation

#### 12.1 User-Facing Documentation

Add to CAMPFIRE help/FAQ:

**Q: How do I download my filtered results?**

A: Use the download buttons above the results table:
- **CSV Table**: Downloads metadata for all filtered results (no limit)
- **FITS ZIP**: Downloads spectroscopic data files as a ZIP archive (limited to 200 objects)

**Q: Why is the FITS download limited to 200 objects?**

A: To ensure downloads complete quickly and reliably. For larger datasets, please refine your filters or contact support.

**Q: What's included in the CSV file?**

A: Object ID, coordinates, redshift, quality, signal-to-noise, and other metadata.

**Q: What's included in the FITS ZIP?**

A: All FITS files for the selected objects, organized by object ID and grating.

#### 12.2 Developer Documentation

Add to `web/docs/`:
- This implementation plan
- Worker deployment guide
- Troubleshooting guide
- API reference for download actions

### 13. Security Considerations

#### 13.1 Authentication

- Downloads respect user authentication (must be logged in)
- Token includes user ID (future: for audit logging)
- Rate limiting on token generation (10 requests/minute per user)

#### 13.2 Authorization

- Only download objects user has access to
- Respect proprietary data access codes
- Filter results based on user permissions before generating token

#### 13.3 Token Security

- Short-lived tokens (10 minutes)
- Signed with strong secret (32 bytes)
- Cannot be tampered with
- Single-use tokens (future enhancement)

### 14. Rollout Plan

#### 14.1 Development

- [ ] Implement on local environment
- [ ] Test with local Wrangler dev server
- [ ] Test with development Supabase instance

#### 14.2 Staging

- [ ] Deploy Worker to staging environment
- [ ] Test with staging R2 bucket
- [ ] Test with small team (5-10 users)
- [ ] Collect feedback

#### 14.3 Production

- [ ] Deploy Worker to production
- [ ] Enable for all users
- [ ] Monitor error rates closely for first week
- [ ] Gather user feedback

### 15. Success Criteria

The implementation will be considered successful when:

- [ ] Users can download CSV metadata for any filtered results
- [ ] Users can download FITS ZIP for up to 200 objects
- [ ] Download success rate >95%
- [ ] Average download time <30s for 100 objects
- [ ] No server timeouts or memory issues
- [ ] Positive user feedback
- [ ] Error rate <2%

### 16. Required Information from You

Before implementation, I need:

1. **Cloudflare Account:**
   - Account ID (from Cloudflare dashboard)
   - API token (for Wrangler deployment)

2. **R2 Bucket:**
   - Bucket name (e.g., "campfire-data")
   - Current R2 access configuration

3. **Domain:**
   - Subdomain for worker (e.g., `download.campfire.yourdomain.com`)
   - Or we can use workers.dev domain for testing

4. **Access:**
   - Do you want me to deploy, or will you deploy after I provide the code?
   - Should I create the Wrangler configuration, or do you have preferences?

5. **Preferences:**
   - Which ZIP library? (fflate is lightweight, archiver is full-featured)
   - Any specific filename conventions for the ZIP?
   - Should we organize files in folders within the ZIP? (e.g., by object_id/)

Please let me know this information, and we can proceed with implementation!

---

**Document Version:** 1.0
**Last Updated:** 2025-01-26
**Status:** Planning - Ready for Implementation
