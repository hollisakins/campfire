import { NextRequest, NextResponse } from 'next/server';
import { validateAuth } from '@/lib/api-auth';
import { isAdminUser } from '@/lib/api-helpers';
import { resolveBackend } from '@/lib/storage';

/**
 * GET /api/v1/deploy/tiles-credentials
 *
 * Return the R2 **tiles**-bucket credentials to a logged-in admin so the
 * `campfire fitsgl deploy` CLI can construct FitsGL's boto3 `R2Target`
 * (epic #337, Phase 3). FitsGL's deploy needs a real S3 client — it GETs the
 * prior `deploy-manifest.json` ledger, DELETEs orphaned supertiles, and calls
 * PutBucketCors — operations a presigned PutObject URL cannot express, so it
 * cannot ride the `/deploy/presign` path.
 *
 * Bounded relaxation of issue #250: this places raw tiles-bucket write keys on
 * the admin machine (in memory). Acceptable ONLY because the tiles bucket is
 * public, derived data and tile deletion already needs direct `r2_tiles` creds.
 * Do NOT add a data-bucket equivalent.
 *
 * Response (snake_case to mirror the Python BackendConfig the CLI feeds FitsGL):
 * {
 *   endpoint, region, bucket, access_key_id, secret_access_key,
 *   force_path_style, public_url_base
 * }
 */
export async function GET(request: NextRequest) {
  try {
    // Authenticate (API key sk_* or JWT access token).
    const userId = await validateAuth(request);
    if (!userId) {
      return NextResponse.json(
        { error: 'unauthorized', error_description: 'Valid authentication required' },
        { status: 401 }
      );
    }

    // Admin-only (same gate as /deploy/presign — reuses the shared helper so the
    // three deploy routes can't drift on access control).
    if (!(await isAdminUser(userId))) {
      return NextResponse.json(
        { error: 'forbidden', error_description: 'Admin access required for deployment' },
        { status: 403 }
      );
    }

    // Resolve the tiles-bucket backend from the server's S3_TILES_* env.
    let b;
    try {
      b = resolveBackend('tiles');
    } catch {
      return NextResponse.json(
        { error: 'server_error', error_description: 'Tiles storage credentials not configured' },
        { status: 500 }
      );
    }

    // Never let a proxy or the browser cache credentials.
    return NextResponse.json(
      {
        endpoint: b.endpoint,
        region: b.region,
        bucket: b.bucket,
        access_key_id: b.accessKeyId,
        secret_access_key: b.secretAccessKey,
        force_path_style: b.forcePathStyle,
        public_url_base: b.publicUrlBase ?? null,
      },
      { headers: { 'Cache-Control': 'no-store' } }
    );
  } catch (error) {
    console.error('Error in GET /api/v1/deploy/tiles-credentials:', error);
    return NextResponse.json(
      { error: 'server_error', error_description: 'Failed to resolve tiles credentials' },
      { status: 500 }
    );
  }
}
