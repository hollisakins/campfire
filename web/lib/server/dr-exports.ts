import 'server-only';

import { createHash } from 'crypto';
import { createGzip } from 'zlib';
import { PutObjectCommand } from '@aws-sdk/client-s3';
import type { S3Client } from '@aws-sdk/client-s3';
import type { SupabaseClient } from '@supabase/supabase-js';

/**
 * Data-release exports (P4b, docs/dr-format-v1.md).
 *
 * A frozen release is exported as one gzip JSONL file per (stream, partition)
 * under `dr/<tag>/exports/` on the private data bucket, built from the frozen
 * tables by paging `dr_export_rows` and recorded in the `dr_exports` ledger
 * (`dr_export_begin` seeds it, `dr_export_mark` records each file). The admin
 * route /api/v1/dr/<tag>/export runs `runExportBudget` for a slice of wall
 * time and reports what is left; `campfire dr export` loops it. Each partition
 * is idempotent: a re-run rebuilds only the files that are not `ready`.
 *
 * Like the sync snapshots, the keys are not layout products: never registered
 * in storage_objects, never presignable through the storage routes; readers
 * get them only through /api/v1/dr/<tag>/partitions, which hands out the
 * partitions the caller holds.
 */

/** Bump when the row shapes or the file layout change; recorded on dr_releases. */
export const DR_FORMAT_VERSION = 1;

export const DR_EXPORT_STREAMS = ['objects', 'spectra', 'line_fits', 'photometry', 'members'] as const;
export type DrExportStream = (typeof DR_EXPORT_STREAMS)[number];

/** Rows per dr_export_rows page while building a file. */
export const DR_EXPORT_PAGE = 2000;

export interface DrExportLedgerRow {
  tag: string;
  stream: DrExportStream;
  partition: string;
  storage_key: string | null;
  sha256: string | null;
  size_bytes: number | null;
  rows: number | null;
  state: 'pending' | 'building' | 'ready' | 'failed';
  built_at: string | null;
  error: string | null;
}

export interface DrReleaseRow {
  tag: string;
  state: string;
  components: unknown;
  dr_format_version: number;
  counts: Record<string, unknown> | null;
  frozen_at: string | null;
  published_at: string | null;
  withdrawn_at: string | null;
  doi: string | null;
  notes: string | null;
  known_issues: string | null;
}

/** A partition name of a field-level member component (nircam/<field>, photometry/<field>/<catalog>). */
export function isComponentPartition(partition: string): boolean {
  return partition.startsWith('nircam/') || partition.startsWith('photometry/');
}

/**
 * Whether a viewer holding `accessible` programs may fetch a partition:
 * program partitions within their programs; field-level member partitions
 * always (published field deployments are visible to every account, as live).
 */
export function partitionVisible(partition: string, accessible: ReadonlySet<string>): boolean {
  return isComponentPartition(partition) || accessible.has(partition);
}

/** Readers see a published release; admins see every state (the shadow run). */
export function releaseVisible(release: Pick<DrReleaseRow, 'state'>, isAdmin: boolean): boolean {
  return isAdmin || release.state === 'published';
}

/** The `dr_releases` row for a tag, or null. */
export async function fetchRelease(
  supabase: SupabaseClient,
  tag: string,
): Promise<DrReleaseRow | null> {
  const { data, error } = await supabase
    .from('dr_releases')
    .select(
      'tag, state, components, dr_format_version, counts, frozen_at, published_at, withdrawn_at, doi, notes, known_issues',
    )
    .eq('tag', tag)
    .maybeSingle();
  if (error) throw new Error(`dr_releases lookup failed: ${error.message}`);
  return (data as DrReleaseRow | null) ?? null;
}

/** The export ledger of a tag. */
export async function fetchLedger(
  supabase: SupabaseClient,
  tag: string,
): Promise<DrExportLedgerRow[]> {
  const { data, error } = await supabase
    .from('dr_exports')
    .select('tag, stream, partition, storage_key, sha256, size_bytes, rows, state, built_at, error')
    .eq('tag', tag)
    .order('stream')
    .order('partition');
  if (error) throw new Error(`dr_exports lookup failed: ${error.message}`);
  return (data as DrExportLedgerRow[] | null) ?? [];
}

/**
 * Build one partition file: page `dr_export_rows` from the start and gzip one
 * JSON object per line. Returns the gzip bytes and the row count.
 */
