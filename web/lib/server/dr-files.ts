import 'server-only';

import type { SupabaseClient } from '@supabase/supabase-js';
import { toCanonicalKey } from '@/lib/layout';
import { createServiceClient } from '@/lib/supabase/service';
import { frontUrlsForResolved } from '@/lib/server/cdn-front';
import { generateDownloadUrls, type ResolvedObject } from '@/lib/r2';

/**
 * Files of a data release (P6c-2, docs/design-data-releases-impl-plan.md §8).
 *
 * Under a release a file is served ONLY from its archived copy: the member's
 * `archive_key` in `dr_manifest`, once the byte pass has copied or stamped
 * it. A member that is still `pending` (or `failed`) is simply unavailable —
 * the caller omits the control; nothing here ever falls back to the live key.
 *
 * Reads go through the caller's cookie client, so `select_dr_manifest`
 * decides what exists for the viewer (a visible tag; program rows within
 * their programs; field-level and page rows for everyone). The bearer routes,
 * which run as the service role, use the `*As` variant and scope explicitly.
 *
 * Manifest keys are canonical (`data/products/...`); callers may pass legacy
 * keys (`spectra.fits_path` is `spectra/<obs>/<file>`), so every input is
 * canonicalised before the lookup and the answer is keyed by the input.
 */

export const ARCHIVED_STATES = ['copied', 'stamped'] as const;

export interface ReleaseMember {
  /** The live (canonical) storage key the manifest lists. */
  storageKey: string;
  productType: string;
  component: string;
  programSlug: string | null;
  /** Where the archived bytes live (`archive/<hash>/...`, or the page prefix). */
  archiveKey: string;
  /** Hash of the archived bytes (the stamped hash for a stamped FITS). */
  contentHash: string;
  sizeBytes: number | null;
  backend: 'osn' | 'r2';
  bucket: string;
}

interface ManifestRow {
  storage_key: string;
  product_type: string;
  component: string;
  program_slug: string | null;
  archive_key: string | null;
  content_hash: string;
  stamped_hash: string | null;
  size_bytes: number | null;
  backend: string;
  bucket: string;
  byte_state: string;
}

const MANIFEST_COLUMNS =
  'storage_key, product_type, component, program_slug, archive_key, content_hash, stamped_hash, size_bytes, backend, bucket, byte_state';
const LOOKUP_CHUNK = 50;

/** The canonical form of a live key, or null when the layout does not know it. */
export function canonicalOrNull(key: string): string | null {
  try {
    return toCanonicalKey(key);
  } catch {
    return null;
  }
}

function toMember(row: ManifestRow): ReleaseMember | null {
  if (!row.archive_key) return null;
  if (!(ARCHIVED_STATES as readonly string[]).includes(row.byte_state)) return null;
  return {
    storageKey: row.storage_key,
    productType: row.product_type,
    component: row.component,
    programSlug: row.program_slug,
    archiveKey: row.archive_key,
    contentHash: row.stamped_hash ?? row.content_hash,
    sizeBytes: row.size_bytes,
    backend: row.backend === 'r2' ? 'r2' : 'osn',
    bucket: row.bucket,
  };
}

async function fetchMembersByKey(
  supabase: SupabaseClient,
  tag: string,
  canonicalKeys: string[],
  scope?: { accessible: readonly string[]; isAdmin: boolean },
): Promise<Map<string, ReleaseMember>> {
  const out = new Map<string, ReleaseMember>();
  const chunks: string[][] = [];
  for (let i = 0; i < canonicalKeys.length; i += LOOKUP_CHUNK) chunks.push(canonicalKeys.slice(i, i + LOOKUP_CHUNK));
  const results = await Promise.all(
    chunks.map((chunk) => {
      let q = supabase
        .from('dr_manifest')
        .select(MANIFEST_COLUMNS)
        .eq('tag', tag)
        .in('storage_key', chunk)
        .in('byte_state', [...ARCHIVED_STATES]);
      // Service-role callers mirror select_dr_manifest's program rule.
      if (scope && !scope.isAdmin) {
        q = scope.accessible.length > 0
          ? q.or(`program_slug.is.null,program_slug.in.(${scope.accessible.map(quoteSlug).join(',')})`)
          : q.is('program_slug', null);
      }
      return q;
    }),
  );
  for (const { data, error } of results) {
    if (error) throw new Error(`dr_manifest lookup failed: ${error.message}`);
    for (const row of (data ?? []) as ManifestRow[]) {
      const m = toMember(row);
      if (m) out.set(m.storageKey, m);
    }
  }
  return out;
}

