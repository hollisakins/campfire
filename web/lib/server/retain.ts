import 'server-only';
import {
  CopyObjectCommand,
  HeadObjectCommand,
  type S3Client,
} from '@aws-sdk/client-s3';
import type { SupabaseClient } from '@supabase/supabase-js';
import { archiveKey, parseKey, toCanonicalKey } from '@/lib/layout';

/**
 * Retain-on-supersede (data releases; docs/design-data-releases-impl-plan.md
 * §4 for the mechanism, §9a / decision D-P2-4 for what it protects).
 *
 * The archive holds exactly the files that releases reference. Before a
 * redeploy overwrites a live NIRSpec final or sidecar whose current bytes a
 * release has frozen but not yet archived (`retain_required_hashes`), those
 * bytes are copied, server side, into the content-addressed archive
 * (`archive/<sha256>/<relpath>`) and an archive registry row is inserted. The
 * live key is then overwritten as today; the archive row carries the relation
 * (`archived_from_key`). Every other overwrite needs no copy and gets none.
 *
 * The gate is always on and has no override: an overwrite that would destroy
 * a frozen release's only bytes is the one unrecoverable event in the model,
 * so a failed or drifted copy refuses the PUT (409) instead of signing it. The
 * fix is finishing `campfire dr archive` (or purging the release that names
 * the bytes), never overwriting them.
 *
 * Drift means the live object changed between the client's plan and this
 * request: the registry hash no longer matches the client's `expected_old_hash`
 * (when the client sends one), the object's size no longer matches the row, or
 * the conditional copy fails its ETag precondition. Those are exactly the races
 * the design wants surfaced, never papered over. Drift is only judged for a
 * referenced object; an unreferenced one is overwritten whatever the client
 * planned against, since nothing depends on its old bytes.
 */

/** Product types retained on redeploy (decision D-P2-1): the six NIRSpec
 * finals/sidecars. Mosaics are archived by the DR freeze only. */
export const RETAINED_PRODUCT_TYPES: ReadonlySet<string> = new Set([
  'nirspec_spec',
  'spectrum_json',
  'spectrum_1d_json',
  'zfit',
  'nirspec_lines',
  'nirspec_lines_json',
]);

export interface RetainCandidate {
  key: string;
  /** The registry hash the client planned against (optional; enables the drift check). */
  expectedOldHash?: string | null;
  /** The hash of the bytes about to be uploaded (optional; lets us skip a true no-op). */
  newHash?: string | null;
}

export interface LiveRow {
  id: number;
  storage_key: string;
  content_hash: string;
  size_bytes: number;
  stored_size_bytes: number | null;
  content_type: string | null;
  product_type: string;
  instrument: string | null;
  observation: string | null;
  spectrum_id: string | null;
  program_slug: string | null;
  deployment_id: number | null;
  cfpipe_version: string | null;
  updated_at: string | null;
}

export type RetainDecision =
  | { key: string; action: 'skip-new' }
  | { key: string; action: 'skip-type'; productType: string }
  | { key: string; action: 'skip-unchanged'; row: LiveRow }
  | { key: string; action: 'skip-provisional-hash'; row: LiveRow }
  | { key: string; action: 'skip-unreferenced'; row: LiveRow }
  | { key: string; action: 'drift'; reason: string; row: LiveRow }
  | { key: string; action: 'retain'; row: LiveRow; archiveKey: string };

const LOOKUP_CHUNK = 50;
/** retain_required_hashes accepts at most 2000 hashes per call. */
const REQUIRED_CHUNK = 1000;

/** The registry rows that pass every local test; only their hashes are asked about. */
function isRetainableRow(row: LiveRow, c: RetainCandidate): boolean {
  if (!RETAINED_PRODUCT_TYPES.has(row.product_type)) return false;
  if (c.newHash && c.newHash === row.content_hash) return false;
  return row.content_hash.startsWith('sha256:');
}

/**
 * Decide, per candidate, whether a retained copy must exist before the PUT.
 * Reads the live registry rows (canonical keys, root = live, status = active),
 * then asks `retain_required_hashes` which of their hashes a release has
 * frozen and not archived; only those are retained (or drifted).
 */
