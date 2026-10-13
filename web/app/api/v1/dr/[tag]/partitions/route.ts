import { NextRequest, NextResponse } from 'next/server';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { authenticateApiRequest } from '@/lib/api-auth';
import { createServiceClient } from '@/lib/supabase/service';
import { getBucketNameForBackend, getS3ClientForBackend } from '@/lib/storage';
import {
  fetchLedger,
  fetchRelease,
  openComponentPartitions,
  partitionVisible,
  releaseVisible,
} from '@/lib/server/dr-exports';

/**
 * GET /api/v1/dr/<tag>/partitions   (bearer; `campfire pull --dr`)
 *
 * The export partitions of a data release this caller holds, with presigned
 * urls: program partitions within the caller's accessible programs, field-level
 * member partitions (nircam/<field>, photometry/<field>/<catalog>) for every
 * account. A release is visible once published, or in any state to admins
 * (the shadow run). Link accounts are refused by authenticateApiRequest.
 *
 * Response:
 * {
 *   tag, state, format_version, components, counts, frozen_at, published_at,
 *   doi, notes, known_issues,
 *   complete: boolean,            // every ledger file is ready
 *   partitions: [{ stream, partition, sha256, size, rows, url }]
 * }
 */

const URL_TTL_SECONDS = 3600;
const TAG_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$/;

export async function GET(request: NextRequest, context: { params: Promise<{ tag: string }> }) {
  const principal = await authenticateApiRequest(request);
  if (!principal) {
    return NextResponse.json({ error: 'Invalid or missing authentication' }, { status: 401 });
  }
  const { tag } = await context.params;
  if (!TAG_RE.test(tag)) {
    return NextResponse.json({ error: 'invalid_tag' }, { status: 400 });
  }
  const noStore = { 'Cache-Control': 'private, no-store' };

  try {
    const supabase = createServiceClient();
    const release = await fetchRelease(supabase, tag);
    // Unknown and not-yet-visible look the same to a reader.
    if (!release || !releaseVisible(release, principal.access.isAdmin)) {
      return NextResponse.json({ error: 'not_found' }, { status: 404, headers: noStore });
    }

    const accessible = new Set(principal.access.accessibleSlugs);
    const open = openComponentPartitions(release.components);
    const ledger = await fetchLedger(supabase, tag);
    const complete = ledger.length > 0 && ledger.every((r) => r.state === 'ready');
    const held = ledger.filter(
      (r) => r.state === 'ready' && r.storage_key && partitionVisible(r.stream, r.partition, accessible, open),
    );

    const client = getS3ClientForBackend('osn');
    const bucket = getBucketNameForBackend('osn');
    const partitions = await Promise.all(
      held.map(async (r) => ({
        stream: r.stream,
        partition: r.partition,
        sha256: r.sha256,
        size: r.size_bytes,
        rows: r.rows,
        url: await getSignedUrl(
          client,
          new GetObjectCommand({ Bucket: bucket, Key: r.storage_key as string }),
          { expiresIn: URL_TTL_SECONDS },
        ),
      })),
    );

    return NextResponse.json(
      {
        tag: release.tag,
        state: release.state,
        format_version: release.dr_format_version,
        components: release.components,
        counts: release.counts,
        frozen_at: release.frozen_at,
        published_at: release.published_at,
        doi: release.doi,
        notes: release.notes,
        known_issues: release.known_issues,
        complete,
        partitions,
      },
      { headers: noStore },
    );
  } catch (error) {
    console.error(`Error in GET /api/v1/dr/${tag}/partitions:`, error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
