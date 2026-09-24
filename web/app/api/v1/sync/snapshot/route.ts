import { NextRequest, NextResponse } from 'next/server';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { createServiceClient } from '@/lib/supabase/service';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';
import { getBucketNameForBackend, getS3ClientForBackend } from '@/lib/storage';
import { latestReadySnapshot } from '@/lib/server/sync-snapshot';

/**
 * GET /api/v1/sync/snapshot
 *
 * The latest nightly public-scope sync catalog snapshot, for a first-time (or
 * --full) `campfire sync`: presigned urls for one gzip JSONL file per sync
 * stream, plus the snapshot's `started_at` (the client's catch-up cursor) and
 * id (the `snapshot=` param of the extras walk). See lib/server/sync-snapshot.
 *
 * `{available: false, reason}` tells the client to walk the streams live:
 * - `admin`: admins mirror drafts, which a public-scope snapshot lacks;
 * - `none`: no snapshot has been built yet;
 * - `scope`: the snapshot holds a program the caller can no longer see (a
 *   program made private after the build).
 */

const URL_TTL_SECONDS = 3600;

export async function GET(request: NextRequest) {
  const userId = await validateAuth(request);
  if (!userId) {
    return NextResponse.json({ error: 'Invalid or missing authentication' }, { status: 401 });
  }

  const noStore = { 'Cache-Control': 'private, no-store' };
  const unavailable = (reason: string) =>
    NextResponse.json({ available: false, reason }, { headers: noStore });

  try {
    if (await isAdminUser(userId)) return unavailable('admin');

    const supabase = createServiceClient();
    const snapshot = await latestReadySnapshot(supabase);
    if (!snapshot) return unavailable('none');

    const accessible = new Set(await getAccessiblePrograms(userId));
    if (!snapshot.public_programs.every((slug) => accessible.has(slug))) {
      return unavailable('scope');
    }

    const client = getS3ClientForBackend('osn');
    const bucket = getBucketNameForBackend('osn');
    const files = await Promise.all(
      snapshot.files.map(async (f) => ({
        stream: f.stream,
        sha256: f.sha256,
        size: f.size,
        rows: f.rows,
        url: await getSignedUrl(client, new GetObjectCommand({ Bucket: bucket, Key: f.key }), {
          expiresIn: URL_TTL_SECONDS,
        }),
      })),
    );

    return NextResponse.json(
      {
        available: true,
        snapshot_id: snapshot.id,
        started_at: snapshot.started_at,
        format_version: snapshot.format_version,
        files,
      },
      { headers: noStore },
    );
  } catch (error) {
    // The client falls back to a live walk on any non-available answer.
    console.error('Error in API /v1/sync/snapshot:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