function quoteSlug(slug: string): string {
  // PostgREST `in.(...)` list syntax: quote anything beyond the safe charset.
  return /^[A-Za-z0-9_-]+$/.test(slug) ? slug : `"${slug.replace(/"/g, '\\"')}"`;
}

/**
 * The archived members of `tag` for `keys` (live keys, either scheme),
 * keyed by the input key. A key whose member is pending, failed, unknown
 * to the layout, or not visible to the viewer maps to null.
 */
export async function resolveReleaseMembers(
  supabase: SupabaseClient,
  tag: string,
  keys: string[],
): Promise<Map<string, ReleaseMember | null>> {
  return resolveWithScope(supabase, tag, keys, undefined);
}

/**
 * Same as resolveReleaseMembers for a service-role client, which bypasses
 * RLS: the caller's program scope is applied here (program rows within
 * `accessible`, field-level / page rows for everyone, everything for admins).
 * Release visibility (published, or any state for admins) is the caller's
 * check before this — see lib/server/bearer-release.ts.
 */
export async function resolveReleaseMembersAs(
  supabase: SupabaseClient,
  tag: string,
  keys: string[],
  scope: { accessible: readonly string[]; isAdmin: boolean },
): Promise<Map<string, ReleaseMember | null>> {
  return resolveWithScope(supabase, tag, keys, scope);
}

async function resolveWithScope(
  supabase: SupabaseClient,
  tag: string,
  keys: string[],
  scope: { accessible: readonly string[]; isAdmin: boolean } | undefined,
): Promise<Map<string, ReleaseMember | null>> {
  const out = new Map<string, ReleaseMember | null>();
  const canonicalByInput = new Map<string, string | null>();
  for (const k of new Set(keys)) canonicalByInput.set(k, canonicalOrNull(k));
  const canonical = [...new Set([...canonicalByInput.values()].filter((v): v is string => v !== null))];
  const members = canonical.length > 0 ? await fetchMembersByKey(supabase, tag, canonical, scope) : new Map<string, ReleaseMember>();
  for (const [input, c] of canonicalByInput) out.set(input, c ? members.get(c) ?? null : null);
  return out;
}

/**
 * The archived members of one component (field products, page assets),
 * optionally narrowed to product types. Viewer-scoped like resolveReleaseMembers.
 */
export async function listReleaseMembers(
  supabase: SupabaseClient,
  tag: string,
  opts: { component: string; productTypes?: readonly string[] },
): Promise<ReleaseMember[]> {
  let q = supabase
    .from('dr_manifest')
    .select(MANIFEST_COLUMNS)
    .eq('tag', tag)
    .eq('component', opts.component)
    .in('byte_state', [...ARCHIVED_STATES])
    .order('storage_key');
  if (opts.productTypes && opts.productTypes.length > 0) q = q.in('product_type', [...opts.productTypes]);
  const { data, error } = await q;
  if (error) throw new Error(`dr_manifest listing failed: ${error.message}`);
  return ((data ?? []) as ManifestRow[]).map(toMember).filter((m): m is ReleaseMember => m !== null);
}

/**
 * The archived members of several components at once (the /nircam landing
 * grid: every `nircam/<field>` layout in one query). Viewer-scoped.
 */
export async function listReleaseMembersIn(
  supabase: SupabaseClient,
  tag: string,
  opts: { components: readonly string[]; productTypes?: readonly string[] },
): Promise<ReleaseMember[]> {
  if (opts.components.length === 0) return [];
  let q = supabase
    .from('dr_manifest')
    .select(MANIFEST_COLUMNS)
    .eq('tag', tag)
    .in('component', [...opts.components])
    .in('byte_state', [...ARCHIVED_STATES])
    .order('storage_key');
  if (opts.productTypes && opts.productTypes.length > 0) q = q.in('product_type', [...opts.productTypes]);
  const { data, error } = await q;
  if (error) throw new Error(`dr_manifest listing failed: ${error.message}`);
  return ((data ?? []) as ManifestRow[]).map(toMember).filter((m): m is ReleaseMember => m !== null);
}