export async function buildPartitionFile(
  supabase: SupabaseClient,
  tag: string,
  stream: DrExportStream,
  partition: string,
  page: number = DR_EXPORT_PAGE,
): Promise<{ body: Buffer; rows: number }> {
  const gzip = createGzip({ level: 6 });
  const chunks: Buffer[] = [];
  gzip.on('data', (c: Buffer) => chunks.push(c));
  const finished = new Promise<void>((resolve, reject) => {
    gzip.on('end', resolve);
    gzip.on('error', reject);
  });

  let after: string | null = null;
  let rows = 0;
  for (;;) {
    const { data, error } = await supabase.rpc('dr_export_rows', {
      p_tag: tag,
      p_stream: stream,
      p_partition: partition,
      p_after: after,
      p_limit: page,
    });
    if (error) throw new Error(`${stream}/${partition} after ${after}: ${error.message}`);
    const pageRows = (data ?? []) as { next_cursor: string; row_json: unknown }[];
    if (pageRows.length > 0) {
      const text = pageRows.map((r) => JSON.stringify(r.row_json)).join('\n') + '\n';
      await new Promise<void>((resolve, reject) =>
        gzip.write(text, (err) => (err ? reject(err) : resolve())),
      );
      rows += pageRows.length;
    }
    if (pageRows.length < page) break;
    after = pageRows[pageRows.length - 1].next_cursor;
  }
  gzip.end();
  await finished;
  return { body: Buffer.concat(chunks), rows };
}

export interface ExportRunResult {
  tag: string;
  /** Release state after this run (exporting | exported). */
  state: string;
  built: { stream: string; partition: string; rows: number; size: number }[];
  failed: { stream: string; partition: string; error: string }[];
  /** Ledger rows still not ready after this run. */
  remaining: number;
  /** True when the time budget ran out before every file was built. */
  incomplete: boolean;
  duration_ms: number;
}

/**
 * Headroom kept before starting another partition: a partition starts only
 * while the budget left covers the larger of this and twice the slowest
 * partition built so far, so the last one started finishes inside the
 * route's `maxDuration` instead of being cut off mid-upload (and left
 * `building` for the next call to redo).
 */
export const PARTITION_RESERVE_MS = 30_000;

/**
 * Seed the ledger, then build pending / failed partitions until the budget
 * is spent. One partition at a time, each marked `building` → `ready` (or
 * `failed` with the error, and the run continues with the next one). The
 * first partition always starts, so every call makes progress.
 */
export async function runExportBudget(
  supabase: SupabaseClient,
  s3: S3Client,
  bucket: string,
  tag: string,
  budgetMs: number,
  now: () => number = Date.now,
): Promise<ExportRunResult> {
  const t0 = now();
  const { data: begun, error: beginError } = await supabase.rpc('dr_export_begin', { p_tag: tag });
  if (beginError) throw new Error(beginError.message);
  let state = String((begun as { state?: string } | null)?.state ?? 'exporting');

  const ledger = await fetchLedger(supabase, tag);
  const todo = ledger.filter((r) => r.state !== 'ready');
  const built: ExportRunResult['built'] = [];
  const failed: ExportRunResult['failed'] = [];
  let incomplete = false;
  let slowestMs = 0;

  for (const entry of todo) {
    const left = budgetMs - (now() - t0);
    const started = built.length + failed.length;
    if (started > 0 && left < Math.max(PARTITION_RESERVE_MS, 2 * slowestMs)) {
      incomplete = true;
      break;
    }
    const tp = now();
    const key = entry.storage_key ?? '';
    try {
      await supabase.rpc('dr_export_mark', {
        p_tag: tag, p_stream: entry.stream, p_partition: entry.partition, p_state: 'building',
      });
      const { body, rows } = await buildPartitionFile(supabase, tag, entry.stream, entry.partition);
      await s3.send(
        new PutObjectCommand({ Bucket: bucket, Key: key, Body: body, ContentType: 'application/gzip' }),
      );
      const sha256 = `sha256:${createHash('sha256').update(body).digest('hex')}`;
      const { data: marked, error: markError } = await supabase.rpc('dr_export_mark', {
        p_tag: tag, p_stream: entry.stream, p_partition: entry.partition, p_state: 'ready',
        p_sha256: sha256, p_size_bytes: body.length, p_rows: rows,
      });
      if (markError) throw new Error(`mark ready: ${markError.message}`);
      state = String((marked as { state?: string } | null)?.state ?? state);
      built.push({ stream: entry.stream, partition: entry.partition, rows, size: body.length });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      failed.push({ stream: entry.stream, partition: entry.partition, error: message });
      await supabase.rpc('dr_export_mark', {
        p_tag: tag, p_stream: entry.stream, p_partition: entry.partition, p_state: 'failed',
        p_error: message,
      });
    }
    slowestMs = Math.max(slowestMs, now() - tp);
  }

  const remaining = todo.length - built.length;
  return { tag, state, built, failed, remaining, incomplete, duration_ms: now() - t0 };
}
