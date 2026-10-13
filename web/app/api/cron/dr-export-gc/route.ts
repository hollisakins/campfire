import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { getOsnWriteBucket, getOsnWriteClient } from '@/lib/storage';
import { sweepSupersededExports } from '@/lib/server/dr-exports';

/**
 * GET /api/cron/dr-export-gc  (Vercel cron, daily; see web/vercel.json)
 *
 * Collects superseded data-release export files (P4b): for every release,
 * deletes the objects under `dr/<tag>/exports/` the ledger no longer names
 * once their grace (`EXPORT_GARBAGE_GRACE_MS`, longer than a presigned url
 * lives) has passed. An export run sweeps its own release when it finishes,
 * but a file it superseded is still inside its grace then; this is the caller
 * that comes back for it (Codex on #600).
 *
 * Auth: Vercel sends `Authorization: Bearer $CRON_SECRET`. Trigger by hand the
 * same way.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const maxDuration = 300;

export async function GET(request: NextRequest) {
  const secret = process.env.CRON_SECRET;
  if (!secret || request.headers.get('authorization') !== `Bearer ${secret}`) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }
  const supabase = createServiceClient();
  const { data, error } = await supabase.from('dr_releases').select('tag').order('tag');
  if (error) {
    console.error('dr-export-gc: dr_releases lookup failed', error);
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  const s3 = getOsnWriteClient();
  const bucket = getOsnWriteBucket();
  const swept: Record<string, number> = {};
  const failed: Record<string, string> = {};
  for (const { tag } of (data as { tag: string }[] | null) ?? []) {
    try {
      swept[tag] = await sweepSupersededExports(supabase, s3, bucket, tag);
    } catch (e) {
      // one release never stops the others
      failed[tag] = e instanceof Error ? e.message : String(e);
      console.error(`dr-export-gc: ${tag}`, e);
    }
  }
  return NextResponse.json(
    { swept, failed },
    { status: Object.keys(failed).length ? 500 : 200, headers: { 'Cache-Control': 'no-store' } },
  );
}
