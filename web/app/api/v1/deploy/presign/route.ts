import { NextRequest, NextResponse } from 'next/server';
import { PutObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { validateAuth } from '@/lib/api-auth';
import { createClient } from '@supabase/supabase-js';
import { getS3Client, getBucketName, type StoragePurpose } from '@/lib/storage';
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
    const { bucket: bucketId = 'data', uploads, cache_control } = body;

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

    // Resolve the storage client + bucket for the requested purpose.
    const purpose = bucketId as StoragePurpose;
    const client = getS3Client(purpose);
    const bucket = getBucketName(purpose);

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
