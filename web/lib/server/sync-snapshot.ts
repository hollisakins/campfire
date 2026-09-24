import 'server-only';

import { NextResponse } from 'next/server';
import type { SupabaseClient } from '@supabase/supabase-js';
import type { SyncStreamName } from '@/lib/server/sync-streams';

/**
 * Nightly public-scope sync catalog snapshots.
 *
 * A first-time `campfire sync` used to page all five /api/v1/sync/* streams
 * (~250k rows) out of Postgres; on 2026-09-24 one such walk stalled the
 * database. The /api/cron/sync-snapshot route now walks the streams once a
 * night for the PUBLIC programs only and writes one gzip JSONL file per
 * stream to the private data bucket (OSN). /api/v1/sync/snapshot hands a
 * client presigned urls for the latest ready snapshot; the client loads it,
 * runs an "extras" walk (`?snapshot=<id>` on the sync routes) for what a
 * public-scope snapshot cannot carry for that caller, then catches up
 * incrementally from the snapshot's `started_at`.
 *
 * The files are deliberately not a layout product: never registered in
 * storage_objects, never presignable through /storage/presign -- reachable
 * only through the snapshot endpoint, which requires an account.
 */

/** Bump when the file format changes; the client refuses versions it does not know. */
export const SYNC_SNAPSHOT_FORMAT_VERSION = 1;

/** Newest ready snapshots kept; older files and rows are pruned by the builder. */
export const SYNC_SNAPSHOT_KEEP = 3;

export const SYNC_SNAPSHOT_PREFIX = 'sync-snapshots/';

export function syncSnapshotKey(id: number, stream: SyncStreamName): string {
  return `${SYNC_SNAPSHOT_PREFIX}${id}/${stream}.jsonl.gz`;
}

export interface SyncSnapshotFile {
  stream: SyncStreamName;
  key: string;
  /** "sha256:<hex>" of the gzip bytes as stored. */
  sha256: string;
  size: number;
  rows: number;
}

export interface SyncSnapshotRow {
  id: number;
  started_at: string;
  format_version: number;
  public_programs: string[];
  files: SyncSnapshotFile[];
}

/** The newest `ready` snapshot, or null when none has been built yet. */
export async function latestReadySnapshot(
  supabase: SupabaseClient,
): Promise<SyncSnapshotRow | null> {
  const { data, error } = await supabase
    .from('sync_snapshots')
    .select('id, started_at, format_version, public_programs, files')
    .eq('status', 'ready')
    .order('id', { ascending: false })
    .limit(1)
    .maybeSingle();
  if (error) throw new Error(`sync_snapshots lookup failed: ${error.message}`);
  return (data as SyncSnapshotRow | null) ?? null;
}

/**
 * Extras mode for a sync route (`?snapshot=<id>`): the caller's accessible
 * programs minus the programs the snapshot was built for. The program set is
 * read from the snapshot row, never from the request, so a client cannot
 * widen its scope; a program made public after the build is not in the
 * snapshot's set and so lands in the extras by construction.
 *
 * Returns a 400 response for a malformed / unknown snapshot, or when the
 * caller also sent `updated_since` (an extras walk is a full walk of a
 * narrow scope; the catch-up after it is a normal incremental sync).
 */
export async function resolveSnapshotExtras(
  supabase: SupabaseClient,
  snapshotParam: string,
  accessibleProgramSlugs: string[],
  searchParams: URLSearchParams,
): Promise<{ extras: string[]; error: null } | { extras: null; error: NextResponse }> {
  const bad = (message: string) => ({
    extras: null,
    error: NextResponse.json({ error: message }, { status: 400 }),
  });
  if (searchParams.get('updated_since')) {
    return bad('snapshot extras walks are full walks: drop updated_since');
  }
  const id = Number(snapshotParam);
  if (!Number.isSafeInteger(id) || id <= 0) return bad('invalid snapshot id');

  const { data, error } = await supabase
    .from('sync_snapshots')
    .select('public_programs')
    .eq('id', id)
    .eq('status', 'ready')
    .maybeSingle();
  if (error) throw new Error(`sync_snapshots lookup failed: ${error.message}`);
  if (!data) return bad('unknown snapshot');

  const inSnapshot = new Set((data as { public_programs: string[] }).public_programs);
  return { extras: accessibleProgramSlugs.filter((s) => !inSnapshot.has(s)), error: null };
}