export async function planRetain(
  candidates: RetainCandidate[],
  supabase: SupabaseClient,
): Promise<RetainDecision[]> {
  const canonicalByKey = new Map<string, string | null>();
  for (const c of candidates) {
    try {
      canonicalByKey.set(c.key, toCanonicalKey(c.key));
    } catch {
      canonicalByKey.set(c.key, null);
    }
  }
  const canonicals = Array.from(new Set(Array.from(canonicalByKey.values()).filter((v): v is string => !!v)));
  const rows = new Map<string, LiveRow>();
  const chunks: string[][] = [];
  for (let i = 0; i < canonicals.length; i += LOOKUP_CHUNK) chunks.push(canonicals.slice(i, i + LOOKUP_CHUNK));
  const results = await Promise.all(
    chunks.map((chunk) =>
      supabase
        .from('storage_objects')
        .select(
          'id, storage_key, content_hash, size_bytes, stored_size_bytes, content_type, product_type, instrument, observation, spectrum_id, program_slug, deployment_id, cfpipe_version, updated_at',
        )
        .in('storage_key', chunk)
        .eq('status', 'active')
        .eq('root', 'live'),
    ),
  );
  for (const { data, error } of results) {
    if (error) throw error;
    for (const r of (data ?? []) as LiveRow[]) rows.set(r.storage_key, r);
  }

  const askHashes = Array.from(
    new Set(
      candidates.flatMap((c) => {
        const canonical = canonicalByKey.get(c.key);
        const row = canonical ? rows.get(canonical) : undefined;
        return row && isRetainableRow(row, c) ? [row.content_hash] : [];
      }),
    ),
  );
  const required = new Set<string>();
  for (let i = 0; i < askHashes.length; i += REQUIRED_CHUNK) {
    const { data, error } = await supabase.rpc('retain_required_hashes', { p_hashes: askHashes.slice(i, i + REQUIRED_CHUNK) });
    if (error) throw error;
    for (const h of (data ?? []) as string[]) required.add(h);
  }

  return candidates.map((c): RetainDecision => {
    const canonical = canonicalByKey.get(c.key);
    const row = canonical ? rows.get(canonical) : undefined;
    if (!row) return { key: c.key, action: 'skip-new' };
    if (!RETAINED_PRODUCT_TYPES.has(row.product_type)) return { key: c.key, action: 'skip-type', productType: row.product_type };
    if (c.newHash && c.newHash === row.content_hash) return { key: c.key, action: 'skip-unchanged', row };
    if (!row.content_hash.startsWith('sha256:')) return { key: c.key, action: 'skip-provisional-hash', row };
    if (!required.has(row.content_hash)) return { key: c.key, action: 'skip-unreferenced', row };
    if (c.expectedOldHash && c.expectedOldHash !== row.content_hash) {
      return { key: c.key, action: 'drift', row, reason: `registry hash ${row.content_hash} != planned ${c.expectedOldHash}` };
    }
    const pk = parseKey(row.storage_key);
    const ak = archiveKey(pk.productType, pk.scope, pk.filename, row.content_hash);
    return { key: c.key, action: 'retain', row, archiveKey: ak };
  });
}

export type RetainOutcome =
  | { key: string; status: 'archived'; archiveKey: string; copied: boolean }
  | { key: string; status: 'drift'; reason: string }
  | { key: string; status: 'error'; reason: string };

export interface RetainDeps {
  s3: S3Client;
  bucket: string;
  supabase: SupabaseClient;
  actorId?: string | null;
  now?: () => Date;
}

function errCode(e: unknown): string {
  const anyE = e as { name?: string; Code?: string; $metadata?: { httpStatusCode?: number } };
  return anyE?.name || anyE?.Code || String(anyE?.$metadata?.httpStatusCode ?? 'unknown');
}

/**
 * Make one retained copy exist: HEAD the live object (size must match the
 * row), register the archive row FIRST through `archive_register_retained`
 * (an existing archived row means someone else already registered the same
 * content; a row a collection has claimed is refused and the gate fails
 * closed; once the row is there no collection can claim it), then skip the
 * copy if the archive object already exists (dedup by hash), otherwise a
 * conditional same-bucket CopyObject (412 on ETag drift), and an `archive`
 * deploy event.
 */