/**
 * The archived copies as resolved objects for the delivery front: content
 * identity and registration time from the archive registry rows (`root =
 * 'archive'`, written by the byte pass), read with the service client the way
 * resolveObjectBackends reads live rows. A copy with no registry row (should
 * not happen: dr_bytes_mark requires it) keeps the manifest hash and mints
 * with r=0 (servable, never cacheable).
 */
export async function resolveArchived(members: ReleaseMember[]): Promise<Map<string, ResolvedObject>> {
  const out = new Map<string, ResolvedObject>();
  if (members.length === 0) return out;
  const byArchiveKey = new Map(members.map((m) => [m.archiveKey, m]));
  const registered = new Map<string, { contentHash: string; registeredAt: number | null }>();
  try {
    const service = createServiceClient();
    const keys = [...byArchiveKey.keys()];
    const chunks: string[][] = [];
    for (let i = 0; i < keys.length; i += LOOKUP_CHUNK) chunks.push(keys.slice(i, i + LOOKUP_CHUNK));
    const results = await Promise.all(
      chunks.map((chunk) =>
        service
          .from('storage_objects')
          .select('storage_key, content_hash, archived_at')
          .eq('root', 'archive')
          .in('status', ['active', 'archived'])
          .in('storage_key', chunk),
      ),
    );
    for (const { data, error } of results) {
      if (error) throw error;
      for (const row of data ?? []) {
        const ms = row.archived_at ? Date.parse(String(row.archived_at)) : NaN;
        registered.set(row.storage_key as string, {
          contentHash: String(row.content_hash),
          registeredAt: Number.isFinite(ms) ? Math.floor(ms / 1000) : null,
        });
      }
    }
  } catch (err) {
    console.error('dr-files: archive registry lookup failed; minting without registration times', err);
  }
  for (const m of members) {
    const reg = registered.get(m.archiveKey);
    out.set(m.archiveKey, {
      backend: m.backend,
      key: m.archiveKey,
      contentHash: reg?.contentHash ?? m.contentHash,
      registeredAt: reg?.registeredAt ?? null,
    });
  }
  return out;
}

/**
 * Delivery-front urls for archived members, keyed like the input map (null
 * for an unavailable member, or when the front is off / a mint failed —
 * callers then stream the archive key themselves).
 */
export async function releaseFrontUrls(
  byKey: Map<string, ReleaseMember | null>,
): Promise<Map<string, string | null>> {
  const out = new Map<string, string | null>([...byKey.keys()].map((k) => [k, null]));
  const members = [...byKey.values()].filter((m): m is ReleaseMember => m !== null);
  if (members.length === 0) return out;
  const resolved = await resolveArchived(members);
  const archiveKeys = members.map((m) => m.archiveKey);
  const urls = await frontUrlsForResolved(archiveKeys, archiveKeys.map((k) => resolved.get(k) as ResolvedObject));
  for (const [input, m] of byKey) if (m) out.set(input, urls.get(m.archiveKey) ?? null);
  return out;
}

/**
 * Presigned download urls for archived members, keyed like the input map
 * (null for an unavailable member). The archive key is OSN-only in every
 * resolution path of lib/r2, so this never signs a live key.
 */
export async function releaseDownloadUrls(
  byKey: Map<string, ReleaseMember | null>,
  expiresIn: number,
  opts: { attachment?: boolean } = {},
): Promise<Map<string, string | null>> {
  const out = new Map<string, string | null>([...byKey.keys()].map((k) => [k, null]));
  const entries = [...byKey].filter((e): e is [string, ReleaseMember] => e[1] !== null);
  if (entries.length === 0) return out;
  const signed = await generateDownloadUrls(entries.map(([, m]) => m.archiveKey), expiresIn, opts);
  entries.forEach(([input], i) => out.set(input, signed[i]));
  return out;
}
