import { NextRequest, NextResponse } from 'next/server';
import { PutObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { validateAuth } from '@/lib/api-auth';
import { createClient } from '@supabase/supabase-js';
import {
  getS3Client,
  getBucketName,
  getOsnWriteClient,
  getOsnWriteBucket,
  type StoragePurpose,
} from '@/lib/storage';
import { isKnownKey } from '@/lib/layout';

const MAX_BATCH_SIZE = 500;
const PRESIGN_EXPIRY_SECONDS = 3600; // 1 hour

/**
 * POST /api/v1/deploy/presign
 *
 * Generate presigned PutObject URLs for batch R2 uploads.
 * Requires admin authentication.
 *
 * Request body:
 * {
 *   bucket: "data" | "tiles",
 *   backend?: "r2" | "osn",   // default "r2"; "osn" signs against the OSN data
 *                             // bucket (epic #210 / #216 — deploy → OSN). Only
 *                             // valid with bucket="data".
 *   uploads: [
 *     { key: "spectra/obs_name/file.fits", content_type: "application/fits" },
 *     ...
 *   ],
 *   cache_control?: string  // optional, applied to all uploads
 * }
 *
 * Response:
 * {
 *   urls: {
 *     "spectra/obs_name/file.fits": "https://...",
 *     ...
 *   }
 * }
 */
export async function POST(request: NextRequest) {
  try {
    // Authenticate
    const userId = await validateAuth(request);
    if (!userId) {
      return NextResponse.json(
        { error: 'unauthorized', error_description: 'Valid authentication required' },
        { status: 401 }
      );
    }

    // Check admin role
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    const { data: profile } = await supabase
      .from('user_profiles')
      .select('is_admin')
      .eq('user_id', userId)
      .single();

    if (!profile?.is_admin) {
      return NextResponse.json(
        { error: 'forbidden', error_description: 'Admin access required for deployment' },
        { status: 403 }
      );
    }

    // Parse request
    const body = await request.json();
    const { bucket: bucketId = 'data', backend = 'r2', uploads, cache_control } = body;

    if (!uploads || !Array.isArray(uploads) || uploads.length === 0) {
      return NextResponse.json(
        { error: 'invalid_request', error_description: 'uploads array is required' },
        { status: 400 }
      );
    }

    if (uploads.length > MAX_BATCH_SIZE) {
      return NextResponse.json(
        { error: 'invalid_request', error_description: `Maximum ${MAX_BATCH_SIZE} uploads per request` },
        { status: 400 }
      );
    }

    if (bucketId !== 'data' && bucketId !== 'tiles') {
      return NextResponse.json(
        { error: 'invalid_request', error_description: 'bucket must be "data" or "tiles"' },
        { status: 400 }
      );
    }

    if (backend !== 'r2' && backend !== 'osn') {
      return NextResponse.json(
        { error: 'invalid_request', error_description: 'backend must be "r2" or "osn"' },
        { status: 400 }
      );
    }

    // OSN holds only data-bucket products (tiles stay on R2 permanently).
    if (backend === 'osn' && bucketId !== 'data') {
      return NextResponse.json(
        { error: 'invalid_request', error_description: 'backend "osn" is only valid with bucket "data"' },
        { status: 400 }
      );
    }

    // Allowlist: every key must be a contract-valid key for the requested bucket.
    // Rejects traversal/unsafe keys and anything outside the layout contract, so
    // a presign can never sign an arbitrary or out-of-tree object.
    const badUpload = uploads.find(
      (u: { key?: string }) => !u.key || !isKnownKey(u.key, { bucket: bucketId })
    );
    if (badUpload) {
      return NextResponse.json(
        { error: 'invalid_request', error_description: `key not permitted by layout contract: ${badUpload.key}` },
        { status: 400 }
      );
    }

    // Resolve the storage client + bucket. The key allowlist above is keyed on the
    // logical bucket ('data'/'tiles'); the signing target is chosen by `backend`:
    // 'osn' signs canonical data keys against the OSN bucket with write creds,
    // 'r2' keeps today's behavior (R2 'data'/'tiles' purpose).
    const client = backend === 'osn' ? getOsnWriteClient() : getS3Client(bucketId as StoragePurpose);
    const bucket = backend === 'osn' ? getOsnWriteBucket() : getBucketName(bucketId as StoragePurpose);

    // Generate presigned URLs in parallel
    const urlEntries = await Promise.all(
      uploads.map(async (upload: { key: string; content_type?: string }) => {
        const command = new PutObjectCommand({
          Bucket: bucket,
          Key: upload.key,
          ContentType: upload.content_type || undefined,
          CacheControl: cache_control || undefined,
        });
        const url = await getSignedUrl(client, command, {
          expiresIn: PRESIGN_EXPIRY_SECONDS,
        });

        return [upload.key, url] as const;
      })
    );

    const urls = Object.fromEntries(urlEntries);

    return NextResponse.json({ urls });
  } catch (error) {
    console.error('Error in POST /api/v1/deploy/presign:', error);
    return NextResponse.json(
      { error: 'server_error', error_description: 'Failed to generate presigned URLs' },
      { status: 500 }
    );
  }
}