export async function executeRetain(
  decision: Extract<RetainDecision, { action: 'retain' }>,
  deps: RetainDeps,
): Promise<RetainOutcome> {
  const { s3, bucket, supabase } = deps;
  const { row, archiveKey: ak, key } = decision;
  const expectedSize = row.stored_size_bytes ?? row.size_bytes;

  let etag: string | undefined;
  try {
    const head = await s3.send(new HeadObjectCommand({ Bucket: bucket, Key: row.storage_key }));
    if (typeof head.ContentLength === 'number' && head.ContentLength !== Number(expectedSize)) {
      return { key, status: 'drift', reason: `live object is ${head.ContentLength} bytes, registry says ${expectedSize}` };
    }
    etag = head.ETag?.replace(/^"|"$/g, '');
  } catch (e) {
    const code = errCode(e);
    if (code === 'NotFound' || code === '404') {
      return { key, status: 'drift', reason: 'live object missing in the bucket (registry row dangling)' };
    }
    return { key, status: 'error', reason: `HEAD failed: ${code}` };
  }

  // Register FIRST (P4e): once the row exists it is referenced by the member
  // being retained, so no collection can claim it while the copy below is in
  // flight; an existing archived row is the same content (ensure the object);
  // a row a collection has claimed is refused and the gate fails closed. A
  // crash between here and the copy leaves a row the next run heals.
  const now = (deps.now ?? (() => new Date()))().toISOString();
  const archiveRow = {
    backend: 'osn',
    bucket: 'data',
    storage_key: ak,
    root: 'archive',
    status: 'archived',
    content_hash: row.content_hash,
    size_bytes: row.size_bytes,
    stored_size_bytes: row.stored_size_bytes,
    content_type: row.content_type ?? 'application/octet-stream',
    product_type: row.product_type,
    instrument: row.instrument,
    observation: row.observation,
    spectrum_id: row.spectrum_id,
    program_slug: row.program_slug,
    deployment_id: row.deployment_id,
    cfpipe_version: row.cfpipe_version,
    archived_from_key: row.storage_key,
    archived_at: now,
    archived_by_deployment: null,
    uploaded_by: deps.actorId ?? null,
    updated_at: now,
  };
  {
    const { error } = await supabase.rpc('archive_register_retained', { p_row: archiveRow });
    if (error) return { key, status: 'error', reason: `registry insert failed: ${error.message}` };
  }

  let copied = false;
  let exists = false;
  try {
    await s3.send(new HeadObjectCommand({ Bucket: bucket, Key: ak }));
    exists = true;
  } catch (e) {
    const code = errCode(e);
    if (code !== 'NotFound' && code !== '404') return { key, status: 'error', reason: `HEAD archive failed: ${code}` };
  }
  if (!exists) {
    try {
      await s3.send(
        new CopyObjectCommand({
          Bucket: bucket,
          Key: ak,
          CopySource: `${bucket}/${row.storage_key}`,
          CopySourceIfMatch: etag,
          MetadataDirective: 'COPY',
        }),
      );
      copied = true;
    } catch (e) {
      const code = errCode(e);
      if (code === 'PreconditionFailed' || code === '412') {
        return { key, status: 'drift', reason: 'live object changed during retain (copy precondition failed)' };
      }
      return { key, status: 'error', reason: `copy failed: ${code}` };
    }
  }

  // Audit, best effort (the function is admin/service-role gated and the route is admin).
  try {
    await supabase.rpc('log_deploy_event', {
      p_action: 'archive',
      p_actor: deps.actorId ?? null,
      p_observation: row.observation,
      p_affected_count: 1,
      p_metadata: { live_key: row.storage_key, archive_key: ak, content_hash: row.content_hash, copied },
    });
  } catch {
    /* audit never blocks the retain */
  }
  return { key, status: 'archived', archiveKey: ak, copied };
}

/** Run retains with bounded concurrency; order of results matches input. */
export async function executeRetains(
  decisions: Extract<RetainDecision, { action: 'retain' }>[],
  deps: RetainDeps,
  concurrency = 8,
): Promise<RetainOutcome[]> {
  const out: RetainOutcome[] = new Array(decisions.length);
  let next = 0;
  const worker = async () => {
    for (;;) {
      const i = next++;
      if (i >= decisions.length) return;
      out[i] = await executeRetain(decisions[i], deps);
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, decisions.length) }, worker));
  return out;
}

export function summarizeDecisions(decisions: RetainDecision[]): Record<string, number> {
  const c: Record<string, number> = {};
  for (const d of decisions) c[d.action] = (c[d.action] ?? 0) + 1;
  return c;
}
