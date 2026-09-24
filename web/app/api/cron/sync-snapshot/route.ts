import { NextRequest, NextResponse } from 'next/server';
import { createHash } from 'crypto';
import { createGzip } from 'zlib';
import { DeleteObjectCommand, PutObjectCommand } from '@aws-sdk/client-s3';
import type { SupabaseClient } from '@supabase/supabase-js';
import { createServiceClient } from '@/lib/supabase/service';
import { getOsnWriteBucket, getOsnWriteClient } from '@/lib/storage';
import {
  SYNC_STREAMS,
  fetchSyncPage,
  syncCursorKey,
  type SyncStreamName,
} from '@/lib/server/sync-streams';
import {
  SYNC_SNAPSHOT_FORMAT_VERSION,
  SYNC_SNAPSHOT_KEEP,
  syncSnapshotKey,
  type SyncSnapshotFile,
} from '@/lib/server/sync-snapshot';

/**
 * GET /api/cron/sync-snapshot  (Vercel cron, nightly; see web/vercel.json)
 *
 * Builds the public-scope sync catalog snapshot (lib/server/sync-snapshot):
 * walks the five sync streams one after another for the public programs,
 * exactly as the /api/v1/sync/* routes would for a public-only caller, and
 * uploads one gzip JSONL file per stream (one route `data[]` row per line) to
 * the private OSN data bucket. Serial and count-free on purpose: this is the
 * one full catalog walk the database does per night instead of one per new
 * user, so it should be the gentlest walk there is.
 *
 * Auth: Vercel sends `Authorization: Bearer $CRON_SECRET`. Trigger by hand the
 * same way (`curl -H "Authorization: Bearer $CRON_SECRET" .../api/cron/sync-snapshot`).
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
// ~30-60 s of database time at 2026-09 catalog size, plus upload; 300 s is
// the Hobby-plan ceiling with Fluid compute.
export const maxDuration = 300;

// Must match the Python client's FINAL_PRODUCT_TYPES (python/campfire/db/store.py):
// the storage mirror carries finals only.
const FINAL_PRODUCT_TYPES = ['nirspec_spec', 'nircam_mosaic'];

const PAGE_SIZE: Record<SyncStreamName, number> = {
  objects: 5000,
  spectra: 5000,
  storage: 10000,
  photometry: 5000,
  lines: 5000,
};

// A build still `building` after this long died without marking itself failed.
const STALE_BUILD_MS = 15 * 60 * 1000;

async function buildStreamFile(
  supabase: SupabaseClient,
  stream: SyncStreamName,
  publicPrograms: string[],
): Promise<{ body: Buffer; rows: number }> {
  const gzip = createGzip({ level: 6 });
  const chunks: Buffer[] = [];
  gzip.on('data', (c: Buffer) => chunks.push(c));
  const finished = new Promise<void>((resolve, reject) => {
    gzip.on('end', resolve);
    gzip.on('error', reject);
  });

  const limit = PAGE_SIZE[stream];
  const cursorKey = syncCursorKey(stream);
  let after: string | number | null = null;
  let rows = 0;
  for (;;) {
    const { page, error } = await fetchSyncPage(supabase, stream, {
      programSlugs: publicPrograms,
      userId: null,
      updatedSince: null,
      limit,
      includeCounts: false,
      includeUnpublished: false,
      after,
      productTypes: stream === 'storage' ? FINAL_PRODUCT_TYPES : null,
    });
    if (error) throw new Error(`${stream} page after ${after}: ${error.message}`);
    if (page.rows.length > 0) {
      const text = page.rows.map((r) => JSON.stringify(r)).join('\n') + '\n';
      await new Promise<void>((resolve, reject) =>
        gzip.write(text, (err) => (err ? reject(err) : resolve())),
      );
      rows += page.rows.length;
    }
    if (page.rows.length < limit) break;
    after = page.rows[page.rows.length - 1][cursorKey] as string | number;
  }
  gzip.end();
  await finished;
  return { body: Buffer.concat(chunks), rows };
}

/** Delete a snapshot's files (best effort) and its row. */
async function dropSnapshot(
  supabase: SupabaseClient,
  row: { id: number; files: SyncSnapshotFile[] | null },
): Promise<void> {
  const s3 = getOsnWriteClient();
  const bucket = getOsnWriteBucket();
  for (const f of row.files ?? []) {
    try {
      await s3.send(new DeleteObjectCommand({ Bucket: bucket, Key: f.key }));
    } catch (err) {
      console.warn(`sync-snapshot prune: could not delete ${f.key}:`, err);
    }
  }
  await supabase.from('sync_snapshots').delete().eq('id', row.id);
}

async function prune(supabase: SupabaseClient): Promise<void> {
  const { data: ready } = await supabase
    .from('sync_snapshots')
    .select('id, files')
    .eq('status', 'ready')
    .order('id', { ascending: false });
  for (const row of (ready ?? []).slice(SYNC_SNAPSHOT_KEEP)) await dropSnapshot(supabase, row);

  const staleBefore = new Date(Date.now() - STALE_BUILD_MS).toISOString();
  const { data: dead } = await supabase
    .from('sync_snapshots')
    .select('id, files')
    .or(`status.eq.failed,and(status.eq.building,started_at.lt.${staleBefore})`);
  for (const row of dead ?? []) await dropSnapshot(supabase, row);
}

export async function GET(request: NextRequest) {
  const secret = process.env.CRON_SECRET;
  if (!secret || request.headers.get('authorization') !== `Bearer ${secret}`) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const supabase = createServiceClient();

  // One build at a time (a manual trigger overlapping the nightly run).
  const staleBefore = new Date(Date.now() - STALE_BUILD_MS).toISOString();
  const { data: running } = await supabase
    .from('sync_snapshots')
    .select('id')
    .eq('status', 'building')
    .gte('started_at', staleBefore)
    .limit(1);
  if (running && running.length > 0) {
    return NextResponse.json({ error: 'a snapshot build is already running' }, { status: 409 });
  }

  const { data: begun, error: beginError } = await supabase.rpc('sync_snapshot_begin', {
    p_format_version: SYNC_SNAPSHOT_FORMAT_VERSION,
  });
  if (beginError || !begun?.[0]) {
    console.error('sync-snapshot: begin failed:', beginError);
    return NextResponse.json({ error: 'could not start a snapshot build' }, { status: 500 });
  }
  const snap = begun[0] as { id: number; public_programs: string[] };

  const s3 = getOsnWriteClient();
  const bucket = getOsnWriteBucket();
  const files: SyncSnapshotFile[] = [];
  const t0 = Date.now();
  try {
    for (const stream of SYNC_STREAMS) {
      const ts = Date.now();
      const { body, rows } = await buildStreamFile(supabase, stream, snap.public_programs);
      const key = syncSnapshotKey(snap.id, stream);
      await s3.send(
        new PutObjectCommand({ Bucket: bucket, Key: key, Body: body, ContentType: 'application/gzip' }),
      );
      files.push({
        stream,
        key,
        sha256: `sha256:${createHash('sha256').update(body).digest('hex')}`,
        size: body.length,
        rows,
      });
      // Recorded as each file lands, so a failed build's uploads can be pruned.
      await supabase.from('sync_snapshots').update({ files }).eq('id', snap.id);
      console.log(
        `sync-snapshot ${snap.id}: ${stream} ${rows} rows, ${body.length} B gz, ${Date.now() - ts} ms`,
      );
    }

    const { error: readyError } = await supabase
      .from('sync_snapshots')
      .update({ status: 'ready', completed_at: new Date().toISOString(), files })
      .eq('id', snap.id);
    if (readyError) throw new Error(`mark ready: ${readyError.message}`);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`sync-snapshot ${snap.id} failed:`, err);
    await supabase
      .from('sync_snapshots')
      .update({ status: 'failed', error: message.slice(0, 2000) })
      .eq('id', snap.id);
    return NextResponse.json({ error: 'snapshot build failed', snapshot_id: snap.id }, { status: 500 });
  }

  await prune(supabase).catch((err) => console.warn('sync-snapshot prune failed:', err));

  return NextResponse.json({
    snapshot_id: snap.id,
    files: files.map(({ stream, rows, size }) => ({ stream, rows, size })),
    duration_ms: Date.now() - t0,
  });
}
